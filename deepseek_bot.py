import os
import time
import threading
import logging
import random
import requests
from flask import Flask
from alpaca_trade_api.rest import REST, APIError
from dotenv import load_dotenv
from dateutil import parser
import datetime
from datetime import timezone, timedelta

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

# ---------- TWELVEDATA ----------
TWELVE_RATE_LIMIT = 8
TWELVE_WINDOW = 61
_twelve_calls = []
_twelve_lock = threading.Lock()

def twelve_rate_limit():
    global _twelve_calls
    with _twelve_lock:
        now = time.time()
        _twelve_calls = [t for t in _twelve_calls if now - t < TWELVE_WINDOW]
        if len(_twelve_calls) >= TWELVE_RATE_LIMIT:
            sleep_for = TWELVE_WINDOW - (now - _twelve_calls[0]) + 0.1
            log.info(f"TwelveData rate limit hit, sleeping {sleep_for:.1f}s")
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
        if isinstance(data, dict) and data.get("code") == 429:
            log.info(f"{symbol} RATE LIMITED by TwelveData")
            return []
        if not data or "values" not in data:
            log.info(f"{symbol} no valid bars, returning empty list")
            return []
        bars = [{"time": v.get("datetime"), "close": float(v.get("close",0)), "high": float(v.get("high",0)), "low": float(v.get("low",0)), "volume": float(v.get("volume",0))} for v in reversed(data["values"])]
        return bars
    except Exception:
        log.exception(f"{symbol} bars error")
        return []

def get_intraday_data(symbol):
    bars = fetch_twelvedata_bars(symbol)
    return bars if isinstance(bars,list) else []

# ---------- TECHNICAL ----------
def compute_technical(symbol):
    data = get_intraday_data(symbol)
    if not data:
        return None
    closes = [d["close"] for d in data]
    volumes = [d["volume"] for d in data]

    ma5 = round(sum(closes[-5:])/5,2) if len(closes) >= 5 else None
    ma20 = round(sum(closes[-20:])/20,2) if len(closes) >= 20 else None

    # RSI
    rsi = None
    period = 14
    if len(closes) > period:
        deltas = [closes[i]-closes[i-1] for i in range(1,len(closes))]
        gains = [d if d>0 else 0 for d in deltas]
        losses = [-d if d<0 else 0 for d in deltas]
        avg_gain = sum(gains[:period])/period
        avg_loss = sum(losses[:period])/period
        for g,l in zip(gains[period:], losses[period:]):
            avg_gain = (avg_gain*(period-1)+g)/period
            avg_loss = (avg_loss*(period-1)+l)/period
        rsi = 100 if avg_loss==0 else 0 if avg_gain==0 else round(100-(100/(1+avg_gain/avg_loss)),2)

    last = closes[-1]
    prev = closes[-2] if len(closes)>1 else last
    delta = round(last-prev,2)
    avg_vol30 = (sum(volumes[-30:])/30) if len(volumes)>=30 else volumes[-1]
    vol_change = round((volumes[-1]-avg_vol30)/avg_vol30*100,1) if avg_vol30!=0 else 0

    # ATR for risk-based sizing
    atr = None
    if len(data)>1:
        tr_list = []
        for i in range(1,len(data)):
            h = data[i]['high']
            l = data[i]['low']
            pc = data[i-1]['close']
            tr = max(h-l, abs(h-pc), abs(l-pc))
            tr_list.append(tr)
        atr = sum(tr_list[-14:])/14 if len(tr_list)>=14 else tr_list[-1]

    return {"price": round(last,2), "change": delta, "ma5": ma5, "ma20": ma20, "rsi": rsi, "volume": volumes[-1], "vol_change": vol_change, "atr": atr}

# ---------- STOCK SUMMARY ----------
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
                "atr": tech["atr"],
                "change": tech["change"],
                "volume": tech["volume"],
                "ma5": tech["ma5"],
                "ma20": tech["ma20"],
                "rsi": tech["rsi"],
                "vol_change": tech["vol_change"],
                "sector": SECTORS.get(t,""),
                "news": fetch_finnhub_news(t),
                "analyst": fetch_finnhub_analyst(t),
                "social": fetch_finnhub_social(t)
            })
            time.sleep(0.05)
        except Exception:
            log.exception(f"error building summary for {t}")
    return summaries

# ---------- DEEPSEEK PROMPT ----------
def build_prompt(summaries):
    prompt = (
        "You are a short-term stock trading AI. Calculate a numeric score 0-100 for each stock using these weights:\n"
        "- News/catalysts: 20%\n"
        "- Technical indicators (RSI, MA, volume): 25%\n"
        "- Social sentiment: 4%\n"
        "- Sector/macro context: 7%\n"
        "- Fundamentals: 15%\n\n"
        "Score based on last 3 months of price data, recent news (last 3 days), social sentiment, and sector/macro trends.\n"
        "Normalize all inputs to 0-100. Apply recency weighting: last 48 hours = 2x older data. Penalize scores for high volatility (ATR) or low liquidity. Boost weights conditionally if news/sentiment is extremely strong.\n"
        "Output format: SYMBOL: RECOMMENDATION (1-2 word note if relevant). Include confidence HIGH/MEDIUM/LOW. Do NOT include numeric scores in the output.\n\n"
    )
    prompt += "\n".join(
        f"{s['symbol']}: price {s['price']}, change {s['change']}, MA5 {s['ma5']}, MA20 {s['ma20']}, "
        f"RSI {s['rsi']}, volume {s['volume']} (Δ{s['vol_change']}%), sector {s['sector']}, "
        f"analyst {s['analyst']}, social {s['social']}, news: {s['news'] if isinstance(s['news'],str) else 'no major news'}"
        for s in summaries
    )
    return prompt

