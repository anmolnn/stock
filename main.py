"""
NSE/BSE Stock Alert Telegram Bot - Multi-User Version
- Supabase (PostgreSQL) for persistent multi-user storage
- Google OAuth + JWT for authentication
- Runs between 9:15 AM - 5:00 PM IST
- Sends hourly updates per linked Telegram user
- Flask web server for Render port binding
"""

import os
import time
import random
import string
import requests
import yfinance as yf
import pytz
import jwt
from datetime import datetime
from functools import wraps
from threading import Thread
from flask import Flask, jsonify, request
from flask_cors import CORS
from supabase import create_client, Client
from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

# ── Setup ─────────────────────────────────────────────────────────────────────

BOT_TOKEN        = os.getenv("BOT_TOKEN")
SUPABASE_URL     = os.getenv("SUPABASE_URL")
SUPABASE_KEY     = os.getenv("SUPABASE_KEY")
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
JWT_SECRET       = os.getenv("JWT_SECRET")
IST              = pytz.timezone("Asia/Kolkata")

supabase: Client = create_client(SUPABASE_URL, SUPABASE_KEY)

app = Flask(__name__)
CORS(app)

# ── JWT Auth Decorator ────────────────────────────────────────────────────────

def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth_header = request.headers.get("Authorization", "")
        if not auth_header.startswith("Bearer "):
            return jsonify({"error": "Unauthorized"}), 401
        token = auth_header.split(" ")[1]
        try:
            payload = jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            request.user_id = payload["user_id"]
        except Exception:
            return jsonify({"error": "Invalid or expired token"}), 401
        return f(*args, **kwargs)
    return decorated

# ── Supabase Helpers ──────────────────────────────────────────────────────────

def get_user_by_telegram_id(chat_id: str):
    res = supabase.table("users").select("*").eq("telegram_user_id", str(chat_id)).execute()
    return res.data[0] if res.data else None

def get_user_by_id(user_id: int):
    res = supabase.table("users").select("*").eq("id", user_id).execute()
    return res.data[0] if res.data else None

def get_holdings(user_id: int):
    res = supabase.table("holdings").select("*").eq("user_id", user_id).execute()
    return res.data or []

def get_watchlist(user_id: int):
    res = supabase.table("watchlist").select("*").eq("user_id", user_id).execute()
    return res.data or []

def get_all_linked_users():
    res = supabase.table("users").select("*").neq("telegram_user_id", None).execute()
    return res.data or []

def upsert_holding(user_id: int, ticker: str, qty: float, buy_price: float, alert_below=None):
    supabase.table("holdings").delete().eq("user_id", user_id).eq("ticker", ticker).execute()
    row = {"user_id": user_id, "ticker": ticker, "qty": qty, "buy_price": buy_price}
    if alert_below is not None:
        row["alert_below"] = alert_below
    supabase.table("holdings").insert(row).execute()

def remove_holding(user_id: int, ticker: str) -> bool:
    res = supabase.table("holdings").delete().eq("user_id", user_id).eq("ticker", ticker).execute()
    return bool(res.data)

def add_to_watchlist(user_id: int, ticker: str):
    existing = supabase.table("watchlist").select("id").eq("user_id", user_id).eq("ticker", ticker).execute()
    if not existing.data:
        supabase.table("watchlist").insert({"user_id": user_id, "ticker": ticker}).execute()

def remove_from_watchlist(user_id: int, ticker: str) -> bool:
    res = supabase.table("watchlist").delete().eq("user_id", user_id).eq("ticker", ticker).execute()
    return bool(res.data)

def ticker_in_holdings(user_id: int, ticker: str) -> bool:
    res = supabase.table("holdings").select("id").eq("user_id", user_id).eq("ticker", ticker).execute()
    return bool(res.data)

# ── Telegram Helpers ──────────────────────────────────────────────────────────

