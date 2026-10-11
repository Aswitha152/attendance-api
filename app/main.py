"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience; real
      environment variables take priority)
"""
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date as date_cls
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Annotated, Literal, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()  # does NOT override variables already set in the environment

# tz_aware=True: PyMongo returns UTC-aware datetimes instead of naive ones,
# so we never mix naive and aware values in arithmetic.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

DATE_RE = r"^\d{4}-\d{2}-\d{2}$"
MONTH_RE = r"^\d{4}-(0[1-9]|1[0-2])$"
TIME_RE = r"^([01]\d|2[0-3]):[0-5]\d$"
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
STATUSES = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]
# Epoch milliseconds per the contract: seconds-looking values (< 1e11) are rejected.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]


# --------------------------------------------------------------------------- #
# Startup: indexes (create_index is idempotent)
# --------------------------------------------------------------------------- #
def create_indexes():
    emp, log = db.employees, db.attendance_logs
    emp.create_index([("emp_code", ASCENDING)], unique=True)
    emp.create_index([("department", ASCENDING), ("emp_code", ASCENDING)])  # list by dept, sorted
    emp.create_index([("joined_on", ASCENDING)])  # summary headcount (R9)
    emp.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])  # trend / dept summary
    # One record per employee per day. This is what makes punch-in race-safe.
    log.create_index([("emp_code", ASCENDING), ("date", ASCENDING)], unique=True)
    # /attendance default listing and month-wide analytics (date range first).
    log.create_index([("date", DESCENDING), ("emp_code", ASCENDING)])
    # Punch-out: latest record with punch_in <= punched_at.
    log.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)])


@asynccontextmanager
async def lifespan(_: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helpers: errors, validation
# --------------------------------------------------------------------------- #
def unprocessable(msg: str):
    """422 with the same body shape FastAPI uses for validation errors."""
    raise HTTPException(422, detail=[{"loc": [], "msg": msg, "type": "value_error"}])


def parse_date(s: str) -> date_cls:
    if not re.fullmatch(DATE_RE, s):
        unprocessable(f"invalid date: {s}")
    try:
        return datetime.strptime(s, "%Y-%m-%d").date()
    except ValueError:
        unprocessable(f"invalid date: {s}")


def month_bounds(month: str) -> tuple[str, str]:
    """'2026-07' -> ('2026-07-01', '2026-07-31')."""
    if not re.fullmatch(MONTH_RE, month):
        unprocessable(f"invalid month: {month}")
    first = datetime.strptime(month + "-01", "%Y-%m-%d").date()
    nxt = (first.replace(day=28) + timedelta(days=4)).replace(day=1)
    return first.isoformat(), (nxt - timedelta(days=1)).isoformat()


def half_up(x, places: int) -> float:
    """R8: round half-up. (Python round() and Mongo $round are half-to-even.)"""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(repr(round(float(x), 10))).quantize(quantum, rounding=ROUND_HALF_UP))


# --------------------------------------------------------------------------- #
# Helpers: time conversion
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(milliseconds=ms // 1000 * 1000)


def dt_to_ms(dt: datetime) -> int:
    if dt.tzinfo is None:  # safety net; should not happen with tz_aware=True
        dt = dt.replace(tzinfo=timezone.utc)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def to_api(value):
    """Recursively convert datetimes to epoch ms and drop Mongo's _id."""
    if isinstance(value, datetime):
        return dt_to_ms(value)
    if isinstance(value, dict):
        return {k: to_api(v) for k, v in value.items() if k != "_id"}
    if isinstance(value, list):
        return [to_api(v) for v in value]
    return value


def record_out(doc: dict) -> dict:
    """AttendanceRecord response. Seeded docs may omit fields: fill the defaults."""
    return to_api(
        {
            "emp_code": doc["emp_code"],
            "date": doc["date"],
            "status": doc["status"],
            "punch_in": doc.get("punch_in"),
            "punch_out": doc.get("punch_out"),
            "work_hours": doc.get("work_hours"),
            "late_minutes": doc.get("late_minutes") or 0,
            "overtime_minutes": doc.get("overtime_minutes") or 0,
            "half_day": bool(doc.get("half_day", False)),
            "history": doc.get("history") or [],
        }
    )


