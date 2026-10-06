"""NYStore-style Telegram shop bot (V2). Entry point.

Run:
    python bot.py                 # start polling (local dev)
    python bot.py --webhook       # webhook mode (Render free tier);
                                  # needs WEBHOOK_URL (or RENDER_EXTERNAL_URL)
    python bot.py --selftest      # verify token + 15s polling, then clean exit
"""
import asyncio
import glob
import logging
import os
import sys
from datetime import datetime

from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    Defaults,
    MessageHandler,
    filters,
)

from config import config
from db import Database
from handlers import admin as admin_h
from handlers import shop as shop_h
from handlers import wallet as wallet_h
from handlers.common import is_banned, require_access
from utils import RateLimiter, verify_cb

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
log = logging.getLogger("nystore_bot")

limiter = RateLimiter(max_calls=12, window_seconds=10)


# ---------------- callback router ----------------
async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    q = update.callback_query
    user = q.effective_user

    if limiter.hit(user.id):
        try:
            await q.answer("⏳ Too fast — slow down a bit.")
        except Exception:
            pass
        return

    payload = verify_cb(config.hmac_secret, q.data or "")
    if payload is None:
        try:
            await q.answer("⚠️ Invalid button. Please use the latest menu.", show_alert=True)
        except Exception:
            pass
        return
    q.data = payload  # downstream handlers parse the verified payload

    db: Database = context.bot_data["db"]
    if await is_banned(db, user.id):
        await q.answer("🚫 You are banned from using this bot.", show_alert=True)
        return

    # Admin callbacks: re-check admin ID server-side (signature alone is not enough).
    if payload == "a:menu" or payload.startswith("a:"):
        if not config.is_admin(user.id):
            await q.answer("⛔ Admin only.", show_alert=True)
            return
        await q.answer()
        await dispatch_admin(payload, update, context)
        return

    if payload == "join:check":
        await q.answer()
        await shop_h.cb_join_check(update, context)
        return

    # Force-join gate for every other protected action.
    if not await require_access(update, context):
        return
    await q.answer()
    await dispatch_user(payload, update, context)


async def dispatch_user(payload: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if payload == "m:start":
        await shop_h.show_home(update.callback_query, context)
    elif payload == "m:products":
        await shop_h.show_products(update, context)
    elif payload.startswith("pay:"):
        await shop_h.pay_order(update, context)
    elif payload.startswith("p:"):
        await shop_h.show_product(update, context)
    elif payload.startswith("b:"):
        await shop_h.buy_menu(update, context)
    elif payload.startswith("qc:"):
        await shop_h.custom_qty_prompt(update, context)
    elif payload.startswith("q:"):
        await shop_h.confirm_order(update, context)
    elif payload.startswith("dl:"):
        await shop_h.redownload(update, context)
    elif payload == "m:wallet":
        await wallet_h.show_wallet(update, context)
    elif payload == "w:deposit":
        await wallet_h.deposit_menu(update, context)
    elif payload == "w:hist":
        await wallet_h.deposit_history(update, context)
    elif payload == "w:ref":
        await wallet_h.referral_page(update, context)
    elif payload.startswith("d:"):
        await wallet_h.method_chosen(update, context)
    elif payload == "m:orders":
        await shop_h.show_orders(update, context, recover=False)
    elif payload == "m:support":
        await shop_h.show_support(update, context)
    elif payload.startswith("r:list:"):
        await shop_h.review_list(update, context)
    elif payload.startswith("r:new:"):
        await shop_h.review_new(update, context)
    elif payload.startswith("r:stars:"):
        await shop_h.review_stars(update, context)
    elif payload.startswith("r:anon:"):
        await shop_h.review_anon(update, context)
    else:
        await update.callback_query.answer("Unknown action.", show_alert=False)


async def dispatch_admin(payload: str, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    A = admin_h
    if payload == "a:menu":
        await A.admin_menu_cb(update, context)
    elif payload.startswith("a:users:"):
        await A.users_list(update, context)
    elif payload.startswith("a:user:"):
        await A.user_detail(update, context)
    elif payload.startswith("a:ubal:"):
        await A.user_balance_prompt(update, context)
    elif payload.startswith("a:uban:"):
        await A.user_ban_toggle(update, context)
    elif payload == "a:usearch":
        await A.user_search_prompt(update, context)
    elif payload == "a:deps":
        await A.deposits_list(update, context)
    elif payload.startswith("a:depok:"):
        await A.deposit_approve(update, context)
    elif payload.startswith("a:depno:"):
        await A.deposit_reject(update, context)
    elif payload.startswith("a:dep:"):
        await A.deposit_detail(update, context)
    elif payload == "a:revs":
        await A.reviews_list(update, context)
    elif payload.startswith("a:revok:"):
        await A.review_set(update, context, approve=True)
    elif payload.startswith("a:revno:"):
        await A.review_set(update, context, approve=False)
    elif payload.startswith("a:rev:"):
        await A.review_detail(update, context)
    elif payload == "a:prods":
        await A.products_list(update, context)
    elif payload == "a:padd":
        await A.product_add_start(update, context)
    elif payload.startswith("a:pdelok:"):
        await A.product_delete_do(update, context)
    elif payload.startswith("a:pdel:"):
        await A.product_delete_ask(update, context)
    elif payload.startswith("a:ptoggle:"):
        await A.product_toggle(update, context)
    elif payload.startswith("a:stockclearok:"):
        await A.stock_clear_do(update, context)
    elif payload.startswith("a:stockclear:"):
        await A.stock_clear_ask(update, context)
    elif payload.startswith("a:stockadd:"):
        await A.stock_add_prompt(update, context)
    elif payload.startswith(("a:pname:", "a:pprice:", "a:pdesc:",
                              "a:phow:", "a:pterms:", "a:psku:")):
        field = payload.split(":")[1]
        await A.product_edit_prompt(update, context, field=field)
    elif payload.startswith("a:prod:"):
        await A.product_detail(update, context)
    elif payload == "a:pay":
        await A.payment_settings(update, context)
    elif payload.startswith("a:set:"):
        await A.setting_edit_prompt(update, context)
    elif payload == "a:keys":
        await A.api_keys_list(update, context)
    elif payload == "a:keygen":
        await A.api_keygen_prompt(update, context)
    elif payload.startswith("a:keyrevok:"):
        await A.api_key_revoke_do(update, context)
    elif payload.startswith("a:keyrev:"):
        await A.api_key_revoke_ask(update, context)
    elif payload == "a:stats":
        await A.show_stats(update, context)
    elif payload == "a:sync":
        await A.supplier_sync(update, context)
    elif payload == "a:bcast":
        await A.broadcast_menu(update, context)
    elif payload == "a:bcastt":
        await A.broadcast_text_prompt(update, context)
    elif payload == "a:bcastp":
        await A.broadcast_photo_prompt(update, context)
    elif payload == "a:bcastok":
        await A.broadcast_do(update, context)
    else:
        await update.callback_query.answer("Unknown admin action.", show_alert=False)


# ---------------- message routers ----------------
SHOP_TEXT_KINDS = {"custom_qty", "review_comment", "review_anon"}
WALLET_TEXT_KINDS = {"dep_amount", "dep_trxid"}


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    awaiting = context.user_data.get("awaiting")
    if not awaiting:
        return
    if await is_banned(db, user.id):
        await update.message.reply_text("🚫 You are banned from using this bot.")
        return
    kind = awaiting.get("kind")
    if kind in SHOP_TEXT_KINDS:
        if not await require_access(update, context):
            return
        await shop_h.handle_text(update, context)
    elif kind in WALLET_TEXT_KINDS:
        if not await require_access(update, context):
            return
        await wallet_h.handle_text(update, context)
    else:
        # Admin flows
        if not config.is_admin(user.id):
            context.user_data.pop("awaiting", None)
            return
        await admin_h.handle_text(update, context)


async def on_document(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if await is_banned(db, user.id) or not config.is_admin(user.id):
        return
    await admin_h.handle_document(update, context)


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if await is_banned(db, user.id) or not config.is_admin(user.id):
        return
    await admin_h.handle_photo(update, context)


async def cmd_orders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update, context):
        return
    # wrap message as pseudo-query target for show_orders
    class _Q:
        def __init__(self, msg, user):
            self.message = msg
            self.effective_user = user
        async def answer(self, *a, **k):
            pass
        async def edit_message_text(self, *a, **k):
            await self.message.reply_text(*a, **k)
    await shop_h.show_orders(_Q(update.message, update.effective_user), context, recover=False)


async def cmd_recover(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await require_access(update, context):
        return
    class _Q:
        def __init__(self, msg, user):
            self.message = msg
            self.effective_user = user
        async def answer(self, *a, **k):
            pass
        async def edit_message_text(self, *a, **k):
            await self.message.reply_text(*a, **k)
    await shop_h.show_orders(_Q(update.message, update.effective_user), context, recover=True)


# ---------------- background: daily SQLite backup ----------------
async def do_backup(db: Database) -> str | None:
    if db.uses_turso:
        # Turso is a managed cloud DB (durable + replicated by the provider);
        # a local file copy on Render's ephemeral disk would be pointless.
        log.info("Skipping local backup: Turso cloud DB in use")
        return None
    os.makedirs("backups", exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = os.path.abspath(f"backups/bot_{ts}.db")
    try:
        # VACUUM INTO works on the live connection and produces a consistent copy.
        await db.db.execute(f"VACUUM INTO '{dest}'")
    except Exception:
        # Fallback: checkpoint then copy the files.
        try:
            await db.db.execute("PRAGMA wal_checkpoint(TRUNCATE);")
        except Exception:
            pass
        import shutil
        shutil.copy2(db.path, dest)
    # prune: keep newest 7
    files = sorted(glob.glob(os.path.abspath("backups/bot_*.db")))
    for old in files[:-7]:
        try:
            os.remove(old)
        except OSError:
            pass
    log.info("Backup written: %s", dest)
    return dest


async def backup_loop(db: Database) -> None:
    await asyncio.sleep(60)  # let startup settle
    while True:
        try:
            await do_backup(db)
        except Exception:
            log.exception("Daily backup failed")
        await asyncio.sleep(24 * 3600)


# ---------------- app ----------------
async def post_init(app) -> None:
    db = Database(config.db_path, turso_url=config.turso_url, turso_token=config.turso_token)
    await db.init()
    app.bot_data["db"] = db
    if db.uses_turso:
        log.info("Database ready (Turso cloud)")
    else:
        asyncio.get_running_loop().create_task(backup_loop(db))
        log.info("Database ready at %s", config.db_path)


async def post_shutdown(app) -> None:
    db: Database | None = app.bot_data.get("db")
    if db:
        await db.close()


def build_app(request=None, base_url: str | None = None):
    config.validate()
    builder = (
        Application.builder()
        .token(config.bot_token)
        .defaults(Defaults(parse_mode=ParseMode.HTML))
        .post_init(post_init)
        .post_shutdown(post_shutdown)
    )
    if request is not None:
        builder = builder.request(request)
    if base_url:
        builder = builder.base_url(base_url)
    app = builder.build()
    app.add_handler(CommandHandler("start", shop_h.cmd_start))
    app.add_handler(CommandHandler("admin", admin_h.cmd_admin))
    app.add_handler(CommandHandler("orders", cmd_orders))
    app.add_handler(CommandHandler("recover", cmd_recover))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(MessageHandler(filters.Document.ALL, on_document))
    app.add_handler(MessageHandler(filters.PHOTO, on_photo))
    return app


async def selftest_mock() -> None:
    """Full end-to-end test against a local mock Telegram server.

    Exercises: initialize, getMe, getUpdates polling, /start handling
    (including force-join check and admin menu), then clean shutdown.
    """
    import importlib.util
    import threading

    spec = importlib.util.spec_from_file_location("mock_telegram", "/tmp/mock_telegram.py")
    mock = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mock)
    t = threading.Thread(target=mock.run, kwargs={"port": 8471}, daemon=True)
    t.start()
    await asyncio.sleep(0.5)

    app = build_app(base_url="http://127.0.0.1:8471/bot")
    await app.initialize()
    await post_init(app)  # manual startup path: run_polling would call this
    try:
        me = await app.bot.get_me()
        print(f"SELFTEST-MOCK OK: connected as @{me.username} (id={me.id})")
        await app.start()
        await app.updater.start_polling()
        print("SELFTEST-MOCK: polling 20s (will process a /start update)…")
        await asyncio.sleep(20)
        await app.updater.stop()
        await app.updater.shutdown()
        await app.stop()
        await post_shutdown(app)
        print("SELFTEST-MOCK OK: clean shutdown")
    finally:
        await app.shutdown()


async def selftest() -> None:
    """Verify the token works against the real Telegram API and polling
    runs, then shut down cleanly."""
    app = build_app()
    await app.initialize()
    await post_init(app)  # manual startup path: run_polling would call this
    try:
        me = await app.bot.get_me()
        print(f"SELFTEST OK: connected as @{me.username} (id={me.id})")
        await app.start()
        await app.updater.start_polling()
        print("SELFTEST: polling for 15s…")
        await asyncio.sleep(15)
        await app.updater.stop()
        await app.updater.shutdown()
        await app.stop()
        await post_shutdown(app)
        print("SELFTEST OK: clean shutdown")
    finally:
        await app.shutdown()


def run_webhook() -> None:
    """Start the bot in webhook mode (for Render's free tier).

    python-telegram-bot calls setWebhook automatically from `webhook_url`.
    The bot token doubles as the secret URL path so scanners can't hit the
    endpoint, and `secret_token` lets Telegram prove the updates are genuine.
    """
    base = config.webhook_url.rstrip("/")
    if not base:
        raise SystemExit(
            "WEBHOOK_URL is not set. On Render it comes from RENDER_EXTERNAL_URL "
            "automatically; elsewhere export WEBHOOK_URL=https://<your-host>."
        )
    url_path = config.bot_token
    webhook_url = f"{base}/{url_path}"
    app = build_app()
    log.info("Starting bot (webhook) on 0.0.0.0:%s", config.port)
    app.run_webhook(
        listen="0.0.0.0",
        port=config.port,
        url_path=url_path,
        secret_token=config.hmac_secret,
        webhook_url=webhook_url,
        allowed_updates=Update.ALL_TYPES,
    )


def main() -> None:
    if "--selftest-mock" in sys.argv:
        asyncio.run(selftest_mock())
        return
    if "--selftest" in sys.argv:
        asyncio.run(selftest())
        return
    if "--webhook" in sys.argv:
        run_webhook()
        return
    app = build_app()
    log.info("Starting bot (polling)…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
