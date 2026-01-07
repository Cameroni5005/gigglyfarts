import os
import time
import json
import threading
import logging
from datetime import datetime, time as dtime
import statistics
import requests

import yfinance as yf
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from flask import Flask, jsonify

# ================== CONFIG ==================
load_dotenv()

API_KEY = os.getenv("ALPACA_KEY")
API_SECRET = os.getenv("ALPACA_SECRET")
APCA_URL = os.getenv("APCA_URL")

PORT = int(os.getenv("PORT", 10000))

TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO","JPM","BA"
]

MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

INTRADAY_TTL = 60
STATE_FILE = "trade_state.json"

ACCOUNT_EQUITY = 100000
DAILY_MAX_LOSS_PCT = 0.03
TARGET_AVG_TRADE = 10000

# ================== LOGGING ==================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)
log = logging.getLogger("bot")

# ================== STATE ==================
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    return {
        "last_run": None,
        "daily_loss": 0.0,
        "histories": {}
    }

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

STATE = load_state()

# ================== ALPACA ==================
api = tradeapi.REST(API_KEY, API_SECRET, APCA_URL, api_version="v2")

def submit_order(symbol, qty, side):
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side=side,
            type="market",
            time_in_force="day"
        )
        log.info(f"order sent: {side} {symbol} x{qty}")
    except Exception:
        log.exception(f"order failed for {symbol}")

# ================== MARKET TIME ==================
def market_is_open():
    now = datetime.now().time()
    return MARKET_OPEN <= now <= MARKET_CLOSE

# ================== DATA ==================
INTRADAY_CACHE = {}

def get_intraday_data(symbol):
    now = time.time()
    cached = INTRADAY_CACHE.get(symbol)

    if cached and now - cached["ts"] < INTRADAY_TTL:
        return cached["bars"]

    try:
        df = yf.download(
            symbol,
            period="1d",
            interval="1m",
            progress=False
        )

        if df.empty:
            return cached["bars"] if cached else []

        bars = []
        for idx, row in df.iterrows():
            bars.append({
                "time": idx.strftime("%Y-%m-%d %H:%M:%S"),
                "close": float(row["Close"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "volume": float(row["Volume"])
            })

        INTRADAY_CACHE[symbol] = {
            "bars": bars,
            "ts": now
        }
        return bars

    except Exception:
        log.exception(f"data fetch failed for {symbol}")
        return cached["bars"] if cached else []

# ================== INDICATORS ==================
def ema(values, period):
    if len(values) < period:
        return None
    k = 2 / (period + 1)
    val = sum(values[:period]) / period
    for p in values[period:]:
        val = p * k + val * (1 - k)
    return val

def macd(values):
    fast = ema(values, 12)
    slow = ema(values, 26)
    return fast - slow if fast and slow else 0

def rsi(values, period=14):
    if len(values) <= period:
        return 50
    deltas = [values[i]-values[i-1] for i in range(1,len(values))]
    gains = [d if d>0 else 0 for d in deltas]
    losses = [-d if d<0 else 0 for d in deltas]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def normalize(val, hist):
    if not hist:
        return 50
    mean = statistics.mean(hist)
    std = statistics.pstdev(hist) if len(hist) > 1 else 1
    z = (val - mean) / std
    z = max(min(z, 3), -3)
    return (z + 3) / 6 * 100

# ================== STRATEGY ==================
def analyze(symbol):
    data = get_intraday_data(symbol)
    if len(data) < 30:
        return None

    closes = [b["close"] for b in data]
    vols = [b["volume"] for b in data]

    macd_v = macd(closes)
    rsi_v = rsi(closes)
    vol_delta = vols[-1] - statistics.median(vols[-30:])

    hist = STATE["histories"].setdefault(symbol, {
        "macd": [], "rsi": [], "vol": []
    })

    macd_n = normalize(macd_v, hist["macd"])
    rsi_n = normalize(rsi_v, hist["rsi"])
    vol_n = normalize(vol_delta, hist["vol"])

    score = (
        macd_n * 0.45 +
        rsi_n * 0.35 +
        vol_n * 0.20
    )

    hist["macd"].append(macd_v)
    hist["rsi"].append(rsi_v)
    hist["vol"].append(vol_delta)

    for k in hist:
        if len(hist[k]) > 200:
            hist[k].pop(0)

    return {
        "price": closes[-1],
        "score": score
    }

def position_size(price, score):
    base = TARGET_AVG_TRADE / price
    factor = 0.5 + score / 100
    return max(1, int(base * factor))

# ================== EXECUTION ==================
def run_cycle(ignore_market_hours=False):
    if not ignore_market_hours and not market_is_open():
        log.info("market closed, skipping")
        return []

    executed = []

    for symbol in TICKERS:
        res = analyze(symbol)
        if not res:
            continue

        score = res["score"]
        price = res["price"]

        if score > 75:
            qty = position_size(price, score)
            submit_order(symbol, qty, "buy")
            executed.append({"symbol": symbol, "action": "buy", "score": score})

        elif score < 25:
            qty = position_size(price, score)
            submit_order(symbol, qty, "sell")
            executed.append({"symbol": symbol, "action": "sell", "score": score})

    save_state(STATE)
    return executed

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/trigger", methods=["GET"])
def trigger():
    log.info("manual trigger")
    executed = run_cycle(ignore_market_hours=True)
    return jsonify({
        "market_open": market_is_open(),
        "executed": executed
    })

# ================== LOOP ==================
def bot_loop():
    while True:
        if market_is_open():
            now = datetime.now()
            last = STATE.get("last_run")

            if last:
                last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                if (now - last_dt).seconds < 1800:
                    time.sleep(30)
                    continue

            log.info("auto cycle")
            run_cycle()
            STATE["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
            save_state(STATE)

        time.sleep(30)

# ================== START ==================
if __name__ == "__main__":
    log.info("starting trading webservice")
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
