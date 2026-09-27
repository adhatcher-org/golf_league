"""Session-taking staging for CSV roster imports.

Routes call `stage_batch`, `get_batch`, `review_rows`, `revalidate_batch`
and `purge_expired`. May import `sqlalchemy` and `golf_league.models`;
never `fastapi` — this module never sees an `UploadFile`, only the plain
`(name, bytes)` pairs the router reads off the request.
"""

import csv
import io
from collections.abc import Sequence
from datetime import datetime, timedelta

from sqlalchemy import delete, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from golf_league.domain.roster import (
    derive_handicap_status,
    normalize_email,
    normalize_name,
    normalize_phone,
)
from golf_league.domain.roster_import import (
    MAX_DATA_ROWS,
    MAX_PHONE_LENGTH,
    MAX_UPLOAD_BYTES,
    decode_warnings,
    detect_header_set,
    email_is_wellformed,
    encode_warnings,
    likely_reversed,
    near_miss_domain,
    normalize_email_cell,
    parse_handicap_cell,
    phones_equal,
    resolve_status,
    sanitize_display_name,
    selected_handicap,
    split_regular_name,
)
from golf_league.models import (
    Course,
    Golfer,
    RosterImportBatch,
    RosterImportRow,
    TeeSet,
)


class ImportValidationError(Exception):
    """Raised when an upload is rejected; carries field -> message errors.

    Mirrors `golf_league.services.roster.RosterValidationError`: the router
    re-renders the upload form with `errors` and 422 and nothing is written.
    """

    def __init__(self, errors: dict[str, str]):
        self.errors = errors
        super().__init__(str(errors))


class ImportConflictError(Exception):
    """Raised when an optimistic staged-batch mutation lost its race."""


class ImportNotFoundError(Exception):
    """Raised when a requested batch or row is absent from its URL scope."""


class ImportApplyError(Exception):
    """Raised when included rows still have hard errors at apply time."""


def _reject(field: str, message: str) -> None:
    raise ImportValidationError({field: message})


def _cell(row: Sequence[str], index: int) -> str:
    """Cell `index` of `row`, or `""` when the row is too short."""
    return row[index] if index < len(row) else ""


def _build_regular_row(cells: Sequence[str]) -> dict[str, object]:
    """Parse one `summer_regular` data row into its stored, normalized fields."""
    name_cell = _cell(cells, 0)
    status_cell = _cell(cells, 1)
    gold_cell = _cell(cells, 2)
    white_cell = _cell(cells, 3)
    email_cell = _cell(cells, 4)
    phone_cell = _cell(cells, 5)

    split = split_regular_name(name_cell)
    first_name, last_name = split if split is not None else (None, None)

    status_stripped = status_cell.strip()
    tee_label = resolve_status(status_cell)

    handicap_gold, gold_ok = parse_handicap_cell(gold_cell)
    handicap_white, white_ok = parse_handicap_cell(white_cell)

    email_value, email_warnings = normalize_email_cell(email_cell)
    phone_value = normalize_phone(phone_cell)

    static_error = None
    if first_name is None or last_name is None:
        static_error = "name_unparseable"
    elif email_value is None:
        static_error = "email_missing"
    elif not email_is_wellformed(email_value):
        static_error = "email_malformed"
    elif tee_label is None and not status_stripped:
        static_error = "status_blank"
    elif tee_label is None:
        static_error = "status_unknown"
    elif not gold_ok or not white_ok:
        static_error = "handicap_invalid"
    elif phone_value is not None and len(phone_value) > MAX_PHONE_LENGTH:
        static_error = "phone_invalid"

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email_value,
        "phone": phone_value,
        "tee_label": tee_label,
        "handicap_gold": handicap_gold,
        "handicap_white": handicap_white,
        "handicap_single": None,
        "static_error": static_error,
        "stage_warnings": email_warnings,
    }