def now_utc() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


# --------------------------------------------------------------------------- #
# Helpers: business rules (R1 - R5, R7)
# --------------------------------------------------------------------------- #
def at_ist(date_str: str, hhmm: str, plus_days: int = 0) -> datetime:
    """The instant at HH:MM IST on the given YYYY-MM-DD date (+ optional days)."""
    h, m = map(int, hhmm.split(":"))
    base = datetime.strptime(date_str, "%Y-%m-%d").replace(hour=h, minute=m, tzinfo=IST)
    return base + timedelta(days=plus_days)


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return shift_end <= shift_start  # zero-padded HH:MM compares correctly as text


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of punch_in; overnight shifts may belong to the day before."""
    local = punch_in.astimezone(IST)
    d = local.date()
    if is_overnight(shift_start, shift_end):
        h, m = map(int, shift_end.split(":"))
        seconds_into_day = local.hour * 3600 + local.minute * 60 + local.second
        if seconds_into_day < h * 3600 + m * 60:
            d -= timedelta(days=1)
    return d.isoformat()


def compute_late_minutes(punch_in: datetime, shift_start: str, date_str: str) -> int:
    """R2: late only if STRICTLY more than 10:00 after shift start; then floor minutes."""
    diff = (punch_in - at_ist(date_str, shift_start)).total_seconds()
    return int(diff // 60) if diff > 600 else 0


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    """R4: seconds / 3600, rounded to 2 decimals HALF-UP (not Python's banker's round)."""
    seconds = int((punch_out - punch_in).total_seconds())
    hours = Decimal(seconds) / Decimal(3600)
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    """R3: floor minutes after shift_end, counted only if >= 30."""
    end = at_ist(date_str, shift_end, 1 if is_overnight(shift_start, shift_end) else 0)
    minutes = int((punch_out - end).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def working_days(first: str, last: str, joined_on: str) -> int:
    """R7: Mon-Fri between max(month start, joined_on) and month end. (Calendar maths only.)"""
    start = max(first, joined_on)
    if start > last:
        return 0
    s, e = date_cls.fromisoformat(start), date_cls.fromisoformat(last)
    return sum(1 for i in range((e - s).days + 1) if (s + timedelta(days=i)).weekday() < 5)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field("09:30", pattern=TIME_RE)
    shift_end: str = Field("18:30", pattern=TIME_RE)
    joined_on: str = Field(pattern=DATE_RE)

    @field_validator("joined_on")
    @classmethod
    def real_calendar_date(cls, v: str) -> str:
        datetime.strptime(v, "%Y-%m-%d")  # rejects e.g. 2026-13-45 -> 422
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None  # StrictInt: floats / strings / bools -> 422
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str = Field(min_length=1)
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    # Derived fields are not declared, so a client-sent work_hours etc. is ignored.
    status: Optional[STATUSES] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Query / pipeline builders (shared by the real endpoints and /admin/explain)
# --------------------------------------------------------------------------- #
def attendance_list_spec(emp_code, date_from, date_to, status, page, page_size) -> dict:
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)
    if date_from and date_to and date_from > date_to:
        unprocessable("date_from must not be after date_to")
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


def _dow(field: str = "$date") -> dict:
    """ISO weekday (1 = Monday ... 7 = Sunday) of a 'YYYY-MM-DD' string field."""
    return {"$isoDayOfWeek": {"$dateFromString": {"dateString": field, "format": "%Y-%m-%d"}}}


def _present_value(weekdays_only: bool, prefix: str = "") -> dict:
    """1 for a present record, 0.5 for a present half day, else 0 (R5-R7)."""
    cond = [{"$in": [f"${prefix}status", PRESENCE]}]
    if weekdays_only:
        cond.append({"$lte": [_dow(f"${prefix}date"), 5]})
    half = {"$eq": [{"$ifNull": [f"${prefix}half_day", False]}, True]}
    return {"$cond": [{"$and": cond}, {"$cond": [half, 0.5, 1]}, 0]}


def _late(prefix: str = "") -> dict:
    return {"$gt": [{"$ifNull": [f"${prefix}late_minutes", 0]}, 0]}


def employee_monthly_pipeline(emp_code: str, first: str, last: str) -> list:
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value(weekdays_only=True)},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": {"$cond": [_late(), 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


def department_summary_pipeline(first: str, last: str, department: Optional[str]) -> list:
    """Starts from employees (so zero-log people count), joins each one's month of logs."""
    match = {"joined_on": {"$lte": last}}  # R9
    if department:
        match["department"] = department
    has_hours = {"$and": [{"$in": ["$status", PRESENCE]}, {"$isNumber": "$work_hours"}]}
    per_employee = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": _present_value(weekdays_only=True)},
                "hours_sum": {"$sum": {"$cond": [has_hours, "$work_hours", 0]}},
                "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                "late_count": {"$sum": {"$cond": [_late(), 1, 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
    ]

    def total(field):
        return {"$sum": {"$ifNull": [f"$s.{field}", 0]}}

    return [
        {"$match": match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": per_employee,
                "as": "s",
            }
        },
        {"$unwind": {"path": "$s", "preserveNullAndEmptyArrays": True}},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},  # one row per employee, logs or not
                "present_days": total("present_days"),
                "hours_sum": total("hours_sum"),
                "hours_n": total("hours_n"),
                "late_count": total("late_count"),
                "total_late_minutes": total("total_late_minutes"),
                "leave_count": total("leave_count"),
                "on_duty_count": total("on_duty_count"),
            }
        },
        {"$sort": {"_id": 1}},
    ]


