import yfinance as yf
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
run_now = False

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
    "price": {},      # key: symbol, value: (date, dataframe)
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
        # take 2 headlines max
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

# ----- PRICE DATA -----
def get_price_data(symbol):
    today = datetime.date.today()
    if symbol in CACHE['price'] and CACHE['price'][symbol][0] == today:
        return CACHE['price'][symbol][1]
    data = yf.download(symbol, period="3mo", interval="1d", progress=False, auto_adjust=True)
    CACHE['price'][symbol] = (today, data)
    return data

# ----- MACRO -----
def get_macro_snippet():
    today = datetime.date.today()
    if today in CACHE['macro']:
        sp500, vix, crude = CACHE['macro'][today]
        return f"macro: S&P {sp500:.2f}, VIX {vix:.2f}, Crude {crude:.2f}"
    try:
        sp500 = yf.download("^GSPC", period="5d", interval="1d", progress=False)['Close'][-1]
        vix = yf.download("^VIX", period="5d", interval="1d", progress=False)['Close'][-1]
        crude = yf.download("CL=F", period="5d", interval="1d", progress=False)['Close'][-1]
        CACHE['macro'][today] = (sp500, vix, crude)
        return f"macro: S&P {sp500:.2f}, VIX {vix:.2f}, Crude {crude:.2f}"
    except:
        return "macro data unavailable"

# ----- STOCK SUMMARY -----
def get_stock_summary(tickers):
    summaries = []
    for t in tickers:
        data = get_price_data(t)
        if len(data) < 2:
            continue
        last = data.iloc[-1]
        prev = data.iloc[-2]

        ma5 = round(data['Close'][-5:].mean(),2) if len(data)>=5 else None
        ma20 = round(data['Close'][-20:].mean(),2) if len(data)>=20 else None
        delta = last['Close'] - prev['Close']

        # RSI
        delta_diff = data['Close'].diff()
        gain = delta_diff.where(delta_diff>0,0)
        loss = -delta_diff.where(delta_diff<0,0)
        gain14 = gain[-14:]
        loss14 = loss[-14:]
        avg_gain = float(gain14.mean()) if not gain14.empty else 0.0
        avg_loss = float(loss14.mean()) if not loss14.empty else 0.0
        if avg_loss == 0 and avg_gain == 0:
            rsi = 50.0
        elif avg_loss == 0:
            rsi = 100.0
        else:
            rsi = 100 - (100/(1+(avg_gain/avg_loss)))
        rsi = round(rsi,2)

        vol30 = float(data['Volume'][-30:].mean()) if len(data)>=30 else float(last['Volume'])
        vol_change = round((last['Volume']-vol30)/vol30*100,1)

        news = fetch_finnhub_news(t)
        analyst = fetch_finnhub_analyst(t)
        social = fetch_finnhub_social(t)

        summaries.append({
            "symbol": t,
            "price": round(last['Close'],2),
            "change": round(delta,2),
            "volume": int(last['Volume']),
            "ma5": ma5,
            "ma20": ma20,
            "rsi": rsi,
            "vol_change": vol_change,
            "sector": SECTORS.get(t,""),
            "news": news,
            "analyst": analyst,
            "social": social
        })
        time.sleep(0.05)  # small delay to not spam API
    return summaries

# ----- PROMPT -----
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. Calculate a numeric score 0-100 for each stock using these weights:\n"
        "- News/catalysts: 45%\n"
        "- Technical indicators (RSI, MA, volume): 25%\n"
        "- Social sentiment: 10%\n"
        "- Sector/macro context: 10%\n"
        "- Fundamentals: 10%\n\n"
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

    price = float(yf.Ticker(symbol).history(period="1d")['Close'][-1])
    qty = int(position_size // price)
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

# ----- TIME CONTROL -----
def wait_until_1250pm_pst():
    global run_now
    while True:
        if run_now:
            run_now = False
            return
        now_utc = datetime.datetime.utcnow()
        now_pst = now_utc + datetime.timedelta(hours=TIMEZONE_OFFSET)
        if now_pst.hour == 12 and now_pst.minute == 50:
            return
        time.sleep(30)

# on render, skip manual input
run_now = False  # default behavior


def is_market_day():
    today = datetime.datetime.utcnow() + datetime.timedelta(hours=TIMEZONE_OFFSET)
    return today.weekday() < 5  # 0-4 are Mon-Fri

app = Flask(__name__)

def run_bot():
    while True:
        if not is_market_day():
            print("Weekend detected, skipping today...")
            time.sleep(86400)  # wait a full day
            continue

        print("waiting for 12:50 pm PST to run now...")
        wait_until_1250pm_pst()

        summaries = get_stock_summary(TICKERS)
        if not summaries:
            print("no stock data, retrying in 5 minutes")
            time.sleep(300)
            continue

        prompt = build_prompt(summaries)
        try:
            signals = ask_deepseek(prompt)
            print("\nDAILY SHORT-TERM STOCK SIGNALS:")
            print(signals)
        except Exception as e:
            print("error talking to Deepseek:", e)
            time.sleep(300)
            continue

        # execute trades
        for line in signals.splitlines():
            parts = line.split(":")
            if len(parts) >= 2:
                sym = parts[0].strip()
                sig = parts[1].split("(")[0].strip()
                place_order(sym, sig)

        time.sleep(86400)  # wait 24h

# start the bot in a background thread
threading.Thread(target=run_bot, daemon=True).start()

# --- keep awake thread (prevents free-tier sleep) ---
def keep_awake():
    import time
    import requests
    url = f"http://localhost:{os.environ.get('PORT', 10000)}/"
    while True:
        try:
            requests.get(url)
        except:
            pass
        time.sleep(10 * 60)  # ping every 10 minutes

threading.Thread(target=keep_awake, daemon=True).start()

# Flask app
@app.route("/")
def home():
    return "Gigglyfarts bot running"

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

