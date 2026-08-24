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
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

load_dotenv()

# ===========================
# Config (.env se aata hai)
# ===========================
# NTWALLET_BOT_TOKEN=xxxx
# MONGO_URI=xxxx
# NT_ADMIN_IDS=123,456   -> OWNERS, sirf ye naye bot-admin add/remove kar sakte hai

BOT_TOKEN = os.getenv("NTWALLET_BOT_TOKEN")
BRAND = "@NTwallet"
PROVIDER = "@NTwallet"

MONGO_URI = os.getenv("MONGO_URI")
OWNER_IDS = set(
    int(x) for x in os.getenv("NT_ADMIN_IDS", "").split(",") if x.strip().isdigit()
)

# Secondary/limited accounts -> "Escrowed By" me inka username nahi, mapped MAIN
# username dikhega. Apne hisaab se yaha fill karo.
ADMIN_ALIASES = {
    # 8258334055: "primaxog",
}

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["escrow_bots"] if mongo_client else None
coll = mongo_db["deals_ntwallet"] if mongo_db is not None else None
meta_coll = mongo_db["meta_ntwallet"] if mongo_db is not None else None
admins_coll = mongo_db["bot_admins_ntwallet"] if mongo_db is not None else None
users_coll = mongo_db["broadcast_users_ntwallet"] if mongo_db is not None else None

DEALS = {}

if coll is not None:
    for doc in coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc
    print(f"✅ [ntwallet] {len(DEALS)} deal(s) Mongo se load hui")

BOT_ADMINS = set(OWNER_IDS)
if admins_coll is not None:
    for doc in admins_coll.find({}):
        BOT_ADMINS.add(doc["_id"])
    print(f"✅ [ntwallet] {len(BOT_ADMINS)} bot admin(s) load hue")


def save_deal(tid):
    if coll is not None:
        coll.update_one({"_id": tid}, {"$set": dict(DEALS[tid])}, upsert=True)


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in BOT_ADMINS or is_owner(uid)


def admin_only_allowed(update: Update):
    if update.effective_chat.type != "private":
        return False
    return is_admin(update.effective_user.id)