def late_leaderboard_pipeline(first: str, last: str, limit: int, department: Optional[str]) -> list:
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                "late_count": {"$sum": {"$cond": [_late(), 1, 0]}},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},  # logs of unknown employees are dropped
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})  # rank inside the department
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$emp.name",
                "department": "$emp.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]
    return pipeline


def trend_range(frm: str, to: str) -> tuple[date_cls, date_cls]:
    start, end = parse_date(frm), parse_date(to)
    if end < start:
        unprocessable("'to' must not be before 'from'")
    if (end - start).days + 1 > 92:
        unprocessable("range must not exceed 92 days")
    return start, end


def department_trend_pipeline(department: str, start: date_cls, end: date_cls) -> list:
    frm, to = start.isoformat(), end.isoformat()
    lower = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    upper = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)  # exclusive
    half = {"$eq": [{"$ifNull": ["$logs.half_day", False]}, True]}
    return [
        {"$match": {"department": department, "joined_on": {"$lte": to}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$match": {"date": {"$gte": frm, "$lte": to}}},
                    {"$project": {"_id": 0, "date": 1, "status": 1, "half_day": 1, "late_minutes": 1}},
                ],
                "as": "logs",
            }
        },
        {"$unwind": "$logs"},
        {
            "$group": {
                "_id": "$logs.date",
                "present_count": {
                    "$sum": {"$cond": [{"$in": ["$logs.status", PRESENCE]}, {"$cond": [half, 0.5, 1]}, 0]}
                },
                "late_count": {"$sum": {"$cond": [_late("logs."), 1, 0]}},
            }
        },
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        # Gap filling: one document per calendar day in [from, to].
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},
        {
            "$addFields": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$d"}},
                "is_working_day": {"$lte": [{"$isoDayOfWeek": "$d"}, 5]},
                "present_count": {"$ifNull": ["$present_count", 0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
            }
        },
        {
            "$lookup": {  # R9: headcount as of each day
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$day"]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {"$addFields": {"headcount": {"$ifNull": [{"$first": "$hc.n"}, 0]}}},
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, 0]}}},
            }
        },
        {"$sort": {"date": 1}},
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


