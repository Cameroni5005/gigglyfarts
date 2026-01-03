import os
import time
import threading
import logging
from datetime import datetime, timedelta, timezone
import requests
from alpaca_trade_api.rest import REST, APIError
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()

ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")
BASE_URL = "https://paper-api.alpaca.markets"

if not all([ALPACA_KEY, ALPACA_SECRET, TWELVEDATA_KEY]):
    raise SystemExit("missing Alpaca or TwelveData API keys")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- ALPACA ----------------
api = REST(ALPACA_KEY, ALPACA_SECRET, BASE_URL, api_version='v2')
log.info("Connected to Alpaca API (paper trading)")

# ---------------- GLOBAL LOCK ----------------
trade_lock = threading.Lock()

# ---------------- STOCK CONFIG ----------------
TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO"
]

# ---------------- TWELVEDATA ----------------
TWELVE_RATE_LIMIT = 8
TWELVE_WINDOW = 61
_twelve_calls = []
_twelve_lock = threading.Lock()

def safe_json(r):
    try:
        return r.json()
    except ValueError:
        return {}

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
    try:
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
        bars = [{"time": v.get("datetime"), "close": float(v.get("close",0)),
                 "high": float(v.get("high",0)), "low": float(v.get("low",0)),
                 "volume": float(v.get("volume",0))} for v in reversed(data["values"])]
        return bars
    except Exception:
        log.exception(f"{symbol} bars error")
        return []

def get_intraday_data(symbol):
    bars = fetch_twelvedata_bars(symbol)
    return bars if isinstance(bars,list) else []

# ---------------- TECHNICAL CALCULATIONS ----------------
def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    ma5 = sum(closes[-5:])/5 if len(closes)>=5 else None
    ma20 = sum(closes[-20:])/20 if len(closes)>=20 else None

    # RSI 14
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
    avg_vol30 = sum(volumes[-30:])/30 if len(volumes)>=30 else volumes[-1]
    vol_change = (volumes[-1]-avg_vol30)/avg_vol30*100 if avg_vol30!=0 else 0

    # ATR
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
        "ma5": ma5,
        "ma20": ma20,
        "rsi": rsi,
        "volume": volumes[-1],
        "vol_change": vol_change,
        "atr": atr
    }

# ---------------- NORMALIZE TO 0-100 ----------------
def normalize_tech(tech):
    if not tech:
        return 50  # neutral

    score = 50  # start neutral

    # RSI influence
    if tech["rsi"] is not None:
        if tech["rsi"] < 30:
            score += 20
        elif tech["rsi"] > 70:
            score -= 20

    # MA crossover influence
    if tech["ma5"] and tech["ma20"]:
        if tech["ma5"] > tech["ma20"]:
            score += 10
        elif tech["ma5"] < tech["ma20"]:
            score -= 10

    # volume spike
    if tech["vol_change"] > 50:
        score += 10

    # clamp between 0-100
    return max(0, min(100, score))

# ---------------- PLACE ORDER ----------------
def place_order(symbol, score):
    if not api:
        return
    with trade_lock:
        try:
            clock = api.get_clock()
            if not clock.is_open:
                log.info(f"{symbol} market closed — skipping buy/sell")
                return

            account = api.get_account()
            risk_dollars = float(account.equity)*0.01  # 1% per trade

            tech = compute_technical(symbol)
            if not tech or not tech['atr']:
                return

            qty = int(risk_dollars // tech['atr'])
            if qty < 1:
                return

            # decide signal based on math score
            if score >= 60:
                side = 'buy'
            elif score <= 40:
                side = 'sell'
            else:
                return  # HOLD

            price = tech["price"]
            stop_price = round(price - tech["atr"],2) if side=='buy' else round(price + tech["atr"],2)
            take_price = round(price + tech["atr"]*2,2) if side=='buy' else round(price - tech["atr"]*2,2)

            order_class = 'bracket' if side=='buy' else None

            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side=side,
                type='limit' if side=='buy' else 'market',
                limit_price=price if side=='buy' else None,
                time_in_force='day',
                order_class=order_class,
                take_profit={'limit_price':take_price} if side=='buy' else None,
                stop_loss={'stop_price':stop_price} if side=='buy' else None
            )
            log.info(f"{side.upper()} {qty} shares of {symbol} @ {price}, stop {stop_price}, take {take_price}")
        except Exception:
            log.exception(f"place_order error for {symbol}")

# ---------------- RUN MATH BOT ----------------
def run_math_bot():
    log.info("math bot loop online")
    while True:
        try:
            clock = api.get_clock()
            if clock.is_open:
                for sym in TICKERS:
                    tech = compute_technical(sym)
                    score = normalize_tech(tech)
                    log.info(f"{sym} → math score {score}")
                    place_order(sym, score)
            else:
                log.info("market closed — sleeping")
        except Exception:
            log.exception("math bot error")
        time.sleep(300)  # every 5 min

# ---------------- MAIN ----------------
if __name__ == "__main__":
    threading.Thread(target=run_math_bot, daemon=True).start()
    log.info("Math bot started")
    while True:
        time.sleep(60)
