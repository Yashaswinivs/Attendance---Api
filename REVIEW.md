# REVIEW.md

Bugs found and fixed in the starter `app/main.py`, grouped by function.

## health()
- **Bug:** Always returns `{"status":"ok"}`. It never pings MongoDB, so the readiness probe lies even when the DB is down.
- **Fix:** Added `db.command("ping")` inside a `try/except`; returns 503 if the ping fails.

## create_employee()
- **Bug:** Check-then-insert is racy. Two concurrent POSTs with the same `emp_code` both pass the `find_one` check and both insert, producing duplicate employees.
- **Fix:** Added a unique index on `emp_code` at startup. Removed the `find_one` check and let the unique index enforce uniqueness. Catch `DuplicateKeyError` → 409.

- **Bug:** `datetime.now()` returns a naive local-time datetime. When returned, FastAPI serializes it to a string like `"2026-01-05T04:30:00"` instead of epoch milliseconds.
- **Fix:** Store `datetime.now(timezone.utc)` (naive UTC). Convert to epoch ms with `dt_to_ms()` before returning.

## list_employees()
- **Bug:** `skip = page * page_size` — off by one. `page=1` skips the entire first page.
- **Fix:** Changed to `skip = (page - 1) * page_size`.

- **Bug:** `total = db.employees.count_documents({})` ignores the `department` filter. Filtered results showed the whole-collection count.
- **Fix:** Changed to `count_documents(q)`.

- **Bug:** No `.sort("emp_code", 1)`. Documents came back in insertion order, not the required ascending order.
- **Fix:** Added `.sort("emp_code", 1)`.

- **Bug:** `created_at` returned as ISO string, not epoch milliseconds.
- **Fix:** Convert with `dt_to_ms()` before returning.

## punch_in()
- **Bug:** No check that the employee exists. `emp["shift_start"]` raised `TypeError` on an unknown `emp_code`, producing a 500 instead of 404.
- **Fix:** Added `if not emp: raise HTTPException(404, "employee not found")`.

- **Bug:** `datetime.fromtimestamp(ms/1000)` returns local-time naive datetime. Timezone math was wrong on any server not running IST.
- **Fix:** Use `datetime.fromtimestamp(ms/1000, tz=timezone.utc)`, then `.astimezone(IST)` for the shift math. Store naive UTC.

- **Bug:** Check-then-insert race on `(emp_code, date)`. Two concurrent punch-ins could both create records.
- **Fix:** Unique index on `(emp_code, date)` at startup; `DuplicateKeyError` → 409.

- **Bug:** Didn't truncate to whole seconds. R1 says instants are truncated before any calculation.
- **Fix:** Added `.replace(microsecond=0)` immediately after parsing.

- **Bug:** Doesn't handle the overnight-shift date rule (R1). A punch-in at 02:00 for a 22:00–06:00 shift should belong to the previous day.
- **Fix:** If the employee's shift is overnight and the IST punch time is earlier than shift_end, the record's date is set to the previous calendar day.

## compute_late_minutes()
- **Bug:** No truncation to whole seconds before comparing. Boundary `09:40:00.900` was treated as late, but R2 says only `09:40:01` and later are late.
- **Fix:** Truncate input to whole seconds before calling.

## compute_work_hours()
- **Bug:** Uses Python's `round()`, which is banker's rounding. R4 says half-up.
- **Fix:** Replaced with `round_half_up()` using `Decimal` + `ROUND_HALF_UP`.

- **Bug:** Computed `work_hours` but never `return`ed it → always None.
- **Fix:** Added `return`.

## compute_overtime()
- **Bug:** Applied IST tzinfo only to the shift-end; the punch_out side had a different timezone, causing off-by-5:30 errors.
- **Fix:** Recomputed inline in `punch_out` with a consistent IST reference.

- **Bug:** Didn't enforce the ≥ 30-minute threshold (R3). Small overtime was reported instead of 0.
- **Fix:** `int(ot_raw) if ot_raw >= 30 else 0`.

## list_attendance()
- **Bug:** Loaded the entire collection into memory, sorted in Python, sliced in Python. Would fail on ~100k records.
- **Fix:** Replaced with DB-side `.sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)` and `count_documents(q)`.

- **Bug:** Returned `_id` as `"id"` — the contract says `_id` never appears.
- **Fix:** Used `{"_id": 0}` projection.

- **Bug:** No validation that `date_from <= date_to`. Silent empty result.
- **Fix:** Explicit check → 422.

- **Bug:** `page_size` not capped at 100 (R10).
- **Fix:** Added validation.

- **Bug:** `punch_in` / `punch_out` returned as ISO strings, not epoch milliseconds.
- **Fix:** Convert with `dt_to_ms()` before returning.

## Things examined and NOT considered defects

- `MongoClient(...)` is synchronous. FastAPI runs `def` handlers in a threadpool, which is fine at this scale — switching to Motor is a bigger refactor than the assignment needs.
- `IST = timezone(timedelta(hours=5, minutes=30))` — correct offset; India does not observe DST.
- `load_dotenv()` before `MongoClient(...)` — the order is correct.
- `status_code=201` on `create_employee` and `punch-in` — matches the contract.
- `status_code=200` (default) on `punch-out` and `patch` — matches the contract.