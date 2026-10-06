"""Admin panel (V2). Every callback re-checks admin ID server-side."""
import hashlib
import io
import logging
import secrets

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from db import Database
from supplier import get_supplier, sync_supplier_stock
from utils import (
    back_menu_keyboard, bdt, esc, fmt_dt, money, parse_amount, parse_percent,
    parse_price, sign_cb, stars, stock_emoji,
)
from .common import notify_admins

log = logging.getLogger(__name__)

S = lambda p: sign_cb(config.hmac_secret, p)  # noqa: E731

DEFAULT_TERMS = "Digital product: no refund or replacement after delivery."

SETTING_DEFS = [
    ("usd_rate", "💱 USD → BDT rate", "number"),
    ("min_deposit", "⬇️ Min deposit (USD)", "number"),
    ("max_deposit", "⬆️ Max deposit (USD)", "number"),
    ("bkash_number", "📱 bKash number", "text"),
    ("nagad_number", "📱 Nagad number", "text"),
    ("rocket_number", "📱 Rocket number", "text"),
    ("binance_user", "🟡 Binance username/UID", "text"),
    ("bybit_user", "⚫ Bybit username/UID", "text"),
    ("cashout_bkash", "💸 bKash cash-out %", "percent"),
    ("cashout_nagad", "💸 Nagad cash-out %", "percent"),
    ("cashout_rocket", "💸 Rocket cash-out %", "percent"),
    ("force_join_link", "📢 Force-join channel link", "text"),
    ("low_stock_threshold", "⚠️ Low-stock alert at", "int"),
    ("supplier_base_url", "🔌 Supplier base URL", "text"),
    ("supplier_api_key", "🔑 Supplier API key", "text"),
]
SETTING_LABEL = {k: label for k, label, _ in SETTING_DEFS}
SETTING_KIND = {k: kind for k, _, kind in SETTING_DEFS}


def _admin_only(user_id: int) -> bool:
    return config.is_admin(user_id)


async def cmd_admin(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _admin_only(update.effective_user.id):
        await update.message.reply_text("⛔ Admin only.")
        return
    await show_menu(update.message, context)


async def show_menu(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    kb = [
        [InlineKeyboardButton("👥 Users", callback_data=S("a:users:0")),
         InlineKeyboardButton("💰 Deposits", callback_data=S("a:deps"))],
        [InlineKeyboardButton("📦 Products", callback_data=S("a:prods")),
         InlineKeyboardButton("⭐ Reviews", callback_data=S("a:revs"))],
        [InlineKeyboardButton("⚙️ Payment settings", callback_data=S("a:pay")),
         InlineKeyboardButton("🔑 API keys", callback_data=S("a:keys"))],
        [InlineKeyboardButton("📊 Stats", callback_data=S("a:stats")),
         InlineKeyboardButton("🔄 Sync supplier", callback_data=S("a:sync"))],
        [InlineKeyboardButton("📢 Broadcast", callback_data=S("a:bcast"))],
    ]
    text = "🛠 <b>Admin panel</b>"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")
    else:
        await target.reply_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def admin_menu_cb(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await show_menu(update.callback_query, context)


def _admin_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))]])


