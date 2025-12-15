import requests
import datetime
import time
import threading
from alpaca_trade_api.rest import REST
from dotenv import load_dotenv
from flask import Flask
import os

# ---------------- CONFIG ----------------

# load keys from .env
load_dotenv()  # reads .env in the same folder

API_KEY = os.getenv("DEEPSEEK_KEY")     # deepseek
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

# fail fast if any keys are missing
if not all([API_KEY, FINNHUB_KEY, ALPACA_KEY, ALPACA_SECRET]):
    raise SystemExit("missing env vars for API keys")

# Alpaca API init
api = REST(ALPACA_KEY, ALPACA_SECRET, base_url=BASE_URL)

# tickers
TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO"
]

# sectors
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
    "news": {},       # key: (symbol, date), value: [headlines]
    "social": {},     # key: (symbol, date), value: sentiment string
    "macro": {}       # key: date, value: (sp500, vix, crude)
}

# ---------------- HELPER FUNCTIONS ----------------
def safe_json(r):
    try:
        return r.json()
    except:
        return {}

# ----- NEWS -----
def fetch_finnhub_news(symbol):
    today = datetime.date.today()
    if (symbol, today) in CACHE['news']:
        return CACHE['news'][(symbol, today)]
    yesterday = today - datetime.timedelta(days=3)
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/company-news?symbol={symbol}&from={yesterday}&to={today}&token={FINNHUB_KEY}",
            timeout=5
        )
        news_data = safe_json(r)
        headlines = [item['headline'] for item in news_data[:2]] if news_data else ["no major news"]
        summary = " | ".join(headlines)
        CACHE['news'][(symbol, today)] = summary
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
        if data and isinstance(data,list) and "rating" in data[0]:
            return f"analyst avg {data[0]['rating']}"
    except:
        pass
    return ""

# ----- SOCIAL -----
def fetch_finnhub_social(symbol):
    today = datetime.date.today()
    if (symbol, today) in CACHE['social']:
        return CACHE['social'][(symbol, today)]
    yesterday = today - datetime.timedelta(days=1)
    try:
        r = requests.get(
            f"https://finnhub.io/api/v1/stock/social-sentiment?symbol={symbol}&from={yesterday}&token={FINNHUB_KEY}",
            timeout=5
        )
        social = safe_json(r)
        sentiment = ""
        if social.get("reddit") and social["reddit"][0]["mention"] > 5:
            sentiment = "bullish"
        CACHE['social'][(symbol, today)] = sentiment
        return sentiment
    except:
        return ""

# ----- FINNHUB PRICE DATA (intraday) -----
def fetch_finnhub_bars(symbol, resolution="60", count=150):
    now = int(time.time())
    start = now - (count * int(resolution) * 60)
    url = (
        f"https://finnhub.io/api/v1/stock/candle?"
        f"symbol={symbol}&resolution={resolution}&from={start}&to={now}&token={FINNHUB_KEY}"
    )
    try:
        r = requests.get(url, timeout=5)
        j = safe_json(r)
        if j.get("s") == "ok":
            return j
    except:
        pass
    return {}

def get_intraday_data(symbol):
    bars = fetch_finnhub_bars(symbol, resolution="1", count=200)
    if not bars or "c" not in bars:
        return []

    out = []
    for t, c, v in zip(bars["t"], bars["c"], bars["v"]):
        out.append({"time": t, "close": c, "volume": v})
    return out

def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None

    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    ma5 = round(sum(closes[-5:])/5, 2) if len(closes) >= 5 else None
    ma20 = round(sum(closes[-20:])/20, 2) if len(closes) >= 20 else None

    # rsi proper
    rsi_period = 14
    if len(closes) > rsi_period:
        deltas = [closes[i] - closes[i-1] for i in range(1, len(closes))]
        gains = [d if d > 0 else 0 for d in deltas]
        losses = [-d if d < 0 else 0 for d in deltas]

        avg_gain = sum(gains[:rsi_period])/rsi_period
        avg_loss = sum(losses[:rsi_period])/rsi_period

        for g, l in zip(gains[rsi_period:], losses[rsi_period:]):
            avg_gain = (avg_gain * (rsi_period - 1) + g) / rsi_period
            avg_loss = (avg_loss * (rsi_period - 1) + l) / rsi_period

        if avg_loss == 0:
            rsi = 100.0
        elif avg_gain == 0:
            rsi = 0.0
        else:
            rs = avg_gain / avg_loss
            rsi = round(100 - (100/(1+rs)), 2)
    else:
        rsi = None

    last = closes[-1]
    prev = closes[-2] if len(closes) > 1 else last
    delta = round(last - prev, 2)

    avg_vol30 = sum(volumes[-30:])/30 if len(volumes) >= 30 else volumes[-1]
    vol_change = round((volumes[-1]-avg_vol30)/avg_vol30*100, 1)

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
        tech = compute_technical(t)
        if not tech:
            continue

        news = fetch_finnhub_news(t)
        analyst = fetch_finnhub_analyst(t)
        social = fetch_finnhub_social(t)

        summaries.append({
            "symbol": t,
            "price": tech["price"],
            "change": tech["change"],
            "volume": tech["volume"],
            "ma5": tech["ma5"],
            "ma20": tech["ma20"],
            "rsi": tech["rsi"],
            "vol_change": tech["vol_change"],
            "sector": SECTORS.get(t,""),
            "news": news,
            "analyst": analyst,
            "social": social
        })
        time.sleep(0.05)
    return summaries