def send_message_to(chat_id, text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    try:
        requests.post(
            url,
            data={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
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

# ── Stock Price ───────────────────────────────────────────────────────────────

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

def format_qty(qty):
    return int(qty) if qty == int(qty) else qty

# ── Market Hours ──────────────────────────────────────────────────────────────

def is_market_open():
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    market_open  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    market_close = now.replace(hour=17, minute=0,  second=0, microsecond=0)
    return market_open <= now <= market_close

# ── Portfolio Messages ────────────────────────────────────────────────────────

def build_portfolio_message(user_id: int, title: str) -> str:
    holdings  = get_holdings(user_id)
    watchlist = get_watchlist(user_id)

    if not holdings and not watchlist:
        return "No stocks in your watchlist or holdings yet.\nUse /add to add a stock."

    now_str     = datetime.now(IST).strftime("%I:%M %p")
    msg         = f"<b>{title} - {now_str} IST</b>\n{'─'*28}\n"
    all_tickers = {h["ticker"] for h in holdings} | {w["ticker"] for w in watchlist}
    holding_map = {h["ticker"]: h for h in holdings}

    for ticker in sorted(all_tickers):
        price   = get_price(ticker)
        display = ticker.replace(".NS", "").replace(".BO", "")

        if price is None:
            msg += f"\n<b>{display}</b> - Could not fetch price\n"
            continue

        msg += f"\n<b>{display}</b>\n"
        msg += f"   Price: Rs {price:,.2f}\n"

        if ticker in holding_map:
            h         = holding_map[ticker]
            qty       = h["qty"]
            buy_price = h["buy_price"]
            pnl       = (price - buy_price) * qty
            pnl_label = "GAIN" if pnl >= 0 else "LOSS"
            msg += f"   P&L ({pnl_label}): Rs {pnl:+,.2f} ({format_qty(qty)} shares @ Rs {buy_price})\n"

            if h.get("alert_below") and price < h["alert_below"]:
                user = get_user_by_id(user_id)
                if user and user.get("telegram_user_id"):
                    send_message_to(
                        user["telegram_user_id"],
                        f"⚠️ ALERT: <b>{display}</b>\n"
                        f"Price Rs {price:,.2f} dropped below Rs {h['alert_below']:,.2f}!\n"
                        f"P&L: Rs {pnl:+,.2f}"
                    )
    return msg

def send_portfolio_to_all(force=False, title="Hourly Update"):
    if not force and not is_market_open():
        return
    for user in get_all_linked_users():
        msg = build_portfolio_message(user["id"], title)
        send_message_to(user["telegram_user_id"], msg)

def send_daily_summary_to_all():
    for user in get_all_linked_users():
        holdings       = get_holdings(user["id"])
        total_invested = 0
        total_current  = 0

        for h in holdings:
            price = get_price(h["ticker"])
            if price:
                total_invested += h["buy_price"] * h["qty"]
                total_current  += price * h["qty"]

        if total_invested == 0:
            send_message_to(user["telegram_user_id"], "<b>Daily Summary</b>\nNo holdings to calculate P&L.")
            continue

        total_pnl = total_current - total_invested
        percent   = (total_pnl / total_invested) * 100
        label     = "GAIN" if total_pnl >= 0 else "LOSS"

        send_message_to(
            user["telegram_user_id"],
            f"<b>Daily Summary (Market Close)</b>\n"
            f"{'─'*28}\n"
            f"Invested: Rs {total_invested:,.2f}\n"
            f"Current Value: Rs {total_current:,.2f}\n"
            f"Total P&L: Rs {total_pnl:+,.2f} ({percent:+.2f}%) {label}"
        )

def send_stock_added_message(chat_id, ticker, qty, buy_price, alert_below):
    price     = get_price(ticker)
    display   = ticker.replace(".NS", "").replace(".BO", "")
    now_str   = datetime.now(IST).strftime("%I:%M %p")
    alert_txt = f"\nAlert set below Rs {alert_below:,.2f}" if alert_below else ""
    msg       = f"Added <b>{display}</b>\n{format_qty(qty)} shares @ Rs {buy_price:,.2f}{alert_txt}\n\n"

    if price is None:
        msg += "Could not fetch current price.\nCheck that ticker is correct."
    else:
        pnl       = (price - buy_price) * qty
        pnl_label = "GAIN" if pnl >= 0 else "LOSS"
        msg += f"Current Price as of {now_str} IST: Rs {price:,.2f}\n"
        msg += f"P&L ({pnl_label}): Rs {pnl:+,.2f}"

    send_message_to(chat_id, msg)

# ── Telegram Command Handler ──────────────────────────────────────────────────

last_update_id = 0

def handle_commands():
    global last_update_id
    updates = get_updates(offset=last_update_id)

    for update in updates:
        last_update_id = update["update_id"] + 1
        msg     = update.get("message", {})
        text    = msg.get("text", "").strip()
        chat_id = str(msg.get("chat", {}).get("id", ""))

        if not text or not chat_id:
            continue

        parts = text.split()
        if not parts or not parts[0].startswith("/"):
            continue

        cmd = parts[0].lower()

        # /start or /link — open to anyone to link their account
        if cmd in ("/start", "/link"):
            if len(parts) < 2:
                send_message_to(
                    chat_id,
                    "Welcome to StockBot! 👋\n\n"
                    "To get started:\n"
                    "1. Visit the website and sign in with Google\n"
                    "2. Go to your Dashboard and click <b>Link Telegram</b>\n"
                    "3. Copy the 6-digit code and send it here as:\n"
                    "   <code>/link YOUR_CODE</code>"
                )
                continue

            code = parts[1].strip().upper()
            res  = supabase.table("users").select("*").eq("link_code", code).execute()
            if not res.data:
                send_message_to(chat_id, "❌ Invalid or expired code.\nPlease generate a new one from the website.")
                continue

            user = res.data[0]
            supabase.table("users").update({
                "telegram_user_id": chat_id,
                "link_code": None
            }).eq("id", user["id"]).execute()

            name = user.get("name") or "there"
            send_message_to(
                chat_id,
                f"✅ Account linked! Hi <b>{name}</b>!\n\n"
                "Your portfolio is now connected to this Telegram.\n"
                "Type /help to see available commands."
            )
            continue

        # All other commands require a linked account
        user = get_user_by_telegram_id(chat_id)
        if not user:
            send_message_to(
                chat_id,
                "Your Telegram is not linked yet.\n\n"
                "Visit the website, sign in with Google,\n"
                "then send: <code>/link YOUR_CODE</code>"
            )
            continue

        user_id = user["id"]

        if cmd == "/help":
            send_message_to(
                chat_id,
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
                send_message_to(chat_id, "Usage: /add TICKER QTY BUY_PRICE [ALERT_BELOW]")
                continue
            try:
                ticker      = parts[1].upper()
                qty         = float(parts[2])
                buy_price   = float(parts[3])
                alert_below = float(parts[4]) if len(parts) > 4 else None
                upsert_holding(user_id, ticker, qty, buy_price, alert_below)
                remove_from_watchlist(user_id, ticker)
                send_stock_added_message(chat_id, ticker, qty, buy_price, alert_below)
            except Exception as e:
                print(f"[/add Error] {e}")
                send_message_to(chat_id, "Invalid format.\nExample: /add TATSILV.NS 10 23.47 20")

        elif cmd == "/remove":
            if len(parts) < 2:
                send_message_to(chat_id, "Usage: /remove TICKER\nExample: /remove TATSILV.NS")
                continue
            ticker    = parts[1].upper()
            display   = ticker.replace(".NS", "").replace(".BO", "")
            removed_h = remove_holding(user_id, ticker)
            removed_w = remove_from_watchlist(user_id, ticker)
            if removed_h or removed_w:
                send_message_to(chat_id, f"Removed <b>{display}</b> successfully.")
            else:
                send_message_to(
                    chat_id,
                    f"<b>{display}</b> not found.\n"
                    f"Make sure to include .NS or .BO\n"
                    f"Example: /remove TATSILV.NS"
                )

        elif cmd == "/watch":
            if len(parts) < 2:
                send_message_to(chat_id, "Usage: /watch TICKER\nExample: /watch TCS.NS")
                continue
            ticker  = parts[1].upper()
            display = ticker.replace(".NS", "").replace(".BO", "")
            if not ticker_in_holdings(user_id, ticker):
                add_to_watchlist(user_id, ticker)
            price   = get_price(ticker)
            now_str = datetime.now(IST).strftime("%I:%M %p")
            if price:
                send_message_to(
                    chat_id,
                    f"Now watching <b>{display}</b>\n"
                    f"Current Price as of {now_str} IST: Rs {price:,.2f}"
                )
            else:
                send_message_to(
                    chat_id,
                    f"Now watching <b>{display}</b>\n"
                    f"Could not fetch price. Check ticker is correct."
                )

        elif cmd == "/portfolio":
            msg = build_portfolio_message(user_id, "Portfolio Snapshot")
            send_message_to(chat_id, msg)

        else:
            send_message_to(chat_id, "Unknown command. Type /help to see available commands.")

# ── Flask Routes ──────────────────────────────────────────────────────────────

@app.route("/")
def home():
    return "Stock Bot is running!"


@app.route("/api/auth/google", methods=["POST"])
def google_auth():
    """Verify Google token, create/fetch user, return our JWT."""
    body  = request.get_json()
    token = body.get("token")
    if not token:
        return jsonify({"error": "Token is required"}), 400

    try:
        info = id_token.verify_oauth2_token(
            token,
            google_requests.Request(),
            GOOGLE_CLIENT_ID
        )
    except Exception as e:
        print(f"[Google Auth Error] {e}")
        return jsonify({"error": "Invalid Google token"}), 401

    google_id = info["sub"]
    email     = info["email"]
    name      = info.get("name", "")

    res  = supabase.table("users").select("*").eq("google_id", google_id).execute()
    user = res.data[0] if res.data else None

    if not user:
        new_res = supabase.table("users").insert({
            "google_id": google_id,
            "email":     email,
            "name":      name,
        }).execute()
        user = new_res.data[0]

    our_token = jwt.encode(
        {"user_id": user["id"], "email": user["email"], "name": user["name"]},
        JWT_SECRET,
        algorithm="HS256"
    )

    return jsonify({
        "token":           our_token,
        "user_id":         user["id"],
        "name":            user["name"],
        "email":           user["email"],
        "telegram_linked": user["telegram_user_id"] is not None,
    })


@app.route("/api/me")
@require_auth
def api_me():
    user = get_user_by_id(request.user_id)
    if not user:
        return jsonify({"error": "User not found"}), 404
    return jsonify({
        "user_id":         user["id"],
        "name":            user["name"],
        "email":           user["email"],
        "telegram_linked": user["telegram_user_id"] is not None,
    })


@app.route("/api/portfolio")
@require_auth
def api_portfolio():
    user_id        = request.user_id
    holdings_rows  = get_holdings(user_id)
    watchlist_rows = get_watchlist(user_id)

    holdings = []
    for h in holdings_rows:
        price = get_price(h["ticker"])
        holdings.append({
            "ticker":      h["ticker"],
            "qty":         h["qty"],
            "buy_price":   h["buy_price"],
            "alert_below": h.get("alert_below"),
            "price":       price,
        })

    watchlist = []
    for w in watchlist_rows:
        price = get_price(w["ticker"])
        watchlist.append({"ticker": w["ticker"], "price": price})

    return jsonify({"holdings": holdings, "watchlist": watchlist})


@app.route("/api/add", methods=["POST"])
@require_auth
def api_add():
    user_id     = request.user_id
    body        = request.get_json()
    ticker      = body.get("ticker", "").upper().strip()
    qty         = body.get("qty")
    buy_price   = body.get("buy_price")
    alert_below = body.get("alert_below")

    if not ticker or qty is None or buy_price is None:
        return jsonify({"error": "ticker, qty and buy_price are required"}), 400

    upsert_holding(user_id, ticker, float(qty), float(buy_price),
                   float(alert_below) if alert_below else None)
    remove_from_watchlist(user_id, ticker)

    user    = get_user_by_id(user_id)
    display = ticker.replace(".NS", "").replace(".BO", "")
    if user and user.get("telegram_user_id"):
        send_message_to(
            user["telegram_user_id"],
            f"Added via Dashboard: <b>{display}</b>\n"
            f"{format_qty(float(qty))} shares @ Rs {float(buy_price):,.2f}"
        )

    return jsonify({"success": True, "ticker": ticker})


@app.route("/api/remove/<ticker>", methods=["DELETE"])
@require_auth
def api_remove(ticker):
    user_id   = request.user_id
    ticker    = ticker.upper()
    removed_h = remove_holding(user_id, ticker)
    removed_w = remove_from_watchlist(user_id, ticker)
    removed   = removed_h or removed_w

    if removed:
        user    = get_user_by_id(user_id)
        display = ticker.replace(".NS", "").replace(".BO", "")
        if user and user.get("telegram_user_id"):
            send_message_to(user["telegram_user_id"], f"Removed via Dashboard: <b>{display}</b>")

    return jsonify({"success": removed})


@app.route("/api/watch", methods=["POST"])
@require_auth
def api_watch():
    user_id = request.user_id
    body    = request.get_json()
    ticker  = body.get("ticker", "").upper().strip()
    if not ticker:
        return jsonify({"error": "ticker is required"}), 400
    if not ticker_in_holdings(user_id, ticker):
        add_to_watchlist(user_id, ticker)
    display = ticker.replace(".NS", "").replace(".BO", "")
    user    = get_user_by_id(user_id)
    if user and user.get("telegram_user_id"):
        price   = get_price(ticker)
        now_str = datetime.now(IST).strftime("%I:%M %p")
        if price:
            send_message_to(
                user["telegram_user_id"],
                f"Added to Watchlist via Dashboard: <b>{display}</b>\n"
                f"Current Price as of {now_str} IST: Rs {price:,.2f}"
            )
        else:
            send_message_to(
                user["telegram_user_id"],
                f"Added to Watchlist via Dashboard: <b>{display}</b>"
            )
    return jsonify({"success": True, "ticker": ticker})


@app.route("/api/price/<ticker>")
def api_price(ticker):
    price = get_price(ticker.upper())
    if price is None:
        return jsonify({"error": "Could not fetch price"}), 404
    return jsonify({"ticker": ticker.upper(), "price": price})


@app.route("/api/generate-link-code", methods=["POST"])
@require_auth
def generate_link_code():
    user_id = request.user_id
    code    = ''.join(random.choices(string.ascii_uppercase + string.digits, k=6))
    supabase.table("users").update({"link_code": code}).eq("id", user_id).execute()
    return jsonify({"code": code})

# ── Scheduler ─────────────────────────────────────────────────────────────────

last_scheduler_minute = None

def ist_scheduler():
    global last_scheduler_minute
    now = datetime.now(IST)
    if last_scheduler_minute == now.minute:
        return
    last_scheduler_minute = now.minute

    if now.hour == 9 and now.minute == 15:
        for user in get_all_linked_users():
            send_message_to(
                user["telegram_user_id"],
                "<b>Market Open!</b> Tracking started. Updates every hour until 5:00 PM IST."
            )

    if now.minute == 0 and 10 <= now.hour < 15:
        send_portfolio_to_all()

    if now.hour == 15 and now.minute == 0:
        send_portfolio_to_all(force=True, title="End of Day Update")
        send_daily_summary_to_all()
        for user in get_all_linked_users():
            send_message_to(
                user["telegram_user_id"],
                "<b>Tracking Stopped.</b> Resumes tomorrow at 9:15 AM IST."
            )

# ── Web Server ────────────────────────────────────────────────────────────────

def start_web():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    global last_update_id
    missing = [v for v in ["BOT_TOKEN", "SUPABASE_URL", "SUPABASE_KEY", "GOOGLE_CLIENT_ID", "JWT_SECRET"] if not os.getenv(v)]
    if missing:
        print(f"[ERROR] Missing environment variables: {', '.join(missing)}")
        return

    print("Stock Bot started (multi-user, Supabase + Google Auth).")
    Thread(target=start_web, daemon=True).start()
    last_update_id = skip_old_updates()

    while True:
        ist_scheduler()
        handle_commands()
        time.sleep(3)

if __name__ == "__main__":
    main()