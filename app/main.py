"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
import calendar
import os
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path, status
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB Configuration
# Read MONGO_URI and MONGO_DB from environment variables
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def create_indexes():
    """Create all required indexes idempotently at startup."""
    # employees collection
    db.employees.create_index([("emp_code", 1)], unique=True, name="idx_emp_code_unique")
    db.employees.create_index([("department", 1), ("emp_code", 1)], name="idx_department_emp_code")
    db.employees.create_index([("department", 1), ("joined_on", 1)], name="idx_department_joined_on")
    db.employees.create_index([("joined_on", 1)], name="idx_joined_on")

    # attendance_logs collection
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True, name="idx_emp_code_date_unique")
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)], name="idx_date_emp_code")
    db.attendance_logs.create_index([("emp_code", 1), ("date", -1)], name="idx_emp_code_date_desc")
    db.attendance_logs.create_index([("status", 1), ("date", -1)], name="idx_status_date")
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)], name="idx_emp_punch_in")
    db.attendance_logs.create_index([("date", 1), ("late_minutes", 1)], name="idx_date_late_minutes")


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create indexes on startup
    create_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers & Business Calculations (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(value: float | int | Decimal | None, places: int = 2) -> float | None:
    """R8: Decimal half-up rounding (not banker's rounding)."""
    if value is None:
        return None
    d = Decimal(str(value))
    target = Decimal("10") ** -places
    return float(d.quantize(target, rounding=ROUND_HALF_UP))


def to_epoch_ms(dt: datetime | None) -> int | None:
    """Convert BSON datetime to epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def from_epoch_ms(ms: int) -> tuple[int, datetime, datetime]:
    """
    R1: Truncate instant to whole seconds before any calculation.
    Returns: (truncated_ms, dt_utc, dt_ist).
    """
    truncated_ms = (ms // 1000) * 1000
    dt_utc = datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)
    dt_ist = dt_utc.astimezone(IST)
    return truncated_ms, dt_utc, dt_ist


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Shift times and attendance dates are IST (UTC+05:30).
    A record's date is the IST calendar date of its punch_in.
    Overnight shifts (shift_end <= shift_start): a punch-in earlier than shift_end
    belongs to the shift that started the previous day.
    """
    is_overnight = shift_end <= shift_start
    if is_overnight and punch_in_ist.strftime("%H:%M") < shift_end:
        return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, att_date: str, shift_start: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if punch-in is strictly more than 10:00 after shift_start;
    then late_minutes = whole minutes (floor) since shift_start, not since grace period ended.
    """
    h, m = map(int, shift_start.split(":"))
    shift_start_dt = datetime.fromisoformat(att_date).replace(tzinfo=IST) + timedelta(hours=h, minutes=m)
    elapsed_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if elapsed_seconds > 600:  # strictly more than 10:00 (600s)
        return int(elapsed_seconds // 60)
    return 0


def compute_overtime(punch_out_ist: datetime, att_date: str, shift_start: str, shift_end: str) -> int:
    """
    R3: Overtime = whole minutes (floor) from shift_end to punch_out,
    counted only if that is at least 30; otherwise 0.
    """
    h, m = map(int, shift_end.split(":"))
    is_overnight = shift_end <= shift_start
    if is_overnight:
        shift_end_date = datetime.fromisoformat(att_date).date() + timedelta(days=1)
    else:
        shift_end_date = datetime.fromisoformat(att_date).date()
    shift_end_dt = datetime.combine(shift_end_date, datetime.strptime(shift_end, "%H:%M").time(), tzinfo=IST)
    ot_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    if ot_seconds >= 1800:  # >= 30 minutes
        return int(ot_seconds // 60)
    return 0


def compute_work_hours_and_half_day(punch_in_ms: int, punch_out_ms: int) -> tuple[float, bool]:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    R5: half_day is true when the rounded work_hours < 4.50.
    """
    dur_seconds = (punch_out_ms - punch_in_ms) // 1000
    dur_hours = (Decimal(dur_seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    work_hours = float(dur_hours)
    half_day = work_hours < 4.50
    return work_hours, half_day


def format_attendance_doc(doc: dict[str, Any]) -> dict[str, Any]:
    """Convert MongoDB attendance log document to API response shape."""
    res = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": []
    }
    raw_history = doc.get("history") or []
    for h in raw_history:
        entry = {
            "at": to_epoch_ms(h.get("at")),
            "by": h.get("by"),
            "reason": h.get("reason"),
            "changes": {}
        }
        for field, change in h.get("changes", {}).items():
            from_val = change.get("from")
            to_val = change.get("to")
            if field in ("punch_in", "punch_out"):
                if isinstance(from_val, datetime):
                    from_val = to_epoch_ms(from_val)
                if isinstance(to_val, datetime):
                    to_val = to_epoch_ms(to_val)
            entry["changes"][field] = {"from": from_val, "to": to_val}
        res["history"].append(entry)
    return res


# --------------------------------------------------------------------------- #
# Request & Response Schemas
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field("18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(..., pattern=r"^\d{4}-\d{2}-\d{2}$")


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    status: str = Field("PRESENT", pattern=r"^(PRESENT|WFH|ON_DUTY)$")


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


class RegularizeIn(BaseModel):
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)
    status: Optional[str] = Field(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$")
    punch_in: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)
    punch_out: Optional[int] = Field(None, ge=100000000000, le=4102444800000, strict=True)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    """Readiness probe. Returns 200 only if MongoDB answers a ping, otherwise 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"Database unavailable: {str(e)}")


@app.post("/employees", status_code=status.HTTP_201_CREATED)
def create_employee(body: EmployeeIn):
    """Create an employee with unique emp_code enforced by database unique index."""
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="shift_start must differ from shift_end")

    now = datetime.now(timezone.utc)
    doc = body.model_dump()
    doc["created_at"] = now

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Employee with this emp_code already exists")

    doc.pop("_id", None)
    doc["created_at"] = to_epoch_ms(now)
    return doc


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """List employees sorted by emp_code ascending."""
    q: dict[str, Any] = {}
    if department:
        q["department"] = department

    total = db.employees.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)

    items = []
    for doc in cursor:
        doc["created_at"] = to_epoch_ms(doc.get("created_at"))
        items.append(doc)

    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=status.HTTP_201_CREATED)
def punch_in(body: PunchInIn):
    """Record a punch-in. Race-safe via unique index on (emp_code, date)."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    raw_ms = body.punched_at if body.punched_at is not None else now_ms
    trunc_ms, dt_utc, dt_ist = from_epoch_ms(raw_ms)

    att_date = get_attendance_date(dt_ist, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(dt_ist, att_date, emp["shift_start"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": dt_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out", status_code=status.HTTP_200_OK)
def punch_out(body: PunchOutIn):
    """Record a punch-out. Closes most recent punch-in at or before punched_at."""
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    now_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
    raw_ms = body.punched_at if body.punched_at is not None else now_ms
    punch_out_ms, punch_out_utc, punch_out_ist = from_epoch_ms(raw_ms)

    # Find the most recent record whose punch_in <= punched_at
    candidate = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", -1)]
    )
    if not candidate:
        # Check if employee has any open record (punch_in after punched_at => 422)
        any_open = db.attendance_logs.find_one(
            {"emp_code": body.emp_code, "punch_out": None, "punch_in": {"$gt": punch_out_utc}}
        )
        if any_open:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in")
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    if candidate.get("punch_out") is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    punch_in_ms = to_epoch_ms(candidate["punch_in"])
    if punch_in_ms is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No punch-in found")

    # 422 if punched_at <= punch_in or more than 24 hours after it
    if punch_out_ms <= punch_in_ms:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in")
    if (punch_out_ms - punch_in_ms) > 24 * 3600 * 1000:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours, half_day = compute_work_hours_and_half_day(punch_in_ms, punch_out_ms)
    ot_min = compute_overtime(punch_out_ist, candidate["date"], emp["shift_start"], emp["shift_end"])

    # Atomic update to avoid punch-out race conditions
    updated = db.attendance_logs.find_one_and_update(
        {"_id": candidate["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": ot_min,
                "half_day": half_day,
            }
        },
        return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Record is already punched out")

    return format_attendance_doc(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    """List attendance records sorted by date desc, then emp_code asc."""
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

    q: dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if status:
        q["status"] = status
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(q)
    skip = (page - 1) * page_size
    cursor = db.attendance_logs.find(q).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)

    items = [format_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(..., pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeIn = ...
):
    """Regularize (manually correct) one day's record and append audit history."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Attendance record not found")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No fields provided to change")

    # If setting status ABSENT or LEAVE, supplying punch_in or punch_out is 422
    if body.status in ("ABSENT", "LEAVE") and (body.punch_in is not None or body.punch_out is not None):
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Cannot provide punch times when setting status to ABSENT or LEAVE")

    new_status = body.status if body.status is not None else rec["status"]

    if new_status in ("ABSENT", "LEAVE"):
        new_punch_in_ms = None
        new_punch_out_ms = None
        new_punch_in_dt = None
        new_punch_out_dt = None
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        # Presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            new_punch_in_ms = body.punch_in
        else:
            new_punch_in_ms = to_epoch_ms(rec.get("punch_in"))

        if new_punch_in_ms is None:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Presence status requires punch_in")

        new_punch_in_ms, new_punch_in_dt, new_punch_in_ist = from_epoch_ms(new_punch_in_ms)

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(new_punch_in_ist, emp["shift_start"], emp["shift_end"])
        if calc_date != date:
            raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_in must stay on the record's attendance date")

        new_late_minutes = compute_late_minutes(new_punch_in_ist, date, emp["shift_start"])

        # Punch out
        if body.punch_out is not None:
            new_punch_out_ms = body.punch_out
        else:
            new_punch_out_ms = to_epoch_ms(rec.get("punch_out"))

        if new_punch_out_ms is not None:
            new_punch_out_ms, new_punch_out_dt, new_punch_out_ist = from_epoch_ms(new_punch_out_ms)
            if new_punch_out_ms <= new_punch_in_ms:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out must be after punch_in")
            if (new_punch_out_ms - new_punch_in_ms) > 24 * 3600 * 1000:
                raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="punch_out cannot be more than 24 hours after punch_in")

            new_work_hours, new_half_day = compute_work_hours_and_half_day(new_punch_in_ms, new_punch_out_ms)
            new_overtime_minutes = compute_overtime(new_punch_out_ist, date, emp["shift_start"], emp["shift_end"])
        else:
            new_punch_out_dt = None
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute changes
    old_punch_in_ms = to_epoch_ms(rec.get("punch_in"))
    old_punch_out_ms = to_epoch_ms(rec.get("punch_out"))
    old_work_hours = rec.get("work_hours")
    old_late_minutes = rec.get("late_minutes", 0)
    old_overtime_minutes = rec.get("overtime_minutes", 0)
    old_half_day = rec.get("half_day", False)
    old_status = rec["status"]

    changes: dict[str, dict[str, Any]] = {}

    if new_status != old_status:
        changes["status"] = {"from": old_status, "to": new_status}
    if new_punch_in_ms != old_punch_in_ms:
        changes["punch_in"] = {"from": rec.get("punch_in"), "to": new_punch_in_dt}
    if new_punch_out_ms != old_punch_out_ms:
        changes["punch_out"] = {"from": rec.get("punch_out"), "to": new_punch_out_dt}
    if new_work_hours != old_work_hours:
        changes["work_hours"] = {"from": old_work_hours, "to": new_work_hours}
    if new_late_minutes != old_late_minutes:
        changes["late_minutes"] = {"from": old_late_minutes, "to": new_late_minutes}
    if new_overtime_minutes != old_overtime_minutes:
        changes["overtime_minutes"] = {"from": old_overtime_minutes, "to": new_overtime_minutes}
    if new_half_day != old_half_day:
        changes["half_day"] = {"from": old_half_day, "to": new_half_day}

    if not changes:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="No changes detected")

    now_utc = datetime.now(timezone.utc)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes
    }

    # Optimistic concurrency check: match current history length
    curr_history_len = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "$expr": {"$eq": [{"$size": {"$ifNull": ["$history", []]}}, curr_history_len]}
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_dt,
                "punch_out": new_punch_out_dt,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry}
        },
        return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Concurrent modification detected")

    return format_attendance_doc(updated)


# --------------------------------------------------------------------------- #
# Analytics Pipelines (MongoDB Aggregations)
# --------------------------------------------------------------------------- #
def get_month_date_range(month: str) -> tuple[str, str, int]:
    year, m = map(int, month.split("-"))
    num_days = calendar.monthrange(year, m)[1]
    return f"{month}-01", f"{month}-{num_days:02d}", num_days


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
):
    """Monthly attendance summary for one employee backed by MongoDB aggregation."""
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Employee not found")

    month_start, month_end, num_days = get_month_date_range(month)
    year, m = map(int, month.split("-"))

    # Working days (R7): Mon-Fri, counted from joined_on for mid-month joiners
    joined_on_str = emp.get("joined_on", "1970-01-01")
    working_days = 0
    if joined_on_str <= month_end:
        start_day = 1
        if joined_on_str > month_start:
            start_day = int(joined_on_str.split("-")[2])
        for d in range(start_day, num_days + 1):
            dt = datetime(year, m, d)
            if dt.weekday() < 5:  # Mon-Fri
                working_days += 1

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end}
            }
        },
        {
            "$project": {
                "status": 1,
                "date": 1,
                "late_minutes": {"$ifNull": ["$late_minutes", 0]},
                "overtime_minutes": {"$ifNull": ["$overtime_minutes", 0]},
                "half_day": {"$ifNull": ["$half_day", False]},
                "day_of_week": {
                    "$dayOfWeek": {
                        "$dateFromString": {"dateString": "$date"}
                    }
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]

    results = list(db.attendance_logs.aggregate(pipeline))
    if results:
        data = results[0]
        present_days = round_half_up(data.get("present_days", 0.0), 2)
        leave_days = data.get("leave_days", 0)
        late_count = data.get("late_count", 0)
        total_late_minutes = data.get("total_late_minutes", 0)
        total_overtime_minutes = data.get("total_overtime_minutes", 0)
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (Decimal(str(present_days)) / Decimal(working_days)) * Decimal(100)
        attendance_pct = float(raw_pct.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None
):
    """Department summary for a month via cross-collection aggregation."""
    month_start, month_end, _ = get_month_date_range(month)

    match_filter: dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department:
        match_filter["department"] = department

    pipeline = [
        {"$match": match_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]}
                                ]
                            }
                        }
                    }
                ],
                "as": "logs"
            }
        },
        {
            "$unwind": {
                "path": "$logs",
                "preserveNullAndEmptyArrays": True
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "status": "$logs.status",
                "half_day": {"$ifNull": ["$logs.half_day", False]},
                "work_hours": "$logs.work_hours",
                "late_minutes": {"$ifNull": ["$logs.late_minutes", 0]},
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date"}}},
                        None
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            "$work_hours",
                            0.0
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            1,
                            0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]

    items = []
    for r in db.employees.aggregate(pipeline):
        r["present_days"] = round_half_up(r.get("present_days", 0.0), 2)
        if r.get("avg_work_hours") is not None:
            r["avg_work_hours"] = round_half_up(r["avg_work_hours"], 2)
        items.append(r)

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    """Late-comers leaderboard for a month using standard competition ranking ($rank)."""
    month_start, month_end, _ = get_month_date_range(month)

    pipeline: list[dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$project": {
                "_id": 0,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "emp_code": 1}}
    ])

    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$")
):
    """Daily attendance trend for a department with gap-filling ($densify) and 7d moving average."""
    # 404 if department has no employees
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Department not found")

    dt_from = datetime.fromisoformat(date_from).replace(tzinfo=timezone.utc)
    dt_to = datetime.fromisoformat(date_to).replace(tzinfo=timezone.utc)

    if dt_to < dt_from:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="to date cannot be earlier than from date")

    # Range max 92 days
    if (dt_to - dt_from).days > 92:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail="Date range exceeds 92 days")

    dt_to_bound = dt_to + timedelta(days=1)

    pipeline: list[dict[str, Any]] = [
        {"$match": {"date": {"$gte": date_from, "$lte": date_to}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {"$match": {"emp.department": department}},
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": ["$half_day", 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                }
            }
        },
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "date": date_from,
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0}
                        }
                    }
                ]
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"}
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "date_dt": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$densify": {
                "field": "date_dt",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [dt_from, dt_to_bound]
                }
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_dt"}},
                "day_of_week": {"$dayOfWeek": "$date_dt"},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]}
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": {
                    "$and": [
                        {"$gte": ["$day_of_week", 2]},
                        {"$lte": ["$day_of_week", 6]}
                    ]
                },
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day_str"]}
                                ]
                            }
                        }
                    },
                    {"$count": "count"}
                ],
                "as": "hc"
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": {"$ifNull": [{"$first": "$hc.count"}, 0]},
                "present_count": 1,
                "late_count": 1
            }
        },
        {
            "$project": {
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        },
        {"$sort": {"date": 1}}
    ]

    items = []
    for r in db.attendance_logs.aggregate(pipeline):
        rate = r.get("attendance_rate")
        mavg = r.get("moving_avg_7d")
        items.append({
            "date": r["date"],
            "is_working_day": r["is_working_day"],
            "headcount": r["headcount"],
            "present_count": round_half_up(r["present_count"], 2),
            "late_count": r["late_count"],
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(mavg, 4) if mavg is not None else None
        })

    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Part C: Admin Explain Endpoint
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(..., pattern=r"^(attendance_list|employee_monthly|department_summary|late_leaderboard|department_trend)$"),
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    date_to: Optional[str] = Query(None, pattern=r"^\d{4}-\d{2}-\d{2}$"),
    status: Optional[str] = Query(None, pattern=r"^(PRESENT|ABSENT|LEAVE|WFH|ON_DUTY)$"),
    from_date: Optional[str] = Query(None, alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: Optional[str] = Query(None, alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100)
):
    """Runs MongoDB explain with executionStats verbosity for an endpoint's main query or pipeline."""
    if endpoint == "attendance_list":
        if date_from and date_to and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

        q: dict[str, Any] = {}
        if emp_code:
            q["emp_code"] = emp_code
        if status:
            q["status"] = status
        if date_from or date_to:
            q["date"] = {}
            if date_from:
                q["date"]["$gte"] = date_from
            if date_to:
                q["date"]["$lte"] = date_to

        skip = (page - 1) * page_size
        plan = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "employee_monthly":
        if not emp_code:
            raise HTTPException(status_code=422, detail="emp_code is required for employee_monthly")
        if not month:
            raise HTTPException(status_code=422, detail="month is required for employee_monthly")

        month_start, month_end, _ = get_month_date_range(month)
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end}
                }
            },
            {
                "$group": {
                    "_id": None,
                    "total_late": {"$sum": "$late_minutes"}
                }
            }
        ]
        plan = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for department_summary")

        month_start, month_end, _ = get_month_date_range(month)
        match_filter: dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department:
            match_filter["department"] = department

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]}
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs"
                }
            }
        ]
        plan = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "employees", "explain": plan}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required for late_leaderboard")

        month_start, month_end, _ = get_month_date_range(month)
        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0}
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"}
                }
            }
        ]
        plan = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(status_code=422, detail="department is required for department_trend")
        if not from_date or not to_date:
            raise HTTPException(status_code=422, detail="from and to are required for department_trend")

        dt_from = datetime.fromisoformat(from_date).replace(tzinfo=timezone.utc)
        dt_to = datetime.fromisoformat(to_date).replace(tzinfo=timezone.utc)
        if dt_to < dt_from or (dt_to - dt_from).days > 92:
            raise HTTPException(status_code=422, detail="Invalid date range")

        pipeline = [
            {"$match": {"date": {"$gte": from_date, "$lte": to_date}}},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "emp"
                }
            },
            {"$unwind": "$emp"},
            {"$match": {"emp.department": department}}
        ]
        plan = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": plan}

    raise HTTPException(status_code=422, detail="Unknown endpoint")
