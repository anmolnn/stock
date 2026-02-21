"""
NSE/BSE Stock Alert Telegram Bot
- Runs between 9:15 AM - 5:00 PM IST
- Sends hourly updates: current price + gain/loss in Rs
- Sends an immediate price update when a stock is added
- Flask web server for Render port binding
"""

import yfinance as yf
import requests
import time
import json
import os
import schedule
from datetime import datetime
from threading import Thread
import pytz
from flask import Flask

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DATA_FILE = "stock_data.json"
IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# FLASK - RENDER PORT BINDING
# ─────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def home():
    return "Stock Bot is running!"

def start_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

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
        requests.post(
            url,
            data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        print(f"[Telegram Error] {e}")

def get_updates(offset):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    params = {"timeout": 5}
    if offset:
        params["offset"] = offset
    try:
        resp = requests.get(url, params=params, timeout=10)
        return resp.json().get("result", [])
    except Exception as e:
        print(f"[getUpdates Error] {e}")
        return []

def skip_old_updates():
    """On startup, skip all pending messages so they are not reprocessed."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, params={"timeout": 0}, timeout=10)
        results = resp.json().get("result", [])
        if results:
            last_id = results[-1]["update_id"] + 1
            print(f"Skipping old updates, starting from ID: {last_id}")
            return last_id
    except Exception as e:
        print(f"[skip_old_updates Error] {e}")
    return 0

# ─────────────────────────────────────────────
# STOCK PRICE FUNCTIONS
# ─────────────────────────────────────────────
def get_price(ticker):
    """Tries fast_info first, falls back to history for ETFs like TATSILV.NS."""
    try:
        stock = yf.Ticker(ticker)
        try:
            price = stock.fast_info["last_price"]
            if price and price > 0:
                return round(float(price), 2)
        except:
            pass
        hist = stock.history(period="2d")
        if not hist.empty:
            return round(float(hist["Close"].iloc[-1]), 2)
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

def format_qty(qty):
    """Show 10 instead of 10.0 for whole numbers."""
    return int(qty) if qty == int(qty) else qty

# ─────────────────────────────────────────────
# PORTFOLIO MESSAGE
# ─────────────────────────────────────────────
def send_portfolio(force=False, title="Hourly Update"):
    if not force and not is_market_open():
        return

    data = load_data()
    all_tickers = set(data["holdings"].keys()) | set(data["watchlist"])

    if not all_tickers:
        send_message("No stocks in your watchlist or holdings yet.\nUse /add to add a stock.")
        return

    now_str = datetime.now(IST).strftime("%I:%M %p")
    msg = f"<b>{title} - {now_str} IST</b>\n{'─'*28}\n"

    for ticker in sorted(all_tickers):
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
            msg += f"   P&L ({pnl_label}): Rs {pnl:+,.2f} ({format_qty(qty)} shares @ Rs {buy_price})\n"

            if "alert_below" in h and price < h["alert_below"]:
                send_message(
                    f"ALERT: <b>{display}</b>\n"
                    f"Price Rs {price:,.2f} dropped below Rs {h['alert_below']:,.2f}!\n"
                    f"P&L: Rs {pnl:+,.2f}"
                )

    send_message(msg)

def send_market_close():
    """5 PM - send final update then stop notification."""
    send_portfolio(force=True, title="End of Day Update")
    send_message("<b>Tracking Stopped.</b> Resumes tomorrow at 9:15 AM IST.")

def send_stock_added_message(ticker, qty, buy_price, alert_below):
    """Single combined message after /add - no duplicate."""
    price = get_price(ticker)
    display = ticker.replace(".NS", "").replace(".BO", "")
    now_str = datetime.now(IST).strftime("%I:%M %p")

    alert_txt = f"\nAlert set below Rs {alert_below:,.2f}" if alert_below else ""
    msg = f"Added <b>{display}</b>\n{format_qty(qty)} shares @ Rs {buy_price:,.2f}{alert_txt}\n\n"

    if price is None:
        msg += "Could not fetch current price.\nCheck that ticker is correct (e.g. TATSILV.NS)"
    else:
        pnl = (price - buy_price) * qty
        pnl_label = "GAIN" if pnl >= 0 else "LOSS"
        msg += f"Current Price as of {now_str} IST: Rs {price:,.2f}\n"
        msg += f"P&L ({pnl_label}): Rs {pnl:+,.2f}"

    send_message(msg)

# ─────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────
last_update_id = 0

def handle_commands():
    global last_update_id
    updates = get_updates(offset=last_update_id)

    for update in updates:
        last_update_id = update["update_id"] + 1

        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if chat_id != str(CHAT_ID) or not text:
            continue

        parts = text.split()
        if not parts or not parts[0].startswith("/"):
            continue

        cmd = parts[0].lower()

        # ── /help ──────────────────────────────
        if cmd == "/help":
            send_message(
                "<b>Stock Bot Commands</b>\n\n"
                "/add <b>TICKER QTY BUY_PRICE [ALERT_BELOW]</b>\n"
                "  e.g. <code>/add TATSILV.NS 10 23.47 20</code>\n\n"
                "/remove <b>TICKER</b>\n"
                "  e.g. <code>/remove TATSILV.NS</code>\n\n"
                "/watch <b>TICKER</b>\n"
                "  e.g. <code>/watch TCS.NS</code>\n\n"
                "/portfolio - View current snapshot\n"
                "/help - Show this message\n\n"
                "Tip: Always include <code>.NS</code> for NSE or <code>.BO</code> for BSE"
            )

        # ── /add ───────────────────────────────
        elif cmd == "/add":
            if len(parts) < 4:
                send_message(
                    "Usage: /add TICKER QTY BUY_PRICE [ALERT_BELOW]\n"
                    "Example: /add TATSILV.NS 10 23.47 20"
                )
                continue
            try:
                ticker      = parts[1].upper()
                qty         = float(parts[2])
                buy_price   = float(parts[3])
                alert_below = float(parts[4]) if len(parts) > 4 else None

                data = load_data()
                entry = {"qty": qty, "buy_price": buy_price}
                if alert_below:
                    entry["alert_below"] = alert_below
                data["holdings"][ticker] = entry
                if ticker in data["watchlist"]:
                    data["watchlist"].remove(ticker)
                save_data(data)

                send_stock_added_message(ticker, qty, buy_price, alert_below)

            except Exception as e:
                print(f"[/add Error] {e}")
                send_message("Invalid format. Example: /add TATSILV.NS 10 23.47 20")

        # ── /remove ────────────────────────────
        elif cmd == "/remove":
            if len(parts) < 2:
                send_message("Usage: /remove TICKER\nExample: /remove TATSILV.NS")
                continue
            ticker = parts[1].upper()
            data = load_data()
            removed = False
            if ticker in data["holdings"]:
                del data["holdings"][ticker]
                removed = True
            if ticker in data["watchlist"]:
                data["watchlist"].remove(ticker)
                removed = True
            save_data(data)
            display = ticker.replace(".NS", "").replace(".BO", "")
            if removed:
                send_message(f"Removed <b>{display}</b> successfully.")
            else:
                send_message(
                    f"<b>{display}</b> not found.\n"
                    f"Make sure to include .NS or .BO\n"
                    f"Example: /remove TATSILV.NS"
                )

        # ── /watch ─────────────────────────────
        elif cmd == "/watch":
            if len(parts) < 2:
                send_message("Usage: /watch TICKER\nExample: /watch TCS.NS")
                continue
            ticker = parts[1].upper()
            data = load_data()
            if ticker not in data["watchlist"] and ticker not in data["holdings"]:
                data["watchlist"].append(ticker)
                save_data(data)
            display = ticker.replace(".NS", "").replace(".BO", "")
            price = get_price(ticker)
            now_str = datetime.now(IST).strftime("%I:%M %p")
            if price:
                send_message(
                    f"Now watching <b>{display}</b>\n"
                    f"Current Price as of {now_str} IST: Rs {price:,.2f}"
                )
            else:
                send_message(
                    f"Now watching <b>{display}</b>\n"
                    f"Could not fetch price. Check ticker is correct."
                )

        # ── /portfolio ─────────────────────────
        elif cmd == "/portfolio":
            send_portfolio(force=True, title="Portfolio Snapshot")

        else:
            send_message("Unknown command. Type /help to see available commands.")

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
def setup_schedule():
    # Hourly updates 10 AM to 4 PM
    for hour in range(10, 17):
        t = f"{hour:02d}:00"
        schedule.every().day.at(t).do(send_portfolio)

    # 9:15 AM market open notification
    schedule.every().day.at("09:15").do(
        lambda: send_message("<b>Tracking Started!</b> Updates every hour until 5:00 PM IST.")
    )

    # 5 PM end of day
    schedule.every().day.at("17:00").do(send_market_close)

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    global last_update_id

    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN or CHAT_ID is missing.")
        return

    print("Stock Bot started.")

    # Start Flask in background thread for Render port binding
    web_thread = Thread(target=start_web, daemon=True)
    web_thread.start()

    # Skip all old pending messages on startup
    last_update_id = skip_old_updates()

    # Only send startup message on very first run
    first_run = not os.path.exists(DATA_FILE)
    if first_run:
        send_message(
            "<b>Stock Alert Bot is Online!</b>\n\n"
            "Updates run: Mon-Fri, 9:15 AM - 5:00 PM IST\n"
            "Hourly price + P&L reports\n\n"
            "Type /help to get started."
        )
        save_data({"holdings": {}, "watchlist": []})

    setup_schedule()

    while True:
        schedule.run_pending()
        handle_commands()
        time.sleep(3)

if __name__ == "__main__":
    main()