import os
import time
import threading
import logging
import requests
from flask import Flask
from alpaca_trade_api.rest import REST, APIError
from dotenv import load_dotenv
from datetime import datetime, timedelta, timezone

# ---------------- CONFIG ----------------
load_dotenv()

DEEPSEEK_KEY = os.getenv("DEEPSEEK_KEY")
FINNHUB_KEY = os.getenv("FINNHUB_KEY")
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

if not all([DEEPSEEK_KEY, FINNHUB_KEY, ALPACA_KEY, ALPACA_SECRET]):
    raise SystemExit("missing env vars for API keys")

# ---------------- LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- ALPACA ----------------
api = None
try:
    api = REST(ALPACA_KEY, ALPACA_SECRET, BASE_URL, api_version='v2')
    log.info("Connected to Alpaca API (paper trading)")
except Exception:
    log.exception("Failed to initialize Alpaca REST client")

# ---------------- GLOBAL LOCK ----------------
trade_lock = threading.Lock()

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

# ---------- CACHE ----------
CACHE = {"news": {}, "social": {}}

# ---------------- HELPERS ----------------
def safe_json(r):
    try:
        return r.json()
    except ValueError:
        return {}

# ---------- FINNHUB NEWS / SOCIAL ----------
def fetch_finnhub_news(symbol):
    today = datetime.today().date()
    yesterday = today - timedelta(days=3)
    key = (symbol, today)
    if key in CACHE['news']:
        return CACHE['news'][key]
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

def fetch_finnhub_social(symbol):
    today = datetime.today().date()
    key = (symbol, today)
    if key in CACHE['social']:
        return CACHE['social'][key]
    yesterday = today - timedelta(days=1)
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

# ---------- STOCK SUMMARY ----------
def get_stock_summary(tickers, math_scores):
    summaries = []
    for t in tickers:
        try:
            summaries.append({
                "symbol": t,
                "sector": SECTORS.get(t,""),
                "news": fetch_finnhub_news(t),
                "analyst": fetch_finnhub_analyst(t),
                "social": fetch_finnhub_social(t),
                "math_score": math_scores.get(t, 50)  # default neutral
            })
            time.sleep(0.05)
        except Exception:
            log.exception(f"error building summary for {t}")
    return summaries

# ---------- DEEPSEEK PROMPT ----------
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. Combine the numeric math score with the following data to calculate a final 0-100 score:\n"
        "- News/catalysts: 40%\n"
        "- Social sentiment: 20%\n"
        "- Sector/macro context: 20%\n"
        "- Fundamentals/analyst ratings: 20%\n"
        "- Math/technical score: 40% (already normalized 0-100)\n\n"
        "Normalize all inputs to 0-100. Output format: SYMBOL: RECOMMENDATION (1-2 word note if relevant). Include confidence HIGH/MEDIUM/LOW. Do NOT include numeric scores.\n\n"
    )
    for s in summaries:
        prompt += (
            f"{s['symbol']}, sector {s['sector']}, analyst {s['analyst']}, "
            f"social {s['social']}, news: {s['news']}, math score: {s['math_score']}\n"
        )
    return prompt

def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {DEEPSEEK_KEY}","Content-Type":"application/json"}
    payload = {"model":"deepseek-chat","messages":[{"role":"user","content":prompt}],"temperature":0.2,"max_tokens":500}
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("choices",[{}])[0].get("message",{}).get("content","") if isinstance(data,dict) else ""
    except Exception:
        log.exception("error talking to Deepseek")
        return ""

# ---------- PLACE ORDERS ----------
def place_order(symbol, signal):
    if not api:
        return
    with trade_lock:
        try:
            account = api.get_account()
            if account.status != "ACTIVE" or account.trading_blocked:
                return
            signal = signal.upper().strip()
            if "SELL" in signal:
                try:
                    pos = api.get_position(symbol)
                    qty = int(pos.qty)
                    if qty>0:
                        api.submit_order(symbol=symbol, qty=qty, side='sell', type='market', time_in_force='day')
                        log.info(f"sold {qty} shares of {symbol}")
                except Exception:
                    log.exception(f"sell error for {symbol}")
        except Exception:
            log.exception(f"place_order fatal error for {symbol}")

# ---------- EXECUTE TRADING LOGIC ----------
def execute_trading_logic(math_scores):
    log.info("execute_trading_logic() STARTED")
    summaries = get_stock_summary(TICKERS, math_scores)
    if not summaries:
        return

    prompt = build_prompt(summaries)
    signals = ask_deepseek(prompt)

    if not signals or not signals.strip():
        log.warning("DeepSeek returned empty or invalid response")
        return

    log.info("\nDAILY AI STOCK SIGNALS:\n%s", signals)
    seen = set()
    for line in signals.splitlines():
        if ":" not in line:
            continue
        sym, raw_sig = line.split(":", 1)
        sym = sym.strip().upper()
        if sym in seen:
            continue
        seen.add(sym)
        sig = raw_sig.split("(")[0].strip().upper()
        log.info(f"parsed AI signal: {sym} → {sig}")
        place_order(sym, sig)

# ---------- BOT LOOP ----------
def run_ai_bot(math_scores):
    log.info("AI bot loop online")
    while True:
        try:
            execute_trading_logic(math_scores)
        except Exception:
            log.exception("AI bot loop error")
        time.sleep(600)  # every 10 min

# ---------- FLASK APP ----------
app = Flask(__name__)

@app.route("/")
def home():
    return "Deepseek AI Bot Online!"

@app.route("/trigger")
def trigger():
    def run():
        try:
            execute_trading_logic(math_scores={})  # provide math_scores dict if available
        except Exception:
            log.exception("manual trigger failed")
    threading.Thread(target=run).start()
    return "Triggered AI trading logic (manual run)!"

# ---------- MAIN ----------
if __name__ == "__main__":
    # example: math_scores could come from your math script
    example_math_scores = {sym: 50 for sym in TICKERS}  # placeholder neutral scores
    threading.Thread(target=run_ai_bot, args=(example_math_scores,), daemon=True).start()
    port = int(os.getenv("PORT",5000))
    log.info("Starting Flask server on port %s", port)
    app.run(host="0.0.0.0", port=port)