def _build_sub_row(cells: Sequence[str]) -> dict[str, object]:
    """Parse one `summer_sub` data row into its stored, normalized fields."""
    first_cell = _cell(cells, 0)
    last_cell = _cell(cells, 1)
    phone_cell = _cell(cells, 2)
    email_cell = _cell(cells, 3)
    handicap_cell = _cell(cells, 4)

    first_name = normalize_name(first_cell) or None
    last_name = normalize_name(last_cell) or None
    name_ok = first_name is not None and last_name is not None

    handicap_single, handicap_ok = parse_handicap_cell(handicap_cell)
    email_value, email_warnings = normalize_email_cell(email_cell)
    phone_value = normalize_phone(phone_cell)

    static_error = None
    if not name_ok:
        static_error = "name_unparseable"
    elif email_value is None:
        static_error = "email_missing"
    elif not email_is_wellformed(email_value):
        static_error = "email_malformed"
    elif not handicap_ok:
        static_error = "handicap_invalid"
    elif phone_value is not None and len(phone_value) > MAX_PHONE_LENGTH:
        static_error = "phone_invalid"

    return {
        "first_name": first_name,
        "last_name": last_name,
        "email": email_value,
        "phone": phone_value,
        "tee_label": None,
        "handicap_gold": None,
        "handicap_white": None,
        "handicap_single": handicap_single,
        "static_error": static_error,
        "stage_warnings": email_warnings,
    }


def _parse_csv_file(display_name: str, text: str) -> dict[str, object]:
    """Parse one decoded CSV file: header role plus its data rows.

    Raises `ImportValidationError` (field `files`) for an unrecognized
    header set, an empty file, or malformed CSV. Never raises for a
    recognized header with zero data rows — the caller checks that
    separately so every file's header is checked before any file's row
    count is.
    """
    lines = text.splitlines()
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader, None)
    except csv.Error:
        _reject("files", "Unrecognized column headers.")
    if not header:
        _reject("files", "Unrecognized column headers.")
    role = detect_header_set(header)
    if role is None:
        _reject("files", "Unrecognized column headers.")

    data_rows: list[tuple[int, str, list[str]]] = []
    counter = 0
    try:
        while True:
            start = reader.line_num
            row = next(reader, None)
            if row is None:
                break
            end = reader.line_num
            if all(cell.strip() == "" for cell in row):
                continue
            counter += 1
            raw_line = "\n".join(lines[start:end])
            data_rows.append((counter, raw_line, row))
    except csv.Error:
        _reject("files", "Unrecognized column headers.")

    return {"display_name": display_name, "role": role, "data_rows": data_rows}


def _validate_course(session: Session, course_id: str) -> Course:
    """Step 2: `course_id` must decimal-name a real `courses` row."""
    if not isinstance(course_id, str) or not course_id.isdecimal():
        _reject("course_id", "Choose a course.")
    course = session.get(Course, int(course_id))
    if course is None:
        _reject("course_id", "Choose a course.")
    return course


def _kept_files(files: Sequence[tuple[str, bytes]]) -> list[tuple[str, bytes]]:
    """Step 3: drop the empty part an unfilled file input sends."""
    kept = [(name, content) for name, content in files if name or content]
    if not kept:
        _reject("files", "Choose at least one CSV file.")
    return kept


def _check_total_size(kept_files: list[tuple[str, bytes]]) -> None:
    """Step 4: size, summed across the whole request."""
    total_bytes = sum(len(content) for _, content in kept_files)
    if total_bytes > MAX_UPLOAD_BYTES:
        _reject("files", "Upload is larger than 2 MB.")


def _decode_files(kept_files: list[tuple[str, bytes]]) -> list[tuple[str, str]]:
    """Step 5: UTF-8 (with optional BOM) decoding."""
    decoded: list[tuple[str, str]] = []
    for name, content in kept_files:
        try:
            text = content.decode("utf-8-sig")
        except UnicodeDecodeError:
            _reject("files", "File is not valid UTF-8 text.")
        decoded.append((name, text))
    return decoded


