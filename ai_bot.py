import os
import time
import threading
import logging
import requests
from flask import Flask
from alpaca_trade_api.rest import REST
from dotenv import load_dotenv
from datetime import datetime, time as dtime, timedelta


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
def run_system_check(sample_symbols=None):
    if sample_symbols is None:
        sample_symbols = ["AAPL", "TSLA", "GOOG"]  # small sample for quick check
    log.info("=== RUNNING SYSTEM CHECK ===")
    for sym in sample_symbols:
        try:
            news = fetch_finnhub_news(sym)
            social = fetch_finnhub_social(sym)
            analyst = fetch_finnhub_analyst(sym)
            math_score = fetch_math_summaries()[0]['math_score'] if fetch_math_summaries() else None
            log.info(f"SYSTEM CHECK: {sym} -> news: {news}, social: {social}, analyst: {analyst}, math_score: {math_score}")
        except Exception:
            log.exception(f"system check failed for {sym}")
    log.info("=== SYSTEM CHECK COMPLETE ===\n")


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
    return math_bot.get_all_summaries(TICKERS)  # returns [{"symbol":"AAPL","math_score":76.5,"sector":"Technology"}, ...]

# ---------------- DEEPSEEK PROMPT ----------------
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. For each stock, give a single recommendation: STRONG BUY, BUY, HOLD, SELL, or STRONG SELL.\n"
        "Do not include numeric scores, confidence, or extra descriptors.\n"
        "Score based on math score (40%), news (20%), social sentiment (20%), analyst/fundamentals (20%).\n\n"
    )
    for s in summaries:
        # math_score from math_bot, AI will handle news/social/analyst
        prompt += f"{s['symbol']}, math_score {s['math_score']:.1f}, sector {s['sector']}\n"
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
# ---------------- PLACE ORDERS (with position sizing & bracket orders) ----------------
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

            equity = float(account.equity)  # total account equity

            # --- RISK PARAMETERS ---
            risk_pct = 0.03  # 3% max loss per trade (adjust to hit ~$8k per trade on average)
            stop_loss_pct = 0.015  # 1.5% stop loss
            take_profit_pct = 0.03  # 3% take profit
            max_position_pct = 0.25  # never use more than 25% of account on one trade

            last_price = api.get_latest_trade(symbol).price

            # calculate risk-based qty
            risk_dollars = equity * risk_pct
            risk_per_share = last_price * stop_loss_pct
            qty = int(risk_dollars // risk_per_share)

            # cap by max position size
            max_spend = equity * max_position_pct
            qty = min(qty, int(max_spend // last_price))

            if qty <= 0:
                log.info(f"not enough funds to buy {symbol}")
                return

            # ----- BUY LOGIC -----
            if signal in ["BUY", "STRONG BUY"]:
                if held_qty > 0:
                    log.info(f"already holding {symbol}, skipping buy")
                    return

                stop_price = round(last_price * (1 - stop_loss_pct), 2)
                limit_price = round(last_price * (1 + take_profit_pct), 2)

                api.submit_order(
                    symbol=symbol,
                    qty=qty,
                    side="buy",
                    type="market",
                    time_in_force="day",
                    order_class="bracket",
                    take_profit={"limit_price": limit_price},
                    stop_loss={"stop_price": stop_price}
                )

                company = COMPANY_NAMES.get(symbol, symbol)
                log.info(f"bought {qty} shares of {symbol} ({company}) with stop {stop_price} and target {limit_price}")
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
        log.info(f"{sym} ({COMPANY_NAMES.get(sym,sym)}) → {sig}")
        place_order(sym, sig)

# ---------------- BOT LOOP ----------------
def run_bot():
    log.info("AI bot scheduled loop online")
    already_ran = set()  # track which times we've already executed today

    while True:
        now = datetime.now()

        # calculate mid-market time
        open_minutes = MARKET_OPEN.hour * 60 + MARKET_OPEN.minute
        close_minutes = MARKET_CLOSE.hour * 60 + MARKET_CLOSE.minute
        mid_minutes = open_minutes + (close_minutes - open_minutes) // 2
        mid_time = dtime(mid_minutes // 60, mid_minutes % 60)

        # calculate 10 min after open and 10 min before close as datetime objects
        trigger_after_open = datetime.combine(now.date(), MARKET_OPEN) + timedelta(minutes=10)
        market_close_dt = datetime.combine(now.date(), MARKET_CLOSE)
        trigger_before_close = market_close_dt - timedelta(minutes=10)
        mid_time_dt = datetime.combine(now.date(), mid_time)

        # target triggers as datetimes
        triggers = [
            trigger_after_open,
            mid_time_dt,
            trigger_before_close
        ]

        for t in triggers:
            if abs((now - t).total_seconds()) < 30 and t not in already_ran:
                try:
                    log.info(f"Triggering AI trading logic for scheduled time {t}")
                    execute_trading_logic()
                except Exception:
                    log.exception("Scheduled execution failed")
                already_ran.add(t)

        # reset after market close
        if now.time() >= MARKET_CLOSE:
            already_ran.clear()

        time.sleep(20)


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
    # --- RUN SYSTEM CHECK ON STARTUP ---
    run_system_check()  # prints news, social, analyst, math score for a sample of stocks

    # --- START BOT LOOP ---
    threading.Thread(target=run_bot, daemon=True).start()
    
    port = int(os.getenv("PORT", 5000))
    log.info("Starting Flask server on port %s", port)
    app.run(host="0.0.0.0", port=port)





