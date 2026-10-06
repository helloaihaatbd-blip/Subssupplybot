"""Configuration loaded from environment (.env). No secrets are hardcoded."""
import os
import secrets

from dotenv import load_dotenv, set_key

load_dotenv()

ENV_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

# Locked default admin (Telegram user ID). Overridable via ADMIN_IDS in .env.
DEFAULT_ADMIN_ID = 7383329076


def _int_list(value: str) -> list[int]:
    ids: list[int] = []
    for part in (value or "").split(","):
        part = part.strip()
        if part.isdigit():
            ids.append(int(part))
    return ids


def _get_hmac_secret() -> str:
    secret = os.environ.get("HMAC_SECRET", "").strip()
    if not secret:
        # Generate once and persist so button signatures survive restarts.
        secret = secrets.token_hex(32)
        try:
            set_key(ENV_PATH, "HMAC_SECRET", secret)
        except Exception:
            pass
        os.environ["HMAC_SECRET"] = secret
    return secret


class Config:
    def __init__(self) -> None:
        self.bot_token: str = os.environ.get("BOT_TOKEN", "").strip()
        custom_admins = _int_list(os.environ.get("ADMIN_IDS", ""))
        self.admin_ids: list[int] = custom_admins or [DEFAULT_ADMIN_ID]
        self.db_path: str = os.environ.get("DB_PATH", "data/bot.db").strip() or "data/bot.db"
        self.hmac_secret: str = _get_hmac_secret()
        # Turso cloud DB (Render free tier). When both are set, db.py uses
        # the libsql remote backend instead of local SQLite.
        self.turso_url: str = os.environ.get("TURSO_DATABASE_URL", "").strip()
        self.turso_token: str = os.environ.get("TURSO_AUTH_TOKEN", "").strip()
        # Public base URL for Telegram webhooks. Render injects
        # RENDER_EXTERNAL_URL automatically, so WEBHOOK_URL is optional there.
        self.webhook_url: str = (
            os.environ.get("WEBHOOK_URL", "").strip()
            or os.environ.get("RENDER_EXTERNAL_URL", "").strip()
        )
        try:
            self.port: int = int(os.environ.get("PORT", "") or 8000)
        except ValueError:
            self.port = 8000
        self.support_username: str = (
            os.environ.get("SUPPORT_USERNAME", "subsmartbd").strip().lstrip("@") or "subsmartbd"
        )
        self.api_host: str = os.environ.get("API_HOST", "0.0.0.0").strip() or "0.0.0.0"
        self.api_port: int = int(os.environ.get("API_PORT", "8000") or 8000)

    def is_admin(self, user_id: int) -> bool:
        return user_id in self.admin_ids

    def validate(self) -> None:
        if not self.bot_token:
            raise SystemExit("BOT_TOKEN is missing. Copy .env.example to .env and set it.")


config = Config()
