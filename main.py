"""
NSE/BSE Stock Alert Telegram Bot
Replit Hosted Version (With Keep-Alive Server)
"""

import yfinance as yf
import requests
import time
import json
import os
import schedule
from datetime import datetime
import pytz
from flask import Flask
from threading import Thread

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

DATA_FILE = "stock_data.json"
IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# KEEP ALIVE SERVER (FOR REPLIT)
# ─────────────────────────────────────────────
app = Flask('')

@app.route('/')
def home():
    return "Stock Bot is running!"

def run_web():
    port = int(os.environ.get("PORT", 8080))
    app.run(host='0.0.0.0', port=port)

def keep_alive():
    t = Thread(target=run_web)
    t.start()

# ─────────────────────────────────────────────
# DATA PERSISTENCE
# ─────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"holdings": {}, "watchlist": []}

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─────────────────────────────────────────────
# TELEGRAM FUNCTIONS
# ─────────────────────────────────────────────
def send_message(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")

def get_updates(offset=None):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 10}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=15)
        return resp.json().get("result", [])
    except:
        return []

# ─────────────────────────────────────────────
# STOCK PRICE FUNCTIONS
# ─────────────────────────────────────────────
def get_price(ticker):
    try:
        stock = yf.Ticker(ticker)
        price = stock.fast_info["last_price"]
        return round(price, 2)
    except Exception as e:
        print(f"[Price Error] {ticker}: {e}")
        return None

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=17, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close

# ─────────────────────────────────────────────
# HOURLY UPDATE
# ─────────────────────────────────────────────
def send_hourly_update(force=False):
    if not force and not is_market_open():
        return

    data = load_data()
    if not data["holdings"] and not data["watchlist"]:
        send_message("No stocks added yet.\nUse /add to begin.")
        return

    now_str = datetime.now(IST).strftime("%I:%M %p")
    msg = f"<b>Hourly Update - {now_str} IST</b>\n{'─'*28}\n"

    all_tickers = set(data["holdings"].keys()) | set(data["watchlist"])

    for ticker in all_tickers:
        price = get_price(ticker)
        display = ticker.replace(".NS", "").replace(".BO", "")

        if price is None:
            msg += f"\n<b>{display}</b> - Could not fetch price\n"
            continue

        msg += f"\n<b>{display}</b>\n"
        msg += f"   Price: Rs {price:,.2f}\n"

        if ticker in data["holdings"]:
            h = data["holdings"][ticker]
            qty = h["qty"]
            buy_price = h["buy_price"]
            pnl = (price - buy_price) * qty
            pnl_label = "GAIN" if pnl >= 0 else "LOSS"
            msg += f"   P&L ({pnl_label}): Rs {pnl:+,.2f} ({qty} shares @ Rs {buy_price})\n"

            if "alert_below" in h and price < h["alert_below"]:
                send_message(
                    f"ALERT: <b>{display}</b>\n"
                    f"Price Rs {price:,.2f} below alert level Rs {h['alert_below']:,.2f}"
                )

    send_message(msg)

# ─────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────
def handle_commands():
    data = load_data()
    updates = get_updates(offset=handle_commands.last_update_id)

    for update in updates:
        handle_commands.last_update_id = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(CHAT_ID):
            continue

        parts = text.split()
        cmd = parts[0].lower() if parts else ""

        if cmd == "/portfolio":
            send_hourly_update(force=True)

handle_commands.last_update_id = 0

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
def setup_schedule():
    for hour in range(9, 18):
        t = f"{hour:02d}:00"
        schedule.every().day.at(t).do(send_hourly_update)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("Stock Bot started...")
    send_message("<b>Stock Alert Bot is Online!</b>\nType /help")

    setup_schedule()

    while True:
        schedule.run_pending()
        handle_commands()
        time.sleep(3)

if __name__ == "__main__":
    keep_alive()   # 🔥 Start web server for Replit
    main()