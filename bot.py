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

# ============================================================
# NT ESCROW BOT
# ============================================================
# .env:
# NTESCROW_BOT_TOKEN=xxxx
# MONGO_URI=mongodb+srv://...
# ADMIN_IDS=123456789,987654321
# Optional:
# DEAL_FEE_PERCENT=1.0
# ============================================================

BOT_TOKEN = os.getenv("NTESCROW_BOT_TOKEN")
BRAND = "@NTescrowbot"
PROVIDER = "@NTescrowbot"
FORM_TITLE = "#NTwallet [Escrow Form]"
TRADE_PREFIX = "DL-NTWALLET"

MONGO_URI = os.getenv("MONGO_URI")
OWNER_IDS = {
    int(x.strip())
    for x in os.getenv("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
}

try:
    DEFAULT_FEE_PERCENT = float(os.getenv("DEAL_FEE_PERCENT", "1.0"))
except ValueError:
    DEFAULT_FEE_PERCENT = 1.0

SUPPORTED_CURRENCIES = ("TON", "USDT", "INR")

# ============================================================
# MONGODB
# ============================================================

mongo_client = MongoClient(MONGO_URI) if MONGO_URI else None
mongo_db = mongo_client["escrow_bots"] if mongo_client is not None else None

coll = mongo_db["deals_ntescrowbot"] if mongo_db is not None else None
meta_coll = mongo_db["meta_ntescrowbot"] if mongo_db is not None else None
admins_coll = mongo_db["bot_admins_ntescrowbot"] if mongo_db is not None else None
users_coll = mongo_db["broadcast_users_ntescrowbot"] if mongo_db is not None else None

DEALS = {}

if coll is not None:
    for doc in coll.find({}):
        tid = doc.pop("_id")
        DEALS[tid] = doc
    print(f"Loaded {len(DEALS)} NTwallet deal(s) from MongoDB")

BOT_ADMINS = set(OWNER_IDS)
if admins_coll is not None:
    for doc in admins_coll.find({}):
        BOT_ADMINS.add(doc["_id"])
    print(f"Loaded {len(BOT_ADMINS)} bot admin(s)")


# ============================================================
# HELPERS
# ============================================================

def esc(value):
    return html.escape(str(value or ""), quote=False)


def clean_username(value):
    value = str(value or "").strip()
    if not value or value == "-":
        return "-"
    if value.startswith("@"):
        return value
    return "@" + value


def resolve_username(update: Update):
    user = update.effective_user
    if not user:
        return "-"
    if user.username:
        return "@" + user.username
    return user.first_name or f"User {user.id}"


def fmt(amount, currency):
    amount = float(amount or 0)
    if currency in ("TON", "USDT"):
        text = f"{amount:,.8f}".rstrip("0").rstrip(".")
        return f"{text} {currency}"
    if currency == "INR":
        text = f"{amount:,.2f}".rstrip("0").rstrip(".")
        return f"₹{text}"
    return f"{amount:g} {currency}"


def extract_amount(text):
    match = re.search(r"[-+]?[\d,]+(?:\.\d+)?", str(text or ""))
    if not match:
        return 0.0
    try:
        return float(match.group(0).replace(",", ""))
    except ValueError:
        return 0.0


def is_owner(uid):
    return uid in OWNER_IDS


def is_admin(uid):
    return uid in BOT_ADMINS or is_owner(uid)


def save_deal(tid):
    if coll is not None and tid in DEALS:
        coll.update_one({"_id": tid}, {"$set": dict(DEALS[tid])}, upsert=True)


def remember_user(update: Update):
    if users_coll is None or not update.effective_user:
        return

    user = update.effective_user
    users_coll.update_one(
        {"_id": user.id},
        {"$set": {
            "username": user.username,
            "first_name": user.first_name,
            "last_name": user.last_name,
            "last_seen": datetime.now(timezone.utc).isoformat(),
        }},
        upsert=True,
    )


def next_trade_id():
    if meta_coll is not None:
        doc = meta_coll.find_one_and_update(
            {"_id": "trade_counter"},
            {"$inc": {"seq": 1}},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )
        seq = int(doc["seq"])
    else:
        seq = len(DEALS) + 1

    tid = f"{TRADE_PREFIX}-{seq}"
    while tid in DEALS:
        seq += 1
        tid = f"{TRADE_PREFIX}-{seq}"
    return tid


async def add_close_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Private: internal bot admin/owner. Group: Telegram group admin + bot admin."""
    chat = update.effective_chat
    user = update.effective_user

    if not chat or not user:
        return False, None

    if chat.type == "private":
        return is_admin(user.id), None

    if chat.type not in ("group", "supergroup"):
        return False, None

    try:
        bot_member = await context.bot.get_chat_member(chat.id, context.bot.id)
    except Exception:
        return False, "❌ Bot ka admin status check nahi ho paaya."

    if bot_member.status not in ("administrator", "creator"):
        return False, "❌ Pehle bot ko is group me Admin banao."

    try:
        user_member = await context.bot.get_chat_member(chat.id, user.id)
    except Exception:
        return False, "❌ Tumhara group admin status check nahi ho paaya."

    if user_member.status not in ("administrator", "creator"):
        return False, None

    return True, None


# ============================================================
# UI
# ============================================================

def currency_kb():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("TON", callback_data="newdeal:currency:TON"),
            InlineKeyboardButton("USDT", callback_data="newdeal:currency:USDT"),
            InlineKeyboardButton("INR", callback_data="newdeal:currency:INR"),
        ]
    ])


def deal_action_kb(tid):
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Release", callback_data=f"dealaction:{tid}:release"),
            InlineKeyboardButton("Refund", callback_data=f"dealaction:{tid}:refund"),
        ]
    ])


def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📊 My Status", callback_data="menu:status")],
        [InlineKeyboardButton("📁 My Deals", callback_data="menu:deals")],
        [InlineKeyboardButton("⏳ My Pending Deals", callback_data="menu:pending")],
        [InlineKeyboardButton("🌐 Global Status", callback_data="menu:global")],
    ])


def back_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("◀ Back", callback_data="menu:back")]
    ])


def welcome_text(first_name):
    return (
        f"🛡 <b>Welcome {esc(first_name)}!</b>\n"
        "─────────────────\n"
        f"Escrow Bot for {BRAND}\n"
        f"Provided by {PROVIDER}\n\n"
        "Select an option below."
    )


# ============================================================
# DEAL MESSAGES
# ============================================================

def form_text(currency, amount, escrower):
    return (
        f"<b>{esc(FORM_TITLE)}</b> :\n\n"
        f"➥ Deal Type: {esc(currency)}\n"
        "➥ Buyer :\n"
        "➥ Seller :\n"
        "➥ Item :\n"
        f"➥ Amount : {esc(fmt(amount, currency))}\n"
        "➥ Terms :\n\n"
        f"🔒 Escrowed by {esc(escrower)}"
    )


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


def refund_text(tid, deal):
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


def vouch_text(deal):
    amount = fmt(deal["amount"], deal["currency"])
    return (
        f"😐 {esc(deal['buyer'])} and {esc(deal['seller'])} please copy and paste both vouches!\n\n"
        f"<code>Vouch {BRAND} for {esc(amount)} safe Escrow deal</code>\n\n"
        f"<code>Vouch {esc(deal['escrowed_by'])} for {esc(amount)} M'm deal</code>"
    )


# ============================================================
# FORM PARSER
# ============================================================

def parse_form(text):
    if not text:
        return None

    normalized = text.replace("𝙋", "P").replace("𝗧", "T")

    def get_field(name):
        pattern = rf"(?:^|\n)\s*[➥➤•·▪▫●○‣\-]*\s*{name}\s*:\s*(.*?)(?=\n|$)"
        match = re.search(pattern, normalized, re.IGNORECASE)
        return match.group(1).strip() if match else ""

    deal_type = get_field(r"Deal\s*Type").upper()
    buyer = get_field("Buyer")
    seller = get_field("Seller")
    item = get_field("Item")
    amount_raw = get_field("Amount")
    terms = get_field("Terms")

    if deal_type not in SUPPORTED_CURRENCIES:
        return None

    amount = extract_amount(amount_raw)
    if amount <= 0:
        return None

    buyer = clean_username(buyer)
    seller = clean_username(seller)

    if buyer == "-" or seller == "-" or not item or not terms:
        return None

    return {
        "currency": deal_type,
        "buyer": buyer,
        "seller": seller,
        "item": item,
        "amount": amount,
        "terms": terms,
    }


# ============================================================
# START + ADD WIZARD
# ============================================================

async def start_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    if update.effective_chat.type != "private":
        return

    await update.message.reply_text(
        welcome_text(update.effective_user.first_name),
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu_kb(),
    )


async def add_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason:
            await update.message.reply_text(reason)
        return

    context.user_data["new_deal"] = {
        "step": "currency",
        "escrower": resolve_username(update),
        "creator_id": update.effective_user.id,
        "chat_id": update.effective_chat.id,
    }

    await update.message.reply_text(
        "🛡 <b>What type of deal ?</b>\n\n"
        "➤ Select the currency below :",
        parse_mode=ParseMode.HTML,
        reply_markup=currency_kb(),
    )

    try:
        await update.message.delete()
    except Exception:
        pass


async def callback_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    if not query:
        return

    await query.answer()
    data = query.data or ""

    # ---------------- New deal currency ----------------
    if data.startswith("newdeal:currency:"):
        currency = data.rsplit(":", 1)[1].upper()

        if currency not in SUPPORTED_CURRENCIES:
            return

        state = context.user_data.get("new_deal")
        if not state or state.get("step") != "currency":
            await query.answer("Start again with /add", show_alert=True)
            return

        state["currency"] = currency
        state["step"] = "amount"

        await query.edit_message_text(
            f"➤ Tell me deal amount in <b>{currency}</b>\n"
            "ex - <code>1</code>, <code>100</code>, <code>1000</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    # ---------------- Release / Refund ----------------
    if data.startswith("dealaction:"):
        try:
            _, tid, action = data.split(":", 2)
        except ValueError:
            return

        deal = DEALS.get(tid)
        if not deal:
            await query.answer("Deal not found.", show_alert=True)
            return

        if deal.get("status") != "ACTIVE":
            await query.answer("This deal is already closed.", show_alert=True)
            return

        uid = update.effective_user.id
        username = resolve_username(update).lower()

        buyer = str(deal.get("buyer", "")).lower()
        seller = str(deal.get("seller", "")).lower()

        # Only the exact buyer/seller username can vote.
        if username not in (buyer, seller):
            await query.answer("Only this deal's Buyer or Seller can confirm.", show_alert=True)
            return

        votes = deal.setdefault("votes", {
            "release": [],
            "refund": [],
        })

        # If user changes mind, remove from opposite vote.
        opposite = "refund" if action == "release" else "release"
        votes.setdefault(opposite, [])
        votes.setdefault(action, [])

        votes[opposite] = [v for v in votes[opposite] if v.get("id") != uid]
        if not any(v.get("id") == uid for v in votes[action]):
            votes[action].append({
                "id": uid,
                "username": resolve_username(update),
                "at": datetime.now(timezone.utc).isoformat(),
            })

        save_deal(tid)

        await query.answer(f"{action.title()} confirmation saved.")

        needed_ids = set()
        # Prefer real Telegram IDs captured from messages if available.
        if deal.get("buyer_id"):
            needed_ids.add(deal["buyer_id"])
        if deal.get("seller_id"):
            needed_ids.add(deal["seller_id"])

        current_vote_ids = {v.get("id") for v in votes[action]}

        # Username-based fallback: both exact parties must vote.
        party_names = {buyer, seller}
        voted_names = {
            str(v.get("username", "")).lower()
            for v in votes[action]
        }

        both_agreed = (
            party_names.issubset(voted_names)
            if len(party_names) == 2
            else needed_ids.issubset(current_vote_ids)
        )

        if both_agreed:
            escrower = deal.get("escrowed_by", "-")
            label = "Release" if action == "release" else "Refund"

            await context.bot.send_message(
                chat_id=deal["chat_id"],
                text=(
                    f"😐 Buyer [{esc(deal['buyer'])}] & Seller [{esc(deal['seller'])}] "
                    f"agreed to {label}.\n\n"
                    f"Dear {esc(escrower)}, please {action} the funds according to deal.\n\n"
                    "❗️Verify both usernames before proceeding."
                ),
                parse_mode=ParseMode.HTML,
            )

            try:
                await query.edit_message_reply_markup(reply_markup=None)
            except Exception:
                pass

        return

    # ---------------- Dashboard ----------------
    if data == "menu:back":
        await query.edit_message_text(
            welcome_text(update.effective_user.first_name),
            parse_mode=ParseMode.HTML,
            reply_markup=main_menu_kb(),
        )
        return

    if data == "menu:status":
        await query.edit_message_text(
            my_status_text(update),
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return

    if data == "menu:deals":
        await query.edit_message_text(
            my_deals_text(update),
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return

    if data == "menu:pending":
        await query.edit_message_text(
            pending_deals_text(update),
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return

    if data == "menu:global":
        await query.edit_message_text(
            global_status_text(),
            parse_mode=ParseMode.HTML,
            reply_markup=back_kb(),
        )
        return


# ============================================================
# TEXT INPUT HANDLER
# ============================================================

async def text_input_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    remember_user(update)
    text = update.message.text.strip()

    if text.startswith("/"):
        return

    # --------------------------------------------------------
    # Step 1: amount after currency button
    # --------------------------------------------------------
    state = context.user_data.get("new_deal")
    if state and state.get("step") == "amount":
        amount = extract_amount(text)

        if amount <= 0:
            await update.message.reply_text(
                "❌ Valid amount bhejo.\nExample: <code>1</code>, <code>8.1</code>, <code>100</code>",
                parse_mode=ParseMode.HTML,
            )
            return

        state["amount"] = amount
        state["step"] = "form"

        await update.message.reply_text(
            form_text(
                state["currency"],
                amount,
                state["escrower"],
            ),
            parse_mode=ParseMode.HTML,
        )

        await update.message.reply_text(
            "Fill the rest Form properly :\n\n"
            "Buyer, Seller, Item aur Terms complete karke "
            "is form ko <b>reply</b> me bhejo.",
            parse_mode=ParseMode.HTML,
        )
        return

    # --------------------------------------------------------
    # Step 2: filled form
    # --------------------------------------------------------
    parsed = parse_form(text)
    if not parsed:
        return

    reply = update.message.reply_to_message
    reply_text = (reply.text or "") if reply else ""

    # Filled form must be connected to our active wizard, or include the form heading.
    active_state = context.user_data.get("new_deal")
    is_expected_reply = bool(reply and FORM_TITLE.lower() in reply_text.lower())
    is_nt_form = FORM_TITLE.lower() in text.lower()

    if not (is_expected_reply or is_nt_form):
        return

    if not active_state:
        await update.message.reply_text("❌ Form session expire ho gayi. Dobara /add use karo.")
        return

    if active_state.get("step") != "form":
        return

    # Currency and amount are locked from the wizard.
    if parsed["currency"] != active_state["currency"]:
        await update.message.reply_text("❌ Deal Type change nahi kar sakte. Dobara /add use karo.")
        return

    wizard_amount = float(active_state["amount"])
    if abs(parsed["amount"] - wizard_amount) > 1e-9:
        await update.message.reply_text(
            f"❌ Amount must remain {fmt(wizard_amount, active_state['currency'])}."
        )
        return

    tid = next_trade_id()
    fee_percent = DEFAULT_FEE_PERCENT
    fee_amount = wizard_amount * fee_percent / 100
    release_amount = max(0.0, wizard_amount - fee_amount)

    DEALS[tid] = {
        "buyer": parsed["buyer"],
        "seller": parsed["seller"],
        "item": parsed["item"],
        "terms": parsed["terms"],
        "amount": wizard_amount,
        "currency": active_state["currency"],
        "fee_percent": fee_percent,
        "fee_amount": fee_amount,
        "release": release_amount,
        "released": None,
        "refunded": None,
        "status": "ACTIVE",
        "escrowed_by": active_state["escrower"],
        "created_by_id": active_state["creator_id"],
        "chat_id": active_state["chat_id"],
        "created_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "votes": {"release": [], "refund": []},
    }

    save_deal(tid)
    context.user_data.pop("new_deal", None)

    await update.message.reply_text(
        payment_received_text(tid, DEALS[tid]),
        parse_mode=ParseMode.HTML,
        reply_markup=deal_action_kb(tid),
    )


# ============================================================
# /CLOSE
# ============================================================

def extract_tid_from_reply(text):
    match = re.search(rf"\b({re.escape(TRADE_PREFIX)}-\d+)\b", text or "", re.IGNORECASE)
    return match.group(1).upper() if match else None


async def close_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    allowed, reason = await add_close_allowed(update, context)
    if not allowed:
        if reason:
            await update.message.reply_text(reason)
        return

    tid = None
    action_arg = None

    if context.args and re.fullmatch(
        rf"{re.escape(TRADE_PREFIX)}-\d+",
        context.args[0],
        re.IGNORECASE,
    ):
        tid = context.args[0].upper()
        if len(context.args) > 1:
            action_arg = context.args[1].lower()

    elif update.message.reply_to_message:
        tid = extract_tid_from_reply(update.message.reply_to_message.text or "")
        if context.args:
            action_arg = context.args[0].lower()

    if not tid:
        await update.message.reply_text(
            "Usage:\n"
            f"<code>/close {TRADE_PREFIX}-1</code>\n"
            f"<code>/close {TRADE_PREFIX}-1 refund</code>\n\n"
            "Ya Payment received message par reply:\n"
            "<code>/close</code>\n"
            "<code>/close refund</code>",
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

    if deal.get("status") != "ACTIVE":
        await update.message.reply_text(f"❌ Deal already {deal.get('status')}.")
        return

    requested_action = "refund" if action_arg in ("refund", "cancel") else "release"

    # Safety: /close requires both parties to agree to the same action.
    votes = deal.get("votes", {})
    party_names = {
        str(deal.get("buyer", "")).lower(),
        str(deal.get("seller", "")).lower(),
    }
    voted_names = {
        str(v.get("username", "")).lower()
        for v in votes.get(requested_action, [])
    }

    if not party_names.issubset(voted_names):
        await update.message.reply_text(
            f"❌ Buyer aur Seller dono ne abhi <b>{requested_action.title()}</b> confirm nahi kiya.",
            parse_mode=ParseMode.HTML,
        )
        return

    deal["completed_at"] = datetime.now(timezone.utc).isoformat()
    deal["closed_by_id"] = closer_id
    deal["closed_by"] = resolve_username(update)

    if requested_action == "release":
        deal["status"] = "COMPLETED"
        deal["released"] = float(deal.get("release", 0))
        save_deal(tid)

        await update.message.reply_text(
            completed_text(tid, deal),
            parse_mode=ParseMode.HTML,
        )
        await update.message.reply_text(
            vouch_text(deal),
            parse_mode=ParseMode.HTML,
        )

    else:
        deal["status"] = "REFUNDED"
        # Refund returns the full recorded amount by default.
        deal["refunded"] = float(deal.get("amount", 0))
        save_deal(tid)

        await update.message.reply_text(
            refund_text(tid, deal),
            parse_mode=ParseMode.HTML,
        )

    try:
        await update.message.delete()
    except Exception:
        pass


# ============================================================
# STATUS / DASHBOARD
# ============================================================

def my_status_text(update: Update):
    username = resolve_username(update)
    mine = [d for d in DEALS.values() if d.get("escrowed_by") == username]

    active = [d for d in mine if d.get("status") == "ACTIVE"]
    completed = [d for d in mine if d.get("status") == "COMPLETED"]

    totals = {c: 0.0 for c in SUPPORTED_CURRENCIES}
    for deal in completed:
        currency = deal.get("currency")
        if currency in totals:
            totals[currency] += float(deal.get("amount", 0))

    return (
        f"📊 <b>{esc(update.effective_user.first_name)} Deal Status</b>\n"
        "─────────────────\n"
        f"🟡 Active Deals: {len(active)}\n"
        f"✅ Completed Deals: {len(completed)}\n\n"
        "💰 <b>Completed Volume:</b>\n"
        f"🪙 TON: {totals['TON']:g} TON\n"
        f"💵 USDT: {totals['USDT']:g} USDT\n"
        f"₹ INR: ₹{totals['INR']:g}\n"
        "─────────────────\n"
        f"🛡 {BRAND}"
    )


def my_deals_text(update: Update):
    username = resolve_username(update)
    rows = [
        (tid, deal)
        for tid, deal in DEALS.items()
        if deal.get("escrowed_by") == username
    ]

    if not rows:
        return "📭 <b>My Deals</b>\n\nKoi deal nahi mili."

    lines = ["📁 <b>My Deals</b>", "─────────────────"]
    for tid, deal in reversed(rows[-20:]):
        lines.append(
            f"<code>{esc(tid)}</code> — {esc(deal.get('status'))} — "
            f"{esc(fmt(deal.get('amount'), deal.get('currency')))}"
        )
    return "\n".join(lines)


def pending_deals_text(update: Update):
    username = resolve_username(update)
    rows = [
        (tid, deal)
        for tid, deal in DEALS.items()
        if deal.get("escrowed_by") == username and deal.get("status") == "ACTIVE"
    ]

    if not rows:
        return "📭 Koi pending deal nahi hai."

    lines = ["⏳ <b>My Pending Deals</b>", "─────────────────"]
    for tid, deal in reversed(rows):
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"{esc(deal.get('buyer'))} ↔ {esc(deal.get('seller'))} — "
            f"{esc(fmt(deal.get('amount'), deal.get('currency')))}"
        )
    return "\n".join(lines)


def global_status_text():
    completed = [d for d in DEALS.values() if d.get("status") == "COMPLETED"]
    active = [d for d in DEALS.values() if d.get("status") == "ACTIVE"]

    totals = {c: 0.0 for c in SUPPORTED_CURRENCIES}
    for deal in completed:
        currency = deal.get("currency")
        if currency in totals:
            totals[currency] += float(deal.get("amount", 0))

    return (
        "🌐 <b>Escrow Global Statistics</b>\n"
        "─────────────────\n"
        f"📦 Total Deals: {len(DEALS)}\n"
        f"🟡 Active: {len(active)}\n"
        f"✅ Completed: {len(completed)}\n\n"
        f"🪙 TON Volume: {totals['TON']:g} TON\n"
        f"💵 USDT Volume: {totals['USDT']:g} USDT\n"
        f"₹ INR Volume: ₹{totals['INR']:g}\n"
        "─────────────────\n"
        f"🛡 Escrow Bot for {BRAND}"
    )


async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    remember_user(update)
    await update.message.reply_text(
        my_status_text(update),
        parse_mode=ParseMode.HTML,
    )


# ============================================================
# ADMIN COMMANDS
# ============================================================

def admin_only_private(update: Update):
    return (
        update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_user
        and is_admin(update.effective_user.id)
    )


async def alldeals_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_private(update):
        return

    if not DEALS:
        await update.message.reply_text("📭 Koi deal record nahi hai.")
        return

    lines = [f"📊 <b>Total Deals:</b> {len(DEALS)}", ""]
    for tid, deal in list(DEALS.items())[-100:]:
        lines.append(
            f"<code>{esc(tid)}</code> — {esc(deal.get('status'))} — "
            f"{esc(deal.get('buyer'))} ↔ {esc(deal.get('seller'))} — "
            f"{esc(fmt(deal.get('amount'), deal.get('currency')))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def deal_lookup_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_private(update):
        return

    if not context.args:
        await update.message.reply_text(
            f"Usage: <code>/deal {TRADE_PREFIX}-1</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    tid = context.args[0].upper()
    deal = DEALS.get(tid)

    if not deal:
        await update.message.reply_text("❌ Deal not found.")
        return

    lines = [
        f"🆔 <b>{esc(tid)}</b>",
        "─────────────────",
        f"Status: {esc(deal.get('status'))}",
        f"Buyer: {esc(deal.get('buyer'))}",
        f"Seller: {esc(deal.get('seller'))}",
        f"Item: {esc(deal.get('item'))}",
        f"Amount: {esc(fmt(deal.get('amount'), deal.get('currency')))}",
        f"Fee: {deal.get('fee_percent', 0):.1f}%",
        f"Net Release: {esc(fmt(deal.get('release'), deal.get('currency')))}",
        f"Escrower: {esc(deal.get('escrowed_by'))}",
        f"Terms: {esc(deal.get('terms'))}",
    ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def hold_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_private(update):
        return

    active = [
        (tid, deal)
        for tid, deal in DEALS.items()
        if deal.get("status") == "ACTIVE"
    ]

    if not active:
        await update.message.reply_text("🛡 <b>ADMIN HOLD</b>\n\nNo active deals.", parse_mode=ParseMode.HTML)
        return

    lines = ["🛡 <b>ADMIN HOLD</b>", "─────────────────"]
    for tid, deal in active:
        lines.append(
            f"<code>{esc(tid)}</code> — "
            f"{esc(fmt(deal.get('amount'), deal.get('currency')))} — "
            f"{esc(deal.get('escrowed_by'))}"
        )

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def addadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    target = None
    if update.message.reply_to_message:
        target = update.message.reply_to_message.from_user
        target_id = target.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text(
            "Usage: reply karke <code>/addadmin</code> ya <code>/addadmin USER_ID</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    BOT_ADMINS.add(target_id)

    data = {
        "added_by": update.effective_user.id,
        "added_at": datetime.now(timezone.utc).isoformat(),
    }

    if target:
        data.update({
            "username": target.username,
            "first_name": target.first_name,
            "last_name": target.last_name,
        })

    if admins_coll is not None:
        admins_coll.update_one({"_id": target_id}, {"$set": data}, upsert=True)

    await update.message.reply_text(f"✅ <code>{target_id}</code> ab bot admin hai.", parse_mode=ParseMode.HTML)


async def removeadmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != "private" or not is_owner(update.effective_user.id):
        return

    if update.message.reply_to_message:
        target_id = update.message.reply_to_message.from_user.id
    elif context.args and context.args[0].isdigit():
        target_id = int(context.args[0])
    else:
        await update.message.reply_text("Usage: <code>/removeadmin USER_ID</code>", parse_mode=ParseMode.HTML)
        return

    if target_id in OWNER_IDS:
        await update.message.reply_text("❌ Owner ko remove nahi kar sakte.")
        return

    BOT_ADMINS.discard(target_id)
    if admins_coll is not None:
        admins_coll.delete_one({"_id": target_id})

    await update.message.reply_text(f"✅ <code>{target_id}</code> remove ho gaya.", parse_mode=ParseMode.HTML)


async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_private(update):
        return

    lines = ["👑 <b>Owners</b>"]

    for uid in sorted(OWNER_IDS):
        lines.append(f'• <a href="tg://user?id={uid}">Owner</a> <code>({uid})</code>')

    lines.append("\n🛡 <b>Bot Admins</b>")

    extras = sorted(BOT_ADMINS - OWNER_IDS)
    if not extras:
        lines.append("(koi extra admin nahi hai)")
    else:
        for uid in extras:
            doc = admins_coll.find_one({"_id": uid}) if admins_coll is not None else None
            username = doc.get("username") if doc else None
            first_name = doc.get("first_name") if doc else None

            label = first_name or (username if username else "Admin")
            if username:
                lines.append(
                    f'• <a href="https://t.me/{esc(username)}">{esc(label)}</a> '
                    f'<code>({uid})</code>'
                )
            else:
                lines.append(
                    f'• <a href="tg://user?id={uid}">{esc(label)}</a> '
                    f'<code>({uid})</code>'
                )

    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        disable_web_page_preview=True,
    )


async def broadcast_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not admin_only_private(update):
        return

    message = update.message.text.partition(" ")[2].strip()
    if not message:
        await update.message.reply_text("Usage: /broadcast <message>")
        return

    if users_coll is None:
        await update.message.reply_text("❌ MongoDB required for broadcast.")
        return

    sent = failed = 0
    for doc in users_coll.find({}, {"_id": 1}):
        try:
            await context.bot.send_message(doc["_id"], message)
            sent += 1
        except Exception:
            failed += 1

    await update.message.reply_text(f"📢 Broadcast finished.\nSent: {sent}\nFailed: {failed}")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    lines = [
        "📖 <b>NT Escrow Commands</b>",
        "─────────────────",
        "<b>User</b>",
        "/start — Dashboard",
        "/stats — Apna escrow status",
        "/help — Commands",
    ]

    if update.effective_user and is_admin(update.effective_user.id):
        lines += [
            "",
            "<b>Admin</b>",
            "/add — New NTwallet escrow deal",
            "/close — Confirmed deal release",
            "/close refund — Confirmed deal refund",
            "/hold — Active deals",
            "/deal ID — Deal details",
            "/alldeals — All deals",
            "/admins — Admin list",
            "/broadcast MESSAGE — Broadcast",
        ]

    if update.effective_user and is_owner(update.effective_user.id):
        lines += [
            "",
            "<b>Owner</b>",
            "/addadmin — Add bot admin",
            "/removeadmin — Remove bot admin",
        ]

    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


# ============================================================
# KEEP ALIVE
# ============================================================

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
    print(f"HTTP server listening on port {port}")


# ============================================================
# MAIN
# ============================================================

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
    app.add_handler(CommandHandler("add", add_cmd))
    app.add_handler(CommandHandler("close", close_cmd))
    app.add_handler(CommandHandler("stats", stats_cmd))
    app.add_handler(CommandHandler("hold", hold_cmd))
    app.add_handler(CommandHandler("alldeals", alldeals_cmd))
    app.add_handler(CommandHandler("deal", deal_lookup_cmd))
    app.add_handler(CommandHandler("admins", admins_cmd))
    app.add_handler(CommandHandler("addadmin", addadmin_cmd))
    app.add_handler(CommandHandler("removeadmin", removeadmin_cmd))
    app.add_handler(CommandHandler("broadcast", broadcast_cmd))
    app.add_handler(CommandHandler("help", help_cmd))

    app.add_handler(CallbackQueryHandler(callback_router))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_input_handler))

    print("NTescrowbot Running...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
