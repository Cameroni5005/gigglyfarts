import os
import time
import json
import threading
import logging
from datetime import datetime
import yfinance as yf
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from flask import Flask, jsonify
import statistics

# ---------------- CONFIG ----------------
load_dotenv()

API_KEY = os.getenv("ALPACA_KEY")
API_SECRET = os.getenv("ALPACA_SECRET")
APCA_URL = os.getenv("APCA_URL")

TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO"
]

MARKET_OPEN = datetime.strptime("09:30", "%H:%M").time()
MARKET_CLOSE = datetime.strptime("16:00", "%H:%M").time()
INTRADAY_TTL = 30  # seconds
STATE_FILE = "trade_state.json"

# risk settings
ACCOUNT_EQUITY = 100000
RISK_PCT_PER_TRADE = 0.01  # 1% of equity per trade
DAILY_MAX_LOSS_PCT = 0.03  # stop trading if losing 3% of equity
TARGET_AVG_TRADE = 10000  # average size for stable stocks
STOP_MULTIPLIER = 2  # ATR multiple for stop-loss

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- PERSISTENCE ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_run": None, "positions": {}, "histories": {}, "daily_loss": 0.0}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

STATE = load_state()

# ---------------- ALPACA ----------------
api = tradeapi.REST(API_KEY, API_SECRET, APCA_URL, api_version="v2")

def submit_order(symbol, qty, side="buy", order_type="market"):
    try:
        order = api.submit_order(symbol=symbol, qty=qty, side=side, type=order_type, time_in_force="day")
        log.info(f"submitted {side} order for {symbol} qty {qty}")
        return order
    except Exception:
        log.exception(f"failed to submit order for {symbol}")
        return None

# ---------------- DATA ----------------
INTRADAY_CACHE = {}

def get_intraday_data(symbol, retries=3, delay=2):
    now = time.time()
    cached = INTRADAY_CACHE.get(symbol)
    if cached:
        data, ts = cached
        if now - ts < INTRADAY_TTL:
            return data
    for _ in range(retries):
        try:
            df = yf.download(tickers=symbol, period="1d", interval="1m", progress=False)
            bars = []
            for idx, row in df.iterrows():
                bars.append({
                    "time": idx.strftime("%Y-%m-%d %H:%M:%S"),
                    "close": float(row["Close"]),
                    "high": float(row["High"]),
                    "low": float(row["Low"]),
                    "volume": float(row["Volume"].iloc[0] if hasattr(row["Volume"], "iloc") else row["Volume"]),
                })
            if bars:
                INTRADAY_CACHE[symbol] = (bars, now)
            return bars
        except Exception:
            time.sleep(delay)
    log.warning(f"failed to fetch intraday data for {symbol}")
    return []

# ---------------- INDICATORS ----------------
def ema(values, period):
    if not values or len(values) < period:
        return None
    k = 2 / (period + 1)
    ema_val = sum(values[-period:]) / period
    for price in values[-period+1:]:
        ema_val = price * k + ema_val * (1 - k)
    return ema_val

def macd_value(values, fast=12, slow=26):
    efast = ema(values, fast)
    eslow = ema(values, slow)
    if efast is None or eslow is None:
        return 0
    return efast - eslow

def rsi(values, period=14):
    if len(values) <= period:
        return 50
    deltas = [values[i]-values[i-1] for i in range(1,len(values))]
    gains = [d if d>0 else 0 for d in deltas]
    losses = [-d if d<0 else 0 for d in deltas]
    avg_gain = sum(gains[:period])/period
    avg_loss = sum(losses[:period])/period
    for g,l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain*(period-1)+g)/period
        avg_loss = (avg_loss*(period-1)+l)/period
    if avg_loss==0:
        return 100
    rs = avg_gain/avg_loss
    return 100-(100/(1+rs))

def z_clip(x, mean, std, clip=3):
    if std==0 or std is None:
        return 50
    z = (x-mean)/std
    z = max(min(z, clip), -clip)
    return (z + clip)/(2*clip)*100

def normalize_indicator(value, history):
    if not history:
        return 50
    mean = statistics.mean(history)
    stdev = statistics.pstdev(history) if len(history)>1 else 0
    return z_clip(value, mean, stdev)

