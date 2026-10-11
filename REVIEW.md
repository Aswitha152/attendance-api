# REVIEW.md

Defects I found in the starter `app/main.py`. I ran the original starter against a test database (server timezone set to UTC) to confirm each one. Rows marked *(code reading)* are visible in the code but cannot be shown with a single sequential request.

| # | Where | What is wrong | How it shows up | How I fixed it |
|---|---|---|---|---|
| 1 | `punch_in` | An unknown `emp_code` makes `emp` equal `None`, so `emp["shift_start"]` raises. | POST punch-in for `EMP9999` returns **500** instead of 404. | Look the employee up first and return 404. |
| 2 | `punch_in` | `status` accepts any string. | `"status": "banana"` returns 201 and is saved. | `Literal["PRESENT", "WFH", "ON_DUTY"]`, so other values are 422. |
| 3 | `punch_in` | Check-then-insert (`find_one`, then `insert_one`) is a race, and there is no unique index to stop it *(code reading)*. | Two simultaneous identical requests can both pass the check and create two records. `getIndexes()` shows only `_id_`. | Unique index on `(emp_code, date)`; a `DuplicateKeyError` becomes 409. |
| 4 | `punch_in` | `datetime.fromtimestamp` uses the server's local zone and returns a naive datetime, so `date` is not the IST date. | A punch at 00:30 IST on 7 July gets `date` **2026-07-06** on a UTC server. | Convert epoch ms to a UTC-aware datetime and derive the date in IST. |
| 5 | `punch_in` | The date is always the calendar date of the punch, and seconds are not truncated *(code reading)*. | An overnight 22:00-06:00 shift punched at 05:30 would be stored under the wrong day. | `attendance_date()` applies the overnight rule; instants are truncated to whole seconds. |
| 6 | `punch_in` | `punched_at: Optional[int]` is too lax. | `1783657800` (seconds) returns 201 with a record dated **1970-01-21**; the string `"1783657800000"` is also accepted. | Strict integer within a valid epoch-millisecond range, otherwise 422. |
| 7 | `compute_late_minutes` | It floors to whole minutes **before** comparing with 10, so a punch 10 minutes and a few seconds late is not treated as late. | 09:40:01 and 09:40:30 (start 09:30) both return **0**; the rule says 10. | Compare seconds with `> 600`, then floor to minutes. |
| 8 | `compute_late_minutes` | The shift start is built with `punch_in.replace(...)`, i.e. on the punch's own day, so overnight shifts break. | Overnight shift 22:00, punch at 02:00 the next day returns **0**; it should be 240. | Build the shift start from the attendance date in IST. |
| 9 | `compute_work_hours` | Python `round()` is not half-up. | 2h 40m 30s gives **2.67** (should be 2.68); 7.5 minutes gives **0.12** (should be 0.13). | Integer seconds with `Decimal` and `ROUND_HALF_UP`. |
| 10 | `compute_overtime` | No 30-minute minimum; the end of an overnight shift is placed on the same day; a timezone-aware shift end is subtracted from a naive punch time. | 10 minutes after shift end returns **10** (should be 0); overnight shift returns **1480** (should be 40); a naive `punch_out` raises **TypeError**. | Rewritten: floor to minutes, count only if 30 or more, overnight end on the next day, all datetimes timezone-aware. |
| 11 | `create_employee` | Check-then-insert on `emp_code` with no unique index *(code reading)*. | Two parallel creates with the same code can both succeed. | Unique index on `emp_code`; `DuplicateKeyError` becomes 409. |
| 12 | `create_employee` | Almost no validation. | `shift_start: "99:99"` returns 201, and so does `shift_start` equal to `shift_end`; no `emp_code` format or length limits. | Pydantic patterns and length limits, a real-date check on `joined_on`, and a shifts-differ check, so bad input is 422. |
| 13 | `create_employee` | `created_at` is naive local time and is returned as an ISO string. | Response shows `"2026-10-11T06:39:21.064318"`; the contract needs epoch milliseconds. | UTC time, returned as epoch milliseconds. |
| 14 | `list_employees` | `skip = page * page_size` skips the whole first page. | `page=1` starts at `EMP1001`; `EMP0001` can never be reached. | `(page - 1) * page_size`. |
| 15 | `list_employees` | `total` ignores the department filter; no sort, so paging is unstable; `page_size` is unbounded. | `department=Ops` gives `total` 30 but only 5 matching items; `page_size=1000` returns 200. | Count with the same filter, sort by `emp_code`, `page >= 1`, `page_size` between 1 and 100. |
| 16 | `list_attendance` | It loads every matching record into memory, then sorts and slices in Python *(code reading)*. | Memory and time grow with the collection; it will not scale to about 100,000 records. | Sort, skip and limit inside MongoDB with a supporting index; `count_documents` for the total. |
| 17 | `list_attendance` | No input validation, and it sorts by date only. | `date_from` after `date_to` returns 200; `status=banana` returns 200; ties on date have no defined order. | Validate dates and status (reversed range is 422); sort by `date` descending, then `emp_code`. |
| 18 | all attendance responses | Internal `id` (the ObjectId) is returned, times are not epoch milliseconds, and legacy records missing `history` or `half_day` come back incomplete. | Response keys include `id`, and `punch_in` is `"2026-07-06T19:00:00"`. | `record_out()` and `to_api()` convert times to epoch ms, drop `_id`, and fill in defaults. |
| 19 | `/health` | Always returns ok, even when MongoDB is down *(code reading)*. | Stop the database and it still says `{"status": "ok"}`. | Ping MongoDB; return 503 if it fails. |
| 20 | startup, `MongoClient` | No indexes are created, and the client returns naive datetimes. | Index list is only `['_id_']`; naive datetimes caused the TypeError in row 10. | `create_indexes()` runs at startup (idempotent); `MongoClient(tz_aware=True)` with a 5 second selection timeout. |

## Checked and fine

- `load_dotenv()` does not override variables already set in the environment, which matches the requirement that real environment variables win.
- The `IST` constant (UTC+05:30) is correct.
- The paginated response shape (`items`, `total`, `page`, `page_size`) matches the contract.
- Creating an employee with an existing `emp_code` already returns 409 for sequential requests; the problem is only the race (row 11).