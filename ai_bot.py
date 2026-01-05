import os
import time
import threading
import logging
import requests
from flask import Flask
from alpaca_trade_api.rest import REST, APIError
from dotenv import load_dotenv
from datetime import datetime, time as dtime

# ---------------- MARKET TIMES ----------------
MARKET_OPEN = dtime(9, 30)   # 9:30 am ET
MARKET_CLOSE = dtime(16, 0)  # 4:00 pm ET

# ---------------- CONFIG ----------------
load_dotenv()

API_KEY = os.getenv("DEEPSEEK_KEY")
ALPACA_KEY = os.getenv("ALPACA_KEY")
ALPACA_SECRET = os.getenv("ALPACA_SECRET")
BASE_URL = "https://paper-api.alpaca.markets"

if not all([API_KEY, ALPACA_KEY, ALPACA_SECRET]):
    raise SystemExit("missing env vars for API keys")

# ---------------- LOGGING ----------------
from logging.handlers import RotatingFileHandler

log = logging.getLogger()
log.setLevel(logging.INFO)

formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

file_handler = RotatingFileHandler(
    "bot.log",
    maxBytes=5_000_000,
    backupCount=3
)
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

# ---------------- COMPANY NAMES ----------------
COMPANY_NAMES = {
    "AAPL":"Apple","MSFT":"Microsoft","AMZN":"Amazon","NVDA":"Nvidia","GOOG":"Google",
    "META":"Meta","TSLA":"Tesla","NFLX":"Netflix","DIS":"Disney","PYPL":"Paypal",
    "INTC":"Intel","CSCO":"Cisco","ADBE":"Adobe","ORCL":"Oracle","IBM":"IBM",
    "CRM":"Salesforce","AMD":"AMD","UBER":"Uber","LYFT":"Lyft","SHOP":"Shopify",
    "BABA":"Alibaba","NKE":"Nike","SBUX":"Starbucks","QCOM":"Qualcomm","PEP":"PepsiCo",
    "KO":"Coca-Cola"
}

# ---------------- FETCH MATH SCRIPT OUTPUT ----------------
def fetch_math_summaries():
    import math_bot
    return math_bot.get_all_summaries(TICKERS)

# ---------------- DEEPSEEK PROMPT ----------------
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. For each stock, give a single recommendation: STRONG BUY, BUY, HOLD, SELL, or STRONG SELL.\n"
        "Do not include numeric scores, confidence, or extra descriptors.\n"
        "Score based on math score (40%), news (20%), social sentiment (20%), analyst/fundamentals (20%).\n\n"
    )
    for s in summaries:
        prompt += (f"{s['symbol']}, math_score {s['math_score']:.1f}, sector {s['sector']}, "
                   f"analyst {s['analyst']}, social {s['social']}, news: {s['news']}\n")
    return prompt

def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}", "Content-Type":"application/json"}
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
        log.warning(f"alpaca api not initialized — skipping {symbol}")
        return

    with trade_lock:
        try:
            account = api.get_account()
            if account.status != "ACTIVE" or account.trading_blocked:
                return

            signal = signal.upper().strip()

            # get current position if exists
            try:
                pos = api.get_position(symbol)
                held_qty = int(pos.qty)
            except Exception:
                held_qty = 0

            cash = float(account.cash)

            # ----- BUY LOGIC -----
            if signal in ["BUY", "STRONG BUY"]:
                if held_qty > 0:
                    log.info(f"already holding {symbol}, skipping buy")
                    return

                max_spend = cash * 0.05
                if max_spend < 10:
                    log.info(f"not enough cash to buy {symbol}")
                    return

                last_price = api.get_latest_trade(symbol).price
                qty = int(max_spend // last_price)

                if qty <= 0:
                    return

                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy",
                    type="market",
                    time_in_force="day"
                )
                company = COMPANY_NAMES.get(symbol, symbol)
                log.info(f"bought {qty} shares of {symbol} ({company})")
                return

            # ----- SELL LOGIC -----
            if "SELL" in signal and held_qty > 0:
                api.submit_order(
                    symbol=symbol,
                    qty=held_qty,
                    side="sell",
                    type="market",
                    time_in_force="day"
                )
                company = COMPANY_NAMES.get(symbol, symbol)
                log.info(f"sold {held_qty} shares of {symbol} ({company})")
                return

        except Exception:
            log.exception(f"order error for {symbol}")

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

    # ---- PARSE AND LOG AI SIGNALS WITH COMPANY NAMES ----
    log.info("DAILY AI STOCK SIGNALS:")
    seen = set()
    for line in signals.splitlines():
        if ":" not in line:
            continue
        sym, raw_sig = line.split(":", 1)
        sym = sym.strip().upper()
        if sym in seen:
            continue
        seen.add(sym)
        sig = raw_sig.strip().upper()
        if sig not in ["STRONG BUY","BUY","HOLD","SELL","STRONG SELL"]:
            sig = "HOLD"
        company = COMPANY_NAMES.get(sym, sym)
        log.info(f"{sym} ({company}) → {sig}")
        place_order(sym, sig)

# ---------------- BOT LOOP ----------------

def run_bot():
    log.info("AI bot scheduled loop online")
    already_ran = set()  # track which times we've already executed today

    while True:
        now = datetime.now()

        # convert current time to ET if needed, assuming server in local time
        # if your server is in ET, this is fine; otherwise adjust for timezone

        # define target trigger times
        triggers = [
            dtime(MARKET_OPEN.hour, MARKET_OPEN.minute + 10),  # 10 min after open
            dtime((MARKET_OPEN.hour + MARKET_CLOSE.hour)//2, 0),  # approximate middle
            dtime(MARKET_CLOSE.hour, MARKET_CLOSE.minute - 10)  # 10 min before close
        ]

        for t in triggers:
            if now.time().hour == t.hour and now.time().minute == t.minute and t not in already_ran:
                try:
                    log.info(f"Triggering AI trading logic for scheduled time {t}")
                    execute_trading_logic()
                except Exception:
                    log.exception("Scheduled execution failed")
                already_ran.add(t)

        # reset after market close
        if now.time() >= MARKET_CLOSE:
            already_ran.clear()

        time.sleep(20)  # check every 20 seconds

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