def _check_row_counts(parsed_files: list[dict[str, object]]) -> None:
    """Steps 7 and 8: at least one data row per file, at most 1000 overall."""
    for entry in parsed_files:
        if not entry["data_rows"]:
            _reject("files", f"{entry['display_name']} has no data rows.")

    total_rows = sum(len(entry["data_rows"]) for entry in parsed_files)
    if total_rows > MAX_DATA_ROWS:
        _reject("files", "More than 1000 rows in one upload.")


def _require_resolved_tees(session: Session, course_id: int) -> None:
    """Step 9: the course must resolve exactly one men's Gold and White tee."""
    tee_ids = _resolved_tee_ids(session, course_id)
    if "Gold" not in tee_ids or "White" not in tee_ids:
        _reject(
            "course_id",
            "Course needs exactly one men's Gold and one men's White tee.",
        )


def _build_all_rows(
    parsed_files: list[dict[str, object]],
) -> list[tuple[dict[str, object], str, str, int, str]]:
    """Step 10 (part 1): parse every data row, in file-submission order."""
    built_rows: list[tuple[dict[str, object], str, str, int, str]] = []
    for entry in parsed_files:
        role = entry["role"]
        display_name = entry["display_name"]
        for source_row, raw_line, cells in entry["data_rows"]:
            built = (
                _build_regular_row(cells)
                if role == "summer_regular"
                else _build_sub_row(cells)
            )
            built_rows.append((built, role, display_name, source_row, raw_line))
    return built_rows


def _next_batch_is_initial(session: Session) -> bool:
    """True exactly when no batch is `is_initial` and `applied` yet."""
    return (
        session.execute(
            select(RosterImportBatch.id).where(
                RosterImportBatch.is_initial.is_(True),
                RosterImportBatch.state == "applied",
            )
        ).first()
        is None
    )


def _insert_batch_and_rows(
    session: Session,
    *,
    created_by_user_id: int,
    course_id: int,
    is_initial: bool,
    source_display_name: str,
    built_rows: list[tuple[dict[str, object], str, str, int, str]],
    now: datetime,
) -> RosterImportBatch:
    """Step 10 (part 2): insert the batch, then its rows, in `position` order."""
    batch = RosterImportBatch(
        created_by_user_id=created_by_user_id,
        course_id=course_id,
        version=1,
        state="staged",
        is_initial=is_initial,
        source_display_name=source_display_name,
        row_count=len(built_rows),
        created_count=0,
        updated_count=0,
        unchanged_count=0,
        skipped_count=0,
        created_at=now,
        applied_at=None,
        expires_at=now + timedelta(days=90),
    )
    session.add(batch)
    session.flush()

    position = 0
    for built, role, display_name, source_row, raw_line in built_rows:
        position += 1
        session.add(
            RosterImportRow(
                batch_id=batch.id,
                position=position,
                version=1,
                first_name=built["first_name"],
                last_name=built["last_name"],
                email=built["email"],
                phone=built["phone"],
                tee_label=built["tee_label"],
                handicap_gold=built["handicap_gold"],
                handicap_white=built["handicap_white"],
                handicap_single=built["handicap_single"],
                source_role=role,
                source_file=display_name,
                source_row=source_row,
                raw_line=raw_line,
                included=True,
                update_opt_in=False,
                validation_error=built["static_error"],
                warnings=encode_warnings(built["stage_warnings"]),
            )
        )
    session.flush()
    return batch


def stage_batch(
    session: Session,
    *,
    created_by_user_id: int,
    course_id: str,
    files: Sequence[tuple[str, bytes]],
    now: datetime,
) -> int:
    """Validate and stage one batch; return the new batch id.

    `files` is `(original_file_name, content_bytes)` per uploaded file, in the
    order submitted. `now` is a naive UTC datetime supplied by the caller.
    Runs the ordered rejection steps below, raising `ImportValidationError`
    and writing nothing on the first failure; otherwise inserts the batch and
    its rows, calls `revalidate_batch`, and commits exactly once.
    """
    course = _validate_course(session, course_id)
    kept_files = _kept_files(files)
    _check_total_size(kept_files)
    decoded = _decode_files(kept_files)

    # Step 6: header set, for every file, in the order submitted.
    parsed_files = [
        _parse_csv_file(sanitize_display_name(name), text) for name, text in decoded
    ]
    _check_row_counts(parsed_files)
    _require_resolved_tees(session, course.id)

    built_rows = _build_all_rows(parsed_files)
    is_initial = _next_batch_is_initial(session)
    source_display_name = ", ".join(
        entry["display_name"] for entry in parsed_files
    )[:255]

    batch = _insert_batch_and_rows(
        session,
        created_by_user_id=created_by_user_id,
        course_id=course.id,
        is_initial=is_initial,
        source_display_name=source_display_name,
        built_rows=built_rows,
        now=now,
    )

    revalidate_batch(session, batch.id)
    session.commit()
    return batch.id


