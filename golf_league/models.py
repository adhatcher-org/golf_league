from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.sql import func


class Base(DeclarativeBase):
    pass


class User(Base):
    """A person who can sign in.

    `username` stores an email address, hence String(254) rather than a
    short handle column — a 40-char column cannot hold every valid email.

    League role (captain, player, etc.) is derived from team membership and
    is never stored here. There is no `default_role` column.

    `golfer_id` is a nullable, unique column only in this task. The actual
    foreign key to `golfers.id` is added by GL-10's migration once that
    table exists; this task must not create a `golfers` table.
    """

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(String(254), nullable=False)
    email: Mapped[str] = mapped_column(String(254), nullable=False, unique=True)
    display_name: Mapped[str] = mapped_column(String(254), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    email_verified_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True
    )
    is_admin: Mapped[bool] = mapped_column(nullable=False, default=False)
    session_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    golfer_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class UserToken(Base):
    """A single-use, purpose-scoped token issued to a user.

    Only the SHA-256 digest of the raw token is ever stored; the raw value
    is returned to the caller once by `services.auth.issue_token` and never
    persisted or logged.

    `purpose` is one of `verify_email`, `reset_password`, `set_password`.
    """

    __tablename__ = "user_tokens"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    token_digest: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True
    )
    purpose: Mapped[str] = mapped_column(String(32), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
