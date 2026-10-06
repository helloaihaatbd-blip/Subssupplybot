"""Helpers: HMAC-signed callbacks, rate limiting, validation, formatting."""
import hashlib
import hmac
import html
import re
import time
from collections import deque
from datetime import datetime
from urllib.parse import urlparse

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

# ---------------- formatting ----------------
def money(value: float) -> str:
    return f"${value:,.2f}"


def bdt(value: float) -> str:
    return f"৳{value:,.0f}"


def esc(text) -> str:
    return html.escape(str(text or ""))


def fmt_dt(dt_str: str | None) -> str:
    if not dt_str:
        return "-"
    try:
        return datetime.fromisoformat(dt_str).strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return dt_str


def stars(n: int) -> str:
    return "⭐" * max(0, min(5, n)) + "☆" * (5 - max(0, min(5, n)))


# ---------------- validation ----------------
MOBILE_TRX_RE = re.compile(r"^[A-Za-z0-9]{6,25}$")
SAFE_TEXT_RE = re.compile(r"^[\w\s.,!?()\-₹$৳@:/+]{1,200}$", re.UNICODE)


def parse_amount(text: str, min_v: float, max_v: float) -> float | None:
    try:
        amount = float((text or "").strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None
    if not (min_v <= amount <= max_v):
        return None
    return round(amount, 2)


def parse_price(text: str) -> float | None:
    try:
        price = float((text or "").strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None
    if price <= 0 or price > 100000:
        return None
    return round(price, 2)


def parse_percent(text: str) -> float | None:
    try:
        v = float((text or "").strip().replace("%", ""))
    except (ValueError, AttributeError):
        return None
    if not (0 <= v <= 50):
        return None
    return round(v, 2)


def parse_qty(text: str, max_qty: int) -> int | None:
    text = (text or "").strip()
    if not text.isdigit():
        return None
    qty = int(text)
    return qty if 1 <= qty <= max_qty else None


def is_trxid(text: str) -> bool:
    """Transaction IDs: bKash/Nagad/Rocket/Binance/Bybit refs are alphanumeric."""
    return bool(MOBILE_TRX_RE.match((text or "").strip()))


def stock_emoji(stock: int) -> str:
    if stock <= 0:
        return "🔴"
    if stock <= 10:
        return "🟠"
    return "🟢"


def channel_username_from_link(link: str) -> str:
    """Extract @username from a t.me link, or return the input if already a username."""
    link = (link or "").strip()
    if link.startswith("@"):
        return link
    try:
        path = urlparse(link).path.strip("/")
        if path:
            return "@" + path.split("/")[0]
    except Exception:
        pass
    return link if link.startswith("@") else "@" + link.lstrip("@")


# ---------------- HMAC-signed callbacks ----------------
def sign_cb(secret: str, payload: str) -> str:
    sig = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return f"{payload}.{sig}"


def verify_cb(secret: str, data: str) -> str | None:
    if not data or "." not in data:
        return None
    payload, sig = data.rsplit(".", 1)
    expected = hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()[:16]
    return payload if hmac.compare_digest(sig, expected) else None


# ---------------- per-user rate limiting ----------------
class RateLimiter:
    """Token-bucket-ish: max_calls per window_seconds per user."""

    def __init__(self, max_calls: int = 12, window_seconds: int = 10):
        self.max_calls = max_calls
        self.window = window_seconds
        self._hits: dict[int, deque] = {}

    def hit(self, user_id: int) -> bool:
        """Returns True if the user is rate-limited (call rejected)."""
        now = time.monotonic()
        dq = self._hits.get(user_id)
        if dq is None:
            dq = self._hits[user_id] = deque()
        while dq and now - dq[0] > self.window:
            dq.popleft()
        if len(dq) >= self.max_calls:
            return True
        dq.append(now)
        # opportunistic cleanup
        if len(self._hits) > 5000:
            self._hits = {k: v for k, v in self._hits.items() if v and now - v[-1] <= self.window}
        return False


# ---------------- keyboards ----------------
def back_menu_keyboard(sign) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("🏠 Main menu", callback_data=sign("m:start"))]
    ])


def join_keyboard(sign, join_url: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 Join Community", url=join_url)],
        [InlineKeyboardButton("✅ I Have Joined", callback_data=sign("join:check"))],
    ])
