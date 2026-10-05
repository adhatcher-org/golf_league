from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
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
    holes: Mapped[list["Hole"]] = relationship("Hole", back_populates="course", order_by="Hole.number")
    seasons: Mapped[list["Season"]] = relationship("Season", back_populates="course")


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
    hole_yardages: Mapped[list["HoleYardage"]] = relationship("HoleYardage", back_populates="tee_set")


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


class Hole(Base):
    """One stored hole on a course; `nine` is deliberately not derived."""

    __tablename__ = "holes"
    __table_args__ = (UniqueConstraint("course_id", "number", name="uq_holes_course_number"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    nine: Mapped[str] = mapped_column(String(5), nullable=False)
    par: Mapped[int] = mapped_column(Integer, nullable=False)
    stroke_index_18: Mapped[int] = mapped_column(Integer, nullable=False)
    stroke_index_9: Mapped[int] = mapped_column(Integer, nullable=False)

    course: Mapped["Course"] = relationship("Course", back_populates="holes")
    yardages: Mapped[list["HoleYardage"]] = relationship("HoleYardage", back_populates="hole")


class HoleYardage(Base):
    """The required yardage for one hole at one tee set."""

    __tablename__ = "hole_yardages"
    __table_args__ = (UniqueConstraint("hole_id", "tee_set_id", name="uq_hole_yardages_hole_tee_set"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    hole_id: Mapped[int] = mapped_column(ForeignKey("holes.id"), nullable=False)
    tee_set_id: Mapped[int] = mapped_column(ForeignKey("tee_sets.id"), nullable=False)
    yards: Mapped[int] = mapped_column(Integer, nullable=False)

    hole: Mapped["Hole"] = relationship("Hole", back_populates="yardages")
    tee_set: Mapped["TeeSet"] = relationship("TeeSet", back_populates="hole_yardages")


class Season(Base):
    """One configured league season; schedule rows are deliberately absent."""

    __tablename__ = "seasons"
    __table_args__ = (
        CheckConstraint("year >= 1 AND year <= 9999", name="ck_seasons_year_range"),
        CheckConstraint(
            "play_weekday >= 0 AND play_weekday <= 6",
            name="ck_seasons_play_weekday_range",
        ),
        CheckConstraint(
            "status IN ('draft', 'active', 'complete')", name="ck_seasons_status"
        ),
        CheckConstraint(
            "first_week_nine IN ('front', 'back')",
            name="ck_seasons_first_week_nine",
        ),
        CheckConstraint("start_date <= end_date", name="ck_seasons_dates"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    year: Mapped[int] = mapped_column(Integer, nullable=False)
    name_override: Mapped[str | None] = mapped_column(String(120), nullable=True)
    course_id: Mapped[int] = mapped_column(
        ForeignKey("courses.id", name="fk_seasons_course_id_courses"),
        nullable=False,
        index=True,
    )
    start_date: Mapped[date] = mapped_column(nullable=False)
    end_date: Mapped[date] = mapped_column(nullable=False)
    play_weekday: Mapped[int] = mapped_column(
        Integer, nullable=False, default=3, server_default="3"
    )
    status: Mapped[str] = mapped_column(
        String(8), nullable=False, default="draft", server_default="draft"
    )
    first_week_nine: Mapped[str] = mapped_column(String(5), nullable=False)

    course: Mapped["Course"] = relationship("Course", back_populates="seasons")


class SeasonGolfer(Base):
    """Explicit inclusion of a golfer in one season's roster."""

    __tablename__ = "season_golfers"
    __table_args__ = (
        UniqueConstraint("season_id", "golfer_id", name="uq_season_golfers_season_golfer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    golfer_id: Mapped[int] = mapped_column(ForeignKey("golfers.id"), nullable=False, index=True)

    season: Mapped["Season"] = relationship()
    golfer: Mapped["Golfer"] = relationship()


class SeasonParticipant(Base):
    """Optional season-specific tee and seed overrides for one golfer."""

    __tablename__ = "season_participants"
    __table_args__ = (
        UniqueConstraint("season_id", "golfer_id", name="uq_season_participants_season_golfer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    golfer_id: Mapped[int] = mapped_column(ForeignKey("golfers.id"), nullable=False, index=True)
    tee_set_id: Mapped[int | None] = mapped_column(ForeignKey("tee_sets.id"), nullable=True, index=True)
    seed_handicap_strokes: Mapped[int | None] = mapped_column(Integer, nullable=True)
    seed_source: Mapped[str | None] = mapped_column(String(120), nullable=True)

    season: Mapped["Season"] = relationship()
    golfer: Mapped["Golfer"] = relationship()
    tee_set: Mapped["TeeSet | None"] = relationship()


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
    default_tee_set_id: Mapped[int | None] = mapped_column(
        ForeignKey("tee_sets.id"), nullable=True
    )
    default_tee_label: Mapped[str] = mapped_column(String(30), nullable=False, default="White", server_default="White")
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


class Team(Base):
    """One manually configured team in a season."""

    __tablename__ = "teams"
    __table_args__ = (
        UniqueConstraint("season_id", "number", name="uq_teams_season_number"),
        UniqueConstraint("id", "season_id", name="uq_teams_id_season"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False)

    season: Mapped["Season"] = relationship()
    members: Mapped[list["TeamMember"]] = relationship(
        back_populates="team", order_by="TeamMember.position", cascade="all, delete-orphan"
    )


class TeamMember(Base):
    """A golfer's explicit position on one team in one season."""

    __tablename__ = "team_members"
    __table_args__ = (
        ForeignKeyConstraint(
            ["team_id", "season_id"], ["teams.id", "teams.season_id"],
            name="fk_team_members_team_season",
        ),
        ForeignKeyConstraint(["season_id"], ["seasons.id"], name="fk_team_members_season"),
        ForeignKeyConstraint(["golfer_id"], ["golfers.id"], name="fk_team_members_golfer"),
        CheckConstraint("position >= 1 AND position <= 4", name="ck_team_members_position"),
        UniqueConstraint("team_id", "position", name="uq_team_members_team_position"),
        UniqueConstraint("team_id", "golfer_id", name="uq_team_members_team_golfer"),
        UniqueConstraint("season_id", "golfer_id", name="uq_team_members_season_golfer"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    season_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    golfer_id: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False)

    team: Mapped["Team"] = relationship(back_populates="members")
    season: Mapped["Season"] = relationship(overlaps="members,team")
    golfer: Mapped["Golfer"] = relationship()


class Week(Base):
    """One printed, season-scoped schedule row."""

    __tablename__ = "weeks"
    __table_args__ = (
        CheckConstraint('"index" >= 1', name="ck_weeks_index_positive"),
        CheckConstraint("week_type IN ('match', 'play_with_team', 'rain_date')", name="ck_weeks_type"),
        CheckConstraint("status IN ('scheduled', 'cancelled', 'played')", name="ck_weeks_status"),
        CheckConstraint("nine IS NULL OR nine IN ('front', 'back')", name="ck_weeks_nine"),
        CheckConstraint(
            "(week_type = 'rain_date' AND nine IS NULL) OR "
            "(week_type IN ('match', 'play_with_team') AND nine IS NOT NULL)",
            name="ck_weeks_type_nine",
        ),
        CheckConstraint("makeup_for_week_id IS NULL OR makeup_for_week_id != id", name="ck_weeks_makeup_not_self"),
        UniqueConstraint("season_id", "index", name="uq_weeks_season_index"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    season_id: Mapped[int] = mapped_column(ForeignKey("seasons.id", name="fk_weeks_season"), nullable=False, index=True)
    index: Mapped[int] = mapped_column(Integer, nullable=False)
    play_date: Mapped[date] = mapped_column(nullable=False)
    nine: Mapped[str | None] = mapped_column(String(5), nullable=True)
    week_type: Mapped[str] = mapped_column(String(16), nullable=False, server_default="match")
    makeup_for_week_id: Mapped[int | None] = mapped_column(ForeignKey("weeks.id", name="fk_weeks_makeup_for"), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(12), nullable=False, server_default="scheduled")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    season: Mapped["Season"] = relationship()
    makeup_for: Mapped["Week | None"] = relationship(remote_side="Week.id", foreign_keys=[makeup_for_week_id])


class RosterImportBatch(Base):
    """One staged CSV roster upload, pending admin review (GL-11/GL-12).

    GL-11 stages with state `"staged"`; GL-12 can transition it once to
    `"applied"` (with result counters and `applied_at`) or `"discarded"`.
    `is_initial` is decided once, when the batch is staged.
    """

    __tablename__ = "roster_import_batches"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    created_by_user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"), nullable=False
    )
    course_id: Mapped[int | None] = mapped_column(ForeignKey("courses.id"), nullable=True)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    state: Mapped[str] = mapped_column(
        String(16), nullable=False, default="staged", server_default="staged"
    )
    is_initial: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    source_display_name: Mapped[str] = mapped_column(String(255), nullable=False)
    row_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    updated_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    unchanged_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    skipped_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, server_default=func.now()
    )
    applied_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class RosterImportRow(Base):
    """One parsed, validated row of a `RosterImportBatch`, pending review.

    `raw_line`, source metadata, position and batch provenance are immutable.
    The normalized review fields, including the source-role-appropriate
    staged handicap, are editable only while GL-12 keeps the batch staged.
    `update_opt_in` is written `false` here and only GL-12's review form
    ever sets it true.
    """

    __tablename__ = "roster_import_rows"
    __table_args__ = (
        UniqueConstraint(
            "batch_id", "position", name="uq_roster_import_rows_batch_position"
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    batch_id: Mapped[int] = mapped_column(
        ForeignKey("roster_import_batches.id"), nullable=False, index=True
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    first_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_name: Mapped[str | None] = mapped_column(String(80), nullable=True)
    email: Mapped[str | None] = mapped_column(String(254), nullable=True)
    phone: Mapped[str | None] = mapped_column(String(40), nullable=True)
    tee_label: Mapped[str | None] = mapped_column(String(30), nullable=True)
    handicap_gold: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handicap_white: Mapped[int | None] = mapped_column(Integer, nullable=True)
    handicap_single: Mapped[int | None] = mapped_column(Integer, nullable=True)
    source_role: Mapped[str] = mapped_column(String(16), nullable=False)
    source_file: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_line: Mapped[str] = mapped_column(Text, nullable=False)
    included: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="1"
    )
    update_opt_in: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0"
    )
    validation_error: Mapped[str | None] = mapped_column(String(40), nullable=True)
    warnings: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
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
