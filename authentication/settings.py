from pathlib import Path
from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent  # goes up from authentication/ to project root

# app-wide settings (named for where they started: auth)
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env",
        env_file_encoding="utf-8",
        extra="ignore",  # don't error on keys this class doesn't define
    )
    # server-side sessions: a login lasts while it's used, up to a hard limit
    SESSION_IDLE_DAYS: int = 30        # signed out after this long without a visit
    SESSION_MAX_DAYS: int = 90         # signed out after this long regardless
    # must be True in production (HTTPS); False only so local http:// dev works
    COOKIE_SECURE: bool = False
    # comma-separated origins allowed to call the API cross-origin; empty = same-origin only
    ALLOWED_ORIGINS: str = ""
    # public address of the web app, used in links sent over WhatsApp (e.g. sign-up)
    APP_PUBLIC_URL: str = ""

    # WhatsApp Cloud API — the webhook stays disabled until all four are set
    WHATSAPP_VERIFY_TOKEN: SecretStr | None = None   # any string; also entered in Meta's webhook setup
    WHATSAPP_APP_SECRET: SecretStr | None = None     # Meta app secret, signs webhook payloads
    WHATSAPP_ACCESS_TOKEN: SecretStr | None = None   # system-user token with whatsapp_business_messaging
    WHATSAPP_PHONE_NUMBER_ID: str | None = None      # the business number's id (not the number itself)
    WHATSAPP_API_VERSION: str = "v23.0"
    # the business number as digits (e.g. 2349012345678), for wa.me links on the website
    WHATSAPP_DISPLAY_NUMBER: str | None = None
    # shown when someone signs up on WhatsApp; required before launch (Nigeria Data Protection Act)
    PRIVACY_POLICY_URL: str | None = None

    # Payment reminders to debtors. The template must be approved in Meta Business Manager
    # first — see Documentation.MD for its exact text and parameters.
    WHATSAPP_REMINDER_TEMPLATE: str | None = None
    WHATSAPP_REMINDER_TEMPLATE_LANG: str = "en"
    REMINDER_COOLDOWN_DAYS: int = 3    # at most one reminder per debtor per this many days
    REMINDER_DAILY_LIMIT: int = 20     # per trader, rolling 24 hours

    @property
    def reminders_enabled(self) -> bool:
        return self.whatsapp_enabled and bool(self.WHATSAPP_REMINDER_TEMPLATE)

    @property
    def whatsapp_enabled(self) -> bool:
        return all((self.WHATSAPP_VERIFY_TOKEN, self.WHATSAPP_APP_SECRET,
                    self.WHATSAPP_ACCESS_TOKEN, self.WHATSAPP_PHONE_NUMBER_ID))

    @property
    def allowed_origins(self) -> list[str]:
        return [o.strip() for o in self.ALLOWED_ORIGINS.split(",") if o.strip()]

settings = Settings()
