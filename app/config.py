"""Runtime configuration. Everything is read from environment variables (see .env.example).
A local `.env` file is loaded if present; real environment variables always win."""
from __future__ import annotations

import base64
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"


def _load_dotenv() -> None:
    for name in (".env", ".env.local"):
        path = BASE_DIR / name
        if not path.is_file():
            continue
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip().strip("\"'"))


_load_dotenv()


def _int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except ValueError:
        return default


def _first(*names: str) -> str:
    return next((v for n in names if (v := os.getenv(n))), "")


class Settings:
    def __init__(self) -> None:
        self.env = os.getenv("TT_ENV", "development")
        prod = self.env == "production"
        self.database_url = os.getenv("TT_DATABASE_URL", f"sqlite:///{(DATA_DIR / 'timetable.db').as_posix()}")
        # Secure cookies are mandatory in production; off by default only for http://localhost development.
        self.cookie_secure = os.getenv("TT_COOKIE_SECURE", "1" if prod else "0") == "1"
        self.session_minutes = _int("TT_SESSION_MINUTES", 480)
        self.session_idle_minutes = _int("TT_SESSION_IDLE_MINUTES", 60)
        self.max_body_bytes = _int("TT_MAX_BODY_BYTES", 1_000_000)
        self.solver_max_seconds = _int("TT_SOLVER_MAX_SECONDS", 60)
        self.solver_workers = _int("TT_SOLVER_WORKERS", 4)
        self.solver_max_vars = _int("TT_SOLVER_MAX_VARS", 400_000)
        self.max_queued_runs = _int("TT_MAX_QUEUED_RUNS", 3)
        self.scrypt_n = _int("TT_SCRYPT_N", 2**15)
        self.login_max_attempts = _int("TT_LOGIN_MAX_ATTEMPTS", 5)
        self.login_window_seconds = _int("TT_LOGIN_WINDOW_SECONDS", 300)
        self.docs_enabled = os.getenv("TT_DOCS", "0" if prod else "1") == "1"
        self.org_name = os.getenv("TT_ORG_NAME", "")[:120]  # shown under the product name, e.g. the school
        # ---- Clerk (Google sign-in + authenticator MFA). The publishable key is public by design;
        # the secret key is only ever used server-side and is never sent to the browser.
        self.clerk_publishable_key = _first("CLERK_PUBLISHABLE_KEY", "VITE_CLERK_PUBLISHABLE_KEY", "NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY")
        self.clerk_secret_key = os.getenv("CLERK_SECRET_KEY", "")
        self.clerk_frontend_api = self._fapi(self.clerk_publishable_key)
        # Origins allowed to mint Clerk tokens for this app (checked against the token's `azp` claim).
        self.allowed_origins = [o.strip().rstrip("/") for o in os.getenv(
            "TT_ALLOWED_ORIGINS", "http://localhost:8000,http://127.0.0.1:8000").split(",") if o.strip()]
        self.local_login = os.getenv("TT_LOCAL_LOGIN", "0" if prod else "1") == "1"
        self.require_admin_mfa = os.getenv("TT_REQUIRE_ADMIN_MFA", "1" if prod else "0") == "1"

    @staticmethod
    def _fapi(pk: str) -> str:
        """pk_test_<base64('host$')> -> host of the instance's Frontend API."""
        try:
            raw = pk.split("_", 2)[2]
            host = base64.b64decode(raw + "=" * (-len(raw) % 4)).decode().rstrip("$")
            return host if host and all(c.isalnum() or c in ".-" for c in host) else ""
        except Exception:  # noqa: BLE001
            return ""

    @property
    def clerk_enabled(self) -> bool:
        return bool(self.clerk_frontend_api)


settings = Settings()