def get_batch(session: Session, batch_id: int) -> RosterImportBatch | None:
    """The batch row, or None when no batch has that id."""
    return session.get(RosterImportBatch, batch_id)


def review_rows(session: Session, batch_id: int) -> list[dict[str, object]]:
    """One dict per row of `batch_id`, in `position` order, for the review page.

    Each dict carries every column of the row plus `warning_codes`
    (`decode_warnings(row.warnings)`) and `near_miss_suggestion` — the domain
    `near_miss_domain` suggestion for this row's email when the row carries
    `email_domain_near_miss`, else None. The suggestion is recomputed here
    rather than stored: no column holds it.
    """
    rows = list(
        session.execute(
            select(RosterImportRow)
            .where(RosterImportRow.batch_id == batch_id)
            .order_by(RosterImportRow.position)
        ).scalars()
    )
    result: list[dict[str, object]] = []
    for row in rows:
        codes = decode_warnings(row.warnings)
        suggestion = None
        if "email_domain_near_miss" in codes and row.email and "@" in row.email:
            domain = row.email.rsplit("@", 1)[-1]
            suggestion = near_miss_domain(domain)
        result.append(
            {
                "id": row.id,
                "batch_id": row.batch_id,
                "position": row.position,
                "version": row.version,
                "first_name": row.first_name,
                "last_name": row.last_name,
                "email": row.email,
                "phone": row.phone,
                "tee_label": row.tee_label,
                "handicap_gold": row.handicap_gold,
                "handicap_white": row.handicap_white,
                "handicap_single": row.handicap_single,
                "source_role": row.source_role,
                "source_file": row.source_file,
                "source_row": row.source_row,
                "raw_line": row.raw_line,
                "included": row.included,
                "update_opt_in": row.update_opt_in,
                "validation_error": row.validation_error,
                "warnings": row.warnings,
                "warning_codes": codes,
                "near_miss_suggestion": suggestion,
            }
        )
    return result


def _resolved_tee_ids(session: Session, course_id: int) -> dict[str, int]:
    """`{"Gold": id, "White": id}` for the men's tees of `course_id`.

    A label missing from the result means it did not resolve to exactly
    one tee set; callers treat that as "cannot compare" rather than raise,
    since tee resolution was already enforced at stage time.
    """
    resolved: dict[str, int] = {}
    for label in ("Gold", "White"):
        matches = list(
            session.execute(
                select(TeeSet.id).where(
                    TeeSet.course_id == course_id,
                    TeeSet.gender == "men",
                    TeeSet.color_label == label,
                )
            ).scalars()
        )
        if len(matches) == 1:
            resolved[label] = matches[0]
    return resolved


def _protected_field_changed(
    row: RosterImportRow, golfer: Golfer, tee_ids: dict[str, int]
) -> bool:
    """True when `row` differs from its matched `golfer` in a protected field.

    A blank cell on the row is never a difference (R-IMPORT-MATCHING). By
    the time this runs, `row.first_name`/`row.last_name` are always
    populated (a name that failed to parse is `name_unparseable`, checked
    earlier), so only phone, tee and handicap can be legitimately blank.
    """
    if row.first_name != golfer.first_name or row.last_name != golfer.last_name:
        return True
    if row.phone is not None and not phones_equal(row.phone, golfer.phone):
        return True
    if row.tee_label is not None:
        resolved_id = tee_ids.get(row.tee_label)
        if resolved_id is not None and resolved_id != golfer.default_tee_set_id:
            return True
    selected = selected_handicap(
        source_role=row.source_role,
        tee_label=row.tee_label,
        handicap_gold=row.handicap_gold,
        handicap_white=row.handicap_white,
        handicap_single=row.handicap_single,
    )
    if selected is not None and selected != golfer.handicap_strokes:
        return True
    return False


