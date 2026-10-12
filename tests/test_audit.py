"""
Audit tests — targets hidden-test risk areas in the HROne assignment.
Run with:  pytest tests/test_audit.py -v
"""
import os, sys
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
os.environ["MONGO_DB"] = "test_audit_db"
os.environ["MONGO_URI"] = "mongodb://localhost:27017"

import pytest
from datetime import datetime, timezone, timedelta
from decimal import Decimal, ROUND_HALF_UP
from fastapi.testclient import TestClient
from app.main import (
    app, db, create_indexes, round_half_up, compute_late_minutes,
    compute_work_hours_and_half_day, get_attendance_date, from_epoch_ms,
    compute_overtime, IST, format_attendance_doc, to_epoch_ms,
)


@pytest.fixture(autouse=True)
def clean_db():
    create_indexes()
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield


@pytest.fixture
def c():
    with TestClient(app) as tc:
        yield tc


def ms(y, mo, d, h, mi, s=0, tz=timezone.utc):
    return int(datetime(y, mo, d, h, mi, s, tzinfo=tz).timestamp() * 1000)


# === 1. compute_late_minutes edge cases per R2 ===
class TestR2Late:
    def test_exactly_600s_is_not_late(self):
        """09:40:00 IST => exactly 600s => NOT late (strictly more than)"""
        dt = datetime(2026, 7, 6, 9, 40, 0, tzinfo=IST)
        assert compute_late_minutes(dt, "2026-07-06", "09:30") == 0

    def test_601s_is_late_gives_10(self):
        """09:40:01 IST => 601s => late_minutes = floor(601/60) = 10"""
        dt = datetime(2026, 7, 6, 9, 40, 1, tzinfo=IST)
        assert compute_late_minutes(dt, "2026-07-06", "09:30") == 10

    def test_45min59s(self):
        """10:15:59 IST => 2759s => floor(2759/60) = 45"""
        dt = datetime(2026, 7, 6, 10, 15, 59, tzinfo=IST)
        assert compute_late_minutes(dt, "2026-07-06", "09:30") == 45

    def test_early_is_zero(self):
        dt = datetime(2026, 7, 6, 9, 0, 0, tzinfo=IST)
        assert compute_late_minutes(dt, "2026-07-06", "09:30") == 0

    def test_negative_elapsed_is_zero(self):
        """Punch before shift start"""
        dt = datetime(2026, 7, 6, 8, 0, 0, tzinfo=IST)
        assert compute_late_minutes(dt, "2026-07-06", "09:30") == 0


# === 2. R4 work_hours rounding edge ===
class TestR4WorkHours:
    def test_half_up_rounding_boundary(self):
        """2.675 should round to 2.68 (half-up), not 2.67 (banker's)"""
        # 2.675 h = 9630 s; in ms: 9630000
        in_ms = 0
        out_ms = 9630 * 1000
        wh, _ = compute_work_hours_and_half_day(in_ms, out_ms)
        assert wh == 2.68

    def test_exactly_4_50_not_half_day(self):
        """R5: half_day = true when work_hours < 4.50. 4.50 => false"""
        # 4.5h = 16200s
        in_ms = 0
        out_ms = 16200 * 1000
        wh, hd = compute_work_hours_and_half_day(in_ms, out_ms)
        assert wh == 4.50
        assert hd is False

    def test_4_49_is_half_day(self):
        # 4.49h rounded: we need seconds such that seconds/3600 rounds to 4.49
        # 4.49 * 3600 = 16164s
        in_ms = 0
        out_ms = 16164 * 1000
        wh, hd = compute_work_hours_and_half_day(in_ms, out_ms)
        assert wh == 4.49
        assert hd is True


