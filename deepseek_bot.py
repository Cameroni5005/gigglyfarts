import requests
import datetime
import time
import threading
from alpaca_trade_api.rest import REST, APIError
from dotenv import load_dotenv
from flask import Flask
import os
from io import StringIO
import sys
import logging

# ---------------- CONFIG ----------------
load_dotenv()

API_KEY = os.getenv("DEEPSEEK_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

if not all([API_KEY, FINNHUB_KEY, ALPACA_KEY, ALPACA_SECRET, TWELVEDATA_KEY]):
    raise SystemExit("missing env vars for API keys")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- ALPACA API ----------------
api = None
try:
    api = REST(ALPACA_KEY, ALPACA_SECRET, base_url=BASE_URL)
    log.info("Connected to Alpaca API (paper trading)")
except Exception:
    log.exception("Failed to initialize Alpaca REST client")

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

TIMEZONE_OFFSET = -8  # PST

# ---------- CACHE ----------
CACHE = {
    "news": {},
    "social": {}
}

# ---------------- HELPERS ----------------
def safe_json(r):
    try:
        return r.json()
    except ValueError:
        return {}

# ----- NEWS -----
def fetch_finnhub_news(symbol):
    today = datetime.date.today()
    key = (symbol, today)
    if key in CACHE['news']:
        return CACHE['news'][key]
    yesterday = today - datetime.timedelta(days=3)
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = safe_json(r)
        headlines = [item['headline'] for item in data[:2]] if data else ["no major news"]
        summary = " | ".join(headlines)
        CACHE['news'][key] = summary
        return summary
    except Exception:
        return "no major news"

# ----- ANALYST -----
def fetch_finnhub_analyst(symbol):
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/analyst-recommendation?symbol={symbol}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = safe_json(r)
        rating = data[0].get("rating") if data and isinstance(data, list) else None
        return f"analyst avg {rating}" if rating else ""
    except Exception:
        return ""

# ----- SOCIAL -----
def fetch_finnhub_social(symbol):
    today = datetime.date.today()
    key = (symbol, today)
    if key in CACHE['social']:
        return CACHE['social'][key]
    yesterday = today - datetime.timedelta(days=1)
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/social-sentiment?symbol={symbol}&from={yesterday}&token={FINNHUB_KEY}",
            timeout=5
        )
        data = safe_json(r)
        sentiment = ""
        if isinstance(data, dict):
            reddit = data.get("reddit")
            if reddit and isinstance(reddit, list) and len(reddit) > 0:
                mention = reddit[0].get("mention", 0) if isinstance(reddit[0], dict) else 0
                sentiment = "bullish" if mention > 5 else ""
        CACHE['social'][key] = sentiment
        return sentiment
    except Exception:
        return ""

# ----- PRICE DATA -----
TWELVE_RATE_LIMIT = 8      # credits per minute
TWELVE_WINDOW = 61         # seconds
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
        log.info(f"{symbol} bars raw: {data}")

        if isinstance(data, dict) and data.get("code") == 429:
            log.warning(f"{symbol} RATE LIMITED by TwelveData")
            return []

        if not data or "values" not in data:
            log.warning(f"{symbol} no valid bars, returning empty list")
            return []

        bars = [{
            "time": v.get("datetime"),
            "close": float(v.get("close", 0)),
            "volume": float(v.get("volume", 0))
        } for v in reversed(data["values"])]

        return bars

    except Exception as e:
        log.exception(f"{symbol} bars error: {e}")
        return []

def get_intraday_data(symbol):
    bars = fetch_twelvedata_bars(symbol)
    if not isinstance(bars, list):
        return []
    return bars

def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    ma5 = round(sum(closes[-5:]) / 5, 2) if len(closes) >= 5 else None
    ma20 = round(sum(closes[-20:]) / 20, 2) if len(closes) >= 20 else None

    rsi = None
    rsi_period = 14
    if len(closes) > rsi_period:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[:rsi_period]) / rsi_period
        avg_loss = sum(losses[:rsi_period]) / rsi_period
        for g, l in zip(gains[rsi_period:], losses[rsi_period:]):
            avg_gain = (avg_gain * (rsi_period - 1) + g) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + l) / rsi_period
        rsi = 100.0 if avg_loss == 0 else 0.0 if avg_gain == 0 else round(100 - (100 / (1 + avg_gain / avg_loss)), 2)

    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    delta = round(last - prev, 2)
    avg_vol30 = (sum(volumes[-30:]) / 30) if len(volumes) >= 30 else volumes[-1]
    vol_change = round((volumes[-1] - avg_vol30) / avg_vol30 * 100, 1) if avg_vol30 != 0 else 0

    return {
        "price": round(last, 2),
        "change": delta,
        "ma5": ma5,
        "ma20": ma20,
        "rsi": rsi,
        "volume": volumes[-1],
        "vol_change": vol_change
    }

