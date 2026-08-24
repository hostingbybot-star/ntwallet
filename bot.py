import os
import re
import html
import asyncio
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from dotenv import load_dotenv
from pymongo import MongoClient, ReturnDocument
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

# ===========================
# Config (.env se aata hai)
# ===========================
# NTESCROW_BOT_TOKEN=xxxx
# MONGO_URI=xxxx
# ADMIN_IDS=123,456   -> ye "OWNERS" hai, sirf ye naye bot-admin add/remove kar sakte hai

BOT_TOKEN = os.getenv("NTESCROW_BOT_TOKEN")
BRAND = "@NTescrowbot"
PROVIDER = "@NTescrowbot"

MONGO_URI = os.getenv("MONGO_URI")
OWNER_IDS = set(
    int(x) for x in os.getenv("ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

# In "limited" secondary accounts se /add ya /close chale to "Escrowed By" me
# inka username nahi, mapped MAIN username dikhega.
ADMIN_ALIASES = {}

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["escrow_bots"] if mongo_client else None
coll = mongo_db["deals_ntescrowbot"] if mongo_db is not None else None
meta_coll = mongo_db["meta_ntescrowbot"] if mongo_db is not None else None
admins_coll = mongo_db["bot_admins_ntescrowbot"] if mongo_db is not None else None
users_coll = mongo_db["broadcast_users_ntescrowbot"] if mongo_db is not None else None

DEALS = {}

if coll is not None:
    for doc in coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc
    print(f"✅ [NTescrowbot] {len(DEALS)} deal(s) Mongo se load hui")

# ---- Bot-admin set (owners + dynamically added admins) ----
BOT_ADMINS = set(OWNER_IDS)
if admins_coll is not None:
    for doc in admins_coll.find({}):
        BOT_ADMINS.add(doc["_id"])
    print(f"✅ [NTescrowbot] {len(BOT_ADMINS)} bot admin(s) load hue")


def save_deal(tid):
    if coll is not None:
        coll.update_one({"_id": tid}, {"$set": dict(DEALS[tid])}, upsert=True)


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in BOT_ADMINS or is_owner(uid)


def admin_only_allowed(update: Update):
    """Admin commands sirf private chat me, aur sirf admin/owner ke liye."""
    if update.effective_chat.type != "private":
        return False
    return is_admin(update.effective_user.id)


async def add_close_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add aur /close ke liye permission check:

    - Private chat: sirf hamari internal list wale bot-admin/owner (BOT_ADMINS / OWNER_IDS)
      use kar sakte hai.
    - Group / Supergroup: us GROUP ka Telegram-level admin ya owner (creator) use kar
      sakta hai — chahe wo hamari internal BOT_ADMINS list me ho ya na ho. Saath hi,
      BOT khud bhi us group me admin/owner hona chahiye, warna message delete/manage
      permission nahi milegi aur command kaam nahi karegi.

    Return: (allowed: bool, reason: str | None)
    reason sirf tab bheja jaata hai jab helpful diagnostic dena ho (warna silent skip).
    """
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type == "private":
        return is_admin(user_id), None

    if chat.type not in ("group", "supergroup"):
        return False, None

    # 1) Bot khud us group me admin/owner hai?
    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except Exception:
        return False, "❌ Bot ka admin status is group me check nahi ho paaya."
    if bot_member.status not in ("administrator", "creator"):
        return False, (
            "❌ Ye command tabhi kaam karegi jab BOT is group me Admin ho "
            "(pehle bot ko group me admin banao)."
        )

    # 2) Command chalane wala us group ka admin/owner hai?
    try:
        user_member = await context.bot.get_chat_member(chat.id, user_id)
    except Exception:
        return False, "❌ Tumhara admin status is group me check nahi ho paaya."
    if user_member.status not in ("administrator", "creator"):
        return False, None  # normal member ke liye silent skip

    return True, None


# ===========================
# Sequential Trade ID: DL-NTWALLET-1, DL-NTWALLET-2, ...
# ===========================

def next_trade_id():
    if meta_coll is not None:
        doc = meta_coll.find_one_and_update(
            {"_id": "trade_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = doc["seq"]
    else:
        seq = len(DEALS) + 1

    tid = f"DL-NTWALLET-{seq}"
    while tid in DEALS:  # safety, collision na ho
        seq += 1
        tid = f"DL-NTWALLET-{seq}"
    return tid


# ===========================
# Helpers
# ===========================

def esc(text):
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def fmt(amount, currency="INR"):
    amount = float(amount or 0)
    if currency in ("USDT", "TON"):
        value = f"{amount:,.8f}".rstrip("0").rstrip(".")
        return f"{value} {currency}"
    if currency == "INR":
        value = f"{amount:,.2f}".rstrip("0").rstrip(".")
        return f"₹{value}"
    return f"{amount:g} {currency}"


def extract_amount(text):
    match = re.search(r"[\d,]+(?:\.\d+)?", text or "")
    if not match:
        return 0.0
    value = match.group(0).replace(",", "")
    try:
        return float(value)
    except ValueError:
        return 0.0


def resolve_username(update: Update):
    user_id = update.effective_user.id
    if user_id in ADMIN_ALIASES:
        return "@" + ADMIN_ALIASES[user_id]
    return (
        "@" + update.effective_user.username
        if update.effective_user.username
        else update.effective_user.first_name
    )


# Bold unicode (Mathematical Sans-Bold) helpers
_UP = ord('𝗔') - ord('A')
_LOW = ord('𝗮') - ord('a')
_DIG = ord('𝟬') - ord('0')


def normalize_bold(text):
    out = []
    for ch in text:
        code = ord(ch)
        if ord('𝗔') <= code <= ord('𝗭'):
            out.append(chr(code - _UP))
        elif ord('𝗮') <= code <= ord('𝘇'):
            out.append(chr(code - _LOW))
        elif ord('𝟬') <= code <= ord('𝟵'):
            out.append(chr(code - _DIG))
        else:
            out.append(ch)
    return "".join(out)


# ===========================
# Premium Emoji IDs
# ===========================
# Ye IDs Telegram Premium custom-emoji document IDs hain. Har entry me "character"
# (jaise ⭐, ❤️) sirf fallback hai un clients ke liye jinke paas Premium nahi hai —
# actual visual wahi custom emoji hoga jiska ID diya gaya hai.
#
# Agar koi ID galat / expired ho jaaye to Telegram sirf fallback character dikha
# dega (crash nahi hoga). Apni khud ki custom emoji IDs nikalne ke liye:
#   1) Us emoji ko kisi message me bhejo jisme HTML/entities dikhne wala export ho
#      (ya koi "emoji id finder" utility bot use karo jo message forward karke
#      custom_emoji entities se ID nikaalta hai).
#   2) Wahan se mile document_id ko yaha neeche waali dict me daal do.
#
# Neeche di gayi IDs me se check / trade / escrow verify ho chuki hain (working).
PE = {
    "⭐️": "5181422544162391976",
    "❤️": "5260535596941582167",
    "💬": "5258330865674494479",
    "🍑": "5323761960829862762",
    "⚡️": "5938539885907415367",
    "🌐": "6041705726206808304",
    "🔥": "5420315771991497307",
    "📈": "5774022692642492953",
    "🪙": "5884428842780594914",
    "💰": "6039802097916974085",
    "🤑": "5893473283696759404",
    "📱": "6152069549442208798",
    "💤": "5895266423952904371",
    "✅": "5197474765387864959",
    "🆔": "5936017305585586269",
    "🛡": "5920052658743283381",
    "📤": "6030822047150512346",
    "⭐": "5879785854284599288",
    "👤": "5258011929993026890",
    "📝": "5879841310902324730",
    "⏱️": "5936170807716745162",
    "📌": "5796440171364749940",
    "🛡️": "5920052658743283381",
    "🚀": "5780773956030043338",
    "🏆": "6194737030165959506",
    "👑": "5807868868886009920",
    "📖": "5258328383183396223",
    "ℹ️": "5994473545650934240",
}


def pe(emoji):
    """Return a Telegram custom emoji tag only for verified IDs."""
    emoji_id = PE.get(emoji)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
    return emoji


# ===========================
# CHARGES (amount ke hisaab se slabs)
# ===========================

def calculate_fee(amount, is_exchange=False):
    if is_exchange:
        return amount * 0.025
    if amount < 200:
        return 10.0
    elif amount <= 500:
        return 20.0
    elif amount <= 2000:
        return amount * 0.04
    elif amount <= 3000:
        return amount * 0.035
    else:
        return amount * 0.03


# ===========================
# Dashboard views
# ===========================

def main_menu_kb():
    rows = [
        [InlineKeyboardButton("✦ My status", callback_data="menu:my_status")],
        [InlineKeyboardButton("★ My Deals Info", callback_data="menu:my_deals")],
        [InlineKeyboardButton("➤ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("✓ Escrow Global status", callback_data="menu:global")],
    ]
    return InlineKeyboardMarkup(rows)


def status_kb():
    """Keyboard jo /status ke saath jaata hai — private aur group dono me kaam karta hai."""
    rows = [
        [InlineKeyboardButton("★ My Deals Info", callback_data="menu:my_deals")],
        [InlineKeyboardButton("➤ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("🔄 Refresh", callback_data="refresh:my_status")],
    ]
    return InlineKeyboardMarkup(rows)


def back_refresh_kb(refresh_target):
    rows = [
        [InlineKeyboardButton("🔄 Refresh", callback_data=f"refresh:{refresh_target}")],
        [InlineKeyboardButton("➤ Back", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)


def welcome_text(first_name):
    return (
        f"{pe('⭐️')} <b>Welcome {esc(first_name)}!</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"{pe('❤️')} Escrow Bot for {BRAND}\n"
        f"{pe('💬')} Provided by {PROVIDER}\n\n"
        f"{pe('🍑')} <b>This is Your Personal Dashboard:</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"Select the option below {pe('⚡️')}\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍"
    )


def global_status_text():
    completed = [d for d in DEALS.values() if d.get("status") == "COMPLETED"]
    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    lines = [
        f"{pe('🌐')} <b>Escrow Global Statistics</b>",
        "──────────────────",
        f"{pe('🔥')} Total Deals: {len(completed)}\n",
        f"{pe('⚡️')} <b>Total Volume:</b>",
        f"  {pe('🪙')} - {totals['TON']:g} TON",
        f"  {pe('💰')} - {totals['USDT']:g} USDT",
        f"  {pe('🤑')} - {totals['INR']:g} ₹",
        "──────────────────",
        f"{pe('📱')} Escrow Bot for {BRAND}",
        f"{pe('💤')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


# ---- Leaderboard / rank ----

def _is_today(iso_ts):
    if not iso_ts:
        return False
    try:
        ts = datetime.fromisoformat(iso_ts)
    except ValueError:
        return False
    return ts.date() == datetime.now(timezone.utc).date()


def build_leaderboard(today_only=False):
    board = {}
    for d in DEALS.values():
        if d.get("status") != "COMPLETED":
            continue
        if today_only and not _is_today(d.get("completed_at")):
            continue
        user = d.get("escrowed_by", "-")
        entry = board.setdefault(user, {"deals": 0, "volume": 0.0})
        entry["deals"] += 1
        entry["volume"] += d.get("amount", 0.0)
    return board


def get_rank(username, board, by="deals"):
    ranked = sorted(board.items(), key=lambda kv: kv[1][by], reverse=True)
    for i, (user, _) in enumerate(ranked, start=1):
        if user == username:
            return i
    return len(ranked) + 1


def my_status_text(update: Update):
    username = resolve_username(update)
    first_name = update.effective_user.first_name

    mine = [d for d in DEALS.values() if d.get("escrowed_by") == username]
    completed = [d for d in mine if d.get("status") == "COMPLETED"]
    active = [d for d in mine if d.get("status") == "ACTIVE"]

    totals = {"TON": 0.0, "USDT": 0.0, "INR": 0.0}
    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    board = build_leaderboard(today_only=False)
    rank = get_rank(username, board, by="deals")

    return (
        f"{pe('📈')} <b>{esc(first_name)} Deal status !</b>\n"
        "──────────────────\n"
        f"{pe('🚀')} Rank ➤ #{rank}\n\n"
        f"{pe('🔥')} Active deals ➤ {len(active)}\n\n"
        f"{pe('✅')} Total Escrow's ➤ {len(completed)}\n\n"
        f"{pe('⚡')} Total Volume :\n"
        f"  {pe('🪙')} ➤ {totals['TON']:g} TON\n"
        f"  {pe('💰')} ➤ {totals['USDT']:g} USDT\n"
        f"  {pe('🤑')} ➤ {totals['INR']:g} ₹\n"
        "──────────────────\n"
        f"{pe('📱')} Escrow Bot for {BRAND}\n"
        f"{pe('💤')} Provided by {PROVIDER} !"
    )


# ---- My Deals Info: paginated list + detail view ----

PAGE_SIZE = 6


def deal_status_display(status):
    return {
        "ACTIVE": "🟡 PENDING",
        "HOLD": "⏸️ HOLD",
        "COMPLETED": "✅ DONE",
        "CANCELLED": "❌ CANCELLED",
        "REFUNDED": "♻️ REFUNDED",
    }.get(status, status)


def deal_detail_text(tid, deal):
    lines = [
        f"Your Deal-{esc(tid)} Info !",
        "──────────────────",
        f"➥ status: {deal_status_display(deal.get('status', '-'))}",
        f"➥ Buyer: {esc(deal.get('buyer', '-'))}",
        f"➥ Seller: {esc(deal.get('seller', '-'))}",
        f"➥ Amount: {fmt(deal.get('amount', 0), deal.get('currency', 'INR'))}",
        f"➥ Fees: {deal.get('fee_percent', 0):.1f}%",
        f"➥ Escrowed by: {esc(deal.get('escrowed_by', '-'))}",
    ]

    if deal.get("created_at"):
        dt = datetime.fromisoformat(deal["created_at"])
        lines.append(f"➥ Start Time: {dt.strftime('%H:%M:%S')}")
        lines.append(f"     [ {dt.strftime('%d %B %Y')} ]")

    if deal.get("completed_at"):
        dt2 = datetime.fromisoformat(deal["completed_at"])
        lines.append(f"➥ End Time: {dt2.strftime('%H:%M:%S')}")
        lines.append(f"     [ {dt2.strftime('%d %B %Y')} ]")

    lines += [
        "──────────────────",
        f"{pe('📱')} Escrow Bot for {BRAND}",
        f"{pe('💤')} Provided by {PROVIDER}",
    ]
    return "\n".join(lines)


def my_deals_header_text(update: Update):
    first_name = update.effective_user.first_name
    return (
        f"{pe('♡')} <b>{esc(first_name)} All deals info !</b>\n"
        "──────────────────\n"
        "Select the deal below for info :\n"
        "──────────────────"
    )


def my_deals_ids(update: Update):
    username = resolve_username(update)
    ids = [tid for tid, d in DEALS.items() if d.get("escrowed_by") == username]
    return list(reversed(ids))  # naye deals upar


def my_deals_kb(update: Update, page=0):
    ids = my_deals_ids(update)
    start = page * PAGE_SIZE
    chunk = ids[start:start + PAGE_SIZE]

    rows = [[InlineKeyboardButton(tid, callback_data=f"dealview:{tid}:{page}")] for tid in chunk]

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀ Prev", callback_data=f"dealspage:{page-1}"))
    if start + PAGE_SIZE < len(ids):
        nav.append(InlineKeyboardButton("Next ▶", callback_data=f"dealspage:{page+1}"))
    if nav:
        rows.append(nav)

    rows.append([InlineKeyboardButton("➤ Back", callback_data="menu:back")])
    return InlineKeyboardMarkup(rows), len(ids)


def deal_view_kb(page):
    rows = [
        [InlineKeyboardButton("◀ Back to My Deals", callback_data=f"dealspage:{page}")],
        [InlineKeyboardButton("➤ Main Menu", callback_data="menu:back")],
    ]
    return InlineKeyboardMarkup(rows)


def pending_deals_text(update: Update):
    username = resolve_username(update)
    pending = [
        (tid, d)
        for tid, d in DEALS.items()
        if d.get("escrowed_by") == username and d.get("status") == "ACTIVE"
    ]
    if not pending:
        return f"{pe('➤')} Koi pending deal nahi hai."

    lines = [f"{pe('➤')} <b>My Pending Deals</b>", "──────────────────"]
    for tid, d in pending:
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )
    return "\n".join(lines)


# ===========================
# Broadcast subscribers
# ===========================

def remember_user(update: Update):
    if users_coll is None or not update.effective_user:
        return
    u = update.effective_user
    users_coll.update_one(
        {"_id": u.id},
        {"$set": {
            "username": u.username,
            "first_name": u.first_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    if users_coll is None:
        await update.message.reply_text("❌ MongoDB required for /broadcast.")
        return

    message = update.message.text.partition(" ")[2].strip()
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    sent = failed = 0
    for doc in users_coll.find({}, {"_id": 1}):
        try:
            await context.bot.send_message(chat_id=doc["_id"], text=message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(
        f"📢 Broadcast finished.\nSent: {sent}\nFailed: {failed}"
    )


# ===========================
# /start
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    if update.effective_chat.type != "private":
        return  # group me /start kaam nahi karega

    await update.message.reply_text(
        welcome_text(update.effective_user.first_name),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # NOTE: Telegram only accepts ONE answerCallbackQuery per click. Every
    # branch below must call query.answer() itself (with or without an
    # alert) instead of relying on a shared call up here — a second answer()
    # after an alert answer() throws and silently kills the response.

    if data.startswith("newdeal:currency:"):
        currency = data.rsplit(":", 1)[1].upper()
        state = get_nt_state(context, update)
        if not state or state.get("step") != "currency" or currency not in SUPPORTED_CURRENCIES:
            await query.answer("Start again with /add", show_alert=True)
            return
        state["currency"] = currency
        state["step"] = "amount"
        set_nt_state(context, update, state)
        await query.answer()
        await query.edit_message_text(
            f"➤ Tell me deal amount in <b>{currency}</b>\nex - <code>1</code>, <code>100</code>, <code>1000</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if data.startswith("dealaction:"):
        try:
            _, tid, action = data.split(":", 2)
        except ValueError:
            await query.answer()
            return
        deal = DEALS.get(tid)
        if not deal or deal.get("status") != "ACTIVE":
            await query.answer("Deal unavailable.", show_alert=True)
            return
        username = resolve_username(update).lower()
        parties = {str(deal.get("buyer","")).lower(), str(deal.get("seller","")).lower()}
        if username not in parties:
            await query.answer("Only Buyer or Seller can confirm.", show_alert=True)
            return

        votes = deal.setdefault("votes", {"release": [], "refund": []})
        votes.setdefault("release", [])
        votes.setdefault("refund", [])
        opposite = "refund" if action == "release" else "release"
        votes[opposite] = [x for x in votes[opposite] if x.lower() != username]
        if username not in [x.lower() for x in votes[action]]:
            votes[action].append(resolve_username(update))
        save_deal(tid)

        await query.answer(f"✅ Your vote recorded: {action.title()}")

        if parties.issubset({x.lower() for x in votes[action]}):
            label = "Release" if action == "release" else "Refund"
            await context.bot.send_message(
                chat_id=deal["chat_id"],
                text=(
                    f"😐 Buyer [{esc(deal['buyer'])}] & Seller [{esc(deal['seller'])}] agreed to {label}.\n\n"
                    f"Dear {esc(deal['escrowed_by'])}, please {action} the funds according to deal.\n\n"
                    "❗️Verify both usernames before proceeding."
                ),
                parse_mode=ParseMode.HTML,
            )
            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass
        return

    # Everything below is the plain dashboard/menu navigation — none of it
    # answers with an alert, so one shared answer() up front is fine.
    await query.answer()

    if data == "menu:back":
        # Private me full dashboard, group me wapas apne status pe.
        if update.effective_chat.type == "private":
            await query.edit_message_text(
                welcome_text(update.effective_user.first_name),
                parse_mode=ParseMode.HTML,
                reply_markup=main_menu_kb(),
            )
        else:
            await query.edit_message_text(
                my_status_text(update),
                parse_mode=ParseMode.HTML,
                reply_markup=status_kb(),
            )
        return

    if data in ("menu:my_deals",) or data.startswith("dealspage:"):
        page = 0
        if data.startswith("dealspage:"):
            page = int(data.split(":", 1)[1])
        kb, total = my_deals_kb(update, page)
        if total == 0:
            text = my_deals_header_text(update) + "\n\n📭 Koi deal nahi mili."
        else:
            text = my_deals_header_text(update)
        await query.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=kb)
        return

    if data.startswith("dealview:"):
        _, tid, page = data.split(":", 2)
        deal = DEALS.get(tid)
        if not deal:
            await query.edit_message_text("❌ Deal not found.", reply_markup=deal_view_kb(int(page)))
            return
        await query.edit_message_text(
            deal_detail_text(tid, deal),
            parse_mode=ParseMode.HTML,
            reply_markup=deal_view_kb(int(page)),
        )
        return

    target = None
    if data in ("menu:my_status", "refresh:my_status"):
        target = "my_status"
        text = my_status_text(update)
    elif data in ("menu:pending", "refresh:pending"):
        target = "pending"
        text = pending_deals_text(update)
    elif data in ("menu:global", "refresh:global"):
        target = "global"
        text = global_status_text()
    else:
        return

    await query.edit_message_text(
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=back_refresh_kb(target),
    )



# ===========================
# NTwallet interactive deal flow
# ===========================

SUPPORTED_CURRENCIES = ("TON", "USDT", "INR")
DEFAULT_FEE_PERCENT = float(os.getenv("DEAL_FEE_PERCENT", "1.0"))
FORM_TITLE = "#NTwallet [Escrow Form]"


def currency_kb():
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("TON", callback_data="newdeal:currency:TON"),
        InlineKeyboardButton("USDT", callback_data="newdeal:currency:USDT"),
        InlineKeyboardButton("INR", callback_data="newdeal:currency:INR"),
    ]])


def deal_action_kb(tid):
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("Release", callback_data=f"dealaction:{tid}:release"),
        InlineKeyboardButton("Refund", callback_data=f"dealaction:{tid}:refund"),
    ]])


def nt_form_text(currency, amount, escrower):
    return (
        f"<b>{FORM_TITLE}</b> :\n\n"
        f"➥ Deal Type: {esc(currency)}\n"
        "➥ Buyer :\n"
        "➥ Seller :\n"
        "➥ Item :\n"
        f"➥ Amount : {esc(fmt(amount, currency))}\n"
        "➥ Terms :\n\n"
        f"🔒 Escrowed by {esc(escrower)}"
    )


def parse_nt_form(text):
    """
    Parse a copied NTwallet form robustly.

    Accepted examples:
      ➥ Deal Type: USDT
      ➥ Buyer : @piyush_ff
      ➥ Seller : @ChainNvr
      ➥ Item : usdt
      ➥ Amount : 50 USDT
      ➥ Terms : no

    The header and "Escrowed by" line are optional.
    """
    text = normalize_bold(text or "").replace("\r\n", "\n").replace("\r", "\n")

    def get_field(label):
        # Match the complete line, allowing any common bullet/prefix.
        pattern = rf"(?im)^[ \t]*(?:➥|➤|•|·|▪|▫|●|○|‣|-)?[ \t]*{label}[ \t]*:[ \t]*(.*?)[ \t]*$"
        m = re.search(pattern, text)
        return m.group(1).strip() if m else ""

    currency = get_field(r"Deal[ \t]*Type").upper()
    buyer = get_field(r"Buyer")
    seller = get_field(r"Seller")
    item = get_field(r"Item")
    amount_raw = get_field(r"Amount")
    terms = get_field(r"Terms")

    if currency not in SUPPORTED_CURRENCIES:
        return None, "Deal Type missing/invalid. Use TON, USDT or INR."

    amount = extract_amount(amount_raw)
    if amount <= 0:
        return None, "Amount missing/invalid."

    if not buyer:
        return None, "Buyer missing."
    if not seller:
        return None, "Seller missing."
    if not item:
        return None, "Item missing."
    if not terms:
        return None, "Terms missing."

    # Normalize usernames. Keep Telegram @username format.
    if not buyer.startswith("@"):
        buyer = "@" + buyer
    if not seller.startswith("@"):
        seller = "@" + seller

    return {
        "currency": currency,
        "buyer": buyer,
        "seller": seller,
        "item": item,
        "amount": amount,
        "terms": terms,
    }, None



def payment_received_text(tid, deal):
    dt = datetime.fromisoformat(deal["created_at"])
    return (
        "😐 <b>Payment received !</b>\n"
        "─────────────────\n"
        f"➥ ID: <code>{esc(tid)}</code>\n"
        f"➥ Buyer: {esc(deal['buyer'])}\n"
        f"➥ Seller: {esc(deal['seller'])}\n"
        f"➥ Amount: {esc(fmt(deal['amount'], deal['currency']))}\n"
        f"➥ Fees: {deal['fee_percent']:.1f}%\n"
        f"➥ Escrower: {esc(deal['escrowed_by'])}\n"
        f"➥ Start Time: {dt.strftime('%H:%M:%S')}\n"
        f"   [ {dt.strftime('%d %B %Y')} ]\n"
        "─────────────────\n"
        f"🔒 Escrowed by {esc(deal['escrowed_by'])}\n"
        f"⭐️ Provided by {PROVIDER}"
    )


def completed_text(tid, deal):
    dt = datetime.fromisoformat(deal["completed_at"])
    return (
        "😐 <b>Escrow deal done!</b>\n"
        "─────────────────\n"
        f"➥ ID: <code>{esc(tid)}</code>\n"
        f"➥ Buyer: {esc(deal['buyer'])}\n"
        f"➥ Seller: {esc(deal['seller'])}\n"
        f"➥ Received: {esc(fmt(deal['amount'], deal['currency']))}\n"
        f"➥ Fees: {deal['fee_percent']:.1f}%\n"
        f"➥ Released: {esc(fmt(deal['released'], deal['currency']))}\n"
        f"➥ Escrower: {esc(deal['escrowed_by'])}\n"
        f"➥ End Time: {dt.strftime('%H:%M:%S')}\n"
        f"   [ {dt.strftime('%d %B %Y')} ]\n"
        "─────────────────\n"
        f"🔒 Escrowed by {esc(deal['escrowed_by'])}\n"
        f"⭐️ Provided by {PROVIDER}"
    )


def refunded_text(tid, deal):
    dt = datetime.fromisoformat(deal["completed_at"])
    return (
        "😐 <b>Escrow deal refunded!</b>\n"
        "─────────────────\n"
        f"➥ ID: <code>{esc(tid)}</code>\n"
        f"➥ Buyer: {esc(deal['buyer'])}\n"
        f"➥ Seller: {esc(deal['seller'])}\n"
        f"➥ Amount: {esc(fmt(deal['amount'], deal['currency']))}\n"
        f"➥ Fees: {deal['fee_percent']:.1f}%\n"
        f"➥ Refunded: {esc(fmt(deal['refunded'], deal['currency']))}\n"
        f"➥ Escrower: {esc(deal['escrowed_by'])}\n"
        f"➥ End Time: {dt.strftime('%H:%M:%S')}\n"
        f"   [ {dt.strftime('%d %B %Y')} ]\n"
        "─────────────────\n"
        f"🔒 Escrowed by {esc(deal['escrowed_by'])}\n"
        f"⭐️ Provided by {PROVIDER}"
    )


def nt_state_key(update: Update):
    """Separate interactive /add state by chat + user."""
    return f"{update.effective_chat.id}:{update.effective_user.id}"


def get_nt_state(context, update):
    return context.user_data.get("nt_new_deal", {}).get(nt_state_key(update))


def set_nt_state(context, update, state):
    all_states = context.user_data.setdefault("nt_new_deal", {})
    all_states[nt_state_key(update)] = state


def pop_nt_state(context, update):
    all_states = context.user_data.get("nt_new_deal", {})
    return all_states.pop(nt_state_key(update), None)


async def nt_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    remember_user(update)
    text = update.message.text.strip()
    state = get_nt_state(context, update)

    # Step 1: amount
    if state and state.get("step") == "amount":
        amount = extract_amount(text)
        if amount <= 0:
            await update.message.reply_text(
                "❌ Valid amount bhejo.\n"
                "Example: <code>50</code> or <code>50.5</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        state["amount"] = amount
        state["step"] = "form"
        set_nt_state(context, update, state)

        await update.message.reply_text(
            nt_form_text(state["currency"], amount, state["escrower"]),
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text(
            "📝 <b>Form fill karke new message me bhejo.</b>\n\n"
            "Buyer, Seller, Item aur Terms required hain.\n"
            "Example:\n\n"
            "<code>➥ Deal Type: USDT\n"
            "➥ Buyer : @piyush_ff\n"
            "➥ Seller : @ChainNvr\n"
            "➥ Item : usdt\n"
            "➥ Amount : 50 USDT\n"
            "➥ Terms : no</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Step 2: completed form — either from the /add wizard ("form", which
    # already has a currency+amount picked) or from /form's one-shot paste
    # ("form_direct", which has neither yet).
    if not state or state.get("step") not in ("form", "form_direct"):
        return

    parsed, error = parse_nt_form(text)
    if not parsed:
        await update.message.reply_text(
            "❌ <b>Form read nahi hua.</b>\n\n"
            f"Reason: {esc(error or 'Unknown error')}\n\n"
            "Isi format me complete form bhejo:\n"
            "<code>➥ Deal Type: USDT\n"
            "➥ Buyer : @piyush_ff\n"
            "➥ Seller : @ChainNvr\n"
            "➥ Item : usdt\n"
            "➥ Amount : 50 USDT\n"
            "➥ Terms : no</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # Wizard values cannot be silently changed by the copied form.
    # (Doesn't apply to /form's "form_direct" — there is no pre-picked
    # currency/amount to cross-check there, the form is the source of truth.)
    if state["step"] == "form":
        if parsed["currency"] != state["currency"]:
            await update.message.reply_text(
                f"❌ Deal Type <b>{esc(parsed['currency'])}</b> hai, "
                f"lekin wizard me <b>{esc(state['currency'])}</b> selected tha.\n"
                "Dobara /add chalao.",
                parse_mode=ParseMode.HTML,
            )
            return

        if abs(parsed["amount"] - float(state["amount"])) > 1e-9:
            await update.message.reply_text(
                f"❌ Amount <b>{esc(str(parsed['amount']))}</b> hai, "
                f"lekin wizard amount <b>{esc(str(state['amount']))}</b> tha.\n"
                "Dobara /add chalao.",
                parse_mode=ParseMode.HTML,
            )
            return

    tid = next_trade_id()
    currency = parsed["currency"]
    amount = float(parsed["amount"])
    fee_percent = DEFAULT_FEE_PERCENT
    fee_amount = amount * fee_percent / 100

    DEALS[tid] = {
        "buyer": parsed["buyer"],
        "seller": parsed["seller"],
        "detail": parsed["item"],
        "item": parsed["item"],
        "terms": parsed["terms"],
        "amount": amount,
        "release": max(0, amount - fee_amount),
        "fee_percent": fee_percent,
        "currency": currency,
        "status": "ACTIVE",
        "escrowed_by": state["escrower"],
        "created_by_id": state["creator_id"],
        "chat_id": state["chat_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "votes": {"release": [], "refund": []},
    }

    save_deal(tid)
    pop_nt_state(context, update)

    await update.message.reply_text(
        payment_received_text(tid, DEALS[tid]),
        parse_mode=ParseMode.HTML,
        reply_markup=deal_action_kb(tid),
    )



# ===========================
# /status  — HAR USER, PRIVATE + GROUP dono me kaam karega
# ===========================

async def mystatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Har user apna khud ka status dekh sakta hai — group ho ya private, koi restriction nahi."""
    remember_user(update)
    await update.message.reply_text(
        my_status_text(update),
        parse_mode=ParseMode.HTML,
        reply_markup=status_kb(),
    )


async def form_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Shortcut for admins who'd rather paste one fully-filled form than go
    through the /add currency+amount wizard step by step."""
    if not update.message:
        return

    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason:
            await update.message.reply_text(reason)
        return

    remember_user(update)
    escrower = resolve_username(update)

    set_nt_state(context, update, {
        "step": "form_direct",
        "escrower": escrower,
        "creator_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
    })

    await update.message.reply_text(
        f"<b>{FORM_TITLE}</b> :\n\n"
        "➥ Deal Type: \n"
        "➥ Buyer :\n"
        "➥ Seller : \n"
        "➥ Item : \n"
        "➥ Amount : \n"
        "➥ Terms : \n\n"
        f"🔒 Escrowed by {esc(escrower)}",
        parse_mode=ParseMode.HTML,
    )

# ===========================
# /add
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason:
            await update.message.reply_text(reason)
        return

    set_nt_state(context, update, {
        "step": "currency",
        "escrower": resolve_username(update),
        "creator_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
    })
    await update.message.reply_text(
        "🛡 <b>What type of deal ?</b>\n\n➤ Select the currency below :",
        parse_mode=ParseMode.HTML,
        reply_markup=currency_kb(),
    )
    try:
        await update.message.delete()
    except Exception:
        pass


async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Bail out of a stuck /add or /form wizard."""
    if not update.message:
        return
    state = pop_nt_state(context, update)
    if state:
        await update.message.reply_text("❌ Deal creation cancelled.")
    else:
        await update.message.reply_text("Koi in-progress deal creation nahi hai.")


# ===========================
# /hold — owner-only admin hold report
# ===========================

HOLD_ADMIN_EMOJI_ID = "5258011929993026890"


def _hold_admin_emoji():
    return pe('🛡️')


def _is_owner(user_id):
    return user_id in OWNER_IDS


async def hold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Owner-only admin hold report.

    Shows every bot admin's currently open (ACTIVE) deal amount.
    /close removes the deal from this report automatically because its
    status changes to COMPLETED/CANCELLED.
    Non-owner users get no response.
    """
    if not update.effective_user or not is_admin(update.effective_user.id):
        return

    # Only the owner can use this command, regardless of chat type.
    open_deals = [
        (tid, deal) for tid, deal in DEALS.items()
        if deal.get("status") == "ACTIVE"
    ]

    # Group ACTIVE deals by the admin/escrower who created them.
    grouped = {}
    for tid, deal in open_deals:
        admin = deal.get("escrowed_by") or deal.get("created_by") or "-"
        grouped.setdefault(admin, []).append((tid, deal))

    lines = [
        f"{_hold_admin_emoji()} <b>ADMIN HOLD</b>",
        "",
    ]

    if not grouped:
        lines.append("No active deals are currently on hold.")
    else:
        grand_total = 0.0

        for admin in sorted(grouped, key=lambda x: x.lower()):
            deals = grouped[admin]
            admin_total = sum(float(d.get("amount", 0) or 0) for _, d in deals)
            grand_total += admin_total

            lines.append(
                f"{_hold_admin_emoji()} <b>{esc(admin)}</b> — "
                f"<b>Total Hold: {fmt(admin_total, 'INR')}</b>"
            )

            for tid, deal in deals:
                amount = float(deal.get("amount", 0) or 0)
                currency = deal.get("currency", "INR")
                buyer = esc(deal.get("buyer", "-"))
                seller = esc(deal.get("seller", "-"))
                detail = esc(deal.get("detail", "-"))
                fee = float(deal.get("fee_percent", 0) or 0)
                release = float(deal.get("release", 0) or 0)

                lines.extend([
                    f"  • <code>{esc(tid)}</code> — <b>{fmt(amount, currency)}</b>",
                    f"    Buyer: {buyer}",
                    f"    Seller: {seller}",
                    f"    Fee: {fee:.2f}% — Net: {fmt(release, currency)}",
                    f"    Detail: {detail}",
                ])
            lines.append("")

        lines.append("──────────────────")
        lines.append(
            f"{_hold_admin_emoji()} <b>ALL ADMINS TOTAL HOLD: "
            f"{fmt(grand_total, 'INR')}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /close
# ===========================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason:
            await update.message.reply_text(reason)
        return

    tid = None
    mode = "release"
    args = list(context.args)

    if args and re.fullmatch(r"DL-NTWALLET-\d+", args[0], re.I):
        tid = args.pop(0).upper()
    elif update.message.reply_to_message:
        m = re.search(r"\b(DL-NTWALLET-\d+)\b", update.message.reply_to_message.text or "", re.I)
        if m:
            tid = m.group(1).upper()

    if args and args[0].lower() in ("refund", "cancel"):
        mode = "refund"

    if not tid:
        await update.message.reply_text(
            "Usage:\n<code>/close DL-NTWALLET-1</code>\n<code>/close DL-NTWALLET-1 refund</code>\n\n"
            "Ya Payment received message par reply: <code>/close</code> / <code>/close refund</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    deal = DEALS.get(tid)
    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    closer_id = update.effective_user.id
    if not is_owner(closer_id) and closer_id != deal.get("created_by_id"):
        await update.message.reply_text("❌ Tum sirf apni create ki hui deal close kar sakte ho.")
        return

    if deal.get("status") == "HOLD":
        await update.message.reply_text("⏸️ Yeh deal HOLD par hai.")
        return
    if deal.get("status") != "ACTIVE":
        await update.message.reply_text(f"❌ Yeh deal already {deal.get('status')} hai.")
        return

    votes = deal.get("votes", {})
    parties = {str(deal.get("buyer","")).lower(), str(deal.get("seller","")).lower()}
    voted = {str(x).lower() for x in votes.get(mode, [])}
    if not parties.issubset(voted):
        await update.message.reply_text(f"❌ Buyer aur Seller dono ne {mode.title()} confirm nahi kiya.")
        return

    deal["completed_at"] = datetime.now(timezone.utc).isoformat()
    deal["closed_by_id"] = closer_id
    deal["closed_by"] = resolve_username(update)

    if mode == "refund":
        deal["status"] = "REFUNDED"
        deal["refunded"] = float(deal["amount"])
        save_deal(tid)
        await update.message.reply_text(refunded_text(tid, deal), parse_mode=ParseMode.HTML)
    else:
        deal["status"] = "COMPLETED"
        deal["released"] = float(deal.get("release", 0))
        save_deal(tid)
        await update.message.reply_text(completed_text(tid, deal), parse_mode=ParseMode.HTML)
        amount = fmt(deal["amount"], deal["currency"])
        await update.message.reply_text(
            f"😐 {esc(deal['buyer'])} and {esc(deal['seller'])} please copy and paste both vouches!\n\n"
            f"<code>Vouch {BRAND} for {esc(amount)} safe Escrow deal</code>\n\n"
            f"<code>Vouch {esc(deal['escrowed_by'])} for {esc(amount)} M'm deal</code>",
            parse_mode=ParseMode.HTML,
        )

    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# /alldeals, /leaderboard, /deal — admin only, private chat only, silent skip warna
# ===========================

async def alldeals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Purana '/status' — ab admin ke liye saari deals ki poori list, private-only."""
    if not admin_only_allowed(update):
        return

    if not DEALS:
        await update.message.reply_text("📭 Koi deal record nahi hai.")
        return

    lines = [f"📊 <b>Total Deals:</b> {len(DEALS)}\n"]
    for tid, d in DEALS.items():
        lines.append(
            f"<code>{esc(tid)}</code> — {d['status']} — "
            f"{esc(d.get('buyer','-'))} ↔ {esc(d.get('seller','-'))} — "
            f"{fmt(d.get('amount',0), d.get('currency','INR'))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    today_board = build_leaderboard(today_only=True)
    all_board = build_leaderboard(today_only=False)

    def top_line(board, by):
        if not board:
            return "  koi data nahi"
        top_user, status = max(board.items(), key=lambda kv: kv[1][by])
        return f"  {esc(top_user)} — {status['deals']} deals, ₹{status['volume']:,.2f}"

    msg = (
        f"{pe('🏆')} <b>Leaderboard</b>\n"
        "──────────────────\n"
        f"<b>📅 Today</b>\n"
        f"🔥 Top Dealer (most deals):\n{top_line(today_board, 'deals')}\n"
        f"💰 Top Earner (most volume):\n{top_line(today_board, 'volume')}\n\n"
        f"<b>♾ All-Time</b>\n"
        f"🔥 Top Dealer (most deals):\n{top_line(all_board, 'deals')}\n"
        f"💰 Top Earner (most volume):\n{top_line(all_board, 'volume')}"
    )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def deal_lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/deal DL-NTWALLET-5 -> admin kisi bhi deal ki full detail (escrowed_by samet) dekh sakta hai."""
    if not admin_only_allowed(update):
        return

    if not context.args:
        await update.message.reply_text("Usage: <code>/deal DL-NTWALLET-5</code>", parse_mode=ParseMode.HTML)
        return

    tid = context.args[0].upper()
    deal = DEALS.get(tid)
    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    await update.message.reply_text(deal_detail_text(tid, deal))


# ===========================
# Bot-admin management — sirf OWNER (.env ADMIN_IDS) add/remove kar sakta hai
# ===========================

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    target_user = None

    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id

    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])

    else:
        await update.message.reply_text(
            "Usage: kisi user ke message pe reply karke /addadmin bhejo, "
            "ya <code>/addadmin &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.add(target_id)

    admin_data = {
        "added_by": update.effective_user.id,
    }

    # Reply se add karne par user's real Telegram details save hongi
    if target_user:
        admin_data.update({
            "username": target_user.username,
            "first_name": target_user.first_name,
            "last_name": target_user.last_name,
        })

    if admins_coll is not None:
        admins_coll.update_one(
            {"_id": target_id},
            {"$set": admin_data},
            upsert=True,
        )

    await update.message.reply_text(
        f"✅ <code>{target_id}</code> ab bot admin hai.",
        parse_mode=ParseMode.HTML,
    )


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text(
            "Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML
        )
        return

    if target_id in OWNER_IDS:
        await update.message.reply_text("❌ Owner ko remove nahi kar sakte.")
        return

    BOT_ADMINS.discard(target_id)
    if admins_coll is not None:
        admins_coll.delete_one({"_id": target_id})
    await update.message.reply_text(f"✅ <code>{target_id}</code> ab admin nahi raha.", parse_mode=ParseMode.HTML)


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    lines = [f"{pe('👑')} <b>Owners</b>"]

    # ==========================
    # OWNERS
    # ==========================
    if not OWNER_IDS:
        lines.append("  (koi owner set nahi hai)")
    else:
        for uid in sorted(OWNER_IDS):
            lines.append(
                f'  • <a href="tg://user?id={uid}">Owner</a> '
                f'<code>({uid})</code>'
            )

    # Extra admins
    extra_admins = BOT_ADMINS - OWNER_IDS

    lines.append(f"\n{pe('🛡')} <b>Bot Admins</b>")

    if not extra_admins:
        lines.append("  (koi extra admin nahi hai)")
    else:
        for uid in sorted(extra_admins):

            # Default values
            username = None
            first_name = None
            last_name = None

            # ==========================
            # 1. MongoDB se saved details
            # ==========================
            if admins_coll is not None:
                admin_doc = admins_coll.find_one(
                    {"_id": uid}
                )

                if admin_doc:
                    username = admin_doc.get("username")
                    first_name = admin_doc.get("first_name")
                    last_name = admin_doc.get("last_name")

            # ==========================
            # 2. Alias fallback
            # ==========================
            if not username and uid in ADMIN_ALIASES:
                username = ADMIN_ALIASES[uid]

            # ==========================
            # 3. Display name banao
            # ==========================
            display_name = ""

            if first_name:
                display_name = first_name

                if last_name:
                    display_name += f" {last_name}"

            elif username:
                display_name = username.replace("_", " ").title()

            else:
                display_name = "Admin"

            # ==========================
            # Clickable display
            # ==========================

            if username:
                # Username hai -> clickable public Telegram link
                lines.append(
                    f'  • <a href="https://t.me/{esc(username)}">'
                    f'{esc(display_name)}</a> '
                    f'<code>({uid})</code>'
                )

            else:
                # Username nahi hai -> ID based clickable mention
                lines.append(
                    f'  • <a href="tg://user?id={uid}">'
                    f'{esc(display_name)}</a> '
                    f'<code>({uid})</code>'
                )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )

# ===========================
# /help — admin/owner ko sab commands, normal user ko sirf user commands
# ===========================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        f"{pe('📖')} <b>Commands</b>",
        "──────────────────",
        "<b>👤 User Commands</b>",
        "/start — Dashboard kholo (private chat)",
        "/stats — Apna deal status dekho (private ya group, kahin bhi)",
        "/help — Ye list dikhata hai",
    ]

    if is_admin(uid):
        lines += [
            "",
            "<b>🛡 Admin Commands</b> (private chat me hi kaam karenge)",
            "/add — New NTwallet interactive escrow deal (guided steps)",
            "/form — Paste one fully-filled deal form directly",
            "/cancel — Cancel an in-progress /add or /form",
            "/close — Dual-confirmed deal release/refund",
            "/alldeals — Saari deals ki poori list",
            "/leaderboard — Today + All-time top dealer/earner",
            "/deal &lt;DL-NTWALLET-N&gt; — Kisi bhi deal ki full detail dekho",
            "/admins — Bot admins ki list dekho",
            "/broadcast &lt;message&gt; — Private subscribers ko broadcast",
        ]

    if is_owner(uid):
        lines += [
            "",
            "<b>👑 Owner Commands</b>",
            "/addadmin — Reply karke (ya ID de ke) naya bot admin banao",
            "/removeadmin — Reply karke (ya ID de ke) admin hatao",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ===========================
# Keep-alive server (Render port check ke liye)
# ===========================

def start_dummy_server():
    port = int(os.getenv("PORT", "10000"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NTescrowbot is running")

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"✅ Dummy HTTP server listening on port {port}")


# ===========================
# Main
# ===========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError("NTESCROW_BOT_TOKEN missing in .env")
    start_dummy_server()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("stats", mystatus_cmd))
    app.add_handler(CommandHandler("add", add))
    app.add_handler(CommandHandler("close", close))
    app.add_handler(CommandHandler("hold", hold_cmd))
    app.add_handler(CommandHandler("form", form_cmd))
    app.add_handler(CommandHandler("cancel", cancel_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("alldeals", alldeals_cmd))
    app.add_handler(CommandHandler("leaderboard", leaderboard_cmd))
    app.add_handler(CommandHandler("deal", deal_lookup_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, nt_text_handler))

    print("✅ NTescrowbot Running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
