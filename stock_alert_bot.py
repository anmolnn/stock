"""
NSE/BSE Stock Alert Telegram Bot
- Runs between 9:15 AM - 5:00 PM IST
- Sends hourly updates: current price + gain/loss in Rs
- Sends an immediate price update when a stock is added
- Lets you add your Groww holdings via Telegram commands
- Sends alert if stock falls below your set threshold
"""

import yfinance as yf
import requests
import time
import json
import os
import schedule
from datetime import datetime
import pytz

import os
from dotenv import load_dotenv

load_dotenv()  # loads from .env file when running locally

# ─────────────────────────────────────────────
# CONFIGURATION
# ─────────────────────────────────────────────
BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")

# File to persist your holdings and watchlist between restarts
DATA_FILE = "stock_data.json"

# IST Timezone
IST = pytz.timezone("Asia/Kolkata")

# ─────────────────────────────────────────────
# DATA PERSISTENCE
# ─────────────────────────────────────────────
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {
        "holdings": {},     # { "RELIANCE.NS": {"qty": 10, "buy_price": 2500, "alert_below": 2400} }
        "watchlist": []     # tickers to monitor even without holdings
    }

def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)

# ─────────────────────────────────────────────
# TELEGRAM FUNCTIONS
# ─────────────────────────────────────────────
def send_message(text):
    """Send a message to your Telegram chat."""
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(url, data={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10)
    except Exception as e:
        print(f"[Telegram Error] {e}")

def get_updates(offset=None):
    """Poll Telegram for new messages (commands from you)."""
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
    """Fetch current price for a given ticker (e.g. RELIANCE.NS)."""
    try:
        stock = yf.Ticker(ticker)
        price = stock.fast_info["last_price"]
        return round(price, 2)
    except Exception as e:
        print(f"[Price Error] {ticker}: {e}")
        return None

def is_market_open():
    """Check if tracking is active (9:15 AM - 5:00 PM IST, Mon-Fri)."""
    now = datetime.now(IST)
    if now.weekday() >= 5:  # Saturday=5, Sunday=6
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=17, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close

# ─────────────────────────────────────────────
# HOURLY UPDATE MESSAGE
# ─────────────────────────────────────────────
def send_hourly_update(force=False):
    if not force and not is_market_open():
        return

    data = load_data()
    if not data["holdings"] and not data["watchlist"]:
        send_message("No stocks in your watchlist or holdings yet.\nUse /add to add a stock.")
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

        # P&L if holding exists
        if ticker in data["holdings"]:
            h = data["holdings"][ticker]
            qty = h["qty"]
            buy_price = h["buy_price"]
            invested = buy_price * qty
            current_value = price * qty
            pnl = current_value - invested
            pnl_label = "GAIN" if pnl >= 0 else "LOSS"
            msg += f"   P&L ({pnl_label}): Rs {pnl:+,.2f} ({qty} shares @ Rs {buy_price})\n"

            # Alert if below threshold
            if "alert_below" in h and price < h["alert_below"]:
                send_message(
                    f"ALERT: <b>{display}</b>\n"
                    f"Price Rs {price:,.2f} has dropped below your alert level of Rs {h['alert_below']:,.2f}!\n"
                    f"P&L: Rs {pnl:+,.2f}"
                )

    send_message(msg)

def send_single_stock_update(ticker):
    """Send an immediate price update for one stock right after it's added."""
    price = get_price(ticker)
    display = ticker.replace(".NS", "").replace(".BO", "")
    now_str = datetime.now(IST).strftime("%I:%M %p")

    if price is None:
        send_message(f"Could not fetch current price for <b>{display}</b>.")
        return

    msg = f"<b>{display}</b> - Current Price as of {now_str} IST\n"
    msg += f"Price: Rs {price:,.2f}\n"

    data = load_data()
    if ticker in data["holdings"]:
        h = data["holdings"][ticker]
        qty = h["qty"]
        buy_price = h["buy_price"]
        pnl = (price - buy_price) * qty
        pnl_label = "GAIN" if pnl >= 0 else "LOSS"
        msg += f"P&L ({pnl_label}): Rs {pnl:+,.2f} ({qty} shares @ Rs {buy_price})\n"

    send_message(msg)

# ─────────────────────────────────────────────
# COMMAND HANDLER
# ─────────────────────────────────────────────
def handle_commands():
    """
    Supported commands:
    /add RELIANCE.NS 10 2500 2400
        -> Add holding: ticker, qty, buy_price, alert_below (optional)
    /remove RELIANCE.NS
        -> Remove a holding
    /watch RELIANCE.NS
        -> Watch a stock without holding info
    /portfolio
        -> Show current portfolio snapshot
    /help
        -> Show all commands
    """
    data = load_data()
    updates = get_updates(offset=handle_commands.last_update_id)

    for update in updates:
        handle_commands.last_update_id = update["update_id"] + 1
        msg = update.get("message", {})
        text = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        # Only respond to your own chat
        if chat_id != str(CHAT_ID):
            continue

        parts = text.split()
        cmd = parts[0].lower() if parts else ""

        # /help
        if cmd == "/help":
            send_message(
                "<b>Stock Bot Commands</b>\n\n"
                "/add <b>TICKER QTY BUY_PRICE [ALERT_BELOW]</b>\n"
                "  e.g. <code>/add RELIANCE.NS 10 2500 2400</code>\n\n"
                "/remove <b>TICKER</b>\n"
                "  e.g. <code>/remove RELIANCE.NS</code>\n\n"
                "/watch <b>TICKER</b>\n"
                "  e.g. <code>/watch TCS.NS</code> (no holdings, just track price)\n\n"
                "/portfolio - View current snapshot\n"
                "/help - Show this message\n\n"
                "Tip: Use <code>.NS</code> for NSE, <code>.BO</code> for BSE"
            )

        # /add TICKER QTY BUY_PRICE [ALERT_BELOW]
        elif cmd == "/add":
            if len(parts) < 4:
                send_message("Usage: /add TICKER QTY BUY_PRICE [ALERT_BELOW]\nExample: /add RELIANCE.NS 10 2500 2400")
                continue
            try:
                ticker = parts[1].upper()
                qty = float(parts[2])
                buy_price = float(parts[3])
                alert_below = float(parts[4]) if len(parts) > 4 else None

                entry = {"qty": qty, "buy_price": buy_price}
                if alert_below:
                    entry["alert_below"] = alert_below

                data = load_data()  # reload fresh before modifying

                data["holdings"][ticker] = entry
                # Remove from watchlist if it was there
                if ticker in data["watchlist"]:
                    data["watchlist"].remove(ticker)

                save_data(data)
                display = ticker.replace(".NS","").replace(".BO","")
                alert_txt = f"\nAlert set below Rs {alert_below:,.2f}" if alert_below else ""
                send_message(f"Added <b>{display}</b>\n{qty} shares @ Rs {buy_price:,.2f}{alert_txt}")
                # Send immediate price update
                send_single_stock_update(ticker)
            except:
                send_message("Invalid format. Example: /add RELIANCE.NS 10 2500 2400")

        # /remove TICKER
        elif cmd == "/remove":
            if len(parts) < 2:
                send_message("Usage: /remove TICKER\nExample: /remove RELIANCE.NS")
                continue
            ticker = parts[1].upper()
            data = load_data()  # reload fresh before modifying
            removed = False
            if ticker in data["holdings"]:
                del data["holdings"][ticker]
                removed = True
            if ticker in data["watchlist"]:
                data["watchlist"].remove(ticker)
                removed = True
            save_data(data)
            display = ticker.replace(".NS","").replace(".BO","")
            send_message(f"Removed <b>{display}</b>" if removed else f"<b>{display}</b> not found in your list.\nMake sure you include .NS or .BO e.g. /remove TATSILV.NS")

        # /watch TICKER
        elif cmd == "/watch":
            if len(parts) < 2:
                send_message("Usage: /watch TICKER\nExample: /watch TCS.NS")
                continue
            ticker = parts[1].upper()
            data = load_data()  # reload fresh before modifying
            if ticker not in data["watchlist"] and ticker not in data["holdings"]:
                data["watchlist"].append(ticker)
                save_data(data)
            display = ticker.replace(".NS","").replace(".BO","")
            send_message(f"Now watching <b>{display}</b> (no holding info)")
            # Send immediate price update
            send_single_stock_update(ticker)

        # /portfolio
        elif cmd == "/portfolio":
            send_hourly_update(force=True)

# Persist last update ID across calls
handle_commands.last_update_id = 0

# ─────────────────────────────────────────────
# SCHEDULER
# ─────────────────────────────────────────────
def setup_schedule():
    # Send update every hour on the hour during tracking hours
    for hour in range(9, 18):  # 9 AM to 5 PM
        t = f"{hour:02d}:00"
        schedule.every().day.at(t).do(send_hourly_update)
    # Also at 9:15 open and 5:00 close
    schedule.every().day.at("09:15").do(lambda: send_message("<b>Tracking Started!</b> Updates every hour until 5:00 PM IST."))
    schedule.every().day.at("17:00").do(lambda: send_message("<b>Tracking Stopped.</b> Will resume tomorrow at 9:15 AM IST."))

# ─────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────
def main():
    print("Stock Bot started. Waiting for commands and market hours...")
    send_message(
        "<b>Stock Alert Bot is Online!</b>\n\n"
        "Updates run: Mon-Fri, 9:15 AM - 5:00 PM IST\n"
        "Hourly price + P&L reports\n\n"
        "Type /help to get started."
    )

    setup_schedule()

    while True:
        schedule.run_pending()
        handle_commands()
        time.sleep(3)  # Poll for commands every 3 seconds

if __name__ == "__main__":
    main()