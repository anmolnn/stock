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
from flask import Flask, jsonify, request

BOT_TOKEN = os.getenv("BOT_TOKEN")
CHAT_ID = os.getenv("CHAT_ID")
DATA_FILE = "stock_data.json"
IST = pytz.timezone("Asia/Kolkata")

app = Flask(__name__)
from flask_cors import CORS
CORS(app)
@app.route("/")
def home():
    return "Stock Bot is running!"
@app.route("/api/portfolio")
def api_portfolio():
    """Return all holdings + watchlist with live prices."""
    data     = load_data()
    holdings = []
    for ticker, h in data["holdings"].items():
        price = get_price(ticker)
        holdings.append({
            "ticker":      ticker,
            "qty":         h["qty"],
            "buy_price":   h["buy_price"],
            "alert_below": h.get("alert_below"),
            "price":       price,
        })
    watchlist = []
    for ticker in data["watchlist"]:
        price = get_price(ticker)
        watchlist.append({ "ticker": ticker, "price": price })
    return jsonify({ "holdings": holdings, "watchlist": watchlist })


@app.route("/api/add", methods=["POST"])
def api_add():
    """Add a holding from the web dashboard."""
    body        = request.get_json()
    ticker      = body.get("ticker", "").upper().strip()
    qty         = body.get("qty")
    buy_price   = body.get("buy_price")
    alert_below = body.get("alert_below")

    if not ticker or qty is None or buy_price is None:
        return jsonify({"error": "ticker, qty and buy_price are required"}), 400

    data  = load_data()
    entry = {"qty": float(qty), "buy_price": float(buy_price)}
    if alert_below:
        entry["alert_below"] = float(alert_below)
    data["holdings"][ticker] = entry
    if ticker in data["watchlist"]:
        data["watchlist"].remove(ticker)
    save_data(data)

    # Also send a Telegram notification
    display = ticker.replace(".NS","").replace(".BO","")
    send_message(f"Added via Dashboard: <b>{display}</b>\n{format_qty(qty)} shares @ Rs {float(buy_price):,.2f}")

    return jsonify({"success": True, "ticker": ticker})


@app.route("/api/remove/<ticker>", methods=["DELETE"])
def api_remove(ticker):
    """Remove a holding or watchlist item from the web dashboard."""
    ticker = ticker.upper()
    data   = load_data()
    removed = False
    if ticker in data["holdings"]:
        del data["holdings"][ticker]
        removed = True
    if ticker in data["watchlist"]:
        data["watchlist"].remove(ticker)
        removed = True
    save_data(data)

    if removed:
        display = ticker.replace(".NS","").replace(".BO","")
        send_message(f"Removed via Dashboard: <b>{display}</b>")

    return jsonify({"success": removed})


@app.route("/api/price/<ticker>")
def api_price(ticker):
    """Fetch live price for any ticker."""
    price = get_price(ticker.upper())
    if price is None:
        return jsonify({"error": "Could not fetch price"}), 404
    return jsonify({"ticker": ticker.upper(), "price": price})
def start_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)
def load_data():
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    return {"holdings": {}, "watchlist": []}
def save_data(data):
    with open(DATA_FILE, "w") as f:
        json.dump(data, f, indent=2)
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

def get_price(ticker):
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
    return int(qty) if qty == int(qty) else qty

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
    send_portfolio(force=True, title="End of Day Update")
    send_message("<b>Tracking Stopped.</b> Resumes tomorrow at 9:15 AM IST.")

def send_stock_added_message(ticker, qty, buy_price, alert_below):
    price = get_price(ticker)
    display = ticker.replace(".NS", "").replace(".BO", "")
    now_str = datetime.now(IST).strftime("%I:%M %p")
    alert_txt = f"\nAlert set below Rs {alert_below:,.2f}" if alert_below else ""
    msg = f"Added <b>{display}</b>\n{format_qty(qty)} shares @ Rs {buy_price:,.2f}{alert_txt}\n\n"
    if price is None:
        msg += "Could not fetch current price.\nCheck that ticker is correct"
    else:
        pnl = (price - buy_price) * qty
        pnl_label = "GAIN" if pnl >= 0 else "LOSS"
        msg += f"Current Price as of {now_str} IST: Rs {price:,.2f}\n"
        msg += f"P&L ({pnl_label}): Rs {pnl:+,.2f}"

    send_message(msg)

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
        elif cmd == "/add":
            if len(parts) < 4:
                send_message(
                    "Usage: /add TICKER QTY BUY_PRICE [ALERT_BELOW]\n"
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

        elif cmd == "/portfolio":
            send_portfolio(force=True, title="Portfolio Snapshot")

        else:
            send_message("Unknown command. Type /help to see available commands.")

def send_daily_summary():
    data = load_data()

    total_invested = 0
    total_current = 0

    for ticker, h in data["holdings"].items():
        price = get_price(ticker)
        if price:
            invested = h["buy_price"] * h["qty"]
            current = price * h["qty"]

            total_invested += invested
            total_current += current

    if total_invested == 0:
        send_message("<b>Daily Summary</b>\nNo holdings to calculate P&L.")
        return

    total_pnl = total_current - total_invested
    percent = (total_pnl / total_invested) * 100

    label = "GAIN" if total_pnl >= 0 else "LOSS"

    send_message(
        f"<b>Daily Summary (Market Close)</b>\n"
        f"{'─'*28}\n"
        f"Invested: Rs {total_invested:,.2f}\n"
        f"Current Value: Rs {total_current:,.2f}\n"
        f"Total P&L: Rs {total_pnl:+,.2f} ({percent:+.2f}%) {label}"
    )
last_scheduler_minute = None
def ist_scheduler():
    global last_scheduler_minute
    now = datetime.now(IST)
    if last_scheduler_minute == now.minute:
        return
    last_scheduler_minute = now.minute
    if now.hour == 9 and now.minute == 15:
        send_message("<b>Tracking Started!</b> Updates every hour until 5:00 PM IST.")

    if now.minute == 0 and 10 <= now.hour < 15:
        send_portfolio()
    if now.hour == 15 and now.minute == 0:
        send_market_close()
        send_daily_summary()
        data = load_data()
        total_pnl = 0
        for ticker, h in data["holdings"].items():
            price = get_price(ticker)
            if price:
                total_pnl += (price - h["buy_price"]) * h["qty"]
        label = "GAIN " if total_pnl >= 0 else "LOSS "
        send_message(
            f"<b>Daily Summary</b>\n"
            f"Total P&L: Rs {total_pnl:+,.2f} ({label})"
        )

def main():
    global last_update_id
    if not BOT_TOKEN or not CHAT_ID:
        print("[ERROR] BOT_TOKEN or CHAT_ID is missing.")
        return
    print("Stock Bot started.")

    web_thread = Thread(target=start_web, daemon=True)
    web_thread.start()

    last_update_id = skip_old_updates()

    first_run = not os.path.exists(DATA_FILE)
    if first_run:
        send_message(
            "<b>Stock Alert Bot is Online!</b>\n\n"
            "Updates run: Mon-Fri, 9:15 AM - 5:00 PM IST\n"
            "Hourly price + P&L reports\n\n"
            "Type /help to get started."
        )
        save_data({"holdings": {}, "watchlist": []})
    while True:
        ist_scheduler()
        handle_commands()
        time.sleep(3)
if __name__ == "__main__":
    main()