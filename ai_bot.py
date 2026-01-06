import os
import time
import threading
import logging
import requests
from flask import Flask
from alpaca_trade_api.rest import REST
from dotenv import load_dotenv
from datetime import datetime, time as dtime, timedelta
from logging.handlers import RotatingFileHandler

from math_bot import (
    get_all_summaries,
    fetch_finnhub_news,
    fetch_finnhub_social,
    fetch_finnhub_analyst
)

# ---------------- BASIC LOGGING ----------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s"
)

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
log = logging.getLogger()
log.setLevel(logging.INFO)

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

# ---------------- SYSTEM CHECK ----------------
def run_system_check(symbols):
    log.info("=== RUNNING SYSTEM CHECK ===")

    for sym in symbols:
        try:
            time.sleep(1)

            try:
                news = fetch_finnhub_news(sym)
            except Exception:
                news = ""
                log.warning(f"news fetch failed for {sym}")

            try:
                social = fetch_finnhub_social(sym)
            except Exception:
                social = ""
                log.warning(f"social fetch failed for {sym}")

            try:
                analyst = fetch_finnhub_analyst(sym)
            except Exception:
                analyst = ""
                log.warning(f"analyst fetch failed for {sym}")

            try:
                summaries = get_all_summaries([sym])
                math_score = summaries[0]["math_score"] if summaries else None
            except Exception:
                math_score = None
                log.warning(f"math score fetch failed for {sym}")

            log.info(
                f"SYSTEM CHECK: {sym} -> news: {news}, social: {social}, "
                f"analyst: {analyst}, math_score: {math_score}"
            )

        except Exception:
            log.exception(f"system check failed for {sym}")

    log.info("=== SYSTEM CHECK COMPLETE ===")

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

COMPANY_NAMES = {
    "AAPL":"Apple","MSFT":"Microsoft","AMZN":"Amazon","NVDA":"Nvidia","GOOG":"Google",
    "META":"Meta","TSLA":"Tesla","NFLX":"Netflix","DIS":"Disney","PYPL":"Paypal",
    "INTC":"Intel","CSCO":"Cisco","ADBE":"Adobe","ORCL":"Oracle","IBM":"IBM",
    "CRM":"Salesforce","AMD":"AMD","UBER":"Uber","LYFT":"Lyft","SHOP":"Shopify",
    "BABA":"Alibaba","NKE":"Nike","SBUX":"Starbucks","QCOM":"Qualcomm",
    "PEP":"PepsiCo","KO":"Coca-Cola"
}

# ---------------- FETCH MATH ----------------
def fetch_math_summaries(timeout=20):
    result = {}

    def worker():
        try:
            result["data"] = get_all_summaries(TICKERS)
        except Exception as e:
            result["error"] = e

    t = threading.Thread(target=worker)
    t.start()
    t.join(timeout)

    if t.is_alive():
        log.warning("math fetch timed out (twelvedata rate limit). skipping this cycle")
        return []

    if "error" in result:
        log.warning(f"math fetch failed: {result['error']}")
        return []

    return result.get("data", [])


# ---------------- DEEPSEEK ----------------
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI.\n"
        "Return only: STRONG BUY, BUY, HOLD, SELL, STRONG SELL.\n\n"
    )
    for s in summaries:
        prompt += f"{s['symbol']}, math_score {s['math_score']:.1f}, sector {s['sector']}\n"
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
            take_pct = 0.03

            risk_dollars = equity * risk_pct
            qty = int(risk_dollars // (price * stop_pct))

            if qty <= 0:
                return

            if signal in ["BUY", "STRONG BUY"] and held_qty == 0:
                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy",
                    type="market",
                    time_in_force="day"
                )
                log.info(f"bought {qty} {symbol}")

            if "SELL" in signal and held_qty > 0:
                api.submit_order(
                    symbol=symbol,
                    qty=held_qty,
                    side="sell",
                    type="market",
                    time_in_force="day"
                )
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
def trigger():
    threading.Thread(target=execute_trading_logic).start()
    return "manual trigger fired"

# ---------------- ENTRY POINT ----------------
if __name__ == "__main__":
    sample_symbols = ["AAPL", "TSLA", "GOOG"]

    run_system_check(sample_symbols)

    threading.Thread(target=run_bot, daemon=True).start()

    port = int(os.getenv("PORT", 5000))
    log.info(f"starting flask on {port}")
    app.run(host="0.0.0.0", port=port)

