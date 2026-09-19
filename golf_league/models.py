from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
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
        Integer, ForeignKey("golfers.id"), nullable=True, unique=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now(), onupdate=func.now()
    )


class Course(Base):
    """A golf course. GL-20 stores the league's own course only."""

    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    city: Mapped[str | None] = mapped_column(String(80), nullable=True)
    state: Mapped[str | None] = mapped_column(String(2), nullable=True)
    website: Mapped[str | None] = mapped_column(String(255), nullable=True)
    total_holes: Mapped[int] = mapped_column(
        Integer, nullable=False, default=18, server_default="18"
    )

    tee_sets: Mapped[list["TeeSet"]] = relationship(
        "TeeSet", back_populates="course", order_by="TeeSet.sort_order"
    )


class TeeSet(Base):
    """A named, gendered tee (e.g. "Deer" / "White" / men) at a course.

    Both the club's animal `name` and the league's `color_label` are stored
    because the league speaks in colours and the scorecard speaks in
    animals. See MVP Build Plan §8: Gold is Snake, White is Deer.
    """

    __tablename__ = "tee_sets"
    __table_args__ = (
        UniqueConstraint("course_id", "name", "gender", name="uq_tee_sets_course_name_gender"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(60), nullable=False)
    color_label: Mapped[str] = mapped_column(String(30), nullable=False)
    gender: Mapped[str] = mapped_column(String(8), nullable=False)
    total_yards: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    course: Mapped["Course"] = relationship("Course", back_populates="tee_sets")
    ratings: Mapped[list["TeeRating"]] = relationship(
        "TeeRating", back_populates="tee_set"
    )


class TeeRating(Base):
    """One scope (front/back/full) rating row for a tee set."""

    __tablename__ = "tee_ratings"
    __table_args__ = (
        UniqueConstraint("tee_set_id", "scope", name="uq_tee_ratings_tee_set_scope"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tee_set_id: Mapped[int] = mapped_column(
        ForeignKey("tee_sets.id"), nullable=False
    )
    scope: Mapped[str] = mapped_column(String(5), nullable=False)
    rating: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False)
    slope: Mapped[int] = mapped_column(Integer, nullable=False)
    par: Mapped[int] = mapped_column(Integer, nullable=False)

    tee_set: Mapped["TeeSet"] = relationship("TeeSet", back_populates="ratings")


class Golfer(Base):
    """A person in the league's roster.

    `handicap_strokes` is a signed Integer, not Numeric: NULL means no
    handicap on file, 0 is a scratch golfer, and a negative value is a
    legitimate plus handicap. Neither may be coerced into the other, and
    the column has no minimum and no maximum.

    `handicap_status` is derived (never a direct input) and persisted by
    `golf_league.domain.roster.derive_handicap_status`.

    `handicap_index` exists for a future release; this task never writes
    it, and no form field offers it.
    """

    __tablename__ = "golfers"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    first_name: Mapped[str] = mapped_column(String(80), nullable=False)
    last_name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True, unique=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    default_tee_set_id: Mapped[int] = mapped_column(
        ForeignKey("tee_sets.id"), nullable=False
    )
    handicap_strokes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handicap_source: Mapped[str] = mapped_column(String(16), nullable=False)
    handicap_status: Mapped[str] = mapped_column(String(16), nullable=False)
    handicap_index: Mapped[Decimal | None] = mapped_column(
        Numeric(4, 1), nullable=True
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )

    default_tee_set: Mapped["TeeSet"] = relationship("TeeSet")


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