def revalidate_batch(session: Session, batch_id: int) -> None:
    """Recompute `validation_error` and `warnings` for every row of `batch_id`
    against the golfers table as it stands, and write them back. Never touches
    `golfers`, `raw_line`, `included` or `update_opt_in`. Does not commit: the
    caller owns the transaction. GL-12 calls this after a row edit and again at
    apply, which is why it is one reusable function rather than inline code.

    `status_blank`, `status_unknown` and `handicap_invalid` cannot be told
    apart from a blank cell once only the normalized `tee_label` /
    `handicap_*` columns remain (a malformed cell and a blank one both
    normalize to NULL) — this task calls this function exactly once per
    batch, immediately after staging, and relies on the `validation_error`
    already written by `stage_batch`'s row construction (which still had
    the raw cell) to break that tie. A future edit to `tee_label` is
    re-checked against its own current NULL-ness first, so an edit that
    supplies a real tee always clears the code regardless of what was
    frozen; only a *blank* edit could, in principle, re-read a stale
    `status_unknown` instead of `status_blank` — `handicap_gold`,
    `handicap_white` and `handicap_single` are never edited at all, so
    `handicap_invalid` has no such staleness risk.
    """
    batch = session.get(RosterImportBatch, batch_id)
    if batch is None:
        return

    rows = list(
        session.execute(
            select(RosterImportRow)
            .where(RosterImportRow.batch_id == batch_id)
            .order_by(RosterImportRow.position)
        ).scalars()
    )
    if not rows:
        return

    frozen_errors = {row.id: row.validation_error for row in rows}
    duplicate_emails = _duplicate_emails(rows)
    golfers_by_email = _golfers_by_email(session, rows)
    tee_ids = _resolved_tee_ids(session, batch.course_id)
    reversed_positions = likely_reversed(
        [
            (row.position, row.first_name or "", row.last_name or "", row.email)
            for row in rows
        ]
    )

    for row in rows:
        golfer = golfers_by_email.get(row.email) if row.email is not None else None
        code = _compute_validation_code(
            row, batch, duplicate_emails, golfer, tee_ids, frozen_errors[row.id]
        )
        warnings = _compute_row_warnings(row, batch, golfer, reversed_positions)
        row.validation_error = code
        row.warnings = encode_warnings(warnings)


def _duplicate_emails(rows: list[RosterImportRow]) -> set[str]:
    """Normalized emails shared by two or more rows of the same batch."""
    counts: dict[str, int] = {}
    for row in rows:
        if row.email is not None:
            counts[row.email] = counts.get(row.email, 0) + 1
    return {email for email, count in counts.items() if count > 1}


def _golfers_by_email(
    session: Session, rows: list[RosterImportRow]
) -> dict[str, Golfer]:
    """Every existing golfer matching a row's email, keyed by that email."""
    emails = [row.email for row in rows if row.email is not None]
    result: dict[str, Golfer] = {}
    if not emails:
        return result
    for golfer in session.execute(
        select(Golfer).where(Golfer.email.in_(emails))
    ).scalars():
        if golfer.email is not None:
            result[golfer.email] = golfer
    return result


