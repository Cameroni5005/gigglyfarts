import os
import time
import threading
import logging
import requests
from flask import Flask
from alpaca_trade_api.rest import REST, APIError
from dotenv import load_dotenv

# ---------------- CONFIG ----------------
load_dotenv()

API_KEY = os.getenv("DEEPSEEK_KEY")
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

if not all([API_KEY, ALPACA_KEY, ALPACA_SECRET]):
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

# ---------------- FETCH MATH SCRIPT OUTPUT ----------------
# this function should be replaced with actual import from math script
# or call math script as module and get get_all_summaries
def fetch_math_summaries():
    """
    expected output: list of dicts:
    [
        {
            "symbol": "AAPL",
            "math_score": 78.2,
            "news": "some headline",
            "social": "bullish",
            "analyst": "analyst avg buy",
            "sector": "Technology"
        }, ...
    ]
    """
    import math_script  # assume the previous math script saved as math_script.py
    return math_script.get_all_summaries(TICKERS)

# ---------------- DEEPSEEK PROMPT ----------------
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. Calculate a numeric score 0-100 for each stock using these weights:\n"
        "- Math score: 40%\n"
        "- News/catalysts: 20%\n"
        "- Social sentiment: 20%\n"
        "- Analyst/fundamentals: 20%\n\n"
        "Score based on math score, recent news (last 3 days), social sentiment, and analyst ratings.\n"
        "Normalize all inputs to 0-100. Output format: SYMBOL: RECOMMENDATION (1-2 word note if relevant). Include confidence HIGH/MEDIUM/LOW. Do NOT include numeric scores.\n\n"
    )
    for s in summaries:
        prompt += (f"{s['symbol']}, math_score {s['math_score']:.1f}, sector {s['sector']}, "
                   f"analyst {s['analyst']}, social {s['social']}, news: {s['news']}\n")
    return prompt

def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}","Content-Type":"application/json"}
    payload = {
        "model":"deepseek-chat",
        "messages":[{"role":"user","content":prompt}],
        "temperature":0.2,
        "max_tokens":500
    }
    try:
        r = requests.post(url, headers=headers, json=payload, timeout=30)
        r.raise_for_status()
        data = r.json()
        return data.get("choices",[{}])[0].get("message",{}).get("content","") if isinstance(data,dict) else ""
    except Exception:
        log.exception("error talking to Deepseek")
        return ""

# ---------------- PLACE ORDERS ----------------
def place_order(symbol, signal):
    if not api:
        log.warning(f"Alpaca API not initialized — skipping {symbol}")
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
                return
        except Exception:
            log.exception(f"place_order fatal error for {symbol}")

# ---------------- EXECUTE TRADING LOGIC ----------------
def execute_trading_logic():
    log.info("execute_trading_logic() STARTED")
    summaries = fetch_math_summaries()
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

# ---------------- BOT LOOP ----------------
def run_bot():
    log.info("AI bot loop online")
    while True:
        try:
            execute_trading_logic()
        except Exception:
            log.exception("AI bot loop error")
        time.sleep(600)  # every 10 min

# ---------------- FLASK APP ----------------
app = Flask(__name__)

@app.route("/")
def home():
    return "Deepseek AI Bot Online!"

@app.route("/trigger")
def trigger():
    def run():
        try:
            execute_trading_logic()
        except Exception:
            log.exception("manual trigger failed")
    threading.Thread(target=run).start()
    return "Triggered AI trading logic (manual run)!"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT",5000))
    log.info("Starting Flask server on port %s", port)
    app.run(host="0.0.0.0", port=port)
