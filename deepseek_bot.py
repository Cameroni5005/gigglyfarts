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
    except:
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
    except:
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
        sentiment = "bullish" if data.get("reddit") and data["reddit"][0]["mention"] > 5 else ""
        CACHE['social'][key] = sentiment
        return sentiment
    except:
        return ""

# ----- PRICE DATA -----
# rotate through multiple 12Data keys per request
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")

# replace your multiple keys logic with just your single key
TWELVEDATA_KEY = os.getenv("TWELVEDATA_KEY")

import time, threading

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
        # 🔒 GLOBAL rate limiter (THIS is the fix)
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

        # 🚫 Rate limit or API error
        if data.get("code") == 429:
            print(symbol, "RATE LIMITED by TwelveData")
            return []

        # 🚫 Invalid / empty response
        if not data or "values" not in data:
            print(f"{symbol} no valid bars, returning empty list")
            return []

        # ✅ Parse bars
        bars = [{
            "time": v["datetime"],
            "close": float(v["close"]),
            "volume": float(v["volume"])
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

    ma5 = round(sum(closes[-5:])/5, 2) if len(closes) >= 5 else None
    ma20 = round(sum(closes[-20:])/20, 2) if len(closes) >= 20 else None

    rsi = None
    rsi_period = 14
    if len(closes) > rsi_period:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]
        avg_gain = sum(gains[:rsi_period])/rsi_period
        avg_loss = sum(losses[:rsi_period])/rsi_period
        for g, l in zip(gains[rsi_period:], losses[rsi_period:]):
            avg_gain = (avg_gain*(rsi_period-1) + g)/rsi_period
            avg_loss = (avg_loss*(rsi_period-1) + l)/rsi_period
        rsi = 100.0 if avg_loss == 0 else 0.0 if avg_gain == 0 else round(100-(100/(1+avg_gain/avg_loss)), 2)

    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    delta = round(last - prev, 2)
    avg_vol30 = sum(volumes[-30:])/30 if len(volumes) >= 30 else volumes[-1]
    vol_change = round((volumes[-1]-avg_vol30)/avg_vol30*100, 1)

    return {"price": round(last,2), "change": delta, "ma5": ma5, "ma20": ma20, "rsi": rsi,
            "volume": volumes[-1], "vol_change": vol_change}

# ----- STOCK SUMMARY -----
def get_stock_summary(tickers):
    summaries = []
    for t in tickers:
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
    payload = {"model":"deepseek-chat","messages":[{"role":"user","content":prompt}],"temperature":0.2,"max_tokens":500}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ----- TRADING -----
def place_order(symbol, signal):
    signal = signal.upper()
    max_retries = 3
    retry_delay = 5

    for attempt in range(max_retries):
        try:
            account = api.get_account()
            buying_power = float(account.cash)
            break
        except Exception as e:
            if attempt < max_retries-1:
                time.sleep(retry_delay)
            else:
                print(f"Failed to get account: {e}")
                return

    # rest of your logic (BUY/SELL) remains the same
    # wrap api.submit_order in similar retry loop


    if signal == "STRONG BUY":
        position_size = buying_power * 0.1
    elif signal == "BUY":
        position_size = buying_power * 0.05
    elif signal in ["SELL", "STRONG SELL"]:
        for attempt in range(max_retries):
            try:
                qty = int(api.get_position(symbol).qty)
                if qty > 0:
                    api.submit_order(
                        symbol=symbol,
                        qty=qty,
                        side='sell',
                        type='market',
                        time_in_force='day'
                    )
                    print(f"Sold {qty} shares of {symbol}")
                break
            except Exception as e:
                if attempt < max_retries - 1:
                    print(f"sell order error, retrying in {retry_delay}s: {e}")
                    time.sleep(retry_delay)
                else:
                    print(f"No position to sell or failed after {max_retries} attempts for {symbol}")
        return
    else:
        return

    # get intraday price
    intraday = get_intraday_data(symbol)
    price = intraday[-1]["close"] if intraday else 0
    qty = int(position_size // price) if price > 0 else 0
    if qty < 1:
        print(f"Not enough cash to buy {symbol}")
        return

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
            print(f"Bought {qty} shares of {symbol} at ~{price}")
            break
        except Exception as e:
            if attempt < max_retries - 1:
                print(f"buy order error, retrying in {retry_delay}s: {e}")
                time.sleep(retry_delay)
            else:
                print(f"Failed to buy {symbol} after {max_retries} attempts: {e}")


# ----- BOT LOOP -----
def run_bot():
    print("bot loop online")
    MARKET_OPEN = datetime.time(6,30)
    MARKET_CLOSE = datetime.time(13,0)
    run_today = set()
    last_run_date = None

    while True:
        now = datetime.datetime.now(datetime.timezone.utc).astimezone()
        print("bot alive", now)
        if now.date() != last_run_date:
            run_today.clear()
            last_run_date = now.date()

        if now.weekday() >= 5:
            time.sleep(3600)
            continue

        open_run_time = (datetime.datetime.combine(now, MARKET_OPEN)+datetime.timedelta(minutes=20)).time()
        close_run_time = (datetime.datetime.combine(now, MARKET_CLOSE)-datetime.timedelta(minutes=10)).time()

        if "open" not in run_today and now.time() >= open_run_time:
            print("triggering trading logic (open)", now.time())
            run_today.add("open")
            execute_trading_logic()

        if "close" not in run_today and now.time() >= close_run_time:
            print("triggering trading logic (close)", now.time())
            run_today.add("close")
            execute_trading_logic()

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

    for line in signals.splitlines():
        parts = line.split(":")
        if len(parts) >= 2:
            sym = parts[0].strip()
            sig = parts[1].split("(")[0].strip()
            place_order(sym, sig)

# ----- FLASK APP -----
app = Flask(__name__)

@app.route("/")
def home():
    return "Gigglyfarts bot running"

@app.route("/trigger")
def trigger():
    old_stdout = sys.stdout
    sys.stdout = mystdout = StringIO()
    try:
        print("manual trigger activated")

        # ---- check keys ----
        keys = {
            "DEEPSEEK_KEY": API_KEY,
            "FINNHUB_KEY": FINNHUB_KEY,
            "ALPACA_KEY": ALPACA_KEY,
            "ALPACA_SECRET": ALPACA_SECRET,
            "TWELVEDATA_KEY": TWELVEDATA_KEY
        }
        missing = False
        for k, v in keys.items():
            if v:
                print(f"{k}: FOUND")
            else:
                print(f"{k}: MISSING")
                missing = True

if missing:
    print("cannot run trading logic, missing keys")
else:
    execute_trading_logic()


    except Exception as e:
        print("error during manual trigger:", e)
    finally:
        sys.stdout = old_stdout

    return "<pre>" + mystdout.getvalue() + "</pre>"

# start bot thread
threading.Thread(target=run_bot, daemon=True).start()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)












