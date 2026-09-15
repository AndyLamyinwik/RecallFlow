"""Persistence operations for pilot users."""

from sqlalchemy.orm import Session

from app.models import User


class UserRepository:
    def create(
        self,
        session: Session,
        *,
        access_code_hash: str,
        access_code_fingerprint: str | None = None,
        timezone_name: str = "UTC",
    ) -> User:
        user = User(
            access_code_hash=access_code_hash,
            access_code_fingerprint=access_code_fingerprint,
            timezone=timezone_name,
        )
        session.add(user)
        session.flush()
        return user

    def get(self, session: Session, user_id: str) -> User | None:
        return session.get(User, user_id)

    def hard_delete(self, session: Session, user_id: str) -> bool:
        user = self.get(session, user_id)
        if user is None:
            return False
        session.delete(user)
        session.flush()
        return True
