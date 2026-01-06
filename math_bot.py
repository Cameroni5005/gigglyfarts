import os
import time
import threading
import logging
import requests
from datetime import datetime, timedelta
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()

TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")

if not all([TWELVEDATA_KEY, FINNHUB_KEY]):
    raise SystemExit("missing env vars for API keys")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- STOCK CONFIG ----------------
TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO"
]

SECTORS = {
    "AAPL":"Technology","MSFT":"Technology","AMZN":"Consumer Discretionary","NVDA":"Technology",
    "GOOG":"Technology","META":"Communication Services","TSLA":"Consumer Discretionary",
    "NFLX":"Communication Services","DIS":"Communication Services","PYPL":"Financial Services",
    "INTC":"Technology","CSCO":"Technology","ADBE":"Technology","ORCL":"Technology","IBM":"Technology",
    "CRM":"Technology","AMD":"Technology","UBER":"Consumer Discretionary","LYFT":"Consumer Discretionary",
    "SHOP":"Technology","BABA":"Consumer Discretionary","NKE":"Consumer Discretionary","SBUX":"Consumer Discretionary",
    "QCOM":"Technology","PEP":"Consumer Staples","KO":"Consumer Staples"
}

# ---------------- CACHE ----------------
CACHE = {"news": {}, "social": {}}
INTRADAY_CACHE = {}
INTRADAY_TTL = 55  # seconds

# ---------------- HELPERS ----------------
def safe_json(r):
    try:
        return r.json()
    except ValueError:
        return {}

# ---------------- TWELVEDATA RATE LIMIT ----------------
TWELVE_RATE_LIMIT = 8
TWELVE_WINDOW = 60

_twelve_calls = []
_twelve_lock = threading.Lock()

def twelve_rate_limit():
    with _twelve_lock:
        now = time.time()
        _twelve_calls[:] = [t for t in _twelve_calls if now - t < TWELVE_WINDOW]

        if len(_twelve_calls) >= TWELVE_RATE_LIMIT:
            sleep_for = TWELVE_WINDOW - (now - _twelve_calls[0])
            sleep_for = max(sleep_for, 0.5)
            log.info(f"TwelveData rate limit hit, sleeping {sleep_for:.1f}s")
            time.sleep(sleep_for)

        _twelve_calls.append(time.time())

# ---------------- TWELVEDATA FETCH ----------------
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

# ---------------- TECHNICALS ----------------
def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    now = datetime.now().time()
    if now < datetime.strptime("10:30","%H:%M").time():
        ma_short_len, ma_long_len = 5, 15
    elif now < datetime.strptime("14:30","%H:%M").time():
        ma_short_len, ma_long_len = 10, 20
    else:
        ma_short_len, ma_long_len = 7, 15

    ma_short = sum(closes[-ma_short_len:]) / ma_short_len if len(closes) >= ma_short_len else None
    ma_long = sum(closes[-ma_long_len:]) / ma_long_len if len(closes) >= ma_long_len else None

    rsi = None
    period = 14
    if len(closes) > period:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [max(d, 0) for d in deltas]
        losses = [max(-d, 0) for d in deltas]
        avg_gain = sum(gains[:period]) / period
        avg_loss = sum(losses[:period]) / period

        for g, l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain * (period - 1) + g) / period
            avg_loss = (avg_loss * (period - 1) + l) / period

        rsi = 100 if avg_loss == 0 else 100 - (100 / (1 + avg_gain / avg_loss))

    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    delta = last - prev

    avg_vol = sum(volumes[-60:]) / min(60, len(volumes))
    vol_change = ((volumes[-1] - avg_vol) / avg_vol) * 100 if avg_vol else 0

    return {
        "price": last,
        "change": delta,
        "ma_short": ma_short,
        "ma_long": ma_long,
        "rsi": rsi,
        "volume": volumes[-1],
        "vol_change": vol_change,
    }

# ---------------- MATH SCORE ----------------
def compute_math_score(symbol):
    tech = compute_technical(symbol)
    if not tech:
        return None

    ma_score = 100 if tech["ma_short"] and tech["ma_long"] and tech["ma_short"] > tech["ma_long"] else 0
    rsi_score = 100 - tech["rsi"] if tech["rsi"] is not None else 50
    vol_score = min(max(tech["vol_change"], 0), 100)

    return rsi_score * 0.4 + ma_score * 0.3 + vol_score * 0.3

# ---------------- FINNHUB ----------------
def fetch_finnhub_news(symbol):
    today = datetime.today().date()
    key = (symbol, today)

    if key in CACHE["news"]:
        return CACHE["news"][key]

    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news",
            params={
                "symbol": symbol,
                "from": today - timedelta(days=3),
                "to": today,
                "token": FINNHUB_KEY
            },
            timeout=5
        )
        data = safe_json(r)
        headlines = [d["headline"] for d in data[:2]] if data else []
        summary = " | ".join(headlines) if headlines else "no major news"
        CACHE["news"][key] = summary
        return summary
    except Exception:
        return "no major news"

def fetch_finnhub_social(symbol):
    today = datetime.today().date()
    key = (symbol, today)

    if key in CACHE["social"]:
        return CACHE["social"][key]

    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/social-sentiment",
            params={"symbol": symbol, "token": FINNHUB_KEY},
            timeout=5
        )
        data = safe_json(r)
        reddit = data.get("reddit", [])
        sentiment = "bullish" if reddit and reddit[0].get("mention", 0) > 5 else ""
        CACHE["social"][key] = sentiment
        return sentiment
    except Exception:
        return ""

# ---------------- COMBINE ----------------
def build_combined_summary(symbol):
    return {
        "symbol": symbol,
        "math_score": compute_math_score(symbol),
        "news": fetch_finnhub_news(symbol),
        "social": fetch_finnhub_social(symbol),
        "sector": SECTORS.get(symbol, "")
    }

def get_all_summaries(tickers):
    summaries = []
    for sym in tickers:
        try:
            summaries.append(build_combined_summary(sym))
        except Exception:
            log.exception(f"failed summary for {sym}")
    return summaries

# ---------------- TEST ----------------
if __name__ == "__main__":
    for s in get_all_summaries(TICKERS):
        log.info(s)
