import os
import time
import threading
import logging
import requests
from flask import Flask
from alpaca_trade_api.rest import REST
from dotenv import load_dotenv
from datetime import datetime, time as dtime
from logging.handlers import RotatingFileHandler
from math_bot import get_all_summaries


# ---------------- BASIC LOGGING ----------------
logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
log = logging.getLogger(__name__)

# ---------------- MARKET TIMES ----------------
MARKET_OPEN = dtime(9, 30)
MARKET_CLOSE = dtime(16, 0)

# ---------------- CONFIG ----------------
load_dotenv()
API_KEY = os.getenv("DEEPSEEK_KEY")
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

if not all([API_KEY, ALPACA_KEY, ALPACA_SECRET]):
    raise SystemExit("missing env vars for API keys")

# ---------------- ROTATING FILE LOG ----------------
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
file_handler = RotatingFileHandler("bot.log", maxBytes=5_000_000, backupCount=3)
file_handler.setFormatter(formatter)
file_handler.setLevel(logging.INFO)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
stream_handler.setLevel(logging.INFO)
log.handlers.clear()
log.addHandler(file_handler)
log.addHandler(stream_handler)

# ---------------- ALPACA ----------------
api = None
try:
    api = REST(ALPACA_KEY, ALPACA_SECRET, BASE_URL, api_version="v2")
    log.info("Connected to Alpaca API (paper trading)")
except Exception:
    log.exception("Failed to initialize Alpaca")

# ---------------- GLOBAL LOCK ----------------
trade_lock = threading.Lock()

# ---------------- STOCK CONFIG ----------------
TICKERS = [
    "AAPL","MSFT","AMZN","NVDA","GOOG","META","TSLA","NFLX","DIS","PYPL",
    "INTC","CSCO","ADBE","ORCL","IBM","CRM","AMD","UBER","LYFT","SHOP",
    "BABA","NKE","SBUX","QCOM","PEP","KO"
]

SECTORS = {sym: "unknown" for sym in TICKERS}  # ai can still use sector field

# ---------------- GLOBAL THREAD CONTROL ----------------
_math_thread_running = False
_math_thread = None

def start_math_thread_once(target_func):
    """
    trigger-safe starter: only starts one thread per function
    """
    global _math_thread_running, _math_thread
    if _math_thread_running:
        log.info("math thread already running, ignoring trigger")
        return
    _math_thread_running = True
    _math_thread = threading.Thread(target=target_func, daemon=True)
    _math_thread.start()
    log.info("math thread started")


# ---------------- FETCH MATH (fixed) ----------------
def fetch_math_summaries(timeout=20):
    """
    Fetch all math summaries in the current thread.
    Will respect TwelveData rate limits without spawning extra threads.
    """
    try:
        # directly call get_all_summaries; don’t spawn a new thread
        return get_all_summaries(TICKERS)
    except Exception as e:
        log.warning(f"math fetch failed or timed out: {e}")
        return []


# ---------------- DEEPSEEK ----------------
def build_prompt(summaries):
    prompt = "You are a short-term stock trading AI.\nReturn only: STRONG BUY, BUY, HOLD, SELL, STRONG SELL.\n\n"
    for s in summaries:
        prompt += f"{s['symbol']}, math_score {s['math_score']:.1f}, sector {SECTORS.get(s['symbol'],'unknown')}\n"
    return prompt

def ask_deepseek(prompt):
    try:
        r = requests.post(
            "https://api.deepseek.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {API_KEY}"},
            json={
                "model": "deepseek-chat",
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.2,
                "max_tokens": 500
            },
            timeout=30
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    except Exception:
        log.exception("deepseek error")
        return ""

# ---------------- ORDER EXECUTION ----------------
def place_order(symbol, signal):
    if not api:
        return
    with trade_lock:
        try:
            account = api.get_account()
            if account.trading_blocked:
                return
            try:
                pos = api.get_position(symbol)
                held_qty = int(pos.qty)
            except Exception:
                held_qty = 0
            equity = float(account.equity)
            price = api.get_latest_trade(symbol).price
            risk_pct = 0.03
            stop_pct = 0.015
            qty = int((equity * risk_pct) // (price * stop_pct))
            qty *= 3  # triple position size
            if qty <= 0:
                return
            if signal in ["BUY", "STRONG BUY"] and held_qty == 0:
                api.submit_order(symbol=symbol, qty=qty, side="buy", type="market", time_in_force="day")
                log.info(f"bought {qty} {symbol}")
            if "SELL" in signal and held_qty > 0:
                api.submit_order(symbol=symbol, qty=held_qty, side="sell", type="market", time_in_force="day")
                log.info(f"sold {held_qty} {symbol}")
        except Exception:
            log.exception(f"order error {symbol}")

# ---------------- TRADING LOGIC ----------------
def execute_trading_logic():
    summaries = fetch_math_summaries()
    if not summaries:
        return
    prompt = build_prompt(summaries)
    response = ask_deepseek(prompt)
    for line in response.splitlines():
        if ":" not in line:
            continue
        sym, sig = line.split(":", 1)
        place_order(sym.strip(), sig.strip())

# ---------------- BOT LOOP ----------------
def run_bot():
    log.info("AI bot loop started")
    while True:
        try:
            execute_trading_logic()
        except Exception:
            log.exception("bot loop error")
        time.sleep(900)

# ---------------- FLASK ----------------
app = Flask(__name__)
@app.route("/")
def home():
    return "Deepseek AI Bot Online"
@app.route("/trigger")
# manual trigger endpoint
@app.route("/trigger")
def trigger():
    start_math_thread_once(execute_trading_logic)  # will only start one thread
    return "manual trigger fired"

# entry point
if __name__ == "__main__":
    sample_symbols = ["AAPL","TSLA","GOOG"]
    summaries = fetch_math_summaries()
    for s in summaries:
        log.info(f"SYSTEM CHECK: {s['symbol']} -> math_score: {s['math_score']}")
    start_math_thread_once(run_bot)  # run main bot loop safely
    port = int(os.getenv("PORT", 5000))
    log.info(f"starting flask on {port}")
    app.run(host="0.0.0.0", port=port)






