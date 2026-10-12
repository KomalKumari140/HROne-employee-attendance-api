import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal
from fastapi.testclient import TestClient

# Set test database before importing app
os.environ["MONGO_DB"] = "test_attendance_suite_db"
os.environ["MONGO_URI"] = "mongodb://localhost:27017"

from app.main import app, db, client, create_indexes, round_half_up, compute_late_minutes, compute_overtime, compute_work_hours_and_half_day, get_attendance_date, IST


@pytest.fixture(autouse=True)
def clean_db():
    create_indexes()
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def api_client():
    with TestClient(app) as tc:
        yield tc


# --------------------------------------------------------------------------- #
# 1. Health check
# --------------------------------------------------------------------------- #
def test_health_ok(api_client):
    response = api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# --------------------------------------------------------------------------- #
# 2. Employee CRUD & Concurrency
# --------------------------------------------------------------------------- #
def test_create_employee_success(api_client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    response = api_client.post("/employees", json=payload)
    assert response.status_code == 201
    data = response.json()
    assert data["emp_code"] == "EMP0001"
    assert "created_at" in data
    assert isinstance(data["created_at"], int)
    assert "_id" not in data


def test_create_employee_duplicate_emp_code(api_client):
    payload = {
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    }
    r1 = api_client.post("/employees", json=payload)
    assert r1.status_code == 201
    r2 = api_client.post("/employees", json=payload)
    assert r2.status_code == 409


def test_create_employee_validation_errors(api_client):
    # Invalid emp_code regex
    r = api_client.post("/employees", json={
        "emp_code": "BAD_CODE",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-05"
    })
    assert r.status_code == 422

    # shift_start == shift_end
    r2 = api_client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "09:30",
        "joined_on": "2026-01-05"
    })
    assert r2.status_code == 422


def test_list_employees_pagination_and_sorting(api_client):
    for i in range(1, 6):
        api_client.post("/employees", json={
            "emp_code": f"EMP000{i}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering" if i % 2 == 1 else "Sales",
            "joined_on": "2026-01-01"
        })

    # All employees sorted
    r = api_client.get("/employees?page=1&page_size=3")
    assert r.status_code == 200
    data = r.json()
    assert data["total"] == 5
    assert len(data["items"]) == 3
    assert [x["emp_code"] for x in data["items"]] == ["EMP0001", "EMP0002", "EMP0003"]

    # Page 2
    r2 = api_client.get("/employees?page=2&page_size=3")
    assert r2.status_code == 200
    data2 = r2.json()
    assert [x["emp_code"] for x in data2["items"]] == ["EMP0004", "EMP0005"]

    # Filter by department
    r_sales = api_client.get("/employees?department=Sales")
    assert r_sales.status_code == 200
    assert r_sales.json()["total"] == 2


# --------------------------------------------------------------------------- #
# 3. Punch-in, Late calculation, Overnight shifts
# --------------------------------------------------------------------------- #
def test_punch_in_success_and_duplicate(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in 2026-07-06 09:28 IST = 03:58 UTC
    dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)

    r = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
    assert r.status_code == 201
    doc = r.json()
    assert doc["emp_code"] == "EMP0001"
    assert doc["date"] == "2026-07-06"
    assert doc["status"] == "PRESENT"
    assert doc["late_minutes"] == 0
    assert doc["punch_out"] is None
    assert doc["work_hours"] is None
    assert doc["history"] == []
    assert "_id" not in doc
    assert "id" not in doc

    # Duplicate punch in for same date -> 409
    r_dup = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms})
    assert r_dup.status_code == 409


def test_late_minutes_boundary_conditions(api_client):
    """
    R2: Grace period 10 minutes.
    Shift 09:30:
    09:40:00 -> 0; 09:40:01 -> 10; 10:15:59 -> 45.
    """
    # 09:40:00 IST on 2026-07-06
    dt_40_00 = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
    assert compute_late_minutes(dt_40_00, "2026-07-06", "09:30") == 0

    # 09:40:01 IST
    dt_40_01 = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
    assert compute_late_minutes(dt_40_01, "2026-07-06", "09:30") == 10

    # 10:15:59 IST
    dt_15_59 = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
    assert compute_late_minutes(dt_15_59, "2026-07-06", "09:30") == 45


def test_overnight_shift_punch_in(api_client):
    """
    R1: Overnight shift 22:00 - 06:00.
    Punch in at 01:10 IST on 2026-07-07 belongs to 2026-07-06 shift!
    """
    api_client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Overnight Worker",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    # Punch in 2026-07-07 01:10 IST = 2026-07-06 19:40 UTC
    dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=IST)
    ms = int(dt.timestamp() * 1000)

    r = api_client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": ms})
    assert r.status_code == 201
    assert r.json()["date"] == "2026-07-06"


