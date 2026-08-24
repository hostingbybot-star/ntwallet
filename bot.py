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
# Config
# ===========================

BOT_TOKEN = os.getenv("NTESCROW_BOT_TOKEN")
BRAND = "@Tr4derz"
PROVIDER = "@Tr4derz"

ESCROW_OWNER = os.getenv("ESCROW_OWNER_HANDLE", "@tr4degc")

MONGO_URI = os.getenv("MONGO_URI")

OWNER_IDS = set(
    int(x)
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
)

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


# ===========================
# Bot admins
# ===========================

BOT_ADMINS = set(OWNER_IDS)

if admins_coll is not None:
    for doc in admins_coll.find({}):
        BOT_ADMINS.add(doc["_id"])

    print(f"✅ [NTescrowbot] {len(BOT_ADMINS)} bot admin(s) load hue")


def save_deal(tid):
    if coll is not None:
        coll.update_one(
            {"_id": tid},
            {"$set": dict(DEALS[tid])},
            upsert=True,
        )


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in BOT_ADMINS or is_owner(uid)


def admin_only_allowed(update: Update):
    if update.effective_chat.type != "private":
        return False

    return is_admin(update.effective_user.id)


async def add_close_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    return is_admin(update.effective_user.id), None


# ===========================
# Telegram Pin / Unpin Helpers
# ===========================

async def unpin_message(bot, chat_id, message_id):
    """
    Safely unpin a specific message.
    Agar message pinned nahi hai ya permission issue hai,
    bot crash nahi karega.
    """
    if not message_id:
        return False

    try:
        await bot.unpin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
        )
        return True
    except Exception as e:
        print(
            f"⚠️ Could not unpin message "
            f"{message_id} in {chat_id}: {e}"
        )
        return False


async def pin_message(bot, chat_id, message_id):
    """
    Safely pin a specific message.
    """
    if not message_id:
        return False

    try:
        await bot.pin_chat_message(
            chat_id=chat_id,
            message_id=message_id,
            disable_notification=True,
        )
        return True
    except Exception as e:
        print(
            f"⚠️ Could not pin message "
            f"{message_id} in {chat_id}: {e}"
        )
        return False


async def replace_pinned_message(
    bot,
    chat_id,
    old_message_id,
    new_message_id,
):
    """
    Old message unpin -> new message pin.
    """
    if old_message_id:
        await unpin_message(
            bot,
            chat_id,
            old_message_id,
        )

    if new_message_id:
        return await pin_message(
            bot,
            chat_id,
            new_message_id,
        )

    return False


# ===========================
# Sequential Trade ID
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

    tid = f"DL-TR4DE-{seq}"

    while tid in DEALS:
        seq += 1
        tid = f"DL-TR4DE-{seq}"

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
    match = re.search(
        r"[\d,]+(?:\.\d+)?",
        text or "",
    )

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

    if update.effective_user.username:
        return "@" + update.effective_user.username

    return update.effective_user.first_name


# ===========================
# Bold unicode helpers
# ===========================

_UP = ord("𝗔") - ord("A")
_LOW = ord("𝗮") - ord("a")
_DIG = ord("𝟬") - ord("0")


def normalize_bold(text):
    out = []

    for ch in text:
        code = ord(ch)

        if ord("𝗔") <= code <= ord("𝗭"):
            out.append(chr(code - _UP))

        elif ord("𝗮") <= code <= ord("𝘇"):
            out.append(chr(code - _LOW))

        elif ord("𝟬") <= code <= ord("𝟵"):
            out.append(chr(code - _DIG))

        else:
            out.append(ch)

    return "".join(out)


# ===========================
# Premium Emoji IDs
# ===========================

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
    emoji_id = PE.get(emoji)

    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'

    return emoji


# ===========================
# Charges
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
# Dashboard
# ===========================