# ----- STOCK SUMMARY -----
def get_stock_summary(tickers):
    summaries = []
    for t in tickers:
        try:
            tech = compute_technical(t)
            if not tech:
                continue
            summaries.append({
                "symbol": t,
                "price": tech["price"],
                "change": tech["change"],
                "volume": tech["volume"],
                "ma5": tech["ma5"],
                "ma20": tech["ma20"],
                "rsi": tech["rsi"],
                "vol_change": tech["vol_change"],
                "sector": SECTORS.get(t, ""),
                "news": fetch_finnhub_news(t),
                "analyst": fetch_finnhub_analyst(t),
                "social": fetch_finnhub_social(t)
            })
            time.sleep(0.05)
        except Exception as e:
            log.exception(f"error building summary for {t}: {e}")
    return summaries

# ----- PROMPT -----
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. Calculate a numeric score 0-100 for each stock using these weights:\n"
        "- News/catalysts: 40%\n- Technical indicators (RSI, MA, volume): 35%\n"
        "- Social sentiment: 15%\n- Sector/macro context: 7%\n- Fundamentals: 3%\n\n"
        "Score based on last 3 months of price data, recent news (last 3 days), social sentiment, and sector/macro trends.\n"
        "Use the score to assign recommendation:\n"
        "80-100 → STRONG BUY\n65-79 → BUY\n45-64 → HOLD\n30-44 → SELL\n0-29 → STRONG SELL\n\n"
        "Output format: SYMBOL: RECOMMENDATION (1-2 word note if relevant). Do NOT include numeric scores in the output.\n\n"
    )
    prompt += "\n".join(
        f"{s['symbol']}: price {s['price']}, change {s['change']}, MA5 {s['ma5']}, MA20 {s['ma20']}, "
        f"RSI {s['rsi']}, volume {s['volume']} (Δ{s['vol_change']}%), sector {s['sector']}, "
        f"analyst {s['analyst']}, social {s['social']}, news: {s['news'] if isinstance(s['news'], str) else 'no major news'}"
        for s in summaries
    )
    return prompt

# ----- DEEPSEEK -----
def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat", "messages": [{"role": "user", "content": prompt}], "temperature": 0.2, "max_tokens": 500}
    r = requests.post(url, headers=headers, json=payload, timeout=30)
    r.raise_for_status()
    data = r.json()
    return data.get("choices", [])[0].get("message", {}).get("content", "") if isinstance(data, dict) else ""