async def add_close_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Same permission model as the original bot:
    - private chat: only internal BOT_ADMINS/OWNER_IDS
    - group/supergroup: Telegram-level group admin/owner (bot must also be admin)
    """
    chat = update.effective_chat
    user_id = update.effective_user.id

    if chat.type == "private":
        return is_admin(user_id), None

    if chat.type not in ("group", "supergroup"):
        return False, None

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except Exception:
        return False, "❌ Bot ka admin status is group me check nahi ho paaya."
    if bot_member.status not in ("administrator", "creator"):
        return False, (
            "❌ Ye command tabhi kaam karegi jab BOT is group me Admin ho "
            "(pehle bot ko group me admin banao)."
        )

    try:
        user_member = await context.bot.get_chat_member(chat.id, user_id)
    except Exception:
        return False, "❌ Tumhara admin status is group me check nahi ho paaya."
    if user_member.status not in ("administrator", "creator"):
        return False, None

    return True, None


# ===========================
# Sequential Trade ID: DL-NT-1, DL-NT-2, ...
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

    tid = f"DL-NT-{seq}"
    while tid in DEALS:
        seq += 1
        tid = f"DL-NT-{seq}"
    return tid


# ===========================
# Helpers
# ===========================

def esc(text):
    if text is None:
        return ""
    return html.escape(str(text), quote=False)


def fmt(amount, currency="INR"):
    if currency in ("USDT", "TON"):
        return f"{amount:,.2f} {currency}"
    symbol = {"INR": "₹", "USD": "$"}.get(currency, "")
    return f"{symbol}{amount:,.2f}"


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


def norm_handle(text):
    """Lowercase, strip leading @, for matching a typed buyer/seller handle
    against the username of whoever clicked a button."""
    return (text or "").strip().lstrip("@").lower()


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
# NOTE: these document IDs were verified for a different bot's account.
# Custom-emoji IDs are tied to the sending account's Premium status — grab
# fresh IDs for the NTwallet bot's own account before relying on these,
# otherwise you'll just see the plain fallback character.
# ===========================
PE = {
    "⭐️": "5181422544162391976",
    "❤️": "5260535596941582167",
    "💬": "5258330865674494479",
    "⚡️": "5938539885907415367",
    "🌐": "6041705726206808304",
    "🔥": "5420315771991497307",
    "🪙": "5884428842780594914",
    "💰": "6039802097916974085",
    "🤑": "5893473283696759404",
    "📱": "6152069549442208798",
    "💤": "5895266423952904371",
    "✅": "5197474765387864959",
    "🆔": "5936017305585586269",
    "🛡": "5920052658743283381",
    "📤": "6030822047150512346",
    "👤": "5258011929993026890",
    "📝": "5879841310902324730",
    "⏱️": "5936170807716745162",
    "📌": "5796440171364749940",
    "🛡️": "5920052658743283381",
    "ℹ️": "5994473545650934240",
}


def pe(emoji):
    emoji_id = PE.get(emoji)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{emoji}</tg-emoji>'
    return emoji


# ===========================
# CHARGES
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


# ===========================
# Deal record card
# ===========================

def deal_card_text(tid, deal):
    return (
        f"{pe('✅')} <b>Payment received !</b>\n"
        "─────────────────\n"
        f"➥ ID: {esc(tid)}\n"
        f"➥ Buyer: {esc(deal.get('buyer','-'))}\n"
        f"➥ Seller: {esc(deal.get('seller','-'))}\n"
        f"➥ Item: {esc(deal.get('detail','-'))}\n"
        f"➥ Amount: {fmt(deal.get('amount',0), deal.get('currency','INR'))}\n"
        f"➥ Fees: {deal.get('fee_percent',0):.1f}%\n"
        f"➥ Terms: {esc(deal.get('tc','-'))}\n"
        f"➥ Escrower: {esc(deal.get('escrowed_by','-'))}\n"
        f"➥ Start Time: {datetime.fromisoformat(deal['created_at']).strftime('%H:%M:%S')}\n"
        f"     [ {datetime.fromisoformat(deal['created_at']).strftime('%d %B %Y')} ]\n"
        "─────────────────\n"
        f"{pe('🛡')} Escrowed by {esc(deal.get('escrowed_by','-'))}\n"
        f"{pe('⭐️')} Provided by {BRAND}"
    )


def release_refund_kb(tid):
    rows = [[
        InlineKeyboardButton("✅ Release", callback_data=f"agree:release:{tid}"),
        InlineKeyboardButton("♻️ Refund", callback_data=f"agree:refund:{tid}"),
    ]]
    return InlineKeyboardMarkup(rows)


# ===========================
# /start, /help
# ===========================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    if update.effective_chat.type != "private":
        return
    await update.message.reply_text(
        f"{pe('⭐️')} <b>Welcome {esc(update.effective_user.first_name)}!</b>\n"
        f"{pe('💬')} Escrow bot for {BRAND}.\n\n"
        "Use /newdeal in a group to start a guided deal, or reply to a filled "
        "template with /add — either way works.",
        parse_mode=ParseMode.HTML,
    )


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    lines = [
        f"{pe('📱')} <b>Commands</b>",
        "─────────────────",
        "/start — bot intro",
        "/newdeal — guided step-by-step deal creation (buttons)",
        "/cancel — cancel an in-progress /newdeal",
    ]
    if is_admin(uid):
        lines += [
            "",
            "<b>Admin</b>",
            "/add — create deal by replying to a filled template",
            "/close — complete/cancel a deal",
        ]
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ===========================
# /add + /close  (unchanged reply-to-template flow, kept as-is)
# ===========================

async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return

    raw_text = (
        update.message.reply_to_message.text
        if update.message.reply_to_message
        else ""
    )
    text = normalize_bold(raw_text)

    field_prefix = r"(?:^|\n)\s*(?:[•·▪▫●○‣➜➤-]\s*)?"
    seller = re.search(field_prefix + r"SELLER\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    buyer = re.search(field_prefix + r"BUYER\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    detail = re.search(field_prefix + r"(?:DEAL\s+DETAIL|ITEM)\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    amount = re.search(field_prefix + r"(?:DEAL\s+)?AMOUNT\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    tc = re.search(field_prefix + r"T\s*/\s*C\s*(?:\(\s*IF\s+ANY\s*\))?|TERMS\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)
    currency = re.search(field_prefix + r"(?:CURRENCY|DEAL\s+TYPE)\s*:\s*(.*?)\s*(?:\n|$)", text, re.IGNORECASE)

    seller_val = seller.group(1).strip() if seller else "-"
    buyer_val = buyer.group(1).strip() if buyer else "-"
    detail_val = detail.group(1).strip() if detail else "-"
    form_amount_val = extract_amount(amount.group(1)) if amount else 0.0
    tc_val = tc.group(1).strip() if tc and tc.group(1) else "-"
    currency_val = currency.group(1).strip().upper() if currency else "INR"

    is_exchange = False
    amount_val = form_amount_val
    if context.args:
        arg = context.args[0].strip()
        if arg.lower() == "exchange":
            is_exchange = True
        else:
            custom_amount = extract_amount(arg)
            if custom_amount > 0:
                amount_val = custom_amount

    tid = next_trade_id()
    creator_username = resolve_username(update)

    fee_amount = calculate_fee(amount_val, is_exchange)
    release_val = amount_val - fee_amount
    fee_percent = (fee_amount / amount_val * 100) if amount_val else 0.0

    DEALS[tid] = {
        "seller": seller_val,
        "buyer": buyer_val,
        "detail": detail_val,
        "amount": amount_val,
        "release": release_val,
        "fee_percent": fee_percent,
        "tc": tc_val,
        "currency": currency_val,
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "created_by_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "exchange": is_exchange,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    save_deal(tid)

    await update.message.reply_text(
        deal_card_text(tid, DEALS[tid]), parse_mode=ParseMode.HTML
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"{pe('✅')} {esc(buyer_val)} and {esc(seller_val)} confirm the "
            "button below after deal completion and discussion !"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=release_refund_kb(tid),
    )
    try:
        await update.message.delete()
    except Exception:
        pass


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return

    tid = None
    released_amount_arg = None

    if context.args and re.fullmatch(r"DL-NT-\d+", context.args[0], re.IGNORECASE):
        tid = context.args[0].upper()
        if len(context.args) > 1:
            released_amount_arg = context.args[1]
    elif update.message.reply_to_message:
        reply_text = update.message.reply_to_message.text or ""
        match = re.search(r"ID:\s*(DL-NT-\d+)", reply_text, re.IGNORECASE)
        if not match:
            await update.message.reply_text("❌ Reply kiye gaye message me Trade ID nahi mila.")
            return
        tid = match.group(1).upper()
        if context.args:
            released_amount_arg = context.args[0]
    else:
        await update.message.reply_text(
            "❌ Deal close karne ke liye:\n\n"
            "<b>Reply karke:</b>\n<code>/close</code>\n<code>/close 300</code>\n<code>/close cancel</code>\n\n"
            "<b>Ya direct ID se:</b>\n<code>/close DL-NT-4</code>\n<code>/close DL-NT-4 300</code>\n<code>/close DL-NT-4 cancel</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    deal = DEALS.get(tid)
    if not deal:
        await update.message.reply_text(f"❌ Deal <code>{esc(tid)}</code> not found.", parse_mode=ParseMode.HTML)
        return

    closer_id = update.effective_user.id
    if not is_owner(closer_id):
        deal_creator_id = deal.get("created_by_id")
        if deal_creator_id is not None:
            if closer_id != deal_creator_id:
                await update.message.reply_text("❌ Tum sirf apni create ki hui deal close kar sakte ho.")
                return
        elif resolve_username(update) != deal.get("escrowed_by"):
            await update.message.reply_text("❌ Tum sirf apni create ki hui deal close kar sakte ho.")
            return

    if deal.get("status") != "ACTIVE":
        await update.message.reply_text(f"❌ Yeh deal already {deal.get('status','closed')} hai.")
        return

    is_cancel = released_amount_arg and released_amount_arg.lower() == "cancel"
    currency_val = deal.get("currency", "INR")

    if is_cancel:
        released_val = 0.0
    elif released_amount_arg:
        released_val = extract_amount(released_amount_arg)
    else:
        released_val = deal.get("release", 0.0)

    deal["status"] = "CANCELLED" if is_cancel else "COMPLETED"
    deal["released"] = released_val
    deal["completed_at"] = datetime.now(timezone.utc).isoformat()
    deal["closed_by_id"] = closer_id
    deal["closed_by"] = resolve_username(update)
    save_deal(tid)

    if is_cancel:
        msg = (
            f"❌ <b>Deal Cancelled</b>\n"
            f"{pe('🆔')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('ℹ️')} 100% of the charge has been deducted.\n"
            f"{pe('🛡️')} Escrowed By: {esc(deal.get('escrowed_by','-'))}"
        )
    else:
        msg = (
            f"{pe('✅')} <b>Deal Completed</b>\n"
            f"{pe('🆔')} Trade ID: <code>{esc(tid)}</code>\n"
            f"{pe('📤')} Released: {fmt(released_val, currency_val)}\n"
            f"{pe('🛡️')} Escrowed By: {esc(deal.get('escrowed_by','-'))}\n\n"
            f"~ {esc(deal['buyer'])} and {esc(deal['seller'])} are requested to "
            f"drop the vouch before leaving👇🏻\n\n"
            f"<code>Vouch {BRAND} for {fmt(released_val, currency_val)} smooth escrow deal</code>\n"
        )

    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    try:
        await update.message.delete()
    except Exception:
        pass


# ===========================
# NEW: guided /newdeal conversation (Deal Type buttons -> step prompts)
# ===========================

CURRENCY, BUYER, SELLER, ITEM, AMOUNT, TERMS = range(6)


async def newdeal_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason and update.message:
            await update.message.reply_text(reason)
        return ConversationHandler.END

    context.user_data["nd"] = {}
    rows = [[
        InlineKeyboardButton("TON", callback_data="ndcur:TON"),
        InlineKeyboardButton("USDT", callback_data="ndcur:USDT"),
        InlineKeyboardButton("INR", callback_data="ndcur:INR"),
    ]]
    await update.message.reply_text(
        f"{pe('🛡')} What type of deal ?\n➤ Select the currency below :",
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(rows),
    )
    return CURRENCY


async def newdeal_currency(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    currency = query.data.split(":", 1)[1]
    context.user_data["nd"]["currency"] = currency
    await query.edit_message_text(f"Deal Type: {esc(currency)}\n\n➤ Buyer ka username bhejo:")
    return BUYER


async def newdeal_buyer(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nd"]["buyer"] = update.message.text.strip()
    await update.message.reply_text("➤ Seller ka username bhejo:")
    return SELLER


async def newdeal_seller(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nd"]["seller"] = update.message.text.strip()
    await update.message.reply_text("➤ Item / deal detail bhejo:")
    return ITEM


async def newdeal_item(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nd"]["item"] = update.message.text.strip()
    await update.message.reply_text("➤ Amount bhejo (e.g. 5, 100, 1000):")
    return AMOUNT


async def newdeal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["nd"]["amount"] = extract_amount(update.message.text)
    await update.message.reply_text("➤ Terms bhejo (ya '-' agar koi nahi):")
    return TERMS


async def newdeal_terms(update: Update, context: ContextTypes.DEFAULT_TYPE):
    nd = context.user_data.get("nd", {})
    nd["terms"] = update.message.text.strip()

    tid = next_trade_id()
    creator_username = resolve_username(update)
    amount_val = nd.get("amount", 0.0)
    fee_amount = calculate_fee(amount_val, is_exchange=False)
    release_val = amount_val - fee_amount
    fee_percent = (fee_amount / amount_val * 100) if amount_val else 0.0

    DEALS[tid] = {
        "seller": nd.get("seller", "-"),
        "buyer": nd.get("buyer", "-"),
        "detail": nd.get("item", "-"),
        "amount": amount_val,
        "release": release_val,
        "fee_percent": fee_percent,
        "tc": nd.get("terms", "-"),
        "currency": nd.get("currency", "INR"),
        "status": "ACTIVE",
        "escrowed_by": creator_username,
        "created_by_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
        "exchange": False,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "buyer_vote": None,
        "seller_vote": None,
    }
    save_deal(tid)

    await update.message.reply_text(deal_card_text(tid, DEALS[tid]), parse_mode=ParseMode.HTML)
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=(
            f"{pe('✅')} {esc(nd.get('buyer','-'))} and {esc(nd.get('seller','-'))} "
            "confirm the button below after deal completion and discussion !"
        ),
        parse_mode=ParseMode.HTML,
        reply_markup=release_refund_kb(tid),
    )
    context.user_data.pop("nd", None)
    return ConversationHandler.END


async def newdeal_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data.pop("nd", None)
    await update.message.reply_text("❌ Deal creation cancelled.")
    return ConversationHandler.END


newdeal_conv = ConversationHandler(
    entry_points=[CommandHandler("newdeal", newdeal_start)],
    states={
        CURRENCY: [CallbackQueryHandler(newdeal_currency, pattern=r"^ndcur:")],
        BUYER: [MessageHandler(filters.TEXT & ~filters.COMMAND, newdeal_buyer)],
        SELLER: [MessageHandler(filters.TEXT & ~filters.COMMAND, newdeal_seller)],
        ITEM: [MessageHandler(filters.TEXT & ~filters.COMMAND, newdeal_item)],
        AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, newdeal_amount)],
        TERMS: [MessageHandler(filters.TEXT & ~filters.COMMAND, newdeal_terms)],
    },
    fallbacks=[CommandHandler("cancel", newdeal_cancel)],
)


# ===========================
# NEW: Release / Refund agreement buttons
#
# These buttons only RECORD that the buyer/seller agree — they do not move
# any funds themselves. Once both sides agree the same way, the assigned
# escrow admin (escrowed_by) is pinged to actually run /close and release
# or refund for real. This mirrors how manual, human-mediated escrow works:
# the bot tracks agreement, a person still finalizes the transfer.
# ===========================

async def agree_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    _, action, tid = query.data.split(":", 2)
    deal = DEALS.get(tid)

    if not deal:
        await query.answer("Deal not found.", show_alert=True)
        return

    if deal.get("status") != "ACTIVE":
        await query.answer(f"This deal is already {deal.get('status','closed')}.", show_alert=True)
        return

    clicker_username = resolve_username(update)
    clicker_handle = norm_handle(clicker_username)
    buyer_handle = norm_handle(deal.get("buyer"))
    seller_handle = norm_handle(deal.get("seller"))

    if clicker_handle == buyer_handle:
        role = "buyer"
    elif clicker_handle == seller_handle:
        role = "seller"
    else:
        await query.answer("Only the buyer or seller on this deal can use this button.", show_alert=True)
        return

    deal[f"{role}_vote"] = action
    save_deal(tid)
    await query.answer(f"You agreed to {action}.")

    buyer_vote = deal.get("buyer_vote")
    seller_vote = deal.get("seller_vote")

    def vote_line(label, vote):
        if vote == "release":
            return f"✅ {label} agreed for Release"
        if vote == "refund":
            return f"♻️ {label} agreed for Refund"
        return f"⏳ {label} — waiting"

    status_text = (
        f"{pe('✅')} {esc(deal.get('buyer','-'))} and {esc(deal.get('seller','-'))} "
        "confirm the button below after deal completion and discussion !\n\n"
        f"{vote_line('Buyer', buyer_vote)}\n"
        f"{vote_line('Seller', seller_vote)}"
    )

    if buyer_vote and seller_vote and buyer_vote == seller_vote:
        status_text += (
            f"\n\n{pe('ℹ️')} Both sides agreed to <b>{esc(action.upper())}</b>. "
            f"Dear {esc(deal.get('escrowed_by','-'))}, please finalize with /close "
            f"{esc(tid)}{' cancel' if action == 'refund' else ''}.\n"
            "❗️Verify both usernames before releasing."
        )
        await query.edit_message_text(status_text, parse_mode=ParseMode.HTML)
        return

    if buyer_vote and seller_vote and buyer_vote != seller_vote:
        status_text += (
            f"\n\n{pe('ℹ️')} Buyer and seller disagree — one wants Release, the "
            f"other wants Refund. {esc(deal.get('escrowed_by','-'))}, please step "
            "in and resolve this manually."
        )
        await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=release_refund_kb(tid))
        return

    await query.edit_message_text(status_text, parse_mode=ParseMode.HTML, reply_markup=release_refund_kb(tid))


# ===========================
# Bot-admin management (owner only)
# ===========================

async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return
    if update.message.reply_to_message:
        target_user = update.message.reply_to_message.from_user
        target_id = target_user.id
    elif context.args and context.args[0].isdigit():
        target_user = None
        target_id = int(context.args[0])
    else:
        await update.message.reply_text(
            "Usage: kisi user ke message pe reply karke /addadmin bhejo, "
            "ya <code>/addadmin &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.add(target_id)
    admin_data = {"added_by": update.effective_user.id}
    if update.message.reply_to_message:
        admin_data.update({
            "username": target_user.username,
            "first_name": target_user.first_name,
            "last_name": target_user.last_name,
        })
    if admins_coll is not None:
        admins_coll.update_one({"_id": target_id}, {"$set": admin_data}, upsert=True)

    await update.message.reply_text(f"✅ <code>{target_id}</code> ab bot admin hai.", parse_mode=ParseMode.HTML)


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return
    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text("Usage: <code>/removeadmin &lt;user_id&gt;</code>", parse_mode=ParseMode.HTML)
        return
    if target_id in OWNER_IDS:
        await update.message.reply_text("❌ Owner ko remove nahi kar sakte.")
        return
    BOT_ADMINS.discard(target_id)
    if admins_coll is not None:
        admins_coll.delete_one({"_id": target_id})
    await update.message.reply_text(f"✅ <code>{target_id}</code> ab admin nahi raha.", parse_mode=ParseMode.HTML)


# ===========================
# Keep-alive server
# ===========================

def start_dummy_server():
    port = int(os.getenv("PORT", "10001"))

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"NTwallet bot is running")

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
    start_dummy_server()

    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(newdeal_conv)
    app.add_handler(CallbackQueryHandler(agree_button, pattern=r"^agree:"))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))

    print("✅ NTwallet Bot Running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