# === 3. R3 overtime ===
class TestR3Overtime:
    def test_29min_no_overtime(self):
        """29 minutes past shift end => 0 (< 30 min threshold)"""
        punch_out_ist = datetime(2026, 7, 6, 18, 59, 0, tzinfo=IST)
        assert compute_overtime(punch_out_ist, "2026-07-06", "09:30", "18:30") == 0

    def test_30min_gives_30(self):
        """Exactly 30 minutes past shift end => 30"""
        punch_out_ist = datetime(2026, 7, 6, 19, 0, 0, tzinfo=IST)
        assert compute_overtime(punch_out_ist, "2026-07-06", "09:30", "18:30") == 30

    def test_overnight_overtime(self):
        """Overnight shift 22:00-06:00, punch out 06:40 IST next day => 40 min OT"""
        punch_out_ist = datetime(2026, 7, 7, 6, 40, 0, tzinfo=IST)
        assert compute_overtime(punch_out_ist, "2026-07-06", "22:00", "06:00") == 40


# === 4. R1 attendance date ===
class TestR1AttendanceDate:
    def test_normal_day(self):
        ist = datetime(2026, 7, 6, 9, 30, 0, tzinfo=IST)
        assert get_attendance_date(ist, "09:30", "18:30") == "2026-07-06"

    def test_overnight_before_shift_end(self):
        """Punch at 01:10 IST, shift 22:00-06:00: 01:10 < 06:00 => previous day"""
        ist = datetime(2026, 7, 7, 1, 10, 0, tzinfo=IST)
        assert get_attendance_date(ist, "22:00", "06:00") == "2026-07-06"

    def test_overnight_after_shift_start(self):
        """Punch at 22:30 IST, shift 22:00-06:00: 22:30 >= 06:00 => same day"""
        ist = datetime(2026, 7, 6, 22, 30, 0, tzinfo=IST)
        assert get_attendance_date(ist, "22:00", "06:00") == "2026-07-06"

    def test_overnight_punch_at_exactly_shift_end(self):
        """Punch at 06:00 IST, shift 22:00-06:00: 06:00 is NOT < 06:00 => same day (07)"""
        ist = datetime(2026, 7, 7, 6, 0, 0, tzinfo=IST)
        assert get_attendance_date(ist, "22:00", "06:00") == "2026-07-07"


# === 5. from_epoch_ms truncation ===
class TestTruncation:
    def test_truncation(self):
        """R1: truncate to whole seconds"""
        trunc, utc, ist = from_epoch_ms(1783312500999)  # .999
        assert trunc == 1783312500000
        assert utc.microsecond == 0

    def test_truncation_floor(self):
        """1 ms after a second boundary stays on that second"""
        trunc, _, _ = from_epoch_ms(1783312501001)
        assert trunc == 1783312501000