# --------------------------------------------------------------------------- #
# System + employees
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        db.employees.insert_one(doc)  # unique index decides, not a check-then-insert
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return to_api(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {"department": department} if department else {}
    total = db.employees.count_documents(q)  # total respects the filter (R10)
    cursor = (
        db.employees.find(q)
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [to_api(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], d),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)  # unique (emp_code, date) index => race-safe
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    # Most recent record whose punch_in is at or before ts (works across midnight).
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}}, sort=[("punch_in", DESCENDING)]
    )
    if not rec:
        raise HTTPException(404, "no punch-in found")

    pin = rec["punch_in"]
    if ts <= pin or ts - pin > timedelta(hours=24):
        unprocessable("punched_at must be after punch_in and within 24 hours of it")

    hours = compute_work_hours(pin, ts)
    # Conditional update: only succeeds while punch_out is still empty, so two
    # simultaneous punch-outs give exactly one 200.
    updated = db.attendance_logs.find_one_and_update(
        {"emp_code": body.emp_code, "date": rec["date"], "punch_out": None},
        {
            "$set": {
                "punch_out": ts,
                "work_hours": hours,
                "overtime_minutes": compute_overtime(ts, emp["shift_start"], emp["shift_end"], rec["date"]),
                "half_day": hours < 4.5,  # R5: on the rounded value
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[STATUSES] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
    total = db.attendance_logs.count_documents(spec["filter"])
    cursor = (  # sort/skip/limit happen in MongoDB, backed by an index
        db.attendance_logs.find(spec["filter"])
        .sort(list(spec["sort"].items()))
        .skip(spec["skip"])
        .limit(spec["limit"])
    )
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    parse_date(date)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not rec:
        raise HTTPException(404, "no attendance record for that date")

    old = {
        "status": rec["status"],
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day", False)),
    }

    # --- final values of the three editable fields ---------------------------
    new_status = body.status or old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            unprocessable("ABSENT / LEAVE clear the punch times; do not send punch_in or punch_out")
        new_in = new_out = None
    else:
        new_in = ms_to_dt(body.punch_in) if body.punch_in is not None else old["punch_in"]
        if new_in is None:
            unprocessable("a presence status requires a punch_in")
        new_out = ms_to_dt(body.punch_out) if body.punch_out is not None else old["punch_out"]
        if attendance_date(new_in, emp["shift_start"], emp["shift_end"]) != date:
            unprocessable("punch_in must stay on the record's attendance date")
        if new_out is not None and not (new_in < new_out <= new_in + timedelta(hours=24)):
            unprocessable("punch_out must be after punch_in and within 24 hours of it")

    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        unprocessable("the request changes nothing")

    # --- recompute derived fields (R2 - R5) ----------------------------------
    if new_in is None:
        derived = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    else:
        derived = {"late_minutes": compute_late_minutes(new_in, emp["shift_start"], date)}
        if new_out is not None:
            hours = compute_work_hours(new_in, new_out)
            derived.update(
                work_hours=hours,
                overtime_minutes=compute_overtime(new_out, emp["shift_start"], emp["shift_end"], date),
                half_day=hours < 4.5,
            )
        else:
            derived.update(work_hours=None, overtime_minutes=0, half_day=False)

    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, **derived}
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # Optimistic concurrency: write only if the record is still exactly as we read it
    # (same editable fields, and nobody appended to history meanwhile).
    n = len(rec.get("history") or [])
    updated = db.attendance_logs.find_one_and_update(
        {
            "emp_code": emp_code,
            "date": date,
            "status": old["status"],
            "punch_in": old["punch_in"],
            "punch_out": old["punch_out"],
            f"history.{n}": {"$exists": False},
        },
        {"$set": new, "$push": {"history": entry}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "record was modified concurrently; retry")
    return record_out(updated)


# --------------------------------------------------------------------------- #
# Analytics (all computed by MongoDB aggregation pipelines)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=MONTH_RE)):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")
    first, last = month_bounds(month)
    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    s = rows[0] if rows else {}
    wd = working_days(first, last, emp["joined_on"])
    present = s.get("present_days", 0)
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": half_up(present, 2),
        "leave_days": s.get("leave_days", 0),
        "late_count": s.get("late_count", 0),
        "total_late_minutes": s.get("total_late_minutes", 0),
        "total_overtime_minutes": s.get("total_overtime_minutes", 0),
        "attendance_pct": half_up(present / wd * 100, 2) if wd else None,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: str = Query(..., pattern=MONTH_RE), department: Optional[str] = None):
    first, last = month_bounds(month)
    items = []
    for r in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        items.append(
            {
                "department": r["_id"],
                "headcount": r["headcount"],
                "present_days": half_up(r["present_days"], 2),
                "avg_work_hours": half_up(r["hours_sum"] / r["hours_n"], 2) if r["hours_n"] else None,
                "late_count": r["late_count"],
                "total_late_minutes": r["total_late_minutes"],
                "leave_count": r["leave_count"],
                "on_duty_count": r["on_duty_count"],
            }
        )
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=MONTH_RE),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first, last = month_bounds(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department)))
    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    frm: str = Query(..., alias="from", pattern=DATE_RE),
    to: str = Query(..., pattern=DATE_RE),
):
    start, end = trend_range(frm, to)
    if not db.employees.find_one({"department": department}, {"_id": 1}):
        raise HTTPException(404, "unknown department")
    items = []
    for r in db.employees.aggregate(department_trend_pipeline(department, start, end)):
        rate, avg = r.get("attendance_rate"), r.get("moving_avg_7d")
        items.append(
            {
                "date": r["date"],
                "is_working_day": r["is_working_day"],
                "headcount": r["headcount"],
                "present_count": half_up(r["present_count"], 2),
                "late_count": r["late_count"],
                "attendance_rate": None if rate is None else half_up(rate, 4),
                "moving_avg_7d": None if avg is None else half_up(avg, 4),
            }
        )
    return {"department": department, "items": items}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
