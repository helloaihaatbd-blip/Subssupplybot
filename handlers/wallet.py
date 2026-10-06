"""Wallet (V2): balance, manual deposits with BDT calculator, history, referral."""
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from db import Database
from utils import back_menu_keyboard, bdt, esc, fmt_dt, is_trxid, money, parse_amount, sign_cb
from .common import get_bot_username, notify_admins, referral_link

log = logging.getLogger(__name__)

S = lambda p: sign_cb(config.hmac_secret, p)  # noqa: E731

METHODS = {
    "bkash": {"label": "bKash", "num_key": "bkash_number", "cashout_key": "cashout_bkash"},
    "nagad": {"label": "Nagad", "num_key": "nagad_number", "cashout_key": "cashout_nagad"},
    "rocket": {"label": "Rocket", "num_key": "rocket_number", "cashout_key": "cashout_rocket"},
    "binance": {"label": "Binance", "num_key": "binance_user", "cashout_key": None},
    "bybit": {"label": "Bybit", "num_key": "bybit_user", "cashout_key": None},
}

STATUS_EMOJI = {"pending": "🟡 Pending", "approved": "✅ Approved", "rejected": "❌ Rejected"}


async def show_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    u = await db.get_user(q.effective_user.id)
    bal = float(u["wallet_balance"]) if u else 0.0
    earn = float(u["referral_earnings"]) if u else 0.0
    spent = float(u["total_spent"]) if u else 0.0
    text = (
        "👛 <b>Wallet</b>\n\n"
        f"💰 Balance: <b>{money(bal)}</b>\n"
        f"🎁 Referral earnings: <b>{money(earn)}</b>\n"
        f"🛒 Total spent: <b>{money(spent)}</b>\n"
    )
    kb = [
        [InlineKeyboardButton("💳 Deposit", callback_data=S("w:deposit")),
         InlineKeyboardButton("🕘 Deposit History", callback_data=S("w:hist"))],
        [InlineKeyboardButton("👥 Referral", callback_data=S("w:ref"))],
        [InlineKeyboardButton("🏠 Main menu", callback_data=S("m:start"))],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ---------------- deposit ----------------
async def deposit_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    min_d = await db.get_setting("min_deposit", "0.20")
    max_d = await db.get_setting("max_deposit", "100")
    kb = []
    row = []
    for key, m in METHODS.items():
        row.append(InlineKeyboardButton(m["label"], callback_data=S(f"d:{key}")))
        if len(row) == 2:
            kb.append(row)
            row = []
    if row:
        kb.append(row)
    kb.append([InlineKeyboardButton("⬅️ Wallet", callback_data=S("m:wallet"))])
    await q.edit_message_text(
        f"💳 <b>Deposit</b>\n\nChoose a payment method "
        f"(min <b>{money(float(min_d))}</b>, max <b>{money(float(max_d))}</b>):",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
    )


async def method_chosen(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    method = q.data.split(":")[1]
    m = METHODS.get(method)
    if not m:
        await q.answer("Unknown method.", show_alert=True)
        return
    number = await db.get_setting(m["num_key"], "")
    if not number:
        await q.edit_message_text(
            f"⚠️ {m['label']} deposits are not configured right now. Please choose another method.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Back", callback_data=S("w:deposit"))]]),
            parse_mode="HTML",
        )
        return
    min_d = float(await db.get_setting("min_deposit", "0.20") or 0.20)
    max_d = float(await db.get_setting("max_deposit", "100") or 100)
    context.user_data["awaiting"] = {"kind": "dep_amount", "method": method}
    await q.edit_message_text(
        f"💳 <b>Deposit via {m['label']}</b>\n\n"
        f"Type the amount in USD (min {money(min_d)}, max {money(max_d)}):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S("w:deposit"))]]),
        parse_mode="HTML",
    )


async def deposit_amount_entered(message, context, db, method: str, amount: float) -> None:
    m = METHODS[method]
    number = await db.get_setting(m["num_key"], "")
    usd_rate = float(await db.get_setting("usd_rate", "120") or 120)
    cashout = float(await db.get_setting(m["cashout_key"], "0") or 0) if m["cashout_key"] else 0.0
    amount_bdt = round(amount * usd_rate * (1 - cashout / 100))
    label = "number" if method in ("bkash", "nagad", "rocket") else "username/UID"
    context.user_data["awaiting"] = {"kind": "dep_trxid", "method": method,
                                     "amount": amount, "amount_bdt": amount_bdt}
    text = (
        f"💳 <b>Deposit via {m['label']}</b>\n\n"
        f"💵 You pay: <b>{money(amount)}</b>\n"
        f"🇧🇩 Send: <b>{bdt(amount_bdt)}</b>"
    )
    if cashout:
        text += f" <i>(after {cashout:g}% cash-out)</i>"
    text += (
        f"\n📌 {m['label']} {label}: <code>{esc(number)}</code>\n\n"
        f"After sending, type your <b>Transaction ID</b>:"
    )
    await message.reply_text(
        text,
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S("w:deposit"))]]),
        parse_mode="HTML",
    )