def _compute_validation_code(
    row: RosterImportRow,
    batch: RosterImportBatch,
    duplicate_emails: set[str],
    golfer: Golfer | None,
    tee_ids: dict[str, int],
    frozen: str | None,
) -> str | None:
    """The first `HARD_ERRORS` code that applies to `row`, or None."""
    if not row.first_name or not row.last_name:
        return "name_unparseable"
    if row.email is None:
        return "email_missing"
    if not email_is_wellformed(row.email):
        return "email_malformed"
    if row.email in duplicate_emails:
        return "email_duplicate_in_batch"
    if row.source_role == "summer_regular" and row.tee_label is None:
        return "status_unknown" if frozen == "status_unknown" else "status_blank"
    if frozen == "handicap_invalid":
        return "handicap_invalid"
    if row.phone is not None and len(row.phone) > MAX_PHONE_LENGTH:
        return "phone_invalid"
    if golfer is None and row.tee_label is None and not batch.is_initial:
        return "tee_required_for_new_golfer"
    if (
        golfer is not None
        and not row.update_opt_in
        and _protected_field_changed(row, golfer, tee_ids)
    ):
        return "protected_field_changed"
    return None


def _edit_handicap(value: str) -> int | None:
    parsed, valid = parse_handicap_cell(value)
    if not valid:
        raise ImportValidationError({"handicap": "Handicap must be a whole number."})
    return parsed


def _require_row_in_batch(
    batch: RosterImportBatch | None, row: RosterImportRow | None, batch_id: int
) -> tuple[RosterImportBatch, RosterImportRow]:
    """Return matching persisted rows, or hide absent/cross-batch rows as 404."""
    if batch is None or row is None or row.batch_id != batch_id:
        raise ImportNotFoundError()
    return batch, row


def edit_staged_row(
    session: Session,
    *,
    batch_id: int,
    batch_version: int,
    row_id: int,
    row_version: int,
    action: str,
    first_name: str,
    last_name: str,
    email: str,
    phone: str,
    tee_label: str,
    handicap_gold: str,
    handicap_white: str,
    handicap_single: str,
    included: bool,
    update_opt_in: bool,
) -> None:
    """Optimistically edit one normalized staged row and revalidate its batch."""
    batch = session.get(RosterImportBatch, batch_id)
    row = session.get(RosterImportRow, row_id)
    batch, row = _require_row_in_batch(batch, row, batch_id)
    if (
        batch.state != "staged"
        or batch.version != batch_version
        or row.version != row_version
    ):
        raise ImportConflictError()

    if action == "swap_names":
        first_name, last_name = row.last_name or "", row.first_name or ""
        email, phone, tee_label = row.email or "", row.phone or "", row.tee_label or ""
        handicap_gold = "" if row.handicap_gold is None else str(row.handicap_gold)
        handicap_white = "" if row.handicap_white is None else str(row.handicap_white)
        handicap_single = "" if row.handicap_single is None else str(row.handicap_single)
        included, update_opt_in = row.included, row.update_opt_in
    elif action == "accept_domain_suggestion":
        if not row.email or "@" not in row.email:
            raise ImportValidationError({"email": "No domain suggestion is available."})
        suggestion = near_miss_domain(row.email.rsplit("@", 1)[-1])
        if suggestion is None:
            raise ImportValidationError({"email": "No domain suggestion is available."})
        email = f"{row.email.rsplit('@', 1)[0]}@{suggestion}"
        first_name, last_name, phone = row.first_name or "", row.last_name or "", row.phone or ""
        tee_label = row.tee_label or ""
        handicap_gold = "" if row.handicap_gold is None else str(row.handicap_gold)
        handicap_white = "" if row.handicap_white is None else str(row.handicap_white)
        handicap_single = "" if row.handicap_single is None else str(row.handicap_single)
        included, update_opt_in = row.included, row.update_opt_in
    elif action != "edit":
        raise ImportValidationError({"action": "Unknown row action."})

    values: dict[str, object] = {
        "first_name": normalize_name(first_name) or None,
        "last_name": normalize_name(last_name) or None,
        "email": normalize_email(email),
        "phone": normalize_phone(phone),
        "included": included,
        "update_opt_in": update_opt_in,
        "version": row.version + 1,
    }
    if row.source_role == "summer_regular":
        label = tee_label.strip()
        if label not in ("", "Gold", "White"):
            raise ImportValidationError({"tee_label": "Choose Gold or White."})
        values.update(
            tee_label=label or None,
            handicap_gold=_edit_handicap(handicap_gold),
            handicap_white=_edit_handicap(handicap_white),
            validation_error=None if action == "edit" else row.validation_error,
        )
    else:
        values.update(
            handicap_single=_edit_handicap(handicap_single),
            validation_error=None if action == "edit" else row.validation_error,
        )

    claimed_batch = session.execute(
        update(RosterImportBatch)
        .where(
            RosterImportBatch.id == batch_id,
            RosterImportBatch.state == "staged",
            RosterImportBatch.version == batch_version,
        )
        .values(version=batch_version + 1)
    )
    claimed_row = session.execute(
        update(RosterImportRow)
        .where(
            RosterImportRow.id == row_id,
            RosterImportRow.batch_id == batch_id,
            RosterImportRow.version == row_version,
        )
        .values(**values)
    )
    if claimed_batch.rowcount != 1 or claimed_row.rowcount != 1:
        session.rollback()
        raise ImportConflictError()
    session.expire_all()
    revalidate_batch(session, batch_id)
    session.commit()