# ---------------- SMART TECHNICAL & RISK ----------------
def compute_technical_smart(symbol):
    data = get_intraday_data(symbol)
    if not data or len(data)<30:
        return None
    closes = [d["close"] for d in data]
    highs = [d["high"] for d in data]
    lows = [d["low"] for d in data]
    vols = [d["volume"] for d in data]

    # MACD trend
    macd = (macd_value(closes,8,21) + macd_value(closes,12,26))/2

    # ATR
    trs = [max(highs[i]-lows[i], abs(highs[i]-closes[i-1]), abs(lows[i]-closes[i-1])) for i in range(1,len(data))]
    atr = sum(trs[-14:])/min(14,len(trs)) if trs else 0

    # RSI
    rsi_v = rsi(closes)

    # volume
    vol_recent = vols[-120:] if len(vols)>=120 else vols
    vol_med = statistics.median(vol_recent) if vol_recent else 0
    vol_now = vols[-1] if vols else 0
    vol_change = (vol_now-vol_med) if vol_med!=0 else 0

    # historical cache
    histories = STATE.get("histories", {}).get(symbol, {})
    macd_hist = histories.get("macd",[])
    rsi_hist = histories.get("rsi",[])
    vol_hist = histories.get("vol",[])
    atr_hist = histories.get("atr",[])

    # normalize indicators
    macd_norm = normalize_indicator(macd, macd_hist)
    rsi_norm = normalize_indicator(rsi_v, rsi_hist)
    vol_norm = normalize_indicator(vol_change, vol_hist)
    atr_norm = normalize_indicator(atr, atr_hist)

    # trend strength
    trend_strength = max(0.0,min(abs(macd)/atr if atr else 0,1.0))

    # dynamic weights
    w_trend = 0.2 + 0.4*trend_strength
    w_rsi = 0.5 - 0.25*trend_strength
    w_vol = 0.2 - 0.05*trend_strength
    w_atr = 1.0 - (w_trend+w_rsi+w_vol)
    s = w_trend+w_rsi+w_vol+w_atr
    w_trend/=s; w_rsi/=s; w_vol/=s; w_atr/=s

    # final score
    score = macd_norm*w_trend + rsi_norm*w_rsi + vol_norm*w_vol + atr_norm*w_atr
    score = max(0,min(100,score))

    # update histories
    for key,val,hist in zip(["macd","rsi","vol","atr"],[macd,rsi_v,vol_change,atr],[macd_hist,rsi_hist,vol_hist,atr_hist]):
        hist.append(val)
        if len(hist)>200: hist.pop(0)
        if symbol not in STATE["histories"]: STATE["histories"][symbol]={}
        STATE["histories"][symbol][key]=hist

    return {
        "price": closes[-1],
        "score": score,
        "atr": atr,
        "trend_strength": trend_strength,
        "weights": {"trend": w_trend,"rsi":w_rsi,"vol":w_vol,"atr":w_atr}
    }

# ---------------- POSITION SIZING ----------------
def calculate_position_size(price, atr, score):
    # dynamically scale trade based on score & volatility
    base_qty = int(TARGET_AVG_TRADE / price)
    risk_factor = max(0.2, min(1.5, 1/atr))  # higher ATR = smaller size
    score_factor = 0.5 + score/100  # higher score = bigger size
    qty = int(base_qty * risk_factor * score_factor)
    return max(1, qty)

# ---------------- EXECUTION ----------------
def execute_trades(ignore_market_hours=False):
    now = datetime.now().time()
    if not ignore_market_hours and (now<MARKET_OPEN or now>MARKET_CLOSE):
        log.info("market closed, skipping trades")
        return []

    if STATE.get("daily_loss",0)/ACCOUNT_EQUITY >= DAILY_MAX_LOSS_PCT:
        log.info("daily max loss reached, skipping trades")
        return []

    executed=[]
    for symbol in TICKERS:
        summary = compute_technical_smart(symbol)
        if not summary:
            continue
        score = summary["score"]
        action=None
        price = summary["price"]
        atr = summary["atr"] or 1  # fallback

        if score>75:
            qty = calculate_position_size(price, atr, score)
            submit_order(symbol, qty, "buy")
            action="buy"
        elif score<25:
            qty = calculate_position_size(price, atr, score)
            submit_order(symbol, qty, "sell")
            action="sell"

        executed.append({"symbol":symbol,"score":score,"action":action})
    save_state(STATE)
    return executed

# ---------------- FLASK ----------------
app = Flask(__name__)

@app.route("/trigger", methods=["GET"])
def trigger():
    log.info("trigger requested")
    executed = execute_trades(ignore_market_hours=True)
    return jsonify({"executed":executed})

# ---------------- MAIN LOOP ----------------
def bot_loop():
    while True:
        now = datetime.now()
        if now.time()<MARKET_OPEN or now.time()>MARKET_CLOSE:
            log.info("market closed, sleeping")
            time.sleep(60)
            continue

        if STATE.get("last_run"):
            last_run = datetime.strptime(STATE["last_run"],"%Y-%m-%d %H:%M:%S")
            if last_run.hour==now.hour:
                time.sleep(60)
                continue

        log.info("running bot cycle")
        execute_trades()
        STATE["last_run"]=now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(STATE)
        sleep_seconds=1800-(now.minute%30)*60-now.second
        time.sleep(sleep_seconds)

# ---------------- START ----------------
if __name__=="__main__":
    log.info("starting improved risk-aware bot as webservice")
    threading.Thread(target=bot_loop,daemon=True).start()
    app.run(host="0.0.0.0",port=int(os.getenv("PORT",10000)))