async def deposit_trxid_entered(message, context, db, data: dict) -> None:
    trxid = (message.text or "").strip()
    if not is_trxid(trxid):
        await message.reply_text("❌ Invalid Transaction ID. It should be 6–25 letters/digits.")
        return
    if await db.trxid_exists(trxid):
        await message.reply_text("❌ This Transaction ID was already submitted.")
        context.user_data.pop("awaiting", None)
        return
    method = data["method"]
    dep_id = await db.create_deposit(message.from_user.id, method, data["amount"],
                                     data["amount_bdt"], trxid, status="pending")
    context.user_data.pop("awaiting", None)
    await message.reply_text(
        f"🟡 <b>Deposit submitted!</b>\n\n"
        f"🆔 Request #{dep_id}\n"
        f"💵 {money(data['amount'])} via {METHODS[method]['label']}\n"
        f"🔖 TrxID: <code>{esc(trxid)}</code>\n\n"
        f"An admin will review it shortly.",
        reply_markup=back_menu_keyboard(S), parse_mode="HTML",
    )
    await notify_admins(
        context,
        f"💰 <b>New deposit request #{dep_id}</b>\n"
        f"👤 User: <code>{message.from_user.id}</code>\n"
        f"💵 {money(data['amount'])} via {METHODS[method]['label']}\n"
        f"🔖 <code>{esc(trxid)}</code>",
        reply_markup=InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Approve", callback_data=S(f"a:depok:{dep_id}")),
            InlineKeyboardButton("❌ Reject", callback_data=S(f"a:depno:{dep_id}")),
        ]]),
    )


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    kind = awaiting.get("kind")

    if kind == "dep_amount":
        min_d = float(await db.get_setting("min_deposit", "0.20") or 0.20)
        max_d = float(await db.get_setting("max_deposit", "100") or 100)
        amount = parse_amount(update.message.text or "", min_d, max_d)
        if amount is None:
            await update.message.reply_text(
                f"❌ Invalid amount. Enter a number between {money(min_d)} and {money(max_d)}.")
            return
        await deposit_amount_entered(update.message, context, db, awaiting["method"], amount)

    elif kind == "dep_trxid":
        await deposit_trxid_entered(update.message, context, db, awaiting)


# ---------------- history ----------------
async def deposit_history(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    rows = await db.user_deposits(q.effective_user.id)
    if not rows:
        await q.edit_message_text("🕘 No deposits yet.",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    lines = ["🕘 <b>Deposit history:</b>\n"]
    for d in rows:
        status = STATUS_EMOJI.get(d["status"], d["status"])
        lines.append(
            f"#{d['id']} — <b>{money(d['amount'])}</b> via {esc(METHODS.get(d['method'], {}).get('label', d['method']))}\n"
            f"   🔖 <code>{esc(d['trxid'] or '-')}</code>\n"
            f"   <i>{fmt_dt(d['created_at'])}</i> — {status}"
        )
    kb = [[InlineKeyboardButton("⬅️ Wallet", callback_data=S("m:wallet"))]]
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


# ---------------- referral ----------------
async def referral_page(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    username = await get_bot_username(context)
    link = referral_link(username, q.effective_user.id)
    stats = await db.referral_stats(q.effective_user.id)
    text = (
        "👥 <b>Referral program</b>\n\n"
        f"🔗 Your link:\n<code>{esc(link)}</code>\n\n"
        f"👥 Invited: <b>{stats['invited']}</b>\n"
        f"✅ Joined: <b>{stats['rewarded']}</b>\n"
        f"💰 Earnings: <b>{money(stats['earnings'])}</b>\n\n"
        "<i>You earn $0.01 when someone joins via your link and completes channel join.</i>"
    )
    kb = [[InlineKeyboardButton("⬅️ Wallet", callback_data=S("m:wallet"))]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
                              disable_web_page_preview=True)