def discard_batch(
    session: Session, *, batch_id: int, batch_version: int
) -> None:
    """Transition a still-current staged batch to discarded, retaining its rows."""
    result = session.execute(
        update(RosterImportBatch)
        .where(
            RosterImportBatch.id == batch_id,
            RosterImportBatch.state == "staged",
            RosterImportBatch.version == batch_version,
        )
        .values(state="discarded", version=batch_version + 1)
    )
    if result.rowcount != 1:
        session.rollback()
        raise ImportConflictError()
    session.commit()


def _persisted_handicap(row: RosterImportRow, batch: RosterImportBatch) -> int | None:
    selected = selected_handicap(
        source_role=row.source_role,
        tee_label=row.tee_label,
        handicap_gold=row.handicap_gold,
        handicap_white=row.handicap_white,
        handicap_single=row.handicap_single,
    )
    return 0 if selected is None and batch.is_initial else selected


def _apply_row(
    session: Session,
    row: RosterImportRow,
    batch: RosterImportBatch,
    tee_ids: dict[str, int],
    counters: dict[str, int],
) -> None:
    """Apply one already-revalidated included row, or count it as skipped."""
    if not row.included:
        counters["skipped"] += 1
        return
    golfer = session.execute(
        select(Golfer).where(Golfer.email == row.email)
    ).scalar_one_or_none()
    tee_id = tee_ids.get(row.tee_label or "")
    if tee_id is None and batch.is_initial:
        tee_id = tee_ids["White"]
    handicap = _persisted_handicap(row, batch)
    if golfer is None:
        if tee_id is None:
            raise ImportApplyError()
        session.add(Golfer(
            first_name=row.first_name or "", last_name=row.last_name or "",
            email=row.email, phone=row.phone, default_tee_set_id=tee_id,
            handicap_strokes=handicap, handicap_source="imported",
            handicap_status=derive_handicap_status(
                email=row.email, handicap_strokes=handicap
            ),
        ))
        counters["created"] += 1
        return
    changes = _existing_golfer_changes(row, golfer, tee_id, handicap)
    if changes:
        for key, value in changes.items():
            setattr(golfer, key, value)
        counters["updated"] += 1
    else:
        counters["unchanged"] += 1


def _existing_golfer_changes(
    row: RosterImportRow, golfer: Golfer, tee_id: int | None, handicap: int | None
) -> dict[str, object]:
    """Return the nonblank import values that differ from an existing golfer."""
    changes: dict[str, object] = {}
    if row.first_name != golfer.first_name:
        changes["first_name"] = row.first_name
    if row.last_name != golfer.last_name:
        changes["last_name"] = row.last_name
    if row.phone is not None and not phones_equal(row.phone, golfer.phone):
        changes["phone"] = row.phone
    if tee_id is not None and tee_id != golfer.default_tee_set_id:
        changes["default_tee_set_id"] = tee_id
    if handicap is not None and handicap != golfer.handicap_strokes:
        changes.update(
            handicap_strokes=handicap,
            handicap_source="imported",
            handicap_status=derive_handicap_status(
                email=row.email, handicap_strokes=handicap
            ),
        )
    return changes