def ask_deepseek(prompt):
    url = "https://api.deepseek.com/v1/chat/completions"
    headers = {"Authorization": f"Bearer {API_KEY}","Content-Type":"application/json"}
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
        log.warning(f"Alpaca API not initialized — skipping {symbol}")
        return
    with trade_lock:
        try:
            account = api.get_account()
            if account.status != "ACTIVE" or account.trading_blocked:
                log.info(f"{symbol} — skipping trade due to account status")
                return

            signal = signal.upper().strip()

            # SELL
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

            # BUY
            clock = api.get_clock()
            if not clock.is_open:
                log.info(f"{symbol} market closed — skipping buy")
                return

            intraday = get_intraday_data(symbol)
            tech = compute_technical(symbol)
            if not tech or not tech['atr']:
                return
            price = intraday[-1]["close"] if intraday else 0

            # risk-based qty
            risk_dollars = float(account.equity)*0.01
            qty = int(risk_dollars // tech['atr'])
            if qty < 1:
                log.info(f"{symbol} skipped — qty=0")
                return

            # prevent duplicate buys
            try:
                pos = api.get_position(symbol)
                log.info(f"{symbol} — already holding {pos.qty} shares, skipping buy")
                return
            except APIError:
                pass

            # bracket order
            stop_price = round(price - tech['atr'],2)
            take_price = round(price + tech['atr']*2,2)
            order = api.submit_order(
                symbol=symbol,
                qty=qty,
                side='buy',
                type='limit',
                limit_price=price,
                time_in_force='day',
                order_class='bracket',
                take_profit={'limit_price':take_price},
                stop_loss={'stop_price':stop_price}
            )
            log.info(f"BOUGHT {qty} shares of {symbol} @ ~{price}, stop {stop_price}, take {take_price}, order id: {getattr(order,'id','unknown')}")
        except Exception:
            log.exception(f"place_order fatal error for {symbol}")

# ---------- EXECUTE TRADING LOGIC ----------
def execute_trading_logic():
    summaries = get_stock_summary(TICKERS)
    if not summaries:
        log.info("no stock data, skipping")
        return
    prompt = build_prompt(summaries)
    signals = ask_deepseek(prompt)
    log.info("\nDAILY SHORT-TERM STOCK SIGNALS:\n%s", signals)
    seen = set()
    for line in signals.splitlines():
        if ":" not in line:
            continue
        sym, raw_sig = line.split(":",1)
        sym = sym.strip().upper()
        if sym in seen:
            continue
        seen.add(sym)
        sig = raw_sig.split("(")[0].strip().upper()
        log.info(f"parsed signal: {sym} → {sig}")
        place_order(sym, sig)

# ---------- BOT LOOP ----------
def run_bot():
    log.info("bot loop online")
    last_trade_day = None
    traded_open = False
    traded_close = False

    while True:
        try:
            if not api:
                time.sleep(60)
                continue

            clock = api.get_clock()
            now = clock.timestamp.astimezone(timezone.utc)
            log.info(f"utc_now={now}, is_open={clock.is_open}, next_open={clock.next_open}")

            # reset traded flags on new day
            if last_trade_day != now.date():
                traded_open = False
                traded_close = False
                last_trade_day = now.date()

            if clock.is_open:
                # fetch today's market open/close from calendar
                cal = api.get_calendar(start=now.date().isoformat(), end=now.date().isoformat())
                if cal:
                    market_open = datetime.combine(now.date(), cal[0].open, tzinfo=timezone(timedelta(hours=-5)))
                    market_open = market_open.astimezone(timezone.utc)

                    market_close = datetime.combine(now.date(), cal[0].close, tzinfo=timezone(timedelta(hours=-5)))
                    market_close = market_close.astimezone(timezone.utc)

                    # 10 min after open
                    if not traded_open and now >= market_open + timedelta(minutes=10):
                        log.info("running trades 10 minutes after open")
                        execute_trading_logic()
                        traded_open = True

                    # 10 min before close
                    if not traded_close and now >= market_close - timedelta(minutes=10):
                        log.info("running trades 10 minutes before market close")
                        execute_trading_logic()
                        traded_close = True
            else:
                log.info("market closed — sleeping")

        except Exception:
            log.exception("run_bot error")

        time.sleep(60)

# ---------- FLASK APP ----------
app = Flask(__name__)

@app.route("/")
def home():
    return "Giggity Bot Online!"

@app.route("/trigger")
def trigger():
    threading.Thread(target=execute_trading_logic, daemon=True).start()
    return "Triggered trading logic!"

if __name__ == "__main__":
    threading.Thread(target=run_bot, daemon=True).start()
    port = int(os.getenv("PORT",5000))
    log.info("Starting Flask server on port %s", port)
    app.run(host="0.0.0.0", port=port)
