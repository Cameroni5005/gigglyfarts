import requests
import datetime
import time
import threading
from alpaca_trade_api.rest import REST
from dotenv import load_dotenv
from flask import Flask
import os
from io import StringIO
import sys

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

api = REST(ALPACA_KEY, ALPACA_SECRET, base_url=BASE_URL)

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
            print(f"TwelveData rate limit hit, sleeping {sleep_for:.1f}s")
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
        print(symbol, "bars raw:", data)

        if isinstance(data, dict) and data.get("code") == 429:
            print(symbol, "RATE LIMITED by TwelveData")
            return []

        if not data or "values" not in data:
            print(f"{symbol} no valid bars, returning empty list")
            return []

        bars = [{
            "time": v.get("datetime"),
            "close": float(v.get("close", 0)),
            "volume": float(v.get("volume", 0))
        } for v in reversed(data["values"])]

        return bars

    except Exception as e:
        print(symbol, "bars error:", e)
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
            print(f"error building summary for {t}: {e}")
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
        account = api.get_account()
        print("alpaca status:", account.status)
        print("trading blocked:", account.trading_blocked)

        if account.status != "ACTIVE" or account.trading_blocked:
            print("alpaca account not tradable")
            return

        signal = signal.upper().strip()
        max_retries = 3
        retry_delay = 5

        # fetch buying power with retries
        buying_power = 0.0
        for attempt in range(max_retries):
            try:
                account = api.get_account()
                buying_power = float(account.cash)
                break
            except Exception as e:
                print(f"get_account attempt {attempt+1} failed: {e}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    print(f"Failed to get account after {max_retries} attempts: {e}")
                    return

        position_size = 0.0

        if "STRONG BUY" in signal:
            position_size = buying_power * 0.1
        elif signal == "BUY":
            position_size = buying_power * 0.05
        elif "SELL" in signal:
            # sell existing position if any
            for attempt in range(max_retries):
                try:
                    pos = api.get_position(symbol)
                    qty = int(pos.qty)
                    if qty > 0:
                        api.submit_order(
                            symbol=symbol,
                            qty=qty,
                            side='sell',
                            type='market',
                            time_in_force='day'
                        )
                        print(f"Sold {qty} shares of {symbol}")
                    else:
                        print(f"no position to sell for {symbol}")
                    break
                except Exception as e:
                    print(f"sell attempt {attempt+1} error for {symbol}: {e}")
                    if attempt < max_retries - 1:
                        time.sleep(retry_delay)
                    else:
                        print(f"No position to sell or failed after {max_retries} attempts for {symbol}")
            return
        else:
            print(f"unknown or hold signal for {symbol}: '{signal}'")
            return

        # BUY path: get recent intraday price
        intraday = get_intraday_data(symbol)
        price = intraday[-1]["close"] if intraday else 0
        qty = int(position_size // price) if price > 0 else 0

        if qty < 1:
            print(f"{symbol} skipped — qty=0 (price {price}, buying power {buying_power})")
            return

        print("SIGNAL READY:", symbol, signal)
        print("buying power:", buying_power, "price:", price, "calculated qty:", qty)

        # check market clock before submitting market orders
        try:
            clock = api.get_clock()
            if not getattr(clock, "is_open", False):
                print("market closed — skipping trades")
                return
        except Exception as e:
            print("failed to get market clock:", e)

        # submit buy order with retries
        for attempt in range(max_retries):
            try:
                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side='buy',
                    type='market',
                    time_in_force='day'
                )
                print(f"BOUGHT {qty} shares of {symbol} @ ~{price}")
                break
            except Exception as e:
                print(f"alpaca BUY error {symbol} attempt {attempt+1}:", repr(e))
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                else:
                    print(f"FAILED to buy {symbol} after {max_retries} attempts")
    except Exception as e:
        print("place_order fatal error:", e)

# ----- BOT LOOP -----
def run_bot():
    print("bot loop online")

    last_trade_day = None
    traded_open = False
    traded_close = False
    last_trade_time = datetime.datetime.min

    while True:
        try:
            clock = api.get_clock()
            now = clock.timestamp

            print("bot alive", now, "market open:", clock.is_open)

            if last_trade_day != now.date():
                traded_open = False
                traded_close = False
                last_trade_day = now.date()

            if not clock.is_open:
                print("market closed — sleeping")
                time.sleep(60)
                continue

            # SAFER: use Alpaca calendar
            calendar = api.get_calendar(start=now.date(), end=now.date())
            if calendar:
                market_open = calendar[0].open
                market_close = calendar[0].close
            else:
                # fallback
                market_open = now.replace(hour=9, minute=30, second=0)
                market_close = now.replace(hour=16, minute=0, second=0)

            minutes_since_open = (now - market_open).total_seconds() / 60
            minutes_until_close = (market_close - now).total_seconds() / 60

            # original triggers
            if not traded_open and minutes_since_open >= 20:
                print("triggering trading logic (open)")
                execute_trading_logic()
                traded_open = True

            if not traded_close and minutes_until_close <= 10:
                print("triggering trading logic (close)")
                execute_trading_logic()
                traded_close = True

            # FORCE run every 5 min
            if (datetime.datetime.now() - last_trade_time).total_seconds() > 300:
                execute_trading_logic()
                last_trade_time = datetime.datetime.now()

        except Exception as e:
            print("run_bot error:", e)

        time.sleep(30)


def execute_trading_logic():
    summaries = get_stock_summary(TICKERS)
    if not summaries:
        print("no stock data, skipping")
        return

    prompt = build_prompt(summaries)
    try:
        signals = ask_deepseek(prompt)
        print("\nDAILY SHORT-TERM STOCK SIGNALS:")
        print(signals)
    except Exception as e:
        print("error talking to Deepseek:", e)
        return

    # prevent duplicate signals
    seen = set()
    for line in signals.splitlines():
        if ":" not in line:
            continue

        sym, raw_sig = line.split(":", 1)
        sym = sym.strip().upper()

        if sym in seen:
            print(f"duplicate signal ignored for {sym}")
            continue
        seen.add(sym)

        sig = raw_sig.split("(")[0].strip().upper()
        print("parsed signal:", sym, "→", sig)

        place_order(sym, sig)

# ----- FLASK APP -----
app = Flask(__name__)

@app.route("/")
def home():
    return "Giggity Bot Online!"

@app.route("/trigger")
def trigger():
    threading.Thread(target=execute_trading_logic, daemon=True).start()
    return "Triggered trading logic!"

if __name__ == "__main__":
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        t = threading.Thread(target=run_bot, daemon=True)
        t.start()
    port = int(os.getenv("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
