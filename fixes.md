# Fixes

## Test session fixture: SAWarning "transaction already deassociated from connection"

**Issue:** The `session` fixture in `tests/conftest.py` called `transaction.rollback()` unconditionally in teardown. When a test (or garbage collection) closed the session first — the session is `sessionmaker(bind=connection)()`, bound to the connection that owns the transaction — closing the session rolled back and deassociated the transaction from the connection. The subsequent `transaction.rollback()` then raised `SAWarning: transaction already deassociated from connection`.

This appeared in 5 tests:
- `tests/test_courses.py::test_duplicate_tee_name_and_gender_in_one_course_is_rejected`
- `tests/test_courses.py::test_duplicate_scope_for_one_tee_set_is_rejected`
- `tests/test_rain_dates.py::test_preview_is_read_only_and_confirm_refuses_stale_schedule`
- `tests/test_roster.py::test_updating_to_a_duplicate_email_re_raises_and_rolls_back`
- `tests/test_roster.py::test_same_normalized_email_cannot_create_two_golfers`

**Fix:** Close the session first, then guard the rollback with `if transaction.is_active:` so a deassociated transaction is not rolled back a second time. Connection close always runs last.

```python
yield session

session.close()
if transaction.is_active:
    transaction.rollback()
connection.close()
```

**Verification:** All 1545 tests pass; no SAWarning is emitted.
