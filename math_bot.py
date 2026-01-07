import time
import threading
import logging
import yfinance as yf
from datetime import datetime
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()

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
def get_intraday_data(symbol):
    now = time.time()
    cached = INTRADAY_CACHE.get(symbol)
    if cached:
        data, ts = cached
        if now - ts < INTRADAY_TTL:
            return data

    try:
        df = yf.download(
            tickers=symbol,
            period="1d",
            interval="1m",
            progress=False
        )

        bars = []
        for idx, row in df.iterrows():
            bars.append({
                "time": idx.strftime("%Y-%m-%d %H:%M:%S"),
                "close": float(row["Close"].iloc[0]),
                "high": float(row["High"].iloc[0]),
                "low": float(row["Low"].iloc[0]),
                "volume": float(row["Volume"].iloc[0]),
            })

        if bars:
            INTRADAY_CACHE[symbol] = (bars, now)
        return bars

    except Exception:
        log.exception(f"yfinance fetch failed for {symbol}")
        return []

# ---------------- TECHNICALS ----------------
def compute_rsi(closes, period=14):
    if len(closes) <= period:
        return None

    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0) for d in deltas]
    losses = [abs(min(d, 0)) for d in deltas]

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    for g, l in zip(gains[period:], losses[period:]):
        avg_gain = (avg_gain * (period - 1) + g) / period
        avg_loss = (avg_loss * (period - 1) + l) / period

    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def compute_atr(data, period=14):
    if len(data) < period + 1:
        return None

    trs = []
    for i in range(1, len(data)):
        h = data[i]["high"]
        l = data[i]["low"]
        pc = data[i - 1]["close"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))

    return sum(trs[-period:]) / period

# ---------------- CORE TECH ----------------
def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    now = datetime.now().time()
    if now < datetime.strptime("10:30", "%H:%M").time():
        short, long = 5, 15
    elif now < datetime.strptime("14:30", "%H:%M").time():
        short, long = 10, 20
    else:
        short, long = 7, 15

    if len(closes) < long:
        return None

    ema_short = sum(closes[-short:]) / short
    ema_long = sum(closes[-long:]) / long

    rsi = compute_rsi(closes)
    atr = compute_atr(data)

    vol_window = min(120, len(volumes))
    avg_vol = sum(volumes[-vol_window:]) / vol_window
    vol_change = ((volumes[-1] - avg_vol) / avg_vol) * 100 if avg_vol else 0

    return {
        "price": closes[-1],
        "ema_short": ema_short,
        "ema_long": ema_long,
        "rsi": rsi,
        "atr": atr,
        "vol_change": vol_change
    }

# ---------------- MATH SCORE ----------------
def compute_math_score(symbol):
    tech = compute_technical(symbol)
    if not tech:
        return None

    # trend 35%
    trend_score = 100 if tech["ema_short"] > tech["ema_long"] else 0

    # rsi 25% (ideal zone 30–70)
    if tech["rsi"] is None:
        rsi_score = 50
    elif tech["rsi"] < 30:
        rsi_score = 100
    elif tech["rsi"] > 70:
        rsi_score = 0
    else:
        rsi_score = 50

    # volume 20%
    vol_score = max(min(tech["vol_change"], 100), 0)

    # atr sanity 20% (filters chop)
    atr_score = 100 if tech["atr"] and tech["atr"] > 0 else 50

    score = (
        trend_score * 0.35 +
        rsi_score * 0.25 +
        vol_score * 0.20 +
        atr_score * 0.20
    )

    return round(score, 2)

# ---------------- SUMMARY ----------------
def build_summary(symbol):
    tech = compute_technical(symbol)
    if not tech:
        return None

    return {
        "symbol": symbol,
        "score": compute_math_score(symbol),
        "price": tech["price"],
        "rsi": tech["rsi"],
        "atr": tech["atr"]
    }

def scan_market():
    results = []
    for sym in TICKERS:
        try:
            s = build_summary(sym)
            if s:
                results.append(s)
        except Exception:
            log.exception(f"scan failed for {sym}")
    return results

# ---------------- THREAD ----------------
def market_loop():
    while True:
        scans = scan_market()
        scans.sort(key=lambda x: x["score"], reverse=True)
        log.info(f"top picks: {scans[:5]}")
        time.sleep(60)

# ---------------- AUTO START ----------------
if __name__ == "__main__":
    log.info("math bot live. no ai. no bullshit.")
    threading.Thread(target=market_loop, daemon=True).start()
    while True:
        time.sleep(3600)