# --------------------------------------------------------------------------- #
# 4. Punch-out, Work hours rounding (R4), Overtime (R3), Half day (R5)
# --------------------------------------------------------------------------- #
def test_punch_out_and_work_hours_half_up(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in 09:28 IST (03:58 UTC)
    in_dt = datetime(2026, 7, 6, 3, 58, 0, tzinfo=timezone.utc)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000)})

    # Punch out 18:35 IST (13:05 UTC)
    # Duration: 9 hours 7 minutes = 32820 seconds. 32820 / 3600 = 9.11666... -> 9.12
    # Overtime: 5 minutes (< 30 min minimum) -> 0
    out_dt = datetime(2026, 7, 6, 13, 5, 0, tzinfo=timezone.utc)
    r = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)})
    assert r.status_code == 200
    doc = r.json()
    assert doc["work_hours"] == 9.12
    assert doc["overtime_minutes"] == 0
    assert doc["half_day"] is False

    # Second punch out for same record -> 409
    r_dup = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)})
    assert r_dup.status_code == 409


def test_overtime_calculation(api_client):
    """
    R3: Overtime >= 30 min counted, else 0.
    Shift end 18:30. Punch out 19:10 -> 40 min.
    """
    api_client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Neha Iyer",
        "email": "neha@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01"
    })

    # In at 10:05 IST (04:35 UTC)
    in_dt = datetime(2026, 7, 6, 4, 35, 0, tzinfo=timezone.utc)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0003", "punched_at": int(in_dt.timestamp() * 1000)})

    # Out at 19:10 IST (13:40 UTC) -> 40 min overtime
    out_dt = datetime(2026, 7, 6, 13, 40, 0, tzinfo=timezone.utc)
    r = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0003", "punched_at": int(out_dt.timestamp() * 1000)})
    assert r.status_code == 200
    doc = r.json()
    assert doc["late_minutes"] == 35
    assert doc["overtime_minutes"] == 40
    assert doc["work_hours"] == 9.08


def test_half_day_classification(api_client):
    """
    R5: work_hours < 4.50 -> half_day = True.
    """
    api_client.post("/employees", json={
        "emp_code": "EMP0004",
        "name": "Karan Shah",
        "email": "karan@example.com",
        "department": "Sales",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-02-01"
    })

    # 3.5 hours worked
    in_dt = datetime(2026, 7, 7, 4, 0, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 7, 7, 30, 0, tzinfo=timezone.utc)

    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0004", "punched_at": int(in_dt.timestamp() * 1000), "status": "WFH"})
    r = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0004", "punched_at": int(out_dt.timestamp() * 1000)})
    assert r.status_code == 200
    doc = r.json()
    assert doc["work_hours"] == 3.5
    assert doc["half_day"] is True


def test_overnight_punch_out(api_client):
    """
    Overnight shift 22:00 - 06:00.
    In 2026-07-06 21:55 IST (16:25 UTC).
    Out 2026-07-07 06:40 IST (01:10 UTC next day) -> 40 min overtime.
    """
    api_client.post("/employees", json={
        "emp_code": "EMP0005",
        "name": "Overnight Worker",
        "email": "night@example.com",
        "department": "Support",
        "shift_start": "22:00",
        "shift_end": "06:00",
        "joined_on": "2026-01-01"
    })

    in_dt = datetime(2026, 7, 6, 16, 25, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 7, 1, 10, 0, tzinfo=timezone.utc)

    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0005", "punched_at": int(in_dt.timestamp() * 1000)})
    r = api_client.post("/attendance/punch-out", json={"emp_code": "EMP0005", "punched_at": int(out_dt.timestamp() * 1000)})
    assert r.status_code == 200
    doc = r.json()
    assert doc["date"] == "2026-07-06"
    assert doc["work_hours"] == 8.75
    assert doc["overtime_minutes"] == 40


# --------------------------------------------------------------------------- #
# 5. Regularization (PATCH) & History Audit Trail
# --------------------------------------------------------------------------- #
def test_regularize_attendance_and_history(api_client):
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha Rao",
        "email": "asha@example.com",
        "department": "Engineering",
        "shift_start": "09:30",
        "shift_end": "18:30",
        "joined_on": "2026-01-05"
    })

    # Punch in 10:05 IST (35 min late)
    in_dt = datetime(2026, 7, 7, 4, 35, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 7, 13, 0, 0, tzinfo=timezone.utc)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000)})
    api_client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)})

    # Regularize punch_in to 09:28 IST (03:58 UTC)
    new_in = datetime(2026, 7, 7, 3, 58, 0, tzinfo=timezone.utc)
    patch_res = api_client.patch(
        "/attendance/EMP0001/2026-07-07",
        json={
            "punch_in": int(new_in.timestamp() * 1000),
            "reason": "biometric glitch",
            "regularized_by": "hr.admin"
        }
    )
    assert patch_res.status_code == 200
    doc = patch_res.json()
    assert doc["late_minutes"] == 0
    assert doc["work_hours"] == 9.03
    assert len(doc["history"]) == 1
    hist = doc["history"][0]
    assert hist["by"] == "hr.admin"
    assert hist["reason"] == "biometric glitch"
    assert "late_minutes" in hist["changes"]
    assert hist["changes"]["late_minutes"] == {"from": 35, "to": 0}
    assert "punch_in" in hist["changes"]
    assert "work_hours" in hist["changes"]


