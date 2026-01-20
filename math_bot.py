import os
import time
import json
import threading
import logging
from datetime import datetime, time as dtime, timedelta, timezone
import statistics
import math

import yfinance as yf
import alpaca_trade_api as tradeapi
from dotenv import load_dotenv
from flask import Flask, jsonify
import pandas as pd

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

# Yahoo config (FIX #2)
INTERVAL = "5m"
PERIOD = "60d"
INTRADAY_TTL = 300  # 5 minutes

STATE_FILE = "trade_state.json"
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
    return {"last_run": None, "histories": {}}

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

# ================== YAHOO RATE LIMITER (FIX #2) ==================
YF_LOCK = threading.Lock()
LAST_YF_CALL = 0
YF_MIN_DELAY = 1.5  # seconds

def yf_safe_download(*args, **kwargs):
    global LAST_YF_CALL
    with YF_LOCK:
        now = time.time()
        sleep_for = YF_MIN_DELAY - (now - LAST_YF_CALL)
        if sleep_for > 0:
            time.sleep(sleep_for)
        LAST_YF_CALL = time.time()
        return yf.download(*args, **kwargs)

# ================== DATA ==================
INTRADAY_CACHE = {}

def fetch_yf(symbol):
    try:
        log.info(f"fetching {symbol} {INTERVAL}")
        df = yf_safe_download(
            symbol,
            interval=INTERVAL,
            period=PERIOD,
            progress=False,
            threads=False
        )

        if df is None or df.empty:
            log.warning(f"{symbol}: Yahoo empty response")
            return None

        return df.sort_index().dropna()

    except Exception:
        log.exception(f"{symbol}: Yahoo exception")
        return None

def get_intraday_data(symbol):
    now = time.time()
    cached = INTRADAY_CACHE.get(symbol)

    if cached and now - cached["ts"] < INTRADAY_TTL:
        return cached["bars"]

    df = fetch_yf(symbol)
    if df is None or len(df) < 30:
        return []

    bars = []
    for idx, row in df.iterrows():
        try:
            bars.append({
                "time": idx.strftime("%Y-%m-%d %H:%M:%S"),
                "close": float(row["Close"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "volume": float(row["Volume"])
            })
        except Exception:
            continue

    INTRADAY_CACHE[symbol] = {"bars": bars, "ts": now}
    return bars

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
    deltas = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [d if d > 0 else 0 for d in deltas]
    losses = [-d if d < 0 else 0 for d in deltas]

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
        log.warning(f"{symbol}: not enough data")
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

    score = macd_n * 0.45 + rsi_n * 0.35 + vol_n * 0.20

    if score >= 85:
        signal = "strong_buy"
    elif score >= 70:
        signal = "buy"
    elif score >= 30:
        signal = "hold"
    elif score >= 15:
        signal = "sell"
    else:
        signal = "strong_sell"

    hist["macd"].append(macd_v)
    hist["rsi"].append(rsi_v)
    hist["vol"].append(vol_delta)

    for k in hist:
        if len(hist[k]) > 200:
            hist[k].pop(0)

    return {
        "price": closes[-1],
        "score": score,
        "signal": signal,
        "macd_n": macd_n,
        "rsi_n": rsi_n,
        "vol_n": vol_n
    }

def position_size(price, score):
    base = TARGET_AVG_TRADE / price
    factor = 1.5 if score >= 85 else 1.2 if score >= 70 else 1.0 if score >= 30 else 0.8 if score >= 15 else 0.6
    return max(1, int(base * factor))

# ================== EXECUTION ==================
def run_cycle(ignore_market_hours=False):
    log.info("starting run cycle")

    if not ignore_market_hours and not market_is_open():
        log.info("market closed, skipping cycle")
        return []

    executed = []

    for symbol in TICKERS:
        res = analyze(symbol)
        if not res:
            continue

        log.info(
            f"{symbol} | ${res['price']:.2f} | score={res['score']:.1f} | {res['signal'].upper()}"
        )

        if res["signal"] in ["buy", "strong_buy"]:
            submit_order(symbol, position_size(res["price"], res["score"]), "buy")
        elif res["signal"] in ["sell", "strong_sell"]:
            submit_order(symbol, position_size(res["price"], res["score"]), "sell")

    save_state(STATE)
    return executed

# ================== FLASK ==================
app = Flask(__name__)

@app.route("/trigger", methods=["GET"])
def trigger():
    return jsonify({"executed": run_cycle(ignore_market_hours=True)})

# ================== LOOP ==================
def bot_loop():
    while True:
        try:
            if market_is_open():
                now = datetime.now()
                last = STATE.get("last_run")

                if last:
                    last_dt = datetime.strptime(last, "%Y-%m-%d %H:%M:%S")
                    if (now - last_dt).seconds < 1800:
                        time.sleep(30)
                        continue

                run_cycle()
                STATE["last_run"] = now.strftime("%Y-%m-%d %H:%M:%S")
                save_state(STATE)

            time.sleep(30)

        except Exception:
            log.exception("bot loop crashed, restarting in 10s")
            time.sleep(10)

# ================== START ==================
if __name__ == "__main__":
    log.info("starting trading webservice")
    threading.Thread(target=bot_loop, daemon=True).start()
    app.run(host="0.0.0.0", port=PORT)
