"""Clerk as the identity provider (Google sign-in, authenticator/TOTP MFA).

Clerk only proves WHO the person is. What they may do is decided here: a Clerk identity is mapped
to a pre-registered `app_users` row, and roles always come from our own database. After
verification we issue the application's normal server-side session, so CSRF protection, role
checks, idle timeout and revocation work exactly as for any other login.
"""
from __future__ import annotations

import httpx
import jwt

from .config import settings

_jwks: jwt.PyJWKClient | None = None


class ClerkError(Exception):
    pass


def _jwks_client() -> jwt.PyJWKClient:
    global _jwks
    if _jwks is None:
        _jwks = jwt.PyJWKClient(f"https://{settings.clerk_frontend_api}/.well-known/jwks.json", cache_keys=True, lifespan=3600, timeout=5)
    return _jwks


def verify_session_token(token: str, signing_key=None) -> dict:
    """Validate a Clerk session JWT: RS256 signature against the instance JWKS, issuer, exp/nbf
    and the `azp` (authorised party) claim. Returns the claims or raises ClerkError."""
    if not settings.clerk_enabled:
        raise ClerkError("Clerk is not configured")
    try:
        key = signing_key or _jwks_client().get_signing_key_from_jwt(token).key
        claims = jwt.decode(token, key, algorithms=["RS256"], issuer=f"https://{settings.clerk_frontend_api}",
                            options={"require": ["exp", "nbf", "iat", "sub", "iss"], "verify_aud": False}, leeway=5)
    except Exception as exc:  # noqa: BLE001 - any failure means "not authenticated"
        raise ClerkError("invalid session token") from exc
    azp = claims.get("azp")
    if azp and azp.rstrip("/") not in settings.allowed_origins:
        raise ClerkError("token was issued for another origin")
    if claims.get("sts") == "pending":
        raise ClerkError("session is pending")
    return claims


def has_second_factor(claims: dict) -> bool:
    """`fva` = [minutes since first factor, minutes since second factor]; -1 means never verified."""
    fva = claims.get("fva")
    return isinstance(fva, list) and len(fva) == 2 and isinstance(fva[1], int) and fva[1] >= 0


def verified_emails(user_id: str) -> list[str]:
    """Verified e-mail addresses of a Clerk user, via the Backend API (server-side secret key)."""
    if not settings.clerk_secret_key:
        raise ClerkError("CLERK_SECRET_KEY is not configured")
    if not user_id.replace("_", "").isalnum():
        raise ClerkError("bad subject")
    try:
        r = httpx.get(f"https://api.clerk.com/v1/users/{user_id}", headers={"Authorization": f"Bearer {settings.clerk_secret_key}"}, timeout=8)
        r.raise_for_status()
        data = r.json()
    except Exception as exc:  # noqa: BLE001
        raise ClerkError("could not load the Clerk user") from exc
    primary = data.get("primary_email_address_id")
    out = []
    for e in data.get("email_addresses", []):
        if (e.get("verification") or {}).get("status") == "verified" and e.get("email_address"):
            out.insert(0 if e.get("id") == primary else len(out), e["email_address"].lower())
    return out