# --------------------------------------------------------------------------- #
# 6. Analytics: Monthly, Department Summary, Leaderboard, Trend
# --------------------------------------------------------------------------- #
def test_analytics_endpoints_against_sample_seed(api_client):
    from sample_seed import here
    from bson import json_util
    opts = json_util.JSONOptions(tz_aware=True)

    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    for name in ("employees", "attendance_logs"):
        docs = json_util.loads((here / f"{name}.json").read_text(), json_options=opts)
        db[name].insert_many(docs)

    # 1. Employee monthly
    r_month = api_client.get("/analytics/employees/EMP0001/monthly?month=2026-07")
    assert r_month.status_code == 200
    m_data = r_month.json()
    assert m_data["emp_code"] == "EMP0001"
    assert m_data["working_days"] == 23
    assert m_data["present_days"] >= 2.0
    assert isinstance(m_data["attendance_pct"], float)

    # 2. Department summary
    r_dept = api_client.get("/analytics/departments/summary?month=2026-07")
    assert r_dept.status_code == 200
    dept_data = r_dept.json()
    assert dept_data["month"] == "2026-07"
    depts = [d["department"] for d in dept_data["items"]]
    assert "Engineering" in depts
    assert "Sales" in depts
    assert "Support" in depts

    # 3. Leaderboard
    r_lead = api_client.get("/analytics/leaderboard/late?month=2026-07&limit=10")
    assert r_lead.status_code == 200
    lead_data = r_lead.json()
    assert len(lead_data["items"]) >= 1
    assert lead_data["items"][0]["rank"] == 1
    assert lead_data["items"][0]["total_late_minutes"] > 0

    # 4. Department trend
    r_trend = api_client.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-15")
    assert r_trend.status_code == 200
    trend_data = r_trend.json()
    assert trend_data["department"] == "Engineering"
    assert len(trend_data["items"]) == 10  # 10 days inclusive
    assert trend_data["items"][0]["date"] == "2026-07-06"
    assert trend_data["items"][-1]["date"] == "2026-07-15"
    assert "moving_avg_7d" in trend_data["items"][0]


# --------------------------------------------------------------------------- #
# 7. Admin Explain Endpoint
# --------------------------------------------------------------------------- #
def test_admin_explain_endpoint(api_client):
    endpoints = [
        ("/admin/explain/attendance_list?page=1&page_size=20", "attendance_logs"),
        ("/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07", "attendance_logs"),
        ("/admin/explain/department_summary?month=2026-07", "employees"),
        ("/admin/explain/late_leaderboard?month=2026-07", "attendance_logs"),
        ("/admin/explain/department_trend?department=Engineering&from=2026-07-06&to=2026-07-15", "attendance_logs"),
    ]
    for url, expected_coll in endpoints:
        r = api_client.get(url)
        assert r.status_code == 200, f"Failed for {url}: {r.text}"
        data = r.json()
        assert data["collection"] == expected_coll
        assert "explain" in data


# --------------------------------------------------------------------------- #
# 8. Advanced Scenarios: Leaderboard Ties, Zero-Log Headcount, Regularization
# --------------------------------------------------------------------------- #
def test_leaderboard_ties_at_cutoff(api_client):
    """
    Ranks: ties share a rank, next is skipped (1, 2, 2, 4).
    Limit applies after ranking: return every employee whose rank <= limit.
    So if limit=2 and two employees tie at rank 2, both are returned (total 3 returned).
    """
    # Create 4 employees
    for i in range(1, 5):
        api_client.post("/employees", json={
            "emp_code": f"EMP000{i}",
            "name": f"Employee {i}",
            "email": f"emp{i}@example.com",
            "department": "Engineering",
            "joined_on": "2026-01-01"
        })

    # Seed attendance with late minutes:
    # EMP0001: 60 min late
    # EMP0002: 40 min late
    # EMP0003: 40 min late (tied for rank 2!)
    # EMP0004: 20 min late (rank 4!)
    dt = datetime(2026, 7, 6, 4, 30, 0, tzinfo=timezone.utc)
    db.attendance_logs.insert_many([
        {"emp_code": "EMP0001", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 60, "punch_in": dt},
        {"emp_code": "EMP0002", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40, "punch_in": dt},
        {"emp_code": "EMP0003", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 40, "punch_in": dt},
        {"emp_code": "EMP0004", "date": "2026-07-06", "status": "PRESENT", "late_minutes": 20, "punch_in": dt},
    ])

    # Request limit=2
    r = api_client.get("/analytics/leaderboard/late?month=2026-07&limit=2")
    assert r.status_code == 200
    items = r.json()["items"]
    # Both EMP0002 and EMP0003 have rank 2 <= 2, so 3 rows returned!
    assert len(items) == 3
    assert items[0]["emp_code"] == "EMP0001"
    assert items[0]["rank"] == 1
    assert items[1]["rank"] == 2
    assert items[2]["rank"] == 2
    assert items[1]["emp_code"] == "EMP0002"
    assert items[2]["emp_code"] == "EMP0003"