# ----- TRADING -----
def place_order(symbol, signal):
    try:
        # ----- ACCOUNT CHECK -----
        try:
            account = api.get_account()
            log.info("Alpaca account status: %s", account.status)
            log.info("Trading blocked: %s", account.trading_blocked)
            log.info("Cash: %s", account.cash)
            log.info("Buying power: %s", account.buying_power)
        except Exception as e:
            log.exception("Failed to get Alpaca account: %s", e)
            return

        if account.status != "ACTIVE":
            log.warning("Account not ACTIVE — skipping trade")
            return
        if account.trading_blocked:
            log.warning("Trading blocked — skipping trade")
            return

        signal = signal.upper().strip()
        max_retries = 3
        retry_delay = 5

        buying_power = float(account.cash)
        position_size = 0.0

        if "STRONG BUY" in signal:
            position_size = buying_power * 0.1
        elif signal == "BUY":
            position_size = buying_power * 0.05
        elif "SELL" in signal:
            for attempt in range(max_retries):
                try:
                    pos = api.get_position(symbol)
                    qty = int(pos.qty)
                    if qty > 0:
                        api.submit_order(symbol=symbol, qty=qty, side='sell', type='market', time_in_force='day')
                        log.info("Sold %s shares of %s", qty, symbol)
                    else:
                        log.info("No position to sell for %s", symbol)
                    break
                except Exception as e:
                    log.exception("Sell attempt %d error for %s: %s", attempt+1, symbol, e)
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
            return
        else:
            log.info("Unknown or HOLD signal for %s: '%s'", symbol, signal)
            return

        # BUY path
        intraday = get_intraday_data(symbol)
        price = intraday[-1]["close"] if intraday else 0
        qty = int(position_size // price) if price > 0 else 0

        if qty < 1:
            log.warning("%s skipped — qty=0 (price %s, buying power %s)", symbol, price, buying_power)
            return

        log.info("SIGNAL READY: %s → %s, calculated qty: %s", symbol, signal, qty)

        # check market clock
        try:
            clock = api.get_clock()
            log.info("Market open: %s, current time: %s", clock.is_open, clock.timestamp)
            if not getattr(clock, "is_open", False):
                log.info("Market closed — skipping trades")
                return
        except Exception as e:
            log.exception("Failed to get market clock: %s", e)

        for attempt in range(max_retries):
            try:
                api.submit_order(symbol=symbol, qty=qty, side='buy', type='market', time_in_force='day')
                log.info("BOUGHT %s shares of %s @ ~%s", qty, symbol, price)
                break
            except Exception as e:
                log.exception("Alpaca BUY error %s attempt %d: %s", symbol, attempt+1, e)
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    log.error("FAILED to buy %s after %d attempts", symbol, max_retries)

    except Exception as e:
        log.exception("place_order fatal error: %s", e)

# ----- BOT LOOP -----
def run_bot():
    log.info("bot loop online")

    last_trade_day = None
    traded_open = False
    traded_close = False
    last_trade_time = datetime.datetime.min

    while True:
        try:
            try:
                clock = api.get_clock()
                now = clock.timestamp
                log.info("bot alive %s, market open: %s", now, clock.is_open)
            except Exception as e:
                log.exception("Failed to get market clock: %s", e)
                time.sleep(30)
                continue

            if last_trade_day != now.date():
                traded_open = False
                traded_close = False
                last_trade_day = now.date()

            if not clock.is_open:
                log.info("market closed — sleeping")
                time.sleep(60)
                continue

            calendar = api.get_calendar(start=now.date(), end=now.date())
            if calendar:
                market_open = calendar[0].open
                market_close = calendar[0].close
            else:
                market_open = now.replace(hour=9, minute=30, second=0)
                market_close = now.replace(hour=16, minute=0, second=0)

            minutes_since_open = (now - market_open).total_seconds() / 60
            minutes_until_close = (market_close - now).total_seconds() / 60

            if not traded_open and minutes_since_open >= 20:
                log.info("triggering trading logic (open)")
                execute_trading_logic()
                traded_open = True

            if not traded_close and minutes_until_close <= 10:
                log.info("triggering trading logic (close)")
                execute_trading_logic()
                traded_close = True

            if (datetime.datetime.now() - last_trade_time).total_seconds() > 300:
                execute_trading_logic()
                last_trade_time = datetime.datetime.now()

        except Exception as e:
            log.exception("run_bot error: %s", e)

        time.sleep(30)

def execute_trading_logic():
    summaries = get_stock_summary(TICKERS)
    if not summaries:
        log.warning("no stock data, skipping")
        return

    prompt = build_prompt(summaries)
    try:
        signals = ask_deepseek(prompt)
        log.info("\nDAILY SHORT-TERM STOCK SIGNALS:\n%s", signals)
    except Exception as e:
        log.exception("error talking to DeepSeek: %s", e)
        return

    # parse signals naively line by line
    for line in signals.splitlines():
        if not line.strip():
            continue
        parts = line.split(":")
        if len(parts) < 2:
            continue
        symbol = parts[0].strip()
        sig = parts[1].strip()
        place_order(symbol, sig)

# ----- FLASK APP -----
app = Flask(__name__)

@app.route("/")
def home():
    return "Trading bot online — check logs for activity"

def start_flask():
    port = int(os.environ.get("PORT", 5000))
    log.info("starting flask on port %s", port)
    app.run(host="0.0.0.0", port=port)

# ----- THREADING -----
threading.Thread(target=run_bot, daemon=True).start()
threading.Thread(target=start_flask, daemon=True).start()

# keep main alive
while True:
    time.sleep(60)
