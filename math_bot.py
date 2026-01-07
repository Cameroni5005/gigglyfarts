import os
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
        df = yf.download(tickers=symbol, period="1d", interval="1m", progress=False)
        bars = []
        for idx, row in df.iterrows():
            bars.append({
                "time": idx.strftime("%Y-%m-%d %H:%M:%S"),
                "close": float(row["Close"]),
                "high": float(row["High"]),
                "low": float(row["Low"]),
                "volume": float(row["Volume"]),
            })
        if bars:
            INTRADAY_CACHE[symbol] = (bars, now)
        return bars
    except Exception:
        log.exception(f"yfinance fetch failed for {symbol}")
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

# ---------------- THREAD CONTROL ----------------
_yf_thread_running = False
_yf_thread = None

def _yf_thread_loop():
    while True:
        get_all_summaries(TICKERS)
        time.sleep(60)  # adjust polling frequency if needed

def start_yf_thread():
    global _yf_thread_running, _yf_thread
    if _yf_thread_running:
        log.info("yfinance thread already running, ignoring trigger")
        return
    _yf_thread_running = True
    _yf_thread = threading.Thread(target=_yf_thread_loop, daemon=True)
    _yf_thread.start()
    log.info("yfinance thread started")

# ---------------- MAIN BLOCK ----------------
if __name__ == "__main__":
    log.info("script loaded but not triggered")
    # nothing runs until you call start_yf_thread()