def test_department_summary_counts_zero_log_employees(api_client):
    """
    R9: Headcount includes employees with joined_on <= period end,
    even if they have ZERO attendance logs!
    """
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Active Worker",
        "email": "active@example.com",
        "department": "Finance",
        "joined_on": "2026-01-01"
    })
    api_client.post("/employees", json={
        "emp_code": "EMP0002",
        "name": "Zero Log Worker",
        "email": "zerolog@example.com",
        "department": "Finance",
        "joined_on": "2026-01-01"
    })
    # Future joiner who should NOT count in July 2026
    api_client.post("/employees", json={
        "emp_code": "EMP0003",
        "name": "Future Worker",
        "email": "future@example.com",
        "department": "Finance",
        "joined_on": "2026-08-01"
    })

    # Only EMP0001 has a log in July
    dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    db.attendance_logs.insert_one({
        "emp_code": "EMP0001",
        "date": "2026-07-06",
        "status": "PRESENT",
        "work_hours": 8.0,
        "half_day": False,
        "punch_in": dt,
        "punch_out": dt + timedelta(hours=8)
    })

    r = api_client.get("/analytics/departments/summary?month=2026-07&department=Finance")
    assert r.status_code == 200
    items = r.json()["items"]
    assert len(items) == 1
    fin = items[0]
    # Headcount must be 2 (EMP0001 + EMP0002, excluding EMP0003 who joins in Aug)
    assert fin["headcount"] == 2
    assert fin["present_days"] == 1.0
    assert fin["avg_work_hours"] == 8.0


def test_regularize_validation_absent_with_punches(api_client):
    """
    Setting status ABSENT or LEAVE clears both punch times,
    so supplying punch_in or punch_out with it is 422.
    """
    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Asha",
        "email": "asha@example.com",
        "department": "Eng",
        "joined_on": "2026-01-01"
    })
    dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(dt.timestamp() * 1000)})

    r = api_client.patch(
        "/attendance/EMP0001/2026-07-06",
        json={
            "status": "ABSENT",
            "punch_in": int(dt.timestamp() * 1000),
            "reason": "Invalid update",
            "regularized_by": "manager"
        }
    )
    assert r.status_code == 422


def test_attendance_list_date_validation(api_client):
    r = api_client.get("/attendance?date_from=2026-07-10&date_to=2026-07-05")
    assert r.status_code == 422


def test_concurrent_punch_in_race_condition(api_client):
    """
    Two simultaneous punch-ins for the same employee and date:
    exactly one 201 and the other 409.
    """
    import concurrent.futures

    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    ms = int(dt.timestamp() * 1000)

    def do_punch_in():
        with TestClient(app) as client:
            return client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": ms}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_punch_in)
        f2 = executor.submit(do_punch_in)
        codes = sorted([f1.result(), f2.result()])

    assert codes == [201, 409]


def test_concurrent_punch_out_race_condition(api_client):
    """
    Two simultaneous punch-outs for the same open record:
    exactly one 200 and the other 409.
    """
    import concurrent.futures

    api_client.post("/employees", json={
        "emp_code": "EMP0001",
        "name": "Race Tester",
        "email": "race@example.com",
        "department": "Engineering",
        "joined_on": "2026-01-01"
    })

    in_dt = datetime(2026, 7, 6, 4, 0, 0, tzinfo=timezone.utc)
    out_dt = datetime(2026, 7, 6, 13, 0, 0, tzinfo=timezone.utc)
    api_client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": int(in_dt.timestamp() * 1000)})

    def do_punch_out():
        with TestClient(app) as client:
            return client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": int(out_dt.timestamp() * 1000)}).status_code

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        f1 = executor.submit(do_punch_out)
        f2 = executor.submit(do_punch_out)
        codes = sorted([f1.result(), f2.result()])

    assert codes == [200, 409]