@app.get("/admin/explain/{endpoint}")
def explain(
    endpoint: Literal[
        "attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=MONTH_RE),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None, pattern=DATE_RE),
    date_to: Optional[str] = Query(None, pattern=DATE_RE),
    status: Optional[STATUSES] = None,
    frm: Optional[str] = Query(None, alias="from", pattern=DATE_RE),
    to: Optional[str] = Query(None, pattern=DATE_RE),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    def need(**params):
        missing = [k for k, v in params.items() if not v]
        if missing:
            unprocessable(f"missing required parameter(s): {', '.join(missing)}")

    if endpoint == "attendance_list":
        spec = attendance_list_spec(emp_code, date_from, date_to, status, page, page_size)
        collection = "attendance_logs"
        command = {"find": collection, **spec}
    else:
        if endpoint == "employee_monthly":
            need(emp_code=emp_code, month=month)
            collection = "attendance_logs"
            pipeline = employee_monthly_pipeline(emp_code, *month_bounds(month))
        elif endpoint == "department_summary":
            need(month=month)
            collection = "employees"
            pipeline = department_summary_pipeline(*month_bounds(month), department)
        elif endpoint == "late_leaderboard":
            need(month=month)
            collection = "attendance_logs"
            pipeline = late_leaderboard_pipeline(*month_bounds(month), limit, department)
        else:  # department_trend
            need(department=department, **{"from": frm, "to": to})
            collection = "employees"
            pipeline = department_trend_pipeline(department, *trend_range(frm, to))
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {}}

    raw = db.command({"explain": command, "verbosity": "executionStats"})
    plan = json.loads(json_util.dumps(raw, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": plan}