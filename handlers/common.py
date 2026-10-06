"""Shared guards: ban check, force-join verification, admin notifications."""
import logging
import time

from telegram import Update
from telegram.ext import ContextTypes

from config import config
from db import Database
from utils import channel_username_from_link, join_keyboard, sign_cb

log = logging.getLogger(__name__)

S = lambda p: sign_cb(config.hmac_secret, p)  # noqa: E731

# Short-lived cache so rapid button taps don't hammer getChatMember.
_join_cache: dict[int, tuple[bool, float]] = {}
JOIN_CACHE_TTL = 120


async def is_banned(db: Database, user_id: int) -> bool:
    u = await db.get_user(user_id)
    return bool(u and u["banned"])


async def check_membership(bot, user_id: int, link: str, fresh: bool = False) -> bool:
    now = time.monotonic()
    if not fresh:
        cached = _join_cache.get(user_id)
        if cached and now - cached[1] < JOIN_CACHE_TTL:
            return cached[0]
    username = channel_username_from_link(link)
    try:
        member = await bot.get_chat_member(chat_id=username, user_id=user_id)
        joined = member.status in ("member", "administrator", "creator")
    except Exception as e:
        log.warning("getChatMember failed for %s: %s", username, e)
        joined = False
    _join_cache[user_id] = (joined, now)
    return joined


def invalidate_join_cache(user_id: int) -> None:
    _join_cache.pop(user_id, None)


async def get_force_join_link(db: Database) -> str:
    return await db.get_setting("force_join_link", "https://t.me/subsmaart")


async def join_screen_text(db: Database) -> tuple[str, object]:
    link = await get_force_join_link(db)
    text = (
        "👋 <b>Welcome!</b>\n\n"
        "To use this bot you must join our community channel first.\n\n"
        "1️⃣ Tap <b>📢 Join Community</b> below\n"
        "2️⃣ Then tap <b>✅ I Have Joined</b>"
    )
    return text, join_keyboard(S, link)


async def require_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Ban + force-join gate for every protected action. Returns True if allowed."""
    db: Database = context.bot_data["db"]
    user = update.effective_user
    if await is_banned(db, user.id):
        msg = "🚫 You are banned from using this bot."
        if update.callback_query:
            await update.callback_query.answer(msg, show_alert=True)
        elif update.message:
            await update.message.reply_text(msg)
        return False
    link = await get_force_join_link(db)
    if not await check_membership(context.bot, user.id, link):
        text, kb = await join_screen_text(db)
        if update.callback_query:
            q = update.callback_query
            await q.answer("⚠️ Please join the channel first.", show_alert=False)
            try:
                await q.edit_message_text(text, reply_markup=kb, parse_mode="HTML",
                                          disable_web_page_preview=True)
            except Exception:
                pass
        elif update.message:
            await update.message.reply_text(text, reply_markup=kb, parse_mode="HTML",
                                            disable_web_page_preview=True)
        return False
    return True


async def notify_admins(context: ContextTypes.DEFAULT_TYPE, text: str,
                        reply_markup=None) -> None:
    for admin_id in config.admin_ids:
        try:
            await context.bot.send_message(chat_id=admin_id, text=text,
                                           reply_markup=reply_markup, parse_mode="HTML",
                                           disable_web_page_preview=True)
        except Exception as e:
            log.warning("notify admin %s failed: %s", admin_id, e)


async def get_bot_username(context: ContextTypes.DEFAULT_TYPE) -> str:
    if "bot_username" not in context.bot_data:
        me = await context.bot.get_me()
        context.bot_data["bot_username"] = me.username or ""
    return context.bot_data["bot_username"]


def referral_link(username: str, user_id: int) -> str:
    return f"https://t.me/{username}?start=ref_{user_id}"