# ----- PROMPT -----
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. Calculate a numeric score 0-100 for each stock using these weights:\n"
        "- News/catalysts: 40%\n"
        "- Technical indicators (RSI, MA, volume): 35%\n"
        "- Social sentiment: 15%\n"
        "- Sector/macro context: 7%\n"
        "- Fundamentals: 3%\n\n"
        "Score based on last 3 months of price data, recent news (last 3 days), social sentiment, and sector/macro trends.\n"
        "Use the score to assign recommendation:\n"
        "80-100 → STRONG BUY\n"
        "65-79 → BUY\n"
        "45-64 → HOLD\n"
        "30-44 → SELL\n"
        "0-29 → STRONG SELL\n\n"
        "Output format: SYMBOL: RECOMMENDATION (1-2 word note if relevant). Do NOT include numeric scores in the output.\n\n"
    )

    for s in summaries:
        news_str = s['news'] if isinstance(s['news'], str) else "no major news"
        prompt += (
            f"{s['symbol']}: price {s['price']}, change {s['change']}, MA5 {s['ma5']}, MA20 {s['ma20']}, RSI {s['rsi']}, "
            f"volume {s['volume']} (Δ{s['vol_change']}%), sector {s['sector']}, analyst {s['analyst']}, social {s['social']}, "
            f"news: {news_str}\n"
        )
    return prompt

# ----- DEEPSEEK -----
def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type": "application/json"}
    payload = {"model": "deepseek-chat",
               "messages":[{"role":"user","content":prompt}],
               "temperature":0.2,
               "max_tokens":500}
    r = requests.post(url, headers=headers, json=payload)
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]

# ----- TRADING -----
def place_order(symbol, signal):
    account = api.get_account()
    buying_power = float(account.cash)
    position_size = 0
    signal = signal.upper()

    if signal == "STRONG BUY":
        position_size = buying_power * 0.1
    elif signal == "BUY":
        position_size = buying_power * 0.05
    elif signal in ["SELL","STRONG SELL"]:
        try:
            current_qty = int(api.get_position(symbol).qty)
            if current_qty > 0:
                api.submit_order(
                    symbol=symbol,
                    qty=current_qty,
                    side='sell',
                    type='market',
                    time_in_force='day'
                )
                print(f"Sold {current_qty} shares of {symbol}")
        except:
            print(f"No position to sell for {symbol}")
        return
    else:
        return

    intraday = get_intraday_data(symbol)
    price = intraday[-1]["close"] if intraday else 0
    qty = int(position_size // price) if price > 0 else 0
    if qty < 1:
        print(f"Not enough cash to buy {symbol}")
        return
    try:
        api.submit_order(
            symbol=symbol,
            qty=qty,
            side='buy',
            type='market',
            time_in_force='day'
        )
        print(f"Bought {qty} shares of {symbol} at ~{price}")
    except Exception as e:
        print(f"Error buying {symbol}: {e}")

# ----- MARKET TIME CONTROL -----
def run_bot():
    MARKET_OPEN = datetime.time(6,30)
    MARKET_CLOSE = datetime.time(13,0)

    run_today = set()  # track which of the two runs happened today
    last_run_date = None 

    while True:
        now = datetime.datetime.now(datetime.timezone.utc).astimezone()
        if now.weekday() >= 5:  # skip weekends
            run_today.clear()
            time.sleep(3600)
            continue

        open_run_time = (datetime.datetime.combine(now, MARKET_OPEN)+datetime.timedelta(minutes=20)).time()
        close_run_time = (datetime.datetime.combine(now, MARKET_CLOSE)-datetime.timedelta(minutes=10)).time()

        # 20 min after open
        if "open" not in run_today and now.time() >= open_run_time:
            run_today.add("open")
            execute_trading_logic()

        # 10 min before close
        if "close" not in run_today and now.time() >= close_run_time:
            run_today.add("close")
            execute_trading_logic()

      if now.date() != last_run_date:
    run_today.clear()
    last_run_date = now.date()

        time.sleep(30)  # check every 30 sec

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


# start bot thread
threading.Thread(target=run_bot, daemon=True).start()

# Flask app
app = Flask(__name__)

@app.route("/")
def home():
    return "Gigglyfarts bot running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)



