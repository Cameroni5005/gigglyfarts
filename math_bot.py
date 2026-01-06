import os
import time
import threading
import logging
import requests
from datetime import datetime
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()

TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")
if not TWELVEDATA_KEY:
    raise SystemExit("missing env var: TWELVEDATA_KEY")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- STOCK CONFIG ----------------
TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO"
]

# ---------------- CACHE ----------------
INTRADAY_CACHE = {}
INTRADAY_TTL = 30  # seconds

# ---------------- HELPERS ----------------
def safe_json(r):
    try:
        return r.json()
    except ValueError:
        return {}

# ---------------- TWELVEDATA ----------------
TWELVE_RATE_LIMIT = 8
TWELVE_WINDOW = 61
_twelve_calls = []
_twelve_lock = threading.Lock()

def twelve_rate_limit():
    global _twelve_calls
    with _twelve_lock:
        now = time.time()
        _twelve_calls = [t for t in _twelve_calls if now - t < TWELVE_WINDOW]
        if len(_twelve_calls) >= TWELVE_RATE_LIMIT:
            sleep_for = TWELVE_WINDOW - (now - _twelve_calls[0]) + 0.1
            log.info(f"TwelveData rate limit hit, sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)
        _twelve_calls.append(time.time())

def fetch_twelvedata_bars(symbol, interval="1min", limit=200):
    twelve_rate_limit()
    r = requests.get(
        "https://api.twelvedata.com/time_series",
        params={
            "symbol": symbol,
            "interval": interval,
            "outputsize": limit,
            "apikey": TWELVEDATA_KEY
        },
        timeout=8
    )
    data = safe_json(r)
    if not data or "values" not in data:
        return []
    return [
        {
            "time": v["datetime"],
            "close": float(v["close"]),
            "high": float(v["high"]),
            "low": float(v["low"]),
            "volume": float(v["volume"]),
        }
        for v in reversed(data["values"])
    ]

def get_intraday_data(symbol):
    now = time.time()
    cached = INTRADAY_CACHE.get(symbol)
    if cached:
        data, ts = cached
        if now - ts < INTRADAY_TTL:
            return data
    bars = fetch_twelvedata_bars(symbol)
    if bars:
        INTRADAY_CACHE[symbol] = (bars, now)
    return bars

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

    last = closes[-1]
    prev = closes[-2] if len(closes)>1 else last
    delta = last-prev

    vol_window = min(120, len(volumes))
    avg_vol = sum(volumes[-vol_window:])/vol_window if vol_window>0 else 0
    vol_change = (volumes[-1]-avg_vol)/avg_vol*100 if avg_vol!=0 else 0

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
    score = 50
    if tech:
        ma_score = 100 if tech['ma_short'] > tech['ma_long'] else 0
        rsi_score = 100 - tech['rsi'] if tech['rsi'] is not None else 50
        vol_score = min(max(tech['vol_change'],0),100)
        score = rsi_score*0.4 + ma_score*0.3 + vol_score*0.3
    return score

# ---------------- COMBINE ----------------
def build_combined_summary(symbol):
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

def get_all_summaries(tickers):
    summaries = []
    for sym in tickers:
        try:
            summaries.append(build_combined_summary(sym))
        except Exception:
            log.exception(f"error building summary for {sym}")
    return summaries

# ---------------- TEST RUN ----------------
if __name__ == "__main__":
    summaries = get_all_summaries(TICKERS)
    for s in summaries:
        log.info(s)
