# Employee Attendance & Analytics API

FastAPI and MongoDB backend service for managing employee attendance, punch-in/out records, manual regularization, and department/employee analytics.

Built for the HROne Software Engineer Trainee assignment adhering strictly to the `openapi.yaml` contract, `DATA_MODEL.md` schemas, and business rules R1–R10.

---

## 1. Requirements & Prerequisites

- **Python**: 3.11+ (Tested on Python 3.12)
- **MongoDB**: 6.0+ (Tested on MongoDB 8.0)
- **FastAPI**, **Uvicorn**, **PyMongo**, **Pydantic**

---

## 2. Installation & Setup

### Virtual Environment Setup

```bash
# Create virtual environment
python -m venv .venv

# Activate on Windows PowerShell:
.venv\Scripts\Activate.ps1

# Activate on Linux/macOS:
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

### Environment Configuration

The application reads configuration from environment variables (a local `.env` is loaded if present, but real environment variables take precedence):

```bash
# Linux/macOS
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"

# Windows PowerShell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

---

## 3. Loading Sample Data

To load the sample documents provided in `sample_data/`:

```bash
python sample_seed.py
```

This seeds 6 employee documents and 9 sample attendance records demonstrating all standard, overnight, late, overtime, absent, and regularized document shapes.

---

## 4. Starting the Application

Start the API server using the standard evaluator command:

```bash
uvicorn app.main:app --port 8000
```

The application creates all required MongoDB indexes idempotently during startup via the FastAPI `lifespan` handler.

Verify the readiness probe:
```bash
curl http://localhost:8000/health
# Returns: {"status": "ok"}
```

---

## 5. Running the Automated Tests

Automated tests are implemented using `pytest` and `fastapi.testclient.TestClient`.

Run the test suite:

```bash
pytest -v
```

The suite covers:
- Readiness probe with database ping.
- Employee creation, schema validation, unique `emp_code` constraints, and duplicate rejection.
- Employee listing, sorting by `emp_code`, and filtered pagination totals.
- Punch-in instant truncation, overnight shift assignment, and race safety.
- Punch-out validation, atomic updates, and duplicate punch-out rejection.
- Late boundary calculations (grace period at 10:00 vs 10:01).
- Work hours rounding (Decimal `ROUND_HALF_UP`) and overtime thresholding (>= 30 mins).
- Half-day classification (< 4.50 hours).
- Manual regularization, field recalculation, and append-only audit trail.
- Aggregation analytics: employee monthly summary, department summary, late leaderboard with competition ranking, and department daily trend with gap filling (`$densify`).
- MongoDB `explain` endpoint checking query plans and index usage.

---

## 6. Business Rules Summary (R1–R10)

- **R1 (Timezone & Attendance Date)**: Shift times and attendance dates are in IST (`UTC+05:30`). A punch-in's attendance date is its IST calendar date, except for overnight shifts (`shift_end <= shift_start`) where a punch-in before `shift_end` IST belongs to the shift starting the previous day. All instants are truncated to whole seconds before calculation.
- **R2 (Late Minutes)**: Grace period is 10 minutes. A punch-in is late only if it is strictly more than 10:00 after `shift_start`. If late, `late_minutes = floor(minutes since shift_start)`.
- **R3 (Overtime)**: Overtime is whole minutes from `shift_end` to `punch_out`, counted only if >= 30 minutes; otherwise 0.
- **R4 (Work Hours)**: `(punch_out - punch_in) in seconds / 3600`, rounded to 2 decimal places half-up (`ROUND_HALF_UP`).
- **R5 (Half Day)**: When rounded `work_hours < 4.50`, `half_day = True` (counts as 0.5 of a present day).
- **R6 (Presence)**: `PRESENT`, `WFH`, and `ON_DUTY` count as present; `ABSENT` and `LEAVE` do not.
- **R7 (Working Days)**: Mon–Fri, no holiday calendar. For mid-month joiners, counted from `joined_on`. `present_days` only counts Mon–Fri records.
- **R8 (Rounding)**: 2 decimals half-up for metrics; 4 decimals half-up for rates.
- **R9 (Headcount)**: Employees with `joined_on` on or before the period end, whether or not they have attendance logs.
- **R10 (Pagination)**: 1-indexed `page`, `page_size` max 100, `total` reflects active filters.

---

## 7. MongoDB Indexes & Analytics Design

### Indexes Created at Startup
- `employees`:
  - `{emp_code: 1}` (Unique): Fast employee lookup and business-key uniqueness.
  - `{department: 1, emp_code: 1}`: Paginated and sorted employee listing.
  - `{department: 1, joined_on: 1}`: Department headcount filtering.
  - `{joined_on: 1}`: Global join-date lookups.
- `attendance_logs`:
  - `{emp_code: 1, date: 1}` (Unique): Enforces at most one log per employee per day and accelerates employee monthly aggregations.
  - `{date: -1, emp_code: 1}`: Powers `GET /attendance` default reverse chronological sorting.
  - `{emp_code: 1, date: -1}`: Optimizes employee-filtered log history.
  - `{emp_code: 1, punch_in: -1}`: Accelerates finding the open punch-in during punch-out.
  - `{date: 1, late_minutes: 1}`: Powers late leaderboard filtering with index scans (`IXSCAN`).

### Aggregation Pipelines
- **Employee Monthly Summary**: Aggregates stored metrics (`late_minutes`, `overtime_minutes`, `half_day`) with weekday checks for working-day presence.
- **Department Summary**: Starts on `employees` (to capture zero-log employees) and performs a left outer `$lookup` into `attendance_logs`.
- **Late Leaderboard**: Uses `$setWindowFields` with `$rank` for standard competition ranking (1, 2, 2, 4 ties) and filters by `rank <= limit`.
- **Department Trend**: Aggregates presence counts, uses `$unionWith` sentinel and `$densify` for gap filling across all calendar days, and computes a 7-day moving average using `$setWindowFields`.

---

## 8. Query Plan Verification (`/admin/explain`)

Use `GET /admin/explain/{endpoint}` to inspect MongoDB query execution plans:

```bash
# Example: Explain attendance listing
curl "http://localhost:8000/admin/explain/attendance_list?page=1&page_size=20"

# Example: Explain late leaderboard
curl "http://localhost:8000/admin/explain/late_leaderboard?month=2026-07"
```

All queries utilize `IXSCAN` and avoid `COLLSCAN`.

---

## 9. Known Limitations

- The assignment scope does not implement user authentication/RBAC (open endpoints per specification).
- All application code resides in `app/main.py` per assignment constraint.
