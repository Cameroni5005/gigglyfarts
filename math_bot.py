import os
import time
import json
import logging
from datetime import datetime, timedelta
import yfinance as yf
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv

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

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- PERSISTENCE ----------------
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {"last_run": None, "positions": {}}

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
    except Exception as e:
        log.exception(f"failed to submit order for {symbol}")
        return None

# ---------------- DATA INGESTION ----------------
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

# ---------------- TECHNICAL CALCULATIONS ----------------
def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    now = datetime.now().time()
    if now < datetime.strptime("10:30","%H:%M").time():
        ma_short_len = 5
        ma_long_len = 15
    elif now < datetime.strptime("14:30","%H:%M").time():
        ma_short_len = 10
        ma_long_len = 20
    else:
        ma_short_len = 7
        ma_long_len = 15

    ma_short = sum(closes[-ma_short_len:])/ma_short_len if len(closes)>=ma_short_len else None
    ma_long = sum(closes[-ma_long_len:])/ma_long_len if len(closes)>=ma_long_len else None

    # RSI calculation
    rsi = None
    period = 14
    if len(closes) > period:
        deltas = [closes[i]-closes[i-1] for i in range(1,len(closes))]
        gains = [d if d>0 else 0 for d in deltas]
        losses = [-d if d<0 else 0 for d in deltas]
        avg_gain = sum(gains[:period])/period
        avg_loss = sum(losses[:period])/period
        for g,l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain*(period-1)+g)/period
            avg_loss = (avg_loss*(period-1)+l)/period
        rsi = 100 if avg_loss==0 else 0 if avg_gain==0 else 100-(100/(1+avg_gain/avg_loss))

    # ATR calculation
    atr = None
    if len(data)>1:
        tr_list = []
        for i in range(1,len(data)):
            h = data[i]['high']
            l = data[i]['low']
            pc = data[i-1]['close']
            tr = max(h-l, abs(h-pc), abs(l-pc))
            tr_list.append(tr)
        atr = sum(tr_list[-14:])/14 if len(tr_list)>=14 else tr_list[-1]

    last = closes[-1]
    prev = closes[-2] if len(closes)>1 else last
    delta = last-prev
    vol_window = min(120, len(volumes))
    avg_vol = sum(volumes[-vol_window:])/vol_window if vol_window>0 else 0
    vol_change = (volumes[-1]-avg_vol)/avg_vol*100 if avg_vol!=0 else 0

    return {
        "price": last,
        "change": delta,
        "ma_short": ma_short,
        "ma_long": ma_long,
        "rsi": rsi,
        "volume": volumes[-1],
        "vol_change": vol_change,
        "atr": atr
    }

# ---------------- MATH SCORE ----------------
def compute_math_score(symbol):
    tech = compute_technical(symbol)
    if not tech:
        return 50

    ma_diff = tech['ma_short'] - tech['ma_long'] if tech['ma_short'] and tech['ma_long'] else 0
    trending = abs(ma_diff) > 0

    if trending:
        weights = {"trend":0.45, "rsi":0.30, "vol":0.20, "atr":0.05}
    else:
        weights = {"trend":0.20, "rsi":0.45, "vol":0.25, "atr":0.10}

    ma_score = 100 if tech['ma_short'] > tech['ma_long'] else 0
    rsi_score = 100 - tech['rsi'] if tech['rsi'] is not None else 50
    vol_score = min(max(tech['vol_change'],0),100)
    atr_score = tech['atr'] if tech['atr'] else 50
    atr_score = min(max(atr_score,0),100)

    return (ma_score*weights["trend"] +
            rsi_score*weights["rsi"] +
            vol_score*weights["vol"] +
            atr_score*weights["atr"])

# ---------------- COMBINED ----------------
def build_summary(symbol):
    tech = compute_technical(symbol)
    return {
        "symbol": symbol,
        "math_score": compute_math_score(symbol),
        "price": tech['price'] if tech else None,
        "ma_short": tech['ma_short'] if tech else None,
        "ma_long": tech['ma_long'] if tech else None,
        "rsi": tech['rsi'] if tech else None,
        "atr": tech['atr'] if tech else None
    }

# ---------------- EXECUTION ----------------
def execute_trades():
    for symbol in TICKERS:
        summary = build_summary(symbol)
        score = summary["math_score"]
        if score > 70:
            submit_order(symbol, qty=1, side="buy")
        elif score < 30:
            submit_order(symbol, qty=1, side="sell")

# ---------------- MAIN LOOP ----------------
def bot_loop():
    while True:
        now = datetime.now()
        if now.time() < MARKET_OPEN or now.time() > MARKET_CLOSE:
            log.info("market closed, sleeping")
            time.sleep(60)
            continue

        if STATE.get("last_run"):
            last_run = datetime.strptime(STATE["last_run"], "%Y-%m-%d %H:%M:%S")
            if (now - last_run) < timedelta(minutes=30):
                time.sleep(60)
                continue

        log.info("running math bot cycle")
        execute_trades()
        STATE["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
        save_state(STATE)
        log.info("cycle complete, sleeping 30 minutes")
        time.sleep(1800)

# ---------------- START ----------------
if __name__ == "__main__":
    log.info("starting math bot")
    bot_loop()