# === 6. Punch-in API: employee not found, invalid status, seconds-like value ===
class TestPunchInValidation:
    def test_404_unknown_employee(self, c):
        r = c.post("/attendance/punch-in", json={"emp_code": "EMP9999", "punched_at": ms(2026,7,6,4,0)})
        assert r.status_code == 404

    def test_invalid_status(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": ms(2026,7,6,4,0),"status":"ABSENT"})
        assert r.status_code == 422

    def test_seconds_like_value_rejected(self, c):
        """Values < 100000000000 should be rejected as seconds-looking"""
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": 1234567890})
        assert r.status_code == 422

    def test_float_punched_at_rejected(self, c):
        """Floats should be rejected"""
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": 1783312500000.5})
        assert r.status_code == 422


# === 7. Punch-out validation ===
class TestPunchOutValidation:
    def test_punch_out_before_punch_in(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": ms(2026,7,6,10,0)})
        r = c.post("/attendance/punch-out", json={"emp_code":"EMP0001","punched_at": ms(2026,7,6,9,0)})
        assert r.status_code == 422

    def test_punch_out_more_than_24h(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        in_ms = ms(2026,7,6,4,0)
        c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": in_ms})
        r = c.post("/attendance/punch-out", json={"emp_code":"EMP0001","punched_at": in_ms + 25*3600*1000})
        assert r.status_code == 422

    def test_404_no_open_punch_in(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.post("/attendance/punch-out", json={"emp_code":"EMP0001","punched_at": ms(2026,7,6,18,0)})
        assert r.status_code == 404


# === 8. Employee monthly: mid-month joiner, employee who joined AFTER the month ===
class TestEmployeeMonthly:
    def test_mid_month_joiner_working_days(self, c):
        """Employee joins 2026-07-15 (Wednesday). Working days should count from 15th only."""
        c.post("/employees", json={"emp_code":"EMP0001","name":"M","email":"m@b.c","department":"X","joined_on":"2026-07-15"})
        r = c.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.status_code == 200
        d = r.json()
        # July 15-31: 15(W),16(Th),17(F),18(Sa),19(Su),20(M),21(Tu),22(W),23(Th),24(F),25(Sa),26(Su),27(M),28(Tu),29(W),30(Th),31(F)
        # Weekdays: 15,16,17,20,21,22,23,24,27,28,29,30,31 = 13 working days
        assert d["working_days"] == 13
        assert d["present_days"] == 0.0
        assert d["attendance_pct"] == 0.0

    def test_employee_not_yet_joined_month(self, c):
        """If employee joined in Aug, working days for July should be 0."""
        c.post("/employees", json={"emp_code":"EMP0001","name":"M","email":"m@b.c","department":"X","joined_on":"2026-08-01"})
        r = c.get("/analytics/employees/EMP0001/monthly?month=2026-07")
        assert r.status_code == 200
        d = r.json()
        assert d["working_days"] == 0
        assert d["attendance_pct"] is None

    def test_404_unknown_employee(self, c):
        r = c.get("/analytics/employees/EMP9999/monthly?month=2026-07")
        assert r.status_code == 404


# === 9. Department trend: 404 unknown department, 422 range ===
class TestDepartmentTrend:
    def test_404_unknown_department(self, c):
        r = c.get("/analytics/departments/NoDept/trend?from=2026-07-06&to=2026-07-08")
        assert r.status_code == 404

    def test_422_to_before_from(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.get("/analytics/departments/X/trend?from=2026-07-10&to=2026-07-05")
        assert r.status_code == 422

    def test_422_exceeds_92_days(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.get("/analytics/departments/X/trend?from=2026-01-01&to=2026-06-30")
        assert r.status_code == 422

    def test_trend_includes_all_days(self, c):
        """Trend from 07-06 to 07-08 = exactly 3 days"""
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.get("/analytics/departments/X/trend?from=2026-07-06&to=2026-07-08")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 3
        assert items[0]["date"] == "2026-07-06"
        assert items[2]["date"] == "2026-07-08"


# === 10. Regularization: change to ABSENT clears punches and recalculates ===
class TestRegularization:
    def test_change_to_absent(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        in_ms_val = ms(2026,7,6,4,0)
        out_ms_val = ms(2026,7,6,13,0)
        c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": in_ms_val})
        c.post("/attendance/punch-out", json={"emp_code":"EMP0001","punched_at": out_ms_val})

        r = c.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "Employee was not actually present",
            "regularized_by": "hr.admin"
        })
        assert r.status_code == 200
        d = r.json()
        assert d["status"] == "ABSENT"
        assert d["punch_in"] is None
        assert d["punch_out"] is None
        assert d["work_hours"] is None
        assert d["late_minutes"] == 0
        assert d["overtime_minutes"] == 0
        assert d["half_day"] is False
        assert len(d["history"]) == 1
        changes = d["history"][0]["changes"]
        assert "status" in changes
        assert "punch_in" in changes
        assert "punch_out" in changes

    def test_no_change_is_422(self, c):
        """If sent fields result in no actual change, return 422"""
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        in_ms_val = ms(2026,7,6,4,0)
        c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": in_ms_val, "status": "PRESENT"})
        r = c.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "PRESENT",
            "reason": "redundant correction",
            "regularized_by": "hr"
        })
        assert r.status_code == 422

    def test_404_no_record(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "some reason here",
            "regularized_by": "hr"
        })
        assert r.status_code == 404


# === 11. format_attendance_doc: no _id, no id in response ===
class TestResponseSchema:
    def test_no_id_fields(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.post("/attendance/punch-in", json={"emp_code":"EMP0001","punched_at": ms(2026,7,6,4,0)})
        d = r.json()
        assert "_id" not in d
        assert "id" not in d
        # Check all required fields exist
        for field in ["emp_code","date","status","punch_in","punch_out","work_hours","late_minutes","overtime_minutes","half_day","history"]:
            assert field in d, f"Missing field: {field}"

    def test_employee_response_no_id(self, c):
        r = c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        d = r.json()
        assert "_id" not in d
        assert "id" not in d
        for field in ["emp_code","name","email","department","shift_start","shift_end","joined_on","created_at"]:
            assert field in d, f"Missing field: {field}"
        assert isinstance(d["created_at"], int)


# === 12. Leaderboard: department filter ===
class TestLeaderboardDepartmentFilter:
    def test_department_filter(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"Eng","joined_on":"2026-01-01"})
        c.post("/employees", json={"emp_code":"EMP0002","name":"B","email":"b@b.c","department":"Sales","joined_on":"2026-01-01"})
        dt = datetime(2026,7,6,4,30,0,tzinfo=timezone.utc)
        db.attendance_logs.insert_many([
            {"emp_code":"EMP0001","date":"2026-07-06","status":"PRESENT","late_minutes":50,"punch_in":dt},
            {"emp_code":"EMP0002","date":"2026-07-06","status":"PRESENT","late_minutes":30,"punch_in":dt},
        ])
        r = c.get("/analytics/leaderboard/late?month=2026-07&department=Eng")
        assert r.status_code == 200
        items = r.json()["items"]
        assert len(items) == 1
        assert items[0]["emp_code"] == "EMP0001"


# === 13. Department summary: present_days should NOT include weekends per R7 ===
class TestDeptSummaryWeekend:
    def test_weekend_record_not_in_present_days(self, c):
        """R7: present_days counts only Mon-Fri records. Weekend records still contribute to late/OT."""
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        dt = datetime(2026,7,11,4,0,0,tzinfo=timezone.utc)  # July 11 2026 = Saturday
        db.attendance_logs.insert_one({
            "emp_code":"EMP0001","date":"2026-07-11","status":"PRESENT",
            "punch_in":dt,"punch_out":dt+timedelta(hours=8),
            "work_hours":8.0,"late_minutes":20,"overtime_minutes":0,"half_day":False
        })
        r = c.get("/analytics/departments/summary?month=2026-07&department=X")
        assert r.status_code == 200
        item = r.json()["items"][0]
        assert item["present_days"] == 0.0  # Weekend => not counted
        assert item["late_count"] == 1      # Late minutes still count on weekends
        assert item["total_late_minutes"] == 20


# === 14. Employee list: created_at must be epoch ms int ===
class TestEmployeeListCreatedAt:
    def test_created_at_is_int(self, c):
        c.post("/employees", json={"emp_code":"EMP0001","name":"A","email":"a@b.c","department":"X","joined_on":"2026-01-01"})
        r = c.get("/employees")
        items = r.json()["items"]
        assert len(items) == 1
        assert isinstance(items[0]["created_at"], int)
        assert items[0]["created_at"] > 100000000000


# === 15. Explain endpoint: invalid endpoint returns 422 ===
class TestExplainValidation:
    def test_invalid_endpoint_name(self, c):
        r = c.get("/admin/explain/invalid_name")
        assert r.status_code == 422

    def test_employee_monthly_missing_params(self, c):
        r = c.get("/admin/explain/employee_monthly")
        assert r.status_code == 422

    def test_valid_explain(self, c):
        r = c.get("/admin/explain/attendance_list")
        assert r.status_code == 200
        d = r.json()
        assert d["endpoint"] == "attendance_list"
        assert d["collection"] == "attendance_logs"
        assert "explain" in d
