"""Closed-pilot access-code, signed-session, and CSRF primitives."""

from base64 import urlsafe_b64decode, urlsafe_b64encode
from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import scrypt, sha256
import hmac
import secrets
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from itsdangerous import (
    BadSignature,
    SignatureExpired,
    URLSafeSerializer,
    URLSafeTimedSerializer,
)
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import User
from app.repositories import UserRepository


_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_LENGTH = 32


def _b64encode(value: bytes) -> str:
    return urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_access_code(access_code: str, *, salt: bytes | None = None) -> str:
    """Hash one high-entropy invitation code with a random per-user salt."""

    if not 16 <= len(access_code) <= 256:
        raise ValueError("Access codes must contain 16 to 256 characters")
    selected_salt = salt or secrets.token_bytes(16)
    digest = scrypt(
        access_code.encode("utf-8"),
        salt=selected_salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=_SCRYPT_LENGTH,
    )
    return f"scrypt$v1${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${_b64encode(selected_salt)}${_b64encode(digest)}"


def verify_access_code(access_code: str, encoded_hash: str) -> bool:
    """Verify a stored hash without ever recovering or returning the code."""

    if len(access_code) > 256:
        return False
    try:
        algorithm, version, n_value, r_value, p_value, salt_value, digest_value = encoded_hash.split("$")
        if algorithm != "scrypt" or version != "v1":
            return False
        n = int(n_value)
        r = int(r_value)
        p = int(p_value)
        if (n, r, p) != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        expected = _b64decode(digest_value)
        candidate = scrypt(
            access_code.encode("utf-8"),
            salt=_b64decode(salt_value),
            n=n,
            r=r,
            p=p,
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(candidate, expected)


def fingerprint_access_code(access_code: str, *, secret: str) -> str:
    """Return a non-guessable stable key used only for unique code claiming."""

    return hmac.new(
        secret.encode("utf-8"),
        f"recallflow-access-code-v1:{access_code}".encode("utf-8"),
        sha256,
    ).hexdigest()


class InvalidPilotTimezone(ValueError):
    pass


class PilotAccessService:
    """Authenticate existing users or provision one user per configured code."""

    def __init__(self, allowed_codes: tuple[str, ...], *, fingerprint_secret: str) -> None:
        self._allowed_codes = allowed_codes
        self._fingerprint_secret = fingerprint_secret
        self._users = UserRepository()

    def authenticate_or_provision(
        self,
        session: Session,
        *,
        access_code: str,
        timezone_name: str,
    ) -> User | None:
        code = access_code.strip()
        if not 16 <= len(code) <= 256:
            return None

        allowed = False
        for configured_code in self._allowed_codes:
            allowed = hmac.compare_digest(code, configured_code) or allowed
        if not allowed:
            return None

        fingerprint = fingerprint_access_code(code, secret=self._fingerprint_secret)
        existing = session.scalar(
            select(User).where(User.access_code_fingerprint == fingerprint)
        )
        if existing is not None:
            return existing if verify_access_code(code, existing.access_code_hash) else None

        # Older local databases may contain a user created before the opaque
        # fingerprint existed. Upgrade that row on its next valid login.
        for legacy_user in session.scalars(
            select(User)
            .where(User.access_code_fingerprint.is_(None))
            .order_by(User.created_at, User.id)
        ):
            if verify_access_code(code, legacy_user.access_code_hash):
                try:
                    with session.begin_nested():
                        legacy_user.access_code_fingerprint = fingerprint
                        session.flush()
                except IntegrityError:
                    claimed = session.scalar(
                        select(User).where(User.access_code_fingerprint == fingerprint)
                    )
                    if claimed is not None and verify_access_code(code, claimed.access_code_hash):
                        return claimed
                    raise
                return legacy_user

        selected_timezone = timezone_name.strip() or "UTC"
        try:
            ZoneInfo(selected_timezone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise InvalidPilotTimezone("A valid IANA timezone is required") from exc

        try:
            with session.begin_nested():
                return self._users.create(
                    session,
                    access_code_hash=hash_access_code(code),
                    access_code_fingerprint=fingerprint,
                    timezone_name=selected_timezone,
                )
        except IntegrityError:
            # A concurrent first login may have claimed the same invitation.
            claimed = session.scalar(
                select(User).where(User.access_code_fingerprint == fingerprint)
            )
            if claimed is not None and verify_access_code(code, claimed.access_code_hash):
                return claimed
            raise


@dataclass(frozen=True)
class SessionClaims:
    user_id: str
    session_id: str


@dataclass(frozen=True)
class DismissUndoClaims:
    user_id: str
    event_id: str
    item_id: str
    dismiss_action_event_id: str
    original_session_id: str
    local_date_slot: date
    expires_at: datetime


class DismissUndoTokenManager:
    """Sign a short-lived, opaque Undo capability without server-side state."""

    cookie_name = "recallflow_dismiss_undo"

    def __init__(self, secret: str) -> None:
        self._serializer = URLSafeSerializer(
            secret,
            salt="recallflow-dismiss-undo-v1",
        )

    def issue(self, claims: DismissUndoClaims) -> str:
        expires_at = claims.expires_at
        if expires_at.tzinfo is None:
            raise ValueError("Undo expiry must include a timezone")
        return self._serializer.dumps(
            {
                "v": 1,
                "uid": claims.user_id,
                "eid": claims.event_id,
                "iid": claims.item_id,
                "aid": claims.dismiss_action_event_id,
                "sid": claims.original_session_id,
                "day": claims.local_date_slot.isoformat(),
                "exp": expires_at.astimezone(timezone.utc).isoformat(),
            }
        )

    def read(self, token: str | None, *, now: datetime) -> DismissUndoClaims | None:
        if not token or len(token) > 4096:
            return None
        try:
            payload: Any = self._serializer.loads(token)
            if not isinstance(payload, dict) or payload.get("v") != 1:
                return None
            keys = ("uid", "eid", "iid", "aid", "sid", "day", "exp")
            if any(not isinstance(payload.get(key), str) for key in keys):
                return None
            if any(len(payload[key]) > 128 for key in ("uid", "eid", "iid", "aid", "sid")):
                return None
            local_date_slot = date.fromisoformat(payload["day"])
            expires_at = datetime.fromisoformat(payload["exp"])
            if expires_at.tzinfo is None:
                return None
        except (BadSignature, TypeError, ValueError):
            return None

        checked_at = now
        if checked_at.tzinfo is None:
            checked_at = checked_at.replace(tzinfo=timezone.utc)
        checked_at = checked_at.astimezone(timezone.utc)
        expires_at = expires_at.astimezone(timezone.utc)
        if checked_at >= expires_at:
            return None
        return DismissUndoClaims(
            user_id=payload["uid"],
            event_id=payload["eid"],
            item_id=payload["iid"],
            dismiss_action_event_id=payload["aid"],
            original_session_id=payload["sid"],
            local_date_slot=local_date_slot,
            expires_at=expires_at,
        )


class SessionManager:
    """Issue and validate short-lived stateless session and CSRF values."""

    session_cookie_name = "recallflow_session"
    login_csrf_cookie_name = "recallflow_login_csrf"

    def __init__(
        self,
        secret: str,
        *,
        session_max_age_seconds: int,
        login_csrf_max_age_seconds: int,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._sessions = URLSafeTimedSerializer(secret, salt="recallflow-session-v1")
        self._login_csrf = URLSafeTimedSerializer(secret, salt="recallflow-login-csrf-v1")
        self.session_max_age_seconds = session_max_age_seconds
        self.login_csrf_max_age_seconds = login_csrf_max_age_seconds

    def create_session(self, user_id: str) -> tuple[str, SessionClaims]:
        claims = SessionClaims(user_id=user_id, session_id=secrets.token_urlsafe(24))
        token = self._sessions.dumps({"uid": claims.user_id, "sid": claims.session_id})
        return token, claims

    def read_session(self, token: str | None) -> SessionClaims | None:
        if not token:
            return None
        try:
            payload: Any = self._sessions.loads(token, max_age=self.session_max_age_seconds)
        except (BadSignature, SignatureExpired):
            return None
        if not isinstance(payload, dict):
            return None
        user_id = payload.get("uid")
        session_id = payload.get("sid")
        if not isinstance(user_id, str) or not isinstance(session_id, str):
            return None
        if not user_id or not session_id or len(user_id) > 64 or len(session_id) > 128:
            return None
        return SessionClaims(user_id=user_id, session_id=session_id)

    def new_login_csrf(self) -> str:
        return self._login_csrf.dumps({"nonce": secrets.token_urlsafe(24)})

    def valid_login_csrf(self, form_token: str, cookie_token: str | None) -> bool:
        if not cookie_token or not hmac.compare_digest(form_token, cookie_token):
            return False
        try:
            payload: Any = self._login_csrf.loads(
                form_token,
                max_age=self.login_csrf_max_age_seconds,
            )
        except (BadSignature, SignatureExpired):
            return False
        return isinstance(payload, dict) and isinstance(payload.get("nonce"), str)

    def csrf_token(self, claims: SessionClaims) -> str:
        message = f"recallflow-csrf-v1:{claims.session_id}".encode("utf-8")
        return _b64encode(hmac.new(self._secret, message, sha256).digest())

    def valid_csrf(self, claims: SessionClaims, form_token: str) -> bool:
        return hmac.compare_digest(self.csrf_token(claims), form_token)
