# DECISIONS.md

**1. Indexes.** I created three:
- `employees.emp_code` (unique) — enforces the uniqueness contract and speeds up every employee lookup by code.
- `attendance_logs.(emp_code, date)` (unique) — prevents duplicate punch-ins for the same employee and day, and covers per-employee queries like the monthly summary.
- `attendance_logs.(date desc, emp_code)` — serves `GET /attendance`, which sorts by date descending then emp_code, and every analytics pipeline that matches a date range.
I considered an index on `(late_minutes, date)` for the leaderboard, but rejected it: `late_minutes` has low selectivity and the leaderboard always filters by date first, so the date index is enough.

**2. Punch-in race.** Both requests run the same `find_one` check and both find no existing record, so both reach `insert_one`. The unique index on `(emp_code, date)` accepts exactly one insert; the other raises `DuplicateKeyError`, which we catch and translate to 409. The database — not the code — is the arbiter, so there is no window for the race.

**3. Ties.** The leaderboard uses `$setWindowFields` with `$rank`, which implements standard competition ranking: ties share a rank and the next rank is skipped (1, 2, 2, 4). The `limit` filter is applied *after* ranking with `rank <= limit`. If two employees are tied at rank `limit`, both are returned, so the response can contain more rows than `limit` — that is the contract.

**4. Headcount.** The department summary queries the `employees` collection directly and filters by `joined_on <= last_day_of_month`, then aggregates logs separately per employee. Because it starts from employees (not from logs), people with zero logs are counted. Departments with zero headcount for the month are dropped.

**5. 100× data.** For 100× the records I would move the biggest Python-side work into MongoDB (the department summary currently does some per-employee post-processing in Python — a single `$lookup` + `$group` pipeline would scale better). I would also add pre-computed daily rollups so trend queries don't re-aggregate millions of logs, and switch from sync PyMongo to Motor/async so more concurrent analytics requests fit in the same event loop.