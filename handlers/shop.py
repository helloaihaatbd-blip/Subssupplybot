"""Shop flow (V2): home -> products (single list) -> detail -> buy -> confirm -> pay."""
import io
import logging

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from config import config
from db import Database, InsufficientFunds, InsufficientStock
from utils import (
    back_menu_keyboard, esc, fmt_dt, money, parse_qty, sign_cb, stars, stock_emoji,
    verify_cb,
)
from .common import (
    check_membership, get_bot_username, get_force_join_link, invalidate_join_cache,
    is_banned, join_screen_text, notify_admins, referral_link, require_access,
)

log = logging.getLogger(__name__)

S = lambda p: sign_cb(config.hmac_secret, p)  # noqa: E731


def main_menu_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🛍 Products", callback_data=S("m:products")),
         InlineKeyboardButton("👛 Wallet", callback_data=S("m:wallet"))],
        [InlineKeyboardButton("📦 Orders", callback_data=S("m:orders")),
         InlineKeyboardButton("🆘 Support", callback_data=S("m:support"))],
    ])


async def show_home(target, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Render home into a callback query or a message."""
    text = "🏠 <b>Main menu</b>\n\nChoose an option:"
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")
    else:
        await target.reply_text(text, reply_markup=main_menu_keyboard(), parse_mode="HTML")


# ---------------- /start, referral, join ----------------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    await db.ensure_user(user.id, user.username)
    context.user_data.pop("awaiting", None)

    if await is_banned(db, user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return

    # Referral: /start ref_<id>
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_") and arg[4:].isdigit():
            ref_id = int(arg[4:])
            if ref_id != user.id:
                await db.set_referred_by(user.id, ref_id)

    link = await get_force_join_link(db)
    if not await check_membership(context.bot, user.id, link, fresh=True):
        text, kb = await join_screen_text(db)
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                        disable_web_page_preview=True)
        return

    # Already joined: settle any pending referral reward now.
    await db.mark_joined(user.id)
    referrer = await db.reward_referral(user.id, 0.01)
    if referrer:
        try:
            await context.bot.send_message(
                chat_id=referrer,
                text="🎉 <b>New referral!</b> Someone joined via your link.\n"
                     "💰 You earned <b>$0.01</b>.",
                parse_mode="HTML",
            )
        except Exception:
            pass
    await show_home(update.message, context)


async def cb_join_check(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    user = q.effective_user
    if await is_banned(db, user.id):
        await q.answer("🚫 You are banned.", show_alert=True)
        return
    invalidate_join_cache(user.id)
    link = await get_force_join_link(db)
    if await check_membership(context.bot, user.id, link, fresh=True):
        await db.mark_joined(user.id)
        referrer = await db.reward_referral(user.id, 0.01)
        if referrer:
            try:
                await context.bot.send_message(
                    chat_id=referrer,
                    text="🎉 <b>New referral!</b> Someone joined via your link.\n"
                         "💰 You earned <b>$0.01</b>.",
                    parse_mode="HTML",
                )
            except Exception:
                pass
        await q.answer("✅ Welcome!", show_alert=False)
        await show_home(q, context)
    else:
        await q.answer("❌ You haven't joined yet. Tap 'Join Community' first.",
                       show_alert=True)


# ---------------- products ----------------
async def show_products(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    products = await db.list_products_with_stock(active_only=True)
    if not products:
        await q.edit_message_text("🛍 No products available right now.",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    kb = []
    for p in products:
        label = f"{stock_emoji(p['stock'])} {p['name']} ({p['stock']}) — {money(p['price'])}"
        kb.append([InlineKeyboardButton(label, callback_data=S(f"p:{p['id']}"))])
    kb.append([InlineKeyboardButton("🏠 Main menu", callback_data=S("m:start"))])
    await q.edit_message_text("🛍 <b>Products</b>\nTap a product to view details:",
                              reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def product_text(db: Database, pid: int) -> tuple[str, InlineKeyboardMarkup] | None:
    p = await db.get_product(pid)
    if not p or not p["active"]:
        return None
    stock = await db.count_stock(pid)
    avg, count = await db.avg_stars(pid)
    rating = f"⭐ {avg:.1f} ({count})" if count else "⭐ No reviews yet"
    text = (
        f"<b>{esc(p['name'])}</b>\n"
        f"💰 Price: <b>{money(p['price'])}</b>\n"
        f"{stock_emoji(stock)} Stock: <b>{stock}</b>\n"
        f"{rating}\n"
    )
    if p["description"]:
        text += f"\n{esc(p['description'])}\n"
    if p["how_to_use"]:
        text += f"\n📖 <b>How to use:</b>\n{esc(p['how_to_use'])}\n"
    text += f"\n📜 <b>Terms:</b>\n{esc(p['terms'])}"
    kb = []
    if stock > 0:
        kb.append([InlineKeyboardButton("🛒 Buy Now", callback_data=S(f"b:{pid}"))])
    kb.append([InlineKeyboardButton("⭐ Reviews", callback_data=S(f"r:list:{pid}"))])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=S("m:products"))])
    return text, InlineKeyboardMarkup(kb)


async def show_product(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[1])
    rendered = await product_text(db, pid)
    if not rendered:
        await q.edit_message_text("❌ Product not available.",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    text, kb = rendered
    await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


# ---------------- buy flow ----------------
async def buy_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[1])
    p = await db.get_product(pid)
    stock = await db.count_stock(pid) if p else 0
    if not p or not p["active"] or stock <= 0:
        await q.edit_message_text("🔴 This product is out of stock right now.",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    kb = [
        [InlineKeyboardButton("1", callback_data=S(f"q:{pid}:1")),
         InlineKeyboardButton("5", callback_data=S(f"q:{pid}:5")),
         InlineKeyboardButton("10", callback_data=S(f"q:{pid}:10"))],
        [InlineKeyboardButton(f"All stock ({stock})", callback_data=S(f"q:{pid}:{stock}"))],
        [InlineKeyboardButton("✏️ Custom quantity", callback_data=S(f"qc:{pid}"))],
        [InlineKeyboardButton("🔄 Refresh stock", callback_data=S(f"p:{pid}"))],
        [InlineKeyboardButton("⬅️ Back", callback_data=S(f"p:{pid}"))],
    ]
    await q.edit_message_text(
        f"How many <b>{esc(p['name'])}</b>?\n{stock_emoji(stock)} Available: <b>{stock}</b> — {money(p['price'])} each",
        reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML",
    )


async def custom_qty_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[1])
    stock = await db.count_stock(pid)
    if stock <= 0:
        await q.edit_message_text("🔴 Out of stock.", reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    context.user_data["awaiting"] = {"kind": "custom_qty", "product_id": pid, "max": stock}
    await q.edit_message_text(
        f"✏️ Type the quantity (1–{stock}):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("❌ Cancel", callback_data=S(f"b:{pid}"))]]
        ),
        parse_mode="HTML",
    )


async def confirm_text(db: Database, user_id: int, pid: int, qty: int):
    p = await db.get_product(pid)
    stock = await db.count_stock(pid) if p else 0
    if not p or not p["active"]:
        return None, None
    if qty > stock:
        return "short", stock
    total = round(p["price"] * qty, 2)
    balance = await db.get_balance(user_id)
    shortfall = round(total - balance, 2) if balance < total else 0.0
    text = (
        f"🧾 <b>Confirm order</b>\n\n"
        f"📦 {esc(p['name'])}\n🔢 Quantity: <b>{qty}</b>\n"
        f"💰 Unit price: {money(p['price'])}\n🧮 Subtotal: <b>{money(total)}</b>\n\n"
        f"👛 Wallet balance: <b>{money(balance)}</b>\n"
    )
    kb = []
    if shortfall > 0:
        text += f"⚠️ Shortfall: <b>{money(shortfall)}</b> — please load your wallet.\n"
        kb.append([InlineKeyboardButton("💳 Deposit", callback_data=S("m:wallet"))])
    else:
        kb.append([InlineKeyboardButton("💸 Pay from Wallet", callback_data=S(f"pay:{pid}:{qty}"))])
    kb.append([InlineKeyboardButton("⬅️ Change quantity", callback_data=S(f"b:{pid}")),
               InlineKeyboardButton("❌ Cancel", callback_data=S(f"p:{pid}"))])
    return text, InlineKeyboardMarkup(kb)


async def confirm_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    _, pid_s, qty_s = q.data.split(":")
    pid, qty = int(pid_s), int(qty_s)
    text, kb = await confirm_text(db, q.effective_user.id, pid, qty)
    if text == "short":
        await q.edit_message_text(
            f"⚠️ Only <b>{kb}</b> left in stock. Please choose again.",
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("⬅️ Choose quantity", callback_data=S(f"b:{pid}"))]]
            ),
            parse_mode="HTML",
        )
        return
    if text is None:
        await q.edit_message_text("❌ Product not available.",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML")


async def pay_order(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    # Idempotency: ignore rapid double-taps so only ONE order is created.
    if context.user_data.get("paying"):
        await q.answer("⏳ Processing your payment…")
        return
    context.user_data["paying"] = True
    try:
        await q.answer("⏳ Processing…")
        _, pid_s, qty_s = q.data.split(":")
        pid, qty = int(pid_s), int(qty_s)
        try:
            order_id, serials, new_balance, remaining = await db.purchase(
                q.effective_user.id, pid, qty)
        except InsufficientFunds:
            await q.edit_message_text(
                "❌ Insufficient wallet balance. Please deposit and try again.",
                reply_markup=back_menu_keyboard(S), parse_mode="HTML")
            return
        except InsufficientStock as e:
            await q.edit_message_text(
                f"🔴 Not enough stock left (available: {e.available}).",
                reply_markup=back_menu_keyboard(S), parse_mode="HTML")
            return
        p = await db.get_product(pid)
        # Low-stock / out-of-stock alerts to admin
        threshold = int(await db.get_setting("low_stock_threshold", "10") or 10)
        if remaining == 0:
            await notify_admins(context, f"🔴 <b>Out of stock:</b> {esc(p['name'])} (#{pid})")
        elif remaining <= threshold:
            await notify_admins(context,
                                f"🟠 <b>Low stock:</b> {esc(p['name'])} — only {remaining} left")
        bio = io.BytesIO(("\n".join(serials) + "\n").encode("utf-8"))
        caption = (
            f"✅ <b>Order #{order_id} is paid and delivered!</b>\n\n"
            f"📦 {esc(p['name']) if p else ''} × {qty}\n"
            f"👛 New balance: <b>{money(new_balance)}</b>"
        )
        await q.message.reply_document(
            document=bio, filename=f"order_{order_id}.txt", caption=caption, parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("⬇️ Download with S.N", callback_data=S(f"dl:{order_id}"))],
                [InlineKeyboardButton("🏠 Menu", callback_data=S("m:start"))],
            ]),
        )
        await q.edit_message_text(f"✅ <b>Order #{order_id} complete!</b> Your file is above. 👆",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
    finally:
        context.user_data.pop("paying", None)


async def redownload(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    order_id = int(q.data.split(":")[1])
    order = await db.get_order(order_id)
    if not order or order["user_id"] != q.effective_user.id:
        await q.answer("❌ Order not found.", show_alert=True)
        return
    serials = await db.get_order_serials(order_id)
    if not serials:
        await q.answer("❌ No serials found for this order.", show_alert=True)
        return
    bio = io.BytesIO(("\n".join(serials) + "\n").encode("utf-8"))
    await q.answer("⬇️ Sending your file…")
    await q.message.reply_document(
        document=bio, filename=f"order_{order_id}.txt",
        caption=f"⬇️ <b>Order #{order_id}</b> — {esc(order['product'])} × {order['qty']}",
        parse_mode="HTML",
    )


# ---------------- orders / support ----------------
async def show_orders(update: Update, context: ContextTypes.DEFAULT_TYPE, recover: bool = False) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    orders = await db.list_orders(q.effective_user.id)
    if not orders:
        await q.edit_message_text("📦 You have no orders yet.",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    lines = ["📦 <b>Your orders:</b>\n"]
    kb = []
    for o in orders:
        lines.append(f"#{o['id']} — {esc(o['product'])} × {o['qty']} = <b>{money(o['total'])}</b>\n"
                     f"   <i>{fmt_dt(o['created_at'])}</i>")
        if recover:
            kb.append([InlineKeyboardButton(f"⬇️ #{o['id']} {o['product'][:20]}",
                                            callback_data=S(f"dl:{o['id']}"))])
    kb.append([InlineKeyboardButton("🏠 Main menu", callback_data=S("m:start"))])
    await q.edit_message_text("\n".join(lines), reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def show_support(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    await q.edit_message_text(
        f"🆘 <b>Support:</b> message @{esc(config.support_username)} on Telegram.",
        reply_markup=back_menu_keyboard(S), parse_mode="HTML",
    )


# ---------------- reviews ----------------
async def review_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[2])
    p = await db.get_product(pid)
    reviews = await db.approved_reviews(pid, limit=5)
    avg, count = await db.avg_stars(pid)
    text = f"⭐ <b>Reviews — {esc(p['name']) if p else ''}</b>\n"
    text += f"Average: <b>{avg:.1f}/5</b> ({count} reviews)\n\n" if count else "No reviews yet.\n\n"
    for r in reviews:
        who = "🕵️ Anonymous" if r["anonymous"] else f"👤 {esc(r['username'] or 'user')}"
        text += f"{stars(r['stars'])} {who}\n<i>{esc(r['comment'])}</i>\n\n"
    kb = []
    if await db.has_purchased(q.effective_user.id, pid) and not await db.has_reviewed(q.effective_user.id, pid):
        kb.append([InlineKeyboardButton("✍️ Leave a review", callback_data=S(f"r:new:{pid}"))])
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=S(f"p:{pid}"))])
    await q.edit_message_text(text, reply_markup=InlineKeyboardMarkup(kb), parse_mode="HTML")


async def review_new(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    pid = int(q.data.split(":")[2])
    if not await db.has_purchased(q.effective_user.id, pid):
        await q.answer("❌ Only buyers can review this product.", show_alert=True)
        return
    if await db.has_reviewed(q.effective_user.id, pid):
        await q.answer("❌ You already reviewed this product.", show_alert=True)
        return
    kb = [[InlineKeyboardButton(f"{i}⭐", callback_data=S(f"r:stars:{pid}:{i}")) for i in range(1, 6)]]
    kb.append([InlineKeyboardButton("⬅️ Back", callback_data=S(f"r:list:{pid}"))])
    await q.edit_message_text("How many stars?", reply_markup=InlineKeyboardMarkup(kb))


async def review_stars(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    _, _, pid_s, stars_s = q.data.split(":")
    context.user_data["awaiting"] = {"kind": "review_comment",
                                     "product_id": int(pid_s), "stars": int(stars_s)}
    await q.edit_message_text(
        f"You chose {stars_s}⭐. Now type your review comment (or /skip for no comment):",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Cancel", callback_data=S(f"r:list:{pid_s}"))]]
        ),
    )


async def review_anon_prompt(message, context, pid: int, stars_n: int, comment: str) -> None:
    context.user_data["awaiting"] = {"kind": "review_anon", "product_id": pid,
                                     "stars": stars_n, "comment": comment}
    kb = [[InlineKeyboardButton("🕵️ Yes, anonymous", callback_data=S(f"r:anon:{pid}:1")),
           InlineKeyboardButton("👤 No, show my name", callback_data=S(f"r:anon:{pid}:0"))]]
    await message.reply_text("Post anonymously?", reply_markup=InlineKeyboardMarkup(kb))


async def review_anon(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    q = update.callback_query
    awaiting = context.user_data.pop("awaiting", None)
    if not awaiting or awaiting.get("kind") != "review_anon":
        await q.answer("Session expired.", show_alert=True)
        return
    anon = q.data.split(":")[3] == "1"
    rid = await db.add_review(q.effective_user.id, awaiting["product_id"],
                              awaiting["stars"], awaiting["comment"], anon)
    if rid is None:
        await q.edit_message_text("❌ Could not save review (maybe you already reviewed).",
                                  reply_markup=back_menu_keyboard(S), parse_mode="HTML")
        return
    await q.edit_message_text(
        "✅ Review submitted! It will appear publicly after admin approval.",
        reply_markup=InlineKeyboardMarkup(
            [[InlineKeyboardButton("⬅️ Back to product",
                                   callback_data=S(f"p:{awaiting['product_id']}"))]]
        ),
        parse_mode="HTML",
    )
    await notify_admins(context, f"⭐ <b>New review pending approval</b> (#{rid})")


# ---------------- text input router ----------------
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    kind = awaiting.get("kind")

    if kind == "custom_qty":
        qty = parse_qty(update.message.text or "", awaiting["max"])
        context.user_data.pop("awaiting", None)
        if qty is None:
            await update.message.reply_text(
                f"❌ Invalid quantity. Type a number between 1 and {awaiting['max']}.")
            return
        text, kb = await confirm_text(db, update.effective_user.id,
                                      awaiting["product_id"], qty)
        if text == "short" or text is None:
            await update.message.reply_text("⚠️ Stock changed. Please choose again.",
                                            reply_markup=back_menu_keyboard(S))
            return
        await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML")

    elif kind == "review_comment":
        text_in = (update.message.text or "").strip()
        if text_in == "/skip":
            text_in = ""
        pid, stars_n = awaiting["product_id"], awaiting["stars"]
        await review_anon_prompt(update.message, context, pid, stars_n, text_in[:500])