# ---------------- users ----------------
async def users_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    page = int(q.data.split(":")[2])
    per = 10
    users = await db.list_users(limit=per, offset=page * per)
    total = await db.count_users()
    lines = [f"👥 <b>Users</b> (total {total}) — page {page + 1}\n"]
    kb = []
    for u in users:
        ban = "🚫" if u["banned"] else "✅"
        uname = f"@{u['username']}" if u["username"] else f"id:{u['id']}"
        kb.append([InlineKeyboardButton(f"{ban} {uname} — {money(u['wallet_balance'])}",
                                        callback_data=S(f"a:user:{u['id']}"))])
    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("◀️", callback_data=S(f"a:users:{page - 1}")))
    if (page + 1) * per < total:
        nav.append(InlineKeyboardButton("▶️", callback_data=S(f"a:users:{page + 1}")))
    if nav:
        kb.append(nav)
    kb.append([InlineKeyboardButton("🔍 Search user", callback_data=S("a:usearch"))])
    kb.append([InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def user_detail(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int | None = None) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    if uid is None:
        uid = int(q.data.split(":")[2])
    u = await db.get_user(uid)
    if not u:
        await q.answer("User not found.", show_alert=True)
        return
    text = (
        f"👤 <b>User</b> <code>{u['id']}</code>\n"
        f"Username: @{esc(u['username']) if u['username'] else '-'}\n"
        f"💰 Balance: <b>{money(u['wallet_balance'])}</b>\n"
        f"🎁 Referral earnings: {money(u['referral_earnings'])}\n"
        f"🛒 Total spent: {money(u['total_spent'])}\n"
        f"Status: {'🚫 BANNED' if u['banned'] else '✅ Active'}\n"
        f"Joined: {fmt_dt(u['created_at'])}"
    )
    kb = [
        [InlineKeyboardButton("➕ Add balance", callback_data=S(f"a:ubal:{uid}:+")),
         InlineKeyboardButton("➖ Remove balance", callback_data=S(f"a:ubal:{uid}:-"))],
        [InlineKeyboardButton("🚫 Ban" if not u["banned"] else "✅ Unban",
                              callback_data=S(f"a:uban:{uid}"))],
        [InlineKeyboardButton("⬅️ Users", callback_data=S("a:users:0"))],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def user_balance_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, _, uid_s, sign = q.data.split(":")
    context.user_data["awaiting"] = {"kind": "ubal", "user_id": int(uid_s), "sign": sign}
    what = "add to" if sign == "+" else "remove from"
    await q.edit_message_text(
        f"Type the USD amount to {what} user <code>{uid_s}</code>'s wallet:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S(f"a:user:{uid_s}"))]]),
        parse_mode="HTML",
    )


async def user_ban_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    uid = int(q.data.split(":")[2])
    u = await db.get_user(uid)
    if not u:
        await q.answer("User not found.", show_alert=True)
        return
    await db.set_banned(uid, not u["banned"])
    await q.answer("🚫 Banned." if not u["banned"] else "✅ Unbanned.")
    await user_detail(update, context, uid=uid)


async def user_search_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    context.user_data["awaiting"] = {"kind": "usearch"}
    await q.edit_message_text(
        "🔍 Type a Telegram user ID or @username:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S("a:users:0"))]]),
    )


# ---------------- deposits ----------------
async def deposits_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    rows = await db.list_pending_deposits()
    if not rows:
        await q.edit_message_text("💰 No pending deposits.",
                                  reply_markup=_admin_kb(), parse_mode="HTML")
        return
    lines = ["💰 <b>Pending deposits:</b>\n"]
    kb = []
    for d in rows:
        uname = f"@{d['username']}" if d["username"] else str(d["user_id"])
        lines.append(f"#{d['id']} — <b>{money(d['amount'])}</b> via {esc(d['method'])} "
                     f"by {esc(uname)}\n   🔖 <code>{esc(d['trxid'] or '-')}</code>")
        kb.append([InlineKeyboardButton(f"#{d['id']} {money(d['amount'])} {d['method']}",
                                        callback_data=S(f"a:dep:{d['id']}"))])
    kb.append([InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def deposit_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    dep_id = int(q.data.split(":")[2])
    rows = {d["id"]: d for d in await db.list_pending_deposits()}
    d = rows.get(dep_id)
    if not d:
        await q.answer("Deposit not found or already handled.", show_alert=True)
        return
    text = (
        f"💰 <b>Deposit #{d['id']}</b>\n"
        f"👤 User: <code>{d['user_id']}</code> (@{esc(d['username']) if d['username'] else '-'})\n"
        f"💵 Amount: <b>{money(d['amount'])}</b>\n"
        f"🏦 Method: {esc(d['method'])}\n"
        f"🔖 TrxID: <code>{esc(d['trxid'] or '-')}</code>\n"
        f"🕘 {fmt_dt(d['created_at'])}"
    )
    kb = [[InlineKeyboardButton("✅ Approve", callback_data=S(f"a:depok:{dep_id}")),
           InlineKeyboardButton("❌ Reject", callback_data=S(f"a:depno:{dep_id}"))],
          [InlineKeyboardButton("⬅️ Deposits", callback_data=S("a:deps"))]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def deposit_approve(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    dep_id = int(q.data.split(":")[2])
    info = await db.approve_deposit(dep_id)
    if not info:
        await q.answer("Already handled.", show_alert=True)
        return
    await q.edit_message_text(f"✅ Deposit #{dep_id} approved. User credited {money(info['amount'])}.",
                              reply_markup=_admin_kb(), parse_mode="HTML")
    try:
        await context.bot.send_message(
            chat_id=info["user_id"],
            text=f"✅ <b>Deposit approved!</b>\n💰 {money(info['amount'])} added to your wallet.\n"
                 f"👛 New balance: <b>{money(info['new_balance'])}</b>",
            parse_mode="HTML",
        )
    except Exception:
        pass


async def deposit_reject(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    dep_id = int(q.data.split(":")[2])
    info = await db.reject_deposit(dep_id)
    if not info:
        await q.answer("Already handled.", show_alert=True)
        return
    await q.edit_message_text(f"❌ Deposit #{dep_id} rejected.", reply_markup=_admin_kb(),
                              parse_mode="HTML")
    try:
        await context.bot.send_message(
            chat_id=info["user_id"],
            text=f"❌ <b>Deposit #{dep_id} was rejected.</b>\n"
                 "Please check your Transaction ID and try again, or contact support.",
            parse_mode="HTML",
        )
    except Exception:
        pass


# ---------------- reviews ----------------
async def reviews_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    rows = await db.pending_reviews()
    if not rows:
        await q.edit_message_text("⭐ No pending reviews.", reply_markup=_admin_kb(),
                                  parse_mode="HTML")
        return
    lines = ["⭐ <b>Pending reviews:</b>\n"]
    kb = []
    for r in rows:
        who = "🕵️" if False else f"@{r['username']}" if r["username"] else str(r["user_id"])
        lines.append(f"#{r['id']} — {esc(r['product'])} {stars(r['stars'])} by {esc(who)}\n"
                     f"   <i>{esc(r['comment'][:120])}</i>")
        kb.append([InlineKeyboardButton(f"#{r['id']} {r['stars']}⭐ {r['product'][:18]}",
                                        callback_data=S(f"a:rev:{r['id']}"))])
    kb.append([InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def review_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    rid = int(q.data.split(":")[2])
    rows = {r["id"]: r for r in await db.pending_reviews()}
    r = rows.get(rid)
    if not r:
        await q.answer("Already handled.", show_alert=True)
        return
    text = (f"⭐ <b>Review #{r['id']}</b>\n📦 {esc(r['product'])}\n"
            f"{stars(r['stars'])}\n👤 {r['user_id']}\n<i>{esc(r['comment'])}</i>")
    kb = [[InlineKeyboardButton("✅ Approve", callback_data=S(f"a:revok:{rid}")),
           InlineKeyboardButton("❌ Reject", callback_data=S(f"a:revno:{rid}"))],
          [InlineKeyboardButton("⬅️ Reviews", callback_data=S("a:revs"))]]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def review_set(update: Update, context: ContextTypes.DEFAULT_TYPE, approve: bool) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    rid = int(q.data.split(":")[2])
    ok = await db.set_review_status(rid, "approved" if approve else "rejected")
    await q.edit_message_text(
        f"{'✅ Review approved.' if ok and approve else '❌ Review rejected.' if ok else 'Already handled.'}",
        reply_markup=_admin_kb(), parse_mode="HTML")


# ---------------- products ----------------
async def products_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    products = await db.list_products_with_stock(active_only=False)
    kb = []
    lines = ["📦 <b>Products</b>\n"]
    for p in products:
        state = "" if p["active"] else " (disabled)"
        kb.append([InlineKeyboardButton(
            f"{stock_emoji(p['stock'])} {p['name'][:25]} ({p['stock']}){state}",
            callback_data=S(f"a:prod:{p['id']}"))])
    kb.append([InlineKeyboardButton("➕ Add product", callback_data=S("a:padd"))])
    kb.append([InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))])
    if not products:
        lines.append("<i>No products yet.</i>")
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def product_detail(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[2])
    p = await db.get_product(pid)
    if not p:
        await q.answer("Not found.", show_alert=True)
        return
    stock = await db.count_stock(pid)
    avg, count = await db.avg_stars(pid)
    text = (
        f"📦 <b>{esc(p['name'])}</b> (#{pid})\n"
        f"💰 {money(p['price'])} | {stock_emoji(stock)} Stock: {stock} | "
        f"{'✅ Active' if p['active'] else '🚫 Disabled'}\n"
        f"⭐ {avg:.1f} ({count}) | SKU: {esc(p['supplier_sku'] or '-')}\n"
        f"📝 {esc(p['description'][:200])}"
    )
    kb = [
        [InlineKeyboardButton("✏️ Name", callback_data=S(f"a:pname:{pid}")),
         InlineKeyboardButton("💰 Price", callback_data=S(f"a:pprice:{pid}"))],
        [InlineKeyboardButton("📝 Description", callback_data=S(f"a:pdesc:{pid}")),
         InlineKeyboardButton("📖 How to use", callback_data=S(f"a:phow:{pid}"))],
        [InlineKeyboardButton("📜 Terms", callback_data=S(f"a:pterms:{pid}")),
         InlineKeyboardButton("🔌 Supplier SKU", callback_data=S(f"a:psku:{pid}"))],
        [InlineKeyboardButton("📥 Add stock (.txt)", callback_data=S(f"a:stockadd:{pid}")),
         InlineKeyboardButton("🗑 Clear stock", callback_data=S(f"a:stockclear:{pid}"))],
        [InlineKeyboardButton("🚫 Disable" if p["active"] else "✅ Enable",
                              callback_data=S(f"a:ptoggle:{pid}")),
         InlineKeyboardButton("❌ Delete", callback_data=S(f"a:pdel:{pid}"))],
        [InlineKeyboardButton("⬅️ Products", callback_data=S("a:prods"))],
    ]
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def product_toggle(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[2])
    p = await db.get_product(pid)
    if p:
        await db.update_product(pid, active=0 if p["active"] else 1)
    await product_detail(update, context)


async def product_delete_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    pid = q.data.split(":")[2]
    kb = [[InlineKeyboardButton("⚠️ Yes, delete", callback_data=S(f"a:pdelok:{pid}")),
           InlineKeyboardButton("⬅️ Cancel", callback_data=S(f"a:prod:{pid}"))]]
    await q.edit_message_text("⚠️ Delete this product and its unused stock?",
                              reply_markup=InlineKeyboardMarkup(kb))


async def product_delete_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    await db.delete_product(int(q.data.split(":")[2]))
    await q.answer("Deleted.")
    await products_list(update, context)


async def stock_add_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    pid = q.data.split(":")[2]
    context.user_data["awaiting"] = {"kind": "stock_upload", "product_id": int(pid)}
    await q.edit_message_text(
        "📥 Send a <b>.txt</b> file now — one serial per line.\nEmpty lines are ignored.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S(f"a:prod:{pid}"))]]),
        parse_mode="HTML",
    )


async def stock_clear_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    pid = q.data.split(":")[2]
    kb = [[InlineKeyboardButton("⚠️ Yes, clear unused stock", callback_data=S(f"a:stockclearok:{pid}")),
           InlineKeyboardButton("⬅️ Cancel", callback_data=S(f"a:prod:{pid}"))]]
    await q.edit_message_text("Clear ALL unused stock for this product?",
                              reply_markup=InlineKeyboardMarkup(kb))


async def stock_clear_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    n = await db.clear_stock(int(q.data.split(":")[2]))
    await q.answer(f"Cleared {n} items.")
    await product_detail(update, context)


async def product_add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    context.user_data["awaiting"] = {"kind": "padd_name", "data": {}}
    await q.edit_message_text(
        "➕ <b>Add product — step 1/6</b>\nType the product <b>name</b>:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S("a:prods"))]]),
        parse_mode="HTML",
    )


async def product_edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, field: str) -> None:
    q = update.callback_query
    pid = q.data.split(":")[2]
    labels = {"pname": "name", "pprice": "price", "pdesc": "description",
              "phow": "how to use", "pterms": "terms", "psku": "supplier SKU"}
    context.user_data["awaiting"] = {"kind": "pedit", "product_id": int(pid), "field": field}
    await q.edit_message_text(
        f"✏️ Type the new <b>{labels[field]}</b> (or /skip to clear):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S(f"a:prod:{pid}"))]]),
        parse_mode="HTML",
    )


# ---------------- payment settings ----------------
async def payment_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    settings = await db.all_settings()
    lines = ["⚙️ <b>Payment settings</b>\nTap a row to edit:\n"]
    kb = []
    for key, label, _ in SETTING_DEFS:
        val = settings.get(key, "")
        shown = (val[:18] + "…") if len(val) > 18 else (val or "—")
        if "api key" in label.lower() and val:
            shown = "••••••••"
        kb.append([InlineKeyboardButton(f"{label}: {shown}", callback_data=S(f"a:set:{key}"))])
    kb.append([InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def setting_edit_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    key = q.data.split(":")[2]
    label = SETTING_LABEL.get(key, key)
    current = await db.get_setting(key, "")
    if "api key" in label.lower() and current:
        current = "••••••••"
    context.user_data["awaiting"] = {"kind": "setval", "key": key}
    await q.edit_message_text(
        f"✏️ <b>{esc(label)}</b>\nCurrent: <code>{esc(current or '—')}</code>\n\nType the new value:",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S("a:pay"))]]),
        parse_mode="HTML",
    )


# ---------------- API keys ----------------
async def api_keys_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    keys = await db.list_api_keys()
    lines = ["🔑 <b>Supplier API keys</b>\n<i>Resellers authenticate with the X-API-Key header.</i>\n"]
    kb = []
    for k in keys:
        state = "✅" if k["active"] else "🚫"
        lines.append(f"{state} <b>{esc(k['name'])}</b> <code>{esc(k['key_prefix'])}…</code>")
        if k["active"]:
            kb.append([InlineKeyboardButton(f"🚫 Revoke {k['name']}",
                                            callback_data=S(f"a:keyrev:{k['id']}"))])
    kb.append([InlineKeyboardButton("➕ Generate new key", callback_data=S("a:keygen"))])
    kb.append([InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def api_keygen_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    context.user_data["awaiting"] = {"kind": "keygen_name"}
    await q.edit_message_text(
        "🔑 Type a <b>name</b> for the new API key (e.g. reseller name):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S("a:keys"))]]),
        parse_mode="HTML",
    )


async def api_key_revoke_ask(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    kid = q.data.split(":")[2]
    kb = [[InlineKeyboardButton("⚠️ Yes, revoke", callback_data=S(f"a:keyrevok:{kid}")),
           InlineKeyboardButton("⬅️ Cancel", callback_data=S("a:keys"))]]
    await q.edit_message_text("Revoke this API key? It will stop working immediately.",
                              reply_markup=InlineKeyboardMarkup(kb))


async def api_key_revoke_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    await db.revoke_api_key(int(q.data.split(":")[2]))
    await q.answer("Revoked.")
    await api_keys_list(update, context)


# ---------------- stats / sync ----------------
async def show_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    s = await db.shop_stats()
    text = (
        "📊 <b>Shop stats</b>\n\n"
        f"👥 Users: <b>{s['users']}</b> (🚫 {s['banned']} banned)\n"
        f"🧾 Orders: <b>{s['orders']}</b>\n"
        f"💰 Revenue: <b>{money(s['revenue'])}</b>\n"
        f"💳 Pending deposits: <b>{s['pending_deposits']}</b>\n"
        f"📦 Stock units: <b>{s['stock_units']}</b>"
    )
    await q.edit_message_text(text, reply_markup=_admin_kb(), parse_mode="HTML")


async def supplier_sync(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    await q.answer("🔄 Syncing…")
    settings = await db.all_settings()
    supplier = get_supplier(settings)
    result = await sync_supplier_stock(db, supplier)
    if result.get("synced"):
        text = (f"🔄 <b>Supplier sync done</b> ({esc(result['supplier'])})\n"
                f"Updated: {result['updated']}, skipped: {result['skipped']}")
    else:
        text = f"🔄 Supplier sync skipped: {esc(result.get('reason', ''))}"
    await q.edit_message_text(text, reply_markup=_admin_kb(), parse_mode="HTML")


# ---------------- broadcast ----------------
async def broadcast_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    kb = [[InlineKeyboardButton("📝 Text message", callback_data=S("a:bcastt")),
           InlineKeyboardButton("🖼 Photo + caption", callback_data=S("a:bcastp"))],
          [InlineKeyboardButton("🛠 Admin menu", callback_data=S("a:menu"))]]
    await q.edit_message_text("📢 <b>Broadcast</b> — choose type:",
                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def broadcast_text_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    context.user_data["awaiting"] = {"kind": "bcast_text"}
    await q.edit_message_text("📝 Type the broadcast <b>text</b>:",
                              reply_markup=InlineKeyboardMarkup(
                                  [[InlineKeyboardButton("❌ Cancel", callback_data=S("a:menu"))]]),
                              parse_mode="HTML")


async def broadcast_photo_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    context.user_data["awaiting"] = {"kind": "bcast_photo"}
    await q.edit_message_text("🖼 Send the <b>photo</b> now:",
                              reply_markup=InlineKeyboardMarkup(
                                  [[InlineKeyboardButton("❌ Cancel", callback_data=S("a:menu"))]]),
                              parse_mode="HTML")


async def broadcast_confirm(target, context, kind: str, preview_text: str) -> None:
    context.user_data["awaiting"] = {"kind": "bcast_confirm", "bkind": kind,
                                     "text": context.user_data["awaiting"].get("text"),
                                     "photo": context.user_data["awaiting"].get("photo")}
    kb = [[InlineKeyboardButton("✅ Send to all users", callback_data=S("a:bcastok")),
           InlineKeyboardButton("❌ Cancel", callback_data=S("a:menu"))]]
    await target.reply_text(f"📢 <b>Preview:</b>\n\n{preview_text}\n\nSend?",
                            reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def broadcast_do(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    awaiting = context.user_data.pop("awaiting", None)
    if not awaiting or awaiting.get("kind") != "bcast_confirm":
        await q.answer("Expired.", show_alert=True)
        return
    await q.edit_message_text("📢 Broadcasting…")
    user_ids = await db.all_user_ids()
    ok, fail = 0, 0
    for uid in user_ids:
        try:
            if awaiting["bkind"] == "photo":
                await context.bot.send_photo(chat_id=uid, photo=awaiting["photo"],
                                             caption=awaiting.get("text") or None,
                                             parse_mode="HTML")
            else:
                await context.bot.send_message(chat_id=uid, text=awaiting["text"],
                                               parse_mode="HTML",
                                               disable_web_page_preview=True)
            ok += 1
        except Exception:
            fail += 1  # skip failed chats, never crash
    await q.edit_message_text(f"📢 Broadcast done: ✅ {ok} sent, ❌ {fail} failed.",
                              reply_markup=_admin_kb(), parse_mode="HTML")


# ---------------- text input router ----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    kind = awaiting.get("kind")
    text = (update.message.text or "").strip()

    async def cancel_to(cb_data: str):
        context.user_data.pop("awaiting", None)

    if kind == "usearch":
        context.user_data.pop("awaiting", None)
        results = await db.search_users(text)
        if not results:
            await update.message.reply_text("🔍 No users found.")
            return
        kb = [[InlineKeyboardButton(
            f"{'🚫' if u['banned'] else '✅'} {u['username'] or u['id']} — {money(u['wallet_balance'])}",
            callback_data=S(f"a:user:{u['id']}"))] for u in results]
        kb.append([InlineKeyboardButton("⬅️ Users", callback_data=S("a:users:0"))])
        await update.message.reply_text("🔍 <b>Results:</b>",
                                        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif kind == "ubal":
        amount = parse_amount(text, 0.01, 100000)
        if amount is None:
            await update.message.reply_text("❌ Invalid amount. Enter a number like 5 or 12.50.")
            return
        delta = amount if awaiting["sign"] == "+" else -amount
        context.user_data.pop("awaiting", None)
        new_bal = await db.add_balance(awaiting["user_id"], delta)
        await update.message.reply_text(
            f"✅ Balance updated. New balance: <b>{money(new_bal)}</b>", parse_mode="HTML")

    elif kind == "padd_name":
        if not text or len(text) > 100:
            await update.message.reply_text("❌ Name must be 1–100 characters.")
            return
        awaiting["data"]["name"] = text
        awaiting["kind"] = "padd_price"
        await update.message.reply_text("➕ <b>Step 2/6</b> — type the <b>price</b> in USD (e.g. 4.99):",
                                        parse_mode="HTML")

    elif kind == "padd_price":
        price = parse_price(text)
        if price is None:
            await update.message.reply_text("❌ Invalid price. Enter a number like 4.99.")
            return
        awaiting["data"]["price"] = price
        awaiting["kind"] = "padd_desc"
        await update.message.reply_text("➕ <b>Step 3/6</b> — type a short <b>description</b> (or /skip):",
                                        parse_mode="HTML")

    elif kind == "padd_desc":
        awaiting["data"]["description"] = "" if text == "/skip" else text[:500]
        awaiting["kind"] = "padd_how"
        await update.message.reply_text("➕ <b>Step 4/6</b> — <b>how to use</b> text (or /skip):",
                                        parse_mode="HTML")

    elif kind == "padd_how":
        awaiting["data"]["how_to_use"] = "" if text == "/skip" else text[:1000]
        awaiting["kind"] = "padd_terms"
        await update.message.reply_text(
            "➕ <b>Step 5/6</b> — <b>terms</b> (or /skip for default):", parse_mode="HTML")

    elif kind == "padd_terms":
        awaiting["data"]["terms"] = DEFAULT_TERMS if text == "/skip" else text[:1000]
        awaiting["kind"] = "padd_sku"
        await update.message.reply_text("➕ <b>Step 6/6</b> — supplier SKU (or /skip):",
                                        parse_mode="HTML")

    elif kind == "padd_sku":
        d = awaiting["data"]
        d["supplier_sku"] = "" if text == "/skip" else text[:100]
        context.user_data.pop("awaiting", None)
        pid = await db.add_product(d["name"], d["price"], d["description"],
                                   d["how_to_use"], d["terms"], d["supplier_sku"])
        await update.message.reply_text(
            f"✅ Product <b>#{pid}</b> created. Now add stock via Products → Add stock.",
            parse_mode="HTML")

    elif kind == "pedit":
        pid, field = awaiting["product_id"], awaiting["field"]
        value = "" if text == "/skip" else text
        if field == "pprice":
            price = parse_price(value)
            if price is None and value:
                await update.message.reply_text("❌ Invalid price.")
                return
            await db.update_product(pid, price=price or 0)
        else:
            col = {"pname": "name", "pdesc": "description", "phow": "how_to_use",
                   "pterms": "terms", "psku": "supplier_sku"}[field]
            if col == "name" and (not value or len(value) > 100):
                await update.message.reply_text("❌ Name must be 1–100 characters.")
                return
            await db.update_product(pid, **{col: value[:1000]})
        context.user_data.pop("awaiting", None)
        await update.message.reply_text("✅ Updated.")

    elif kind == "setval":
        key = awaiting["key"]
        kind_t = SETTING_KIND.get(key, "text")
        ok, val = True, text
        if kind_t == "number":
            try:
                v = float(text)
                assert v >= 0
                val = str(round(v, 4))
            except (ValueError, AssertionError):
                ok = False
        elif kind_t == "percent":
            v = parse_percent(text)
            ok, val = (v is not None), (str(v) if v is not None else text)
        elif kind_t == "int":
            ok, val = (text.isdigit() and int(text) >= 0), text
        if not ok:
            await update.message.reply_text("❌ Invalid value. Try again.")
            return
        context.user_data.pop("awaiting", None)
        await db.set_setting(key, val)
        await update.message.reply_text(f"✅ <b>{esc(SETTING_LABEL.get(key, key))}</b> set to "
                                        f"<code>{esc(val)}</code>.", parse_mode="HTML")

    elif kind == "keygen_name":
        if not text or len(text) > 60:
            await update.message.reply_text("❌ Name must be 1–60 characters.")
            return
        context.user_data.pop("awaiting", None)
        raw = "psk_" + secrets.token_urlsafe(24)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        await db.create_api_key(text, digest, raw[:12])
        await update.message.reply_text(
            "🔑 <b>API key created!</b>\n\n"
            f"Name: <b>{esc(text)}</b>\n"
            f"Key: <code>{esc(raw)}</code>\n\n"
            "⚠️ Copy it now — it won't be shown again.\n"
            "Use it as the <b>X-API-Key</b> header.",
            parse_mode="HTML",
        )

    elif kind == "bcast_text":
        context.user_data["awaiting"] = {"kind": "bcast_confirm", "bkind": "text", "text": text}
        kb = [[InlineKeyboardButton("✅ Send to all users", callback_data=S("a:bcastok")),
               InlineKeyboardButton("❌ Cancel", callback_data=S("a:menu"))]]
        await update.message.reply_text(f"📢 <b>Preview:</b>\n\n{esc(text)}\n\nSend?",
                                        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")

    elif kind == "bcast_caption":
        context.user_data["awaiting"] = {"kind": "bcast_confirm", "bkind": "photo",
                                         "photo": awaiting["photo"],
                                         "text": "" if text == "/skip" else text}
        kb = [[InlineKeyboardButton("✅ Send to all users", callback_data=S("a:bcastok")),
               InlineKeyboardButton("❌ Cancel", callback_data=S("a:menu"))]]
        await update.message.reply_text("📢 Photo ready. Send to all users?",
                                        reply_markup=InlineKeyboardMarkup(kb))


# ---------------- document / photo routers ----------------
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    awaiting = context.user_data.get("awaiting")
    if not awaiting or awaiting.get("kind") != "stock_upload":
        return
    doc = update.message.document
    if not doc or not (doc.file_name or "").lower().endswith(".txt"):
        await update.message.reply_text("❌ Please send a <b>.txt</b> file.", parse_mode="HTML")
        return
    if doc.file_size and doc.file_size > 5 * 1024 * 1024:
        await update.message.reply_text("❌ File too large (max 5 MB).")
        return
    f = await doc.get_file()
    bio = io.BytesIO()
    await f.download_to_memory(bio)
    try:
        lines = bio.getvalue().decode("utf-8", errors="ignore").splitlines()
    except Exception:
        lines = []
    pid = awaiting["product_id"]
    context.user_data.pop("awaiting", None)
    n = await db.add_stock(pid, lines)
    if n == 0:
        await update.message.reply_text("⚠️ No valid serials found in the file (empty lines ignored).")
        return
    total = await db.count_stock(pid)
    await update.message.reply_text(f"✅ Added <b>{n}</b> serials. Total stock now: <b>{total}</b>.",
                                    parse_mode="HTML")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    awaiting = context.user_data.get("awaiting")
    if not awaiting or awaiting.get("kind") != "bcast_photo":
        return
    photo = update.message.photo[-1].file_id
    context.user_data["awaiting"] = {"kind": "bcast_caption", "photo": photo}
    await update.message.reply_text("🖼 Got the photo. Now type a <b>caption</b> (or /skip for none):",
                                    parse_mode="HTML")