def main_menu_kb():
    rows = [
        [
            InlineKeyboardButton(
                "✦ My status",
                callback_data="menu:my_status",
            )
        ],
        [
            InlineKeyboardButton(
                "★ My Deals Info",
                callback_data="menu:my_deals",
            )
        ],
        [
            InlineKeyboardButton(
                "➤ My Pending Deals",
                callback_data="menu:pending",
            )
        ],
        [
            InlineKeyboardButton(
                "✓ Escrow Global status",
                callback_data="menu:global",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def status_kb():
    rows = [
        [
            InlineKeyboardButton(
                "★ My Deals Info",
                callback_data="menu:my_deals",
            )
        ],
        [
            InlineKeyboardButton(
                "➤ My Pending Deals",
                callback_data="menu:pending",
            )
        ],
        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data="refresh:my_status",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def back_refresh_kb(refresh_target):
    rows = [
        [
            InlineKeyboardButton(
                "🔄 Refresh",
                callback_data=f"refresh:{refresh_target}",
            )
        ],
        [
            InlineKeyboardButton(
                "➤ Back",
                callback_data="menu:back",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def welcome_text(first_name):
    return (
        f"{pe('⭐️')} <b>Welcome {esc(first_name)}!</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"{pe('❤️')} <b>Escrow Bot for {esc(BRAND)}</b>\n"
        f"{pe('💬')} <b>Provided by {esc(PROVIDER)}</b>\n\n"
        f"{pe('🍑')} <b>This is Your Personal Dashboard:</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍\n"
        f"<b>Select the option below {pe('⚡️')}</b>\n"
        "╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍╍"
    )


def global_status_text():
    completed = [
        d for d in DEALS.values()
        if d.get("status") == "COMPLETED"
    ]

    totals = {
        "TON": 0.0,
        "USDT": 0.0,
        "INR": 0.0,
    }

    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    lines = [
        f"{pe('🌐')} <b>Escrow Global Statistics</b>",
        "──────────────────",
        f"{pe('🔥')} <b>Total Deals: {len(completed)}</b>",
        "",
        f"{pe('⚡️')} <b>Total Volume:</b>",
        f"  {pe('🪙')} <b>- {totals['TON']:g} TON</b>",
        f"  {pe('💰')} <b>- {totals['USDT']:g} USDT</b>",
        f"  {pe('🤑')} <b>- {totals['INR']:g} ₹</b>",
        "──────────────────",
        f"{pe('📱')} <b>Escrow Bot for {esc(BRAND)}</b>",
        f"{pe('💤')} <b>Provided by {esc(PROVIDER)}</b>",
    ]

    return "\n".join(lines)


# ===========================
# Leaderboard
# ===========================

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

        entry = board.setdefault(
            user,
            {
                "deals": 0,
                "volume": 0.0,
            },
        )

        entry["deals"] += 1
        entry["volume"] += d.get("amount", 0.0)

    return board


def get_rank(username, board, by="deals"):
    ranked = sorted(
        board.items(),
        key=lambda kv: kv[1][by],
        reverse=True,
    )

    for i, (user, _) in enumerate(ranked, start=1):
        if user == username:
            return i

    return len(ranked) + 1


def my_status_text(update: Update):
    username = resolve_username(update)
    first_name = update.effective_user.first_name

    mine = [
        d for d in DEALS.values()
        if d.get("escrowed_by") == username
    ]

    completed = [
        d for d in mine
        if d.get("status") == "COMPLETED"
    ]

    active = [
        d for d in mine
        if d.get("status") == "ACTIVE"
    ]

    totals = {
        "TON": 0.0,
        "USDT": 0.0,
        "INR": 0.0,
    }

    for d in completed:
        cur = d.get("currency", "INR")
        totals[cur] = totals.get(cur, 0.0) + d.get("amount", 0.0)

    board = build_leaderboard(today_only=False)

    rank = get_rank(
        username,
        board,
        by="deals",
    )

    return (
        f"{pe('📈')} <b>{esc(first_name)} Deal status!</b>\n"
        "──────────────────\n"
        f"{pe('🚀')} <b>Rank ➤ #{rank}</b>\n\n"
        f"{pe('🔥')} <b>Active deals ➤ {len(active)}</b>\n\n"
        f"{pe('✅')} <b>Total Escrow's ➤ {len(completed)}</b>\n\n"
        f"{pe('⚡')} <b>Total Volume:</b>\n"
        f"  {pe('🪙')} <b>➤ {totals['TON']:g} TON</b>\n"
        f"  {pe('💰')} <b>➤ {totals['USDT']:g} USDT</b>\n"
        f"  {pe('🤑')} <b>➤ {totals['INR']:g} ₹</b>\n"
        "──────────────────\n"
        f"{pe('📱')} <b>Escrow Bot for {esc(BRAND)}</b>\n"
        f"{pe('💤')} <b>Provided by {esc(PROVIDER)}!</b>"
    )


# ===========================
# My Deals
# ===========================

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
        f"<b>Your Deal-{esc(tid)} Info!</b>",
        "──────────────────",
        f"➥ <b>status:</b> {deal_status_display(deal.get('status', '-'))}",
        f"➥ <b>Buyer:</b> {esc(deal.get('buyer', '-'))}",
        f"➥ <b>Seller:</b> {esc(deal.get('seller', '-'))}",
        f"➥ <b>Amount:</b> {esc(fmt(deal.get('amount', 0), deal.get('currency', 'INR')))}",
        f"➥ <b>Fees:</b> {deal.get('fee_percent', 0):.1f}%",
        f"➥ <b>Escrower:</b> {esc(deal.get('escrowed_by', '-'))}",
    ]

    if deal.get("created_at"):
        dt = datetime.fromisoformat(
            deal["created_at"]
        )

        lines.append(
            f"➥ <b>Start Time:</b> {dt.strftime('%H:%M:%S')}"
        )

        lines.append(
            f"<b>[ {dt.strftime('%d %B %Y')} ]</b>"
        )

    if deal.get("completed_at"):
        dt2 = datetime.fromisoformat(
            deal["completed_at"]
        )

        lines.append(
            f"➥ <b>End Time:</b> {dt2.strftime('%H:%M:%S')}"
        )

        lines.append(
            f"<b>[ {dt2.strftime('%d %B %Y')} ]</b>"
        )

    lines += [
        "──────────────────",
        f"{pe('📱')} <b>Escrow Bot for {esc(BRAND)}</b>",
        f"{pe('💤')} <b>Provided by {esc(PROVIDER)}</b>",
    ]

    return "\n".join(lines)


def my_deals_header_text(update: Update):
    first_name = update.effective_user.first_name

    return (
        f"♡ <b>{esc(first_name)} All deals info!</b>\n"
        "──────────────────\n"
        "<b>Select the deal below for info:</b>\n"
        "──────────────────"
    )


def my_deals_ids(update: Update):
    username = resolve_username(update)

    ids = [
        tid
        for tid, d in DEALS.items()
        if d.get("escrowed_by") == username
    ]

    return list(reversed(ids))


def my_deals_kb(update: Update, page=0):
    ids = my_deals_ids(update)

    start = page * PAGE_SIZE
    chunk = ids[start:start + PAGE_SIZE]

    rows = [
        [
            InlineKeyboardButton(
                tid,
                callback_data=f"dealview:{tid}:{page}",
            )
        ]
        for tid in chunk
    ]

    nav = []

    if page > 0:
        nav.append(
            InlineKeyboardButton(
                "◀ Prev",
                callback_data=f"dealspage:{page-1}",
            )
        )

    if start + PAGE_SIZE < len(ids):
        nav.append(
            InlineKeyboardButton(
                "Next ▶",
                callback_data=f"dealspage:{page+1}",
            )
        )

    if nav:
        rows.append(nav)

    rows.append(
        [
            InlineKeyboardButton(
                "➤ Back",
                callback_data="menu:back",
            )
        ]
    )

    return InlineKeyboardMarkup(rows), len(ids)


def deal_view_kb(page):
    rows = [
        [
            InlineKeyboardButton(
                "◀ Back to My Deals",
                callback_data=f"dealspage:{page}",
            )
        ],
        [
            InlineKeyboardButton(
                "➤ Main Menu",
                callback_data="menu:back",
            )
        ],
    ]

    return InlineKeyboardMarkup(rows)


def pending_deals_text(update: Update):
    username = resolve_username(update)

    pending = [
        (tid, d)
        for tid, d in DEALS.items()
        if (
            d.get("escrowed_by") == username
            and d.get("status") == "ACTIVE"
        )
    ]

    if not pending:
        return (
            f"{pe('➤')} <b>Koi pending deal nahi hai.</b>"
        )

    lines = [
        f"{pe('➤')} <b>My Pending Deals</b>",
        "──────────────────",
    ]

    for tid, d in pending:
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"<b>{esc(d.get('buyer','-'))}</b> ↔ "
            f"<b>{esc(d.get('seller','-'))}</b> — "
            f"<b>{fmt(d.get('amount',0), d.get('currency','INR'))}</b>"
        )

    return "\n".join(lines)


# ===========================
# Broadcast
# ===========================

def remember_user(update: Update):
    if users_coll is None:
        return

    if not update.effective_user:
        return

    u = update.effective_user

    users_coll.update_one(
        {"_id": u.id},
        {
            "$set": {
                "username": u.username,
                "first_name": u.first_name,
                "last_seen": datetime.now(timezone.utc).isoformat(),
            }
        },
        upsert=True,
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.effective_chat.type != "private"
        or not is_admin(update.effective_user.id)
    ):
        return

    if not context.args:
        await update.message.reply_text(
            "<b>Usage: /broadcast &lt;message&gt;</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if users_coll is None:
        await update.message.reply_text(
            "<b>❌ MongoDB required for /broadcast.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    message = update.message.text.partition(" ")[2].strip()

    if not message:
        await update.message.reply_text(
            "<b>Usage: /broadcast &lt;message&gt;</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    sent = 0
    failed = 0

    for doc in users_coll.find({}, {"_id": 1}):
        try:
            await context.bot.send_message(
                chat_id=doc["_id"],
                text=message,
            )
            sent += 1

        except Exception:
            failed += 1

    await update.message.reply_text(
        f"<b>📢 Broadcast finished.</b>\n"
        f"<b>Sent:</b> {sent}\n"
        f"<b>Failed:</b> {failed}",
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /start
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)

    if update.effective_chat.type != "private":
        return

    await update.message.reply_text(
        welcome_text(update.effective_user.first_name),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


# ===========================
# Callback Router
# ===========================

async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    data = query.data

    # -----------------------
    # Currency
    # -----------------------

    if data.startswith("newdeal:currency:"):
        currency = data.rsplit(":", 1)[1].upper()

        state = get_nt_state(
            context,
            update,
        )

        if (
            not state
            or state.get("step") != "currency"
            or currency not in SUPPORTED_CURRENCIES
        ):
            await query.answer(
                "Start again with /form",
                show_alert=True,
            )
            return

        state["currency"] = currency
        state["step"] = "amount"

        set_nt_state(
            context,
            update,
            state,
        )

        await query.answer()

        await query.edit_message_text(
            f"➤ <b>Tell me deal amount in {esc(currency)}</b>\n"
            "<b>ex - 1, 100, 1000</b>",
            parse_mode=ParseMode.HTML,
        )

        return

    # -----------------------
    # Deal action
    # -----------------------

    if data.startswith("dealaction:"):
        try:
            _, tid, action = data.split(":", 2)

        except ValueError:
            await query.answer()
            return

        deal = DEALS.get(tid)

        if not deal or deal.get("status") != "ACTIVE":
            await query.answer(
                "Deal unavailable.",
                show_alert=True,
            )
            return

        username = resolve_username(update).lower()

        parties = {
            str(deal.get("buyer", "")).lower(),
            str(deal.get("seller", "")).lower(),
        }

        if username not in parties:
            await query.answer(
                "Only Buyer or Seller can confirm.",
                show_alert=True,
            )
            return

        votes = deal.setdefault(
            "votes",
            {
                "release": [],
                "refund": [],
            },
        )

        votes.setdefault("release", [])
        votes.setdefault("refund", [])

        opposite = (
            "refund"
            if action == "release"
            else "release"
        )

        votes[opposite] = [
            x
            for x in votes[opposite]
            if x.lower() != username
        ]

        voter_handle = resolve_username(update)

        if username not in [
            x.lower()
            for x in votes[action]
        ]:
            votes[action].append(voter_handle)

        save_deal(tid)

        await query.answer(
            f"✅ Your vote recorded: {action.title()}"
        )

        await context.bot.send_message(
            chat_id=deal["chat_id"],
            text=(
                f"<b>{esc(voter_handle)} agreed for "
                f"{esc(action)}</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

        if parties.issubset(
            {
                x.lower()
                for x in votes[action]
            }
        ):
            verb = (
                "releasing"
                if action == "release"
                else "refunding"
            )

            label = (
                "Release"
                if action == "release"
                else "Refund"
            )

            await context.bot.send_message(
                chat_id=deal["chat_id"],
                text=(
                    f"😐 <b>Buyer {esc(deal['buyer'])} "
                    f"& Seller {esc(deal['seller'])} "
                    f"agreed to {label}.</b>\n\n"
                    f"<b>Dear {esc(deal['escrowed_by'])}, "
                    f"please {esc(action)} the funds "
                    f"according to deal.</b>\n\n"
                    f"<b>❗️Verify both usernames before "
                    f"{esc(verb)}.</b>"
                ),
                parse_mode=ParseMode.HTML,
            )

            try:
                await query.edit_message_reply_markup(
                    reply_markup=None
                )

            except Exception:
                pass

        return

    # -----------------------
    # Dashboard navigation
    # -----------------------

    await query.answer()

    if data == "menu:back":
        if update.effective_chat.type == "private":
            await query.edit_message_text(
                welcome_text(
                    update.effective_user.first_name
                ),
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

    if (
        data == "menu:my_deals"
        or data.startswith("dealspage:")
    ):
        page = 0

        if data.startswith("dealspage:"):
            page = int(
                data.split(":", 1)[1]
            )

        kb, total = my_deals_kb(
            update,
            page,
        )

        if total == 0:
            text = (
                my_deals_header_text(update)
                + "\n\n📭 <b>Koi deal nahi mili.</b>"
            )

        else:
            text = my_deals_header_text(update)

        await query.edit_message_text(
            text,
            parse_mode=ParseMode.HTML,
            reply_markup=kb,
        )

        return

    if data.startswith("dealview:"):
        _, tid, page = data.split(":", 2)

        deal = DEALS.get(tid)

        if not deal:
            await query.edit_message_text(
                "<b>❌ Deal not found.</b>",
                parse_mode=ParseMode.HTML,
                reply_markup=deal_view_kb(int(page)),
            )
            return

        await query.edit_message_text(
            deal_detail_text(tid, deal),
            parse_mode=ParseMode.HTML,
            reply_markup=deal_view_kb(int(page)),
        )

        return

    target = None

    if data in (
        "menu:my_status",
        "refresh:my_status",
    ):
        target = "my_status"
        text = my_status_text(update)

    elif data in (
        "menu:pending",
        "refresh:pending",
    ):
        target = "pending"
        text = pending_deals_text(update)

    elif data in (
        "menu:global",
        "refresh:global",
    ):
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
# NT Wallet Form
# ===========================

SUPPORTED_CURRENCIES = (
    "TON",
    "USDT",
    "INR",
)

DEFAULT_FEE_PERCENT = float(
    os.getenv(
        "DEAL_FEE_PERCENT",
        "1.0",
    )
)

FORM_TITLE = "#NFTTraders Escrow"


def currency_kb():
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "TON",
                    callback_data="newdeal:currency:TON",
                ),
                InlineKeyboardButton(
                    "USDT",
                    callback_data="newdeal:currency:USDT",
                ),
                InlineKeyboardButton(
                    "INR",
                    callback_data="newdeal:currency:INR",
                ),
            ]
        ]
    )


def deal_action_kb(tid):
    return InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "Release",
                    callback_data=f"dealaction:{tid}:release",
                ),
                InlineKeyboardButton(
                    "Refund",
                    callback_data=f"dealaction:{tid}:refund",
                ),
            ]
        ]
    )


# ===========================
# FORM
# ===========================

def nt_form_text(currency, amount):
    """
    Static labels bold.
    Actual fields blank/normal so user ke entered values
    baad me bold nahi honge.
    """

    return (
        f"<b>{esc(FORM_TITLE)} :</b>\n\n"
        f"➥ <b>Deal Type:</b> {esc(currency)}\n"
        "➥ <b>Buyer:</b>\n"
        "➥ <b>Seller:</b>\n"
        "➥ <b>Item:</b>\n"
        f"➥ <b>Amount:</b> {esc(fmt(amount, currency))}\n"
        "➥ <b>Holding:</b>\n"
        "➥ <b>Terms:</b>\n\n"
        f"<b>🔒 Escrowed by {esc(ESCROW_OWNER)}</b>"
    )


def parse_nt_form(text):
    """
    Filled form parser.

    User-entered values normal reh sakte hain ya bold unicode
    me aa sakte hain. normalize_bold() unhe normal text me convert
    kar deta hai.
    """

    text = normalize_bold(
        text or ""
    ).replace(
        "\r\n",
        "\n",
    ).replace(
        "\r",
        "\n",
    )

    def get_field(label):
        pattern = (
            rf"(?im)^[ \t]*"
            rf"(?:➥|➤|•|·|▪|▫|●|○|‣|-)?"
            rf"[ \t]*{label}"
            rf"[ \t]*:[ \t]*(.*?)[ \t]*$"
        )

        m = re.search(
            pattern,
            text,
        )

        return (
            m.group(1).strip()
            if m
            else ""
        )

    currency = get_field(
        r"Deal[ \t]*Type"
    ).upper()

    buyer = get_field(r"Buyer")
    seller = get_field(r"Seller")
    item = get_field(r"Item")
    amount_raw = get_field(r"Amount")
    holding = get_field(r"Holding")
    terms = get_field(r"Terms")

    if currency not in SUPPORTED_CURRENCIES:
        return (
            None,
            "Deal Type missing/invalid. Use TON, USDT or INR.",
        )

    amount = extract_amount(amount_raw)

    if amount <= 0:
        return (
            None,
            "Amount missing/invalid.",
        )

    if not buyer:
        return None, "Buyer missing."

    if not seller:
        return None, "Seller missing."

    if not item:
        return None, "Item missing."

    if not terms:
        return None, "Terms missing."

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
        "holding": holding,
        "terms": terms,
    }, None


# ===========================
# ACTIVE DEAL MESSAGE
# ===========================

def payment_received_text(tid, deal):
    dt = datetime.fromisoformat(
        deal["created_at"]
    )

    return (
        "😐 <b>Payment received!</b>\n"
        "─────────────────\n"
        f"➥ <b>ID:</b> <code>{esc(tid)}</code>\n"
        f"➥ <b>Buyer:</b> {esc(deal['buyer'])}\n"
        f"➥ <b>Seller:</b> {esc(deal['seller'])}\n"
        f"➥ <b>Amount:</b> {esc(fmt(deal['amount'], deal['currency']))}\n"
        f"➥ <b>Fees:</b> {deal['fee_percent']:.1f}%\n"
        f"➥ <b>Escrower:</b> {esc(deal['escrowed_by'])}\n"
        f"➥ <b>Start Time:</b> {dt.strftime('%H:%M:%S')}\n"
        f"   <b>[ {dt.strftime('%d %B %Y')} ]</b>\n"
        "─────────────────\n"
        f"<b>🔒 Escrowed by {esc(ESCROW_OWNER)}</b>\n"
        f"<b>⭐️ Provided by {esc(PROVIDER)}</b>"
    )


def confirm_prompt_text(deal):
    return (
        f"✅ <b>{esc(deal['buyer'])} and "
        f"{esc(deal['seller'])} confirm the button "
        f"below after deal completion and discussion!</b>"
    )


# ===========================
# COMPLETED MESSAGE
# ===========================

def completed_text(tid, deal):
    dt = datetime.fromisoformat(
        deal["completed_at"]
    )

    return (
        "😐 <b>Escrow deal done!</b>\n"
        "─────────────────\n"
        f"➥ <b>ID:</b> <code>{esc(tid)}</code>\n"
        f"➥ <b>Buyer:</b> {esc(deal['buyer'])}\n"
        f"➥ <b>Seller:</b> {esc(deal['seller'])}\n"
        f"➥ <b>Received:</b> {esc(fmt(deal['amount'], deal['currency']))}\n"
        f"➥ <b>Fees:</b> {deal['fee_percent']:.1f}%\n"
        f"➥ <b>Released:</b> {esc(fmt(deal['released'], deal['currency']))}\n"
        f"➥ <b>Escrower:</b> {esc(deal['escrowed_by'])}\n"
        f"➥ <b>End Time:</b> {dt.strftime('%H:%M:%S')}\n"
        f"   <b>[ {dt.strftime('%d %B %Y')} ]</b>\n"
        "─────────────────\n"
        f"<b>🔒 Escrowed by {esc(ESCROW_OWNER)}</b>\n"
        f"<b>⭐️ Provided by {esc(PROVIDER)}</b>"
    )


def refunded_text(tid, deal):
    dt = datetime.fromisoformat(
        deal["completed_at"]
    )

    return (
        "😐 <b>Escrow deal refunded!</b>\n"
        "─────────────────\n"
        f"➥ <b>ID:</b> <code>{esc(tid)}</code>\n"
        f"➥ <b>Buyer:</b> {esc(deal['buyer'])}\n"
        f"➥ <b>Seller:</b> {esc(deal['seller'])}\n"
        f"➥ <b>Amount:</b> {esc(fmt(deal['amount'], deal['currency']))}\n"
        f"➥ <b>Fees:</b> {deal['fee_percent']:.1f}%\n"
        f"➥ <b>Refunded:</b> {esc(fmt(deal['refunded'], deal['currency']))}\n"
        f"➥ <b>Escrower:</b> {esc(deal['escrowed_by'])}\n"
        f"➥ <b>End Time:</b> {dt.strftime('%H:%M:%S')}\n"
        f"   <b>[ {dt.strftime('%d %B %Y')} ]</b>\n"
        "─────────────────\n"
        f"<b>🔒 Escrowed by {esc(ESCROW_OWNER)}</b>\n"
        f"<b>⭐️ Provided by {esc(PROVIDER)}</b>"
    )


# ===========================
# State
# ===========================

def nt_state_key(update: Update):
    return (
        f"{update.effective_chat.id}:"
        f"{update.effective_user.id}"
    )


def get_nt_state(context, update):
    return context.user_data.get(
        "nt_new_deal",
        {},
    ).get(
        nt_state_key(update)
    )


def set_nt_state(context, update, state):
    all_states = context.user_data.setdefault(
        "nt_new_deal",
        {},
    )

    all_states[
        nt_state_key(update)
    ] = state


def pop_nt_state(context, update):
    all_states = context.user_data.get(
        "nt_new_deal",
        {},
    )

    return all_states.pop(
        nt_state_key(update),
        None,
    )


# ===========================
# Text Handler
# ===========================

async def nt_text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    remember_user(update)

    text = update.message.text.strip()

    state = get_nt_state(
        context,
        update,
    )

    if (
        not state
        or state.get("step") != "amount"
    ):
        return

    amount = extract_amount(text)

    if amount <= 0:
        await update.message.reply_text(
            "<b>❌ Valid amount bhejo.</b>\n"
            "<b>Example: 50 or 50.5</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    currency = state["currency"]

    pop_nt_state(
        context,
        update,
    )

    # ONLY form message.
    # Old instruction message completely removed.
    await update.message.reply_text(
        nt_form_text(
            currency,
            amount,
        ),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /stats
# ===========================

async def mystatus_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)

    await update.message.reply_text(
        my_status_text(update),
        parse_mode=ParseMode.HTML,
        reply_markup=status_kb(),
    )


# ===========================
# /form
# ===========================

async def form_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    remember_user(update)

    set_nt_state(
        context,
        update,
        {
            "step": "currency",
            "creator_id": update.effective_user.id,
            "chat_id": update.effective_chat.id,
        },
    )

    await update.message.reply_text(
        "🛡 <b>What type of deal?</b>\n\n"
        "➤ <b>Select the currency below:</b>",
        parse_mode=ParseMode.HTML,
        reply_markup=currency_kb(),
    )


# ===========================
# /add
# ===========================

async def add(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /add
        -> form amount use karega

    /add 500
        -> custom amount use karega

    IMPORTANT:
        /add ke baad:
        1. Filled form unpin
        2. Payment Received message send
        3. Payment Received message PIN
        4. Confirmation message send
    """

    allowed, reason = await add_close_allowed(
        update,
        context,
    )

    if not allowed:
        if reason:
            await update.message.reply_text(reason)

        return

    reply = update.message.reply_to_message

    raw_text = (
        reply.text
        if reply
        else ""
    )

    if not raw_text.strip():
        await update.message.reply_text(
            "❌ <b>Filled deal-form wale message par "
            "reply karke /add bhejo.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    parsed, error = parse_nt_form(
        raw_text
    )

    if not parsed:
        await update.message.reply_text(
            f"❌ <b>Form read nahi hua.</b>\n"
            f"<b>Reason:</b> {esc(error or 'Unknown error')}",
            parse_mode=ParseMode.HTML,
        )
        return

    currency = parsed["currency"]

    amount_val = parsed["amount"]

    if context.args:
        custom_amount = extract_amount(
            context.args[0]
        )

        if custom_amount > 0:
            amount_val = custom_amount

    tid = next_trade_id()

    creator_username = resolve_username(
        update
    )

    fee_percent = DEFAULT_FEE_PERCENT

    fee_amount = (
        amount_val
        * fee_percent
        / 100
    )

    DEALS[tid] = {
        "buyer": parsed["buyer"],
        "seller": parsed["seller"],
        "detail": parsed["item"],
        "item": parsed["item"],
        "holding": parsed["holding"],
        "terms": parsed["terms"],
        "amount": amount_val,
        "release": max(
            0,
            amount_val - fee_amount,
        ),
        "fee_percent": fee_percent,
        "currency": currency,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "created_by_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "created_at": datetime.now(
            timezone.utc
        ).isoformat(),
        "votes": {
            "release": [],
            "refund": [],
        },

        # Message tracking
        "form_message_id": (
            reply.message_id
            if reply
            else None
        ),
        "payment_message_id": None,
        "confirm_message_id": None,
        "completion_message_id": None,
    }

    save_deal(tid)

    # -----------------------
    # Send ACTIVE deal message
    # -----------------------

    payment_message = await update.message.reply_text(
        payment_received_text(
            tid,
            DEALS[tid],
        ),
        parse_mode=ParseMode.HTML,
    )

    DEALS[tid]["payment_message_id"] = (
        payment_message.message_id
    )

    save_deal(tid)

    # -----------------------
    # UNPIN original form
    # -----------------------

    if reply:
        await unpin_message(
            context.bot,
            update.effective_chat.id,
            reply.message_id,
        )

    # -----------------------
    # PIN Payment Received
    # -----------------------

    await pin_message(
        context.bot,
        update.effective_chat.id,
        payment_message.message_id,
    )

    # -----------------------
    # Confirmation buttons
    # -----------------------

    confirm_message = await update.message.reply_text(
        confirm_prompt_text(
            DEALS[tid]
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=deal_action_kb(tid),
    )

    DEALS[tid]["confirm_message_id"] = (
        confirm_message.message_id
    )

    save_deal(tid)

    # -----------------------
    # Delete /add command
    # -----------------------

    try:
        await update.message.delete()

    except Exception:
        pass


# ===========================
# /cancel
# ===========================

async def cancel_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    state = pop_nt_state(
        context,
        update,
    )

    if state:
        await update.message.reply_text(
            "<b>❌ Form wizard cancelled.</b>",
            parse_mode=ParseMode.HTML,
        )

    else:
        await update.message.reply_text(
            "<b>Koi in-progress /form wizard nahi hai.</b>",
            parse_mode=ParseMode.HTML,
        )


# ===========================
# /hold
# ===========================

def _hold_admin_emoji():
    return pe("🛡️")


async def hold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.effective_user:
        return

    if not is_admin(
        update.effective_user.id
    ):
        return

    open_deals = [
        (tid, deal)
        for tid, deal in DEALS.items()
        if deal.get("status") == "ACTIVE"
    ]

    grouped = {}

    for tid, deal in open_deals:
        admin = (
            deal.get("escrowed_by")
            or deal.get("created_by")
            or "-"
        )

        grouped.setdefault(
            admin,
            [],
        ).append(
            (tid, deal)
        )

    lines = [
        f"{_hold_admin_emoji()} <b>ADMIN HOLD</b>",
        "",
    ]

    if not grouped:
        lines.append(
            "<b>No active deals are currently on hold.</b>"
        )

    else:
        grand_total = 0.0

        for admin in sorted(
            grouped,
            key=lambda x: x.lower(),
        ):
            deals = grouped[admin]

            admin_total = sum(
                float(
                    d.get("amount", 0)
                    or 0
                )
                for _, d in deals
            )

            grand_total += admin_total

            lines.append(
                f"{_hold_admin_emoji()} "
                f"<b>{esc(admin)}</b> — "
                f"<b>Total Hold: "
                f"{fmt(admin_total, 'INR')}</b>"
            )

            for tid, deal in deals:
                amount = float(
                    deal.get("amount", 0)
                    or 0
                )

                currency = deal.get(
                    "currency",
                    "INR",
                )

                buyer = esc(
                    deal.get(
                        "buyer",
                        "-",
                    )
                )

                seller = esc(
                    deal.get(
                        "seller",
                        "-",
                    )
                )

                detail = esc(
                    deal.get(
                        "detail",
                        "-",
                    )
                )

                fee = float(
                    deal.get(
                        "fee_percent",
                        0,
                    )
                    or 0
                )

                release = float(
                    deal.get(
                        "release",
                        0,
                    )
                    or 0
                )

                lines.extend(
                    [
                        f"• <code>{esc(tid)}</code> — "
                        f"<b>{fmt(amount, currency)}</b>",
                        f"<b>Buyer:</b> {buyer}",
                        f"<b>Seller:</b> {seller}",
                        f"<b>Fee:</b> {fee:.2f}% — "
                        f"<b>Net:</b> "
                        f"{fmt(release, currency)}",
                        f"<b>Detail:</b> {detail}",
                    ]
                )

            lines.append("")

        lines.append(
            "──────────────────"
        )

        lines.append(
            f"{_hold_admin_emoji()} "
            f"<b>ALL ADMINS TOTAL HOLD: "
            f"{fmt(grand_total, 'INR')}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# Finalization
# ===========================

async def finalize_deal(
    context,
    tid,
    deal,
    mode,
    closer_id=None,
    custom_amount=None,
):
    """
    ACTIVE deal -> COMPLETED / REFUNDED

    IMPORTANT:
        Old active/payment message unpin.
        New completed/refunded message pin.
    """

    if deal.get("status") != "ACTIVE":
        return False

    chat_id = deal["chat_id"]

    deal["completed_at"] = (
        datetime.now(
            timezone.utc
        ).isoformat()
    )

    if closer_id is not None:
        deal["closed_by_id"] = closer_id

    # -----------------------
    # REFUND
    # -----------------------

    if mode == "refund":
        deal["status"] = "REFUNDED"

        deal["refunded"] = (
            float(custom_amount)
            if custom_amount is not None
            else float(deal["amount"])
        )

        save_deal(tid)

        result_message = await context.bot.send_message(
            chat_id=chat_id,
            text=refunded_text(
                tid,
                deal,
            ),
            parse_mode=ParseMode.HTML,
        )

        deal["completion_message_id"] = (
            result_message.message_id
        )

        save_deal(tid)

        # Old Payment Received unpin
        await unpin_message(
            context.bot,
            chat_id,
            deal.get(
                "payment_message_id"
            ),
        )

        # New Refunded message pin
        await pin_message(
            context.bot,
            chat_id,
            result_message.message_id,
        )

    # -----------------------
    # RELEASE / COMPLETE
    # -----------------------

    else:
        deal["status"] = "COMPLETED"

        deal["released"] = (
            float(custom_amount)
            if custom_amount is not None
            else float(
                deal.get(
                    "release",
                    0,
                )
            )
        )

        save_deal(tid)

        result_message = await context.bot.send_message(
            chat_id=chat_id,
            text=completed_text(
                tid,
                deal,
            ),
            parse_mode=ParseMode.HTML,
        )

        deal["completion_message_id"] = (
            result_message.message_id
        )

        save_deal(tid)

        # -----------------------
        # Old active message unpin
        # -----------------------

        await unpin_message(
            context.bot,
            chat_id,
            deal.get(
                "payment_message_id"
            ),
        )

        # -----------------------
        # New DONE message pin
        # -----------------------

        await pin_message(
            context.bot,
            chat_id,
            result_message.message_id,
        )

        # -----------------------
        # Vouches
        # -----------------------

        amount = fmt(
            deal["amount"],
            deal["currency"],
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"<code>Vouch {esc(ESCROW_OWNER)} "
                f"for {esc(amount)} safe Escrow deal</code>"
            ),
            parse_mode=ParseMode.HTML,
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"<code>Vouch {esc(deal['escrowed_by'])} "
                f"for {esc(amount)} M'm deal</code>"
            ),
            parse_mode=ParseMode.HTML,
        )

        await context.bot.send_message(
            chat_id=chat_id,
            text=(
                f"😐 <b>{esc(deal['buyer'])} and "
                f"{esc(deal['seller'])} please copy "
                f"and paste both vouches!</b>"
            ),
            parse_mode=ParseMode.HTML,
        )

    return True


# ===========================
# /close
# ===========================

async def close(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /close ke baad:

    ACTIVE payment message:
        UNPIN

    Completed / Refunded message:
        PIN

    /close command:
        DELETE
    """

    allowed, reason = await add_close_allowed(
        update,
        context,
    )

    if not allowed:
        if reason:
            await update.message.reply_text(reason)

        return

    tid = None

    mode = "release"

    custom_amount = None

    args = list(
        context.args
    )

    # -----------------------
    # Detect deal ID
    # -----------------------

    if (
        args
        and re.fullmatch(
            r"DL-TR4DE-\d+",
            args[0],
            re.I,
        )
    ):
        tid = args.pop(0).upper()

    elif update.message.reply_to_message:
        m = re.search(
            r"\b(DL-TR4DE-\d+)\b",
            update.message.reply_to_message.text or "",
            re.I,
        )

        if m:
            tid = m.group(1).upper()

    # -----------------------
    # Arguments
    # -----------------------

    for a in args:
        if a.lower() in (
            "refund",
            "cancel",
        ):
            mode = "refund"

        else:
            val = extract_amount(a)

            if val > 0:
                custom_amount = val

    # -----------------------
    # No deal ID
    # -----------------------

    if not tid:
        await update.message.reply_text(
            "<b>Usage:</b>\n"
            "<code>/close DL-TR4DE-1</code>\n"
            "<code>/close DL-TR4DE-1 refund</code>\n"
            "<code>/close 50</code> — "
            "<b>custom amount</b> "
            "(reply to deal message)",
            parse_mode=ParseMode.HTML,
        )
        return

    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text(
            "<b>❌ Deal not found.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    closer_id = update.effective_user.id

    # -----------------------
    # Permission
    # -----------------------

    if (
        not is_owner(closer_id)
        and closer_id != deal.get(
            "created_by_id"
        )
    ):
        await update.message.reply_text(
            "<b>❌ Tum sirf apni create hui "
            "deal close kar sakte ho.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if deal.get("status") == "HOLD":
        await update.message.reply_text(
            "<b>⏸️ Yeh deal HOLD par hai.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if deal.get("status") != "ACTIVE":
        await update.message.reply_text(
            f"<b>❌ Yeh deal already "
            f"{esc(deal.get('status'))} hai.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    # -----------------------
    # Votes
    # -----------------------

    votes = deal.get(
        "votes",
        {},
    )

    parties = {
        str(
            deal.get(
                "buyer",
                "",
            )
        ).lower(),

        str(
            deal.get(
                "seller",
                "",
            )
        ).lower(),
    }

    voted = {
        str(x).lower()
        for x in votes.get(
            mode,
            [],
        )
    }

    if not parties.issubset(voted):
        await update.message.reply_text(
            f"<b>❌ Buyer aur Seller dono ne "
            f"{esc(mode.title())} confirm nahi kiya.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    deal["closed_by"] = resolve_username(
        update
    )

    success = await finalize_deal(
        context,
        tid,
        deal,
        mode,
        closer_id=closer_id,
        custom_amount=custom_amount,
    )

    if not success:
        return

    # -----------------------
    # Delete /close command
    # -----------------------

    try:
        await update.message.delete()

    except Exception:
        pass


# ===========================
# /alldeals
# ===========================

async def alldeals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    if not DEALS:
        await update.message.reply_text(
            "<b>📭 Koi deal record nahi hai.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    lines = [
        f"📊 <b>Total Deals: {len(DEALS)}</b>",
        "",
    ]

    for tid, d in DEALS.items():
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"<b>{esc(d['status'])}</b> — "
            f"{esc(d.get('buyer','-'))} ↔ "
            f"{esc(d.get('seller','-'))} — "
            f"<b>{fmt(d.get('amount',0), d.get('currency','INR'))}</b>"
        )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /leaderboard
# ===========================

async def leaderboard_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    today_board = build_leaderboard(
        today_only=True
    )

    all_board = build_leaderboard(
        today_only=False
    )

    def top_line(board, by):
        if not board:
            return "<b>koi data nahi</b>"

        top_user, status = max(
            board.items(),
            key=lambda kv: kv[1][by],
        )

        return (
            f"<b>{esc(top_user)}</b> — "
            f"<b>{status['deals']} deals, "
            f"₹{status['volume']:,.2f}</b>"
        )

    msg = (
        f"{pe('🏆')} <b>Leaderboard</b>\n"
        "──────────────────\n"
        "<b>📅 Today</b>\n"
        "<b>🔥 Top Dealer (most deals):</b>\n"
        f"{top_line(today_board, 'deals')}\n"
        "<b>💰 Top Earner (most volume):</b>\n"
        f"{top_line(today_board, 'volume')}\n\n"
        "<b>♾ All-Time</b>\n"
        "<b>🔥 Top Dealer (most deals):</b>\n"
        f"{top_line(all_board, 'deals')}\n"
        "<b>💰 Top Earner (most volume):</b>\n"
        f"{top_line(all_board, 'volume')}"
    )

    await update.message.reply_text(
        msg,
        parse_mode=ParseMode.HTML,
    )


# ===========================
# /deal
# ===========================

async def deal_lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    if not context.args:
        await update.message.reply_text(
            "<b>Usage:</b> "
            "<code>/deal DL-TR4DE-5</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    tid = context.args[0].upper()

    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text(
            "<b>❌ Deal not found.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        deal_detail_text(tid, deal),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# Admin management
# ===========================

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.effective_chat.type != "private"
        or not is_owner(
            update.effective_user.id
        )
    ):
        return

    target_user = None

    if update.message.reply_to_message:
        target_user = (
            update.message.reply_to_message.from_user
        )

        target_id = target_user.id

    elif (
        context.args
        and context.args[0].isdigit()
    ):
        target_id = int(
            context.args[0]
        )

    else:
        await update.message.reply_text(
            "Usage: kisi user ke message pe reply karke "
            "/addadmin bhejo, ya "
            "<code>/addadmin &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.add(target_id)

    admin_data = {
        "added_by": update.effective_user.id
    }

    if target_user:
        admin_data.update(
            {
                "username": target_user.username,
                "first_name": target_user.first_name,
                "last_name": target_user.last_name,
            }
        )

    if admins_coll is not None:
        admins_coll.update_one(
            {"_id": target_id},
            {"$set": admin_data},
            upsert=True,
        )

    await update.message.reply_text(
        f"✅ <code>{target_id}</code> "
        f"<b>ab bot admin hai.</b>",
        parse_mode=ParseMode.HTML,
    )


async def settradeid_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.effective_chat.type != "private"
        or not is_owner(update.effective_user.id)
    ):
        return

    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text(
            "<b>Usage:</b> <code>/settradeid 1164</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    new_seq = int(context.args[0])

    if new_seq <= 0:
        await update.message.reply_text(
            "<b>❌ Trade ID number valid nahi hai.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    if meta_coll is None:
        await update.message.reply_text(
            "<b>❌ MongoDB required hai.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    meta_coll.update_one(
        {"_id": "trade_counter"},
        {"$set": {"seq": new_seq - 1}},
        upsert=True,
    )

    await update.message.reply_text(
        f"✅ <b>Next Trade ID set ho gayi:</b>\n"
        f"<code>DL-TR4DE-{new_seq}</code>",
        parse_mode=ParseMode.HTML,
    )


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if (
        update.effective_chat.type != "private"
        or not is_owner(
            update.effective_user.id
        )
    ):
        return

    if update.message.reply_to_message:
        target_id = (
            update.message.reply_to_message
            .from_user
            .id
        )

    elif (
        context.args
        and context.args[0].isdigit()
    ):
        target_id = int(
            context.args[0]
        )

    else:
        await update.message.reply_text(
            "Usage: "
            "<code>/removeadmin &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    if target_id in OWNER_IDS:
        await update.message.reply_text(
            "<b>❌ Owner ko remove nahi kar sakte.</b>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.discard(
        target_id
    )

    if admins_coll is not None:
        admins_coll.delete_one(
            {"_id": target_id}
        )

    await update.message.reply_text(
        f"✅ <code>{target_id}</code> "
        f"<b>ab admin nahi raha.</b>",
        parse_mode=ParseMode.HTML,
    )



async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_allowed(update):
        return

    lines = [
        f"{pe('👑')} <b>Owners</b>"
    ]

    if not OWNER_IDS:
        lines.append(
            "<b>(koi owner set nahi hai)</b>"
        )

    else:
        for uid in sorted(OWNER_IDS):
            lines.append(
                f'• <a href="tg://user?id={uid}">'
                f"<b>Owner</b></a> "
                f"<code>({uid})</code>"
            )

    extra_admins = (
        BOT_ADMINS - OWNER_IDS
    )

    lines.append(
        f"\n{pe('🛡')} <b>Bot Admins</b>"
    )

    if not extra_admins:
        lines.append(
            "<b>(koi extra admin nahi hai)</b>"
        )

    else:
        for uid in sorted(extra_admins):
            username = None
            first_name = None
            last_name = None

            if admins_coll is not None:
                admin_doc = admins_coll.find_one(
                    {"_id": uid}
                )

                if admin_doc:
                    username = admin_doc.get(
                        "username"
                    )

                    first_name = admin_doc.get(
                        "first_name"
                    )

                    last_name = admin_doc.get(
                        "last_name"
                    )

            if not username and uid in ADMIN_ALIASES:
                username = ADMIN_ALIASES[uid]

            if first_name:
                display_name = first_name

                if last_name:
                    display_name += (
                        f" {last_name}"
                    )

            elif username:
                display_name = (
                    username
                    .replace("_", " ")
                    .title()
                )

            else:
                display_name = "Admin"

            if username:
                lines.append(
                    f'• <a href="https://t.me/'
                    f'{esc(username)}">'
                    f"<b>{esc(display_name)}</b>"
                    f"</a> "
                    f"<code>({uid})</code>"
                )

            else:
                lines.append(
                    f'• <a href="tg://user?id={uid}">'
                    f"<b>{esc(display_name)}</b>"
                    f"</a> "
                    f"<code>({uid})</code>"
                )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


# ===========================
# /help
# ===========================

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id

    lines = [
        f"{pe('📖')} <b>Commands</b>",
        "──────────────────",
        "<b>👤 User Commands</b>",
        "<b>/start — Dashboard kholo (private chat)</b>",
        "<b>/stats — Apna deal status dekho</b>",
        "<b>/form — New escrow form banao</b>",
        "<b>/cancel — Form wizard cancel karo</b>",
        "<b>/help — Ye list dikhata hai</b>",
    ]

    if is_admin(uid):
        lines += [
            "",
            "<b>🛡 Admin Commands</b>",
            "<b>/add — Filled form par reply karke deal create karo</b>",
            "<b>/add 500 — Custom amount ke saath deal create karo</b>",
            "<b>/close — Deal complete/refund karo</b>",
            "<b>/alldeals — Saari deals ki list</b>",
            "<b>/leaderboard — Today + All-time leaderboard</b>",
            "<b>/deal &lt;DL-TR4DE-N&gt; — Deal detail</b>",
            "<b>/admins — Bot admins ki list</b>",
            "<b>/broadcast &lt;message&gt; — Broadcast</b>",
            "<b>/settradeid 1164</b>",
        ]

    if is_owner(uid):
        lines += [
            "",
            "<b>👑 Owner Commands</b>",
            "<b>/addadmin — New bot admin add karo</b>",
            "<b>/removeadmin — Bot admin remove karo</b>",
        ]

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
    )


# ===========================
# Keep Alive
# ===========================

def start_dummy_server():
    port = int(
        os.getenv(
            "PORT",
            "10000",
        )
    )

    class Handler(BaseHTTPRequestHandler):

        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(
                b"NTescrowbot is running"
            )

        def do_HEAD(self):
            self.send_response(200)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(
        ("0.0.0.0", port),
        Handler,
    )

    threading.Thread(
        target=server.serve_forever,
        daemon=True,
    ).start()

    print(
        f"✅ Dummy HTTP server listening on port {port}"
    )


# ===========================
# Main
# ===========================

def main():
    if not BOT_TOKEN:
        raise RuntimeError(
            "NTESCROW_BOT_TOKEN missing in .env"
        )

    start_dummy_server()

    try:
        asyncio.get_event_loop()

    except RuntimeError:
        asyncio.set_event_loop(
            asyncio.new_event_loop()
        )

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    # Commands
    app.add_handler(
        CommandHandler(
            "start",
            start_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "stats",
            mystatus_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "add",
            add,
        )
    )

    app.add_handler(
        CommandHandler(
            "close",
            close,
        )
    )

    app.add_handler(
        CommandHandler(
            "hold",
            hold_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "form",
            form_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "cancel",
            cancel_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "broadcast",
            broadcast_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "alldeals",
            alldeals_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "leaderboard",
            leaderboard_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "deal",
            deal_lookup_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "addadmin",
            addadmin_cmd,
        )
    )
    app.add_handler(
        CommandHandler(
            "settradeid",
            settradeid_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "removeadmin",
            removeadmin_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "admins",
            admins_cmd,
        )
    )

    app.add_handler(
        CommandHandler(
            "help",
            help_cmd,
        )
    )

    # Callback buttons
    app.add_handler(
        CallbackQueryHandler(
            callback_router
        )
    )

    # Text handler
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            nt_text_handler,
        )
    )

    print(
        "✅ NTescrowbot Running..."
    )

    app.run_polling(
        allowed_updates=Update.ALL_TYPES
    )


if __name__ == "__main__":
    main()