def apply_batch(
    session: Session, *, batch_id: int, batch_version: int, now: datetime
) -> dict[str, int]:
    """Atomically revalidate and apply a staged batch by normalized email."""
    batch = session.get(RosterImportBatch, batch_id)
    if batch is None or batch.state != "staged" or batch.version != batch_version:
        raise ImportConflictError()
    try:
        revalidate_batch(session, batch_id)
        rows = list(session.execute(select(RosterImportRow).where(
            RosterImportRow.batch_id == batch_id).order_by(RosterImportRow.position)
        ).scalars())
        errors = [row for row in rows if row.included and row.validation_error]
        if errors:
            session.rollback()
            raise ImportApplyError()
        tee_ids = _resolved_tee_ids(session, batch.course_id)
        counters = {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0}
        for row in rows:
            _apply_row(session, row, batch, tee_ids, counters)
        claimed = session.execute(
            update(RosterImportBatch)
            .where(RosterImportBatch.id == batch_id, RosterImportBatch.state == "staged", RosterImportBatch.version == batch_version)
            .values(state="applied", version=batch_version + 1, applied_at=now,
                    created_count=counters["created"], updated_count=counters["updated"],
                    unchanged_count=counters["unchanged"], skipped_count=counters["skipped"])
        )
        if claimed.rowcount != 1:
            session.rollback()
            raise ImportConflictError()
        session.commit()
        return counters
    except ImportApplyError:
        session.rollback()
        raise
    except IntegrityError:
        session.rollback()
        raise ImportConflictError() from None


def _compute_row_warnings(
    row: RosterImportRow,
    batch: RosterImportBatch,
    golfer: Golfer | None,
    reversed_positions: set[int],
) -> set[str]:
    """Every warning code that applies to `row` (R-IMPORT-ISSUES)."""
    selected = selected_handicap(
        source_role=row.source_role,
        tee_label=row.tee_label,
        handicap_gold=row.handicap_gold,
        handicap_white=row.handicap_white,
        handicap_single=row.handicap_single,
    )
    warnings: set[str] = set(decode_warnings(row.warnings)) & {
        "email_whitespace_trimmed",
        "email_lowercased",
    }

    landing_eligible = golfer is None and batch.is_initial
    if selected is None:
        warnings.add("handicap_defaulted_zero" if landing_eligible else "handicap_blank")
    if landing_eligible and row.tee_label is None:
        warnings.add("tee_defaulted_white")

    if row.email is not None and "@" in row.email:
        domain = row.email.rsplit("@", 1)[-1]
        if near_miss_domain(domain) is not None:
            warnings.add("email_domain_near_miss")

    if row.position in reversed_positions:
        warnings.add("name_possibly_reversed")

    return warnings


def purge_expired(session: Session, *, now: datetime) -> tuple[int, int]:
    """Delete the rows, then the batches, whose `expires_at` is before `now`.

    Whatever their `state`. Returns `(rows_deleted, batches_deleted)` and
    commits once. Nothing is purged automatically and no scheduler exists
    (R-IMPORT-FILES).
    """
    expired_batch_ids = list(
        session.execute(
            select(RosterImportBatch.id).where(RosterImportBatch.expires_at < now)
        ).scalars()
    )
    if not expired_batch_ids:
        session.commit()
        return (0, 0)

    rows_deleted = session.execute(
        delete(RosterImportRow).where(RosterImportRow.batch_id.in_(expired_batch_ids))
    ).rowcount
    batches_deleted = session.execute(
        delete(RosterImportBatch).where(RosterImportBatch.id.in_(expired_batch_ids))
    ).rowcount
    session.commit()
    return (rows_deleted, batches_deleted)


__all__ = [
    "ImportValidationError",
    "stage_batch",
    "get_batch",
    "review_rows",
    "revalidate_batch",
    "edit_staged_row",
    "discard_batch",
    "apply_batch",
    "ImportConflictError",
    "ImportNotFoundError",
    "ImportApplyError",
    "purge_expired",
]
