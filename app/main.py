"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
from decimal import Decimal, ROUND_HALF_UP
import os
import calendar
from datetime import datetime, timedelta, timezone
from datetime import date as date_cls
from typing import Optional, Literal
from pydantic import BaseModel, Field, field_validator
from collections import defaultdict

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi import Query as QueryParam
import json
from bson import json_util
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")

@app.on_event("startup")
def create_indexes():
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
    h, m = map(int, shift_start.split(":"))
    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
    minutes = int((punch_in - start).total_seconds() / 60)
    return minutes if minutes > 10 else 0


def round_half_up(value: float, decimals: int) -> float:
    q = Decimal(1).scaleb(-decimals)
    return float(Decimal(str(value)).quantize(q, rounding=ROUND_HALF_UP))


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = (punch_out - punch_in).total_seconds()
    return round_half_up(seconds / 3600, 2)

def date_range(from_str: str, to_str: str):
    """Yield each YYYY-MM-DD from from_str to to_str inclusive."""
    d = date_cls.fromisoformat(from_str)
    end = date_cls.fromisoformat(to_str)
    while d <= end:
        yield d.isoformat()
        d = d.fromordinal(d.toordinal() + 1)


def compute_overtime(punch_out: datetime, shift_end: str, date_str: str) -> int:
    h, m = map(int, shift_end.split(":"))
    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
    return max(0, int((punch_out - end).total_seconds() / 60))

def ms_to_dt(ms: int) -> datetime:
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).replace(tzinfo=None)


def dt_to_ms(dt: datetime) -> int:
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("shift_end")
    @classmethod
    def shift_end_must_differ(cls, v, info):
        if v == info.data.get("shift_start"):
            raise ValueError("shift_start must differ from shift_end")
        return v

class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"

class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)

class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[int] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)
    punch_out: Optional[int] = Field(None, ge=100_000_000_000, le=4_102_444_800_000)
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at:
        out_utc = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        out_utc = datetime.now(timezone.utc)
    out_utc = out_utc.replace(microsecond=0)
    out_ist = out_utc.astimezone(IST)
    out_naive = out_utc.replace(tzinfo=None)

    # Find the most recent open record (punch_in <= out_utc, punch_out is null)
    doc = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": out_naive},
            "punch_out": None,
        },
        sort=[("punch_in", -1)],
    )
    if not doc:
        raise HTTPException(404, "no open punch-in found")

    in_naive = doc["punch_in"]
    in_utc = in_naive.replace(tzinfo=timezone.utc)

    if out_utc <= in_utc:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (out_utc - in_utc).total_seconds() > 86400:
        raise HTTPException(422, "punched_at more than 24 hours after punch_in")

    # Compute work_hours (R4), half_day (R5), overtime_minutes (R3)
    seconds = (out_utc - in_utc).total_seconds()
    work_hours = round_half_up(seconds / 3600, 2)  # TODO: half-up rounding
    half_day = work_hours < 4.50

    # Shift end for this record's date, in IST
    date_str = doc["date"]
    h_end, m_end = map(int, emp["shift_end"].split(":"))
    shift_end_ist = datetime.fromisoformat(date_str).replace(
        hour=h_end, minute=m_end, tzinfo=IST
    )
    # Overnight shift: shift_end is on the next calendar day
    if emp["shift_end"] <= emp["shift_start"]:
        shift_end_ist = shift_end_ist + timedelta(days=1)

    overtime_raw = (out_ist - shift_end_ist).total_seconds() / 60
    overtime_minutes = int(overtime_raw) if overtime_raw >= 30 else 0
    if overtime_minutes < 0:
        overtime_minutes = 0

    result = db.attendance_logs.update_one(
        {"_id": doc["_id"], "punch_out": None},
        {"$set": {
            "punch_out": out_naive,
            "work_hours": work_hours,
            "overtime_minutes": overtime_minutes,
            "half_day": half_day,
        }},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "already punched out")

    updated = db.attendance_logs.find_one({"_id": doc["_id"]}, {"_id": 0})
    if isinstance(updated.get("punch_in"), datetime):
        updated["punch_in"] = dt_to_ms(updated["punch_in"])
    if isinstance(updated.get("punch_out"), datetime):
        updated["punch_out"] = dt_to_ms(updated["punch_out"])
    return updated


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB not reachable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc).replace(tzinfo=None)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    doc.pop("_id", None)
    doc["created_at"] = dt_to_ms(doc["created_at"])
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for it in items:
        if isinstance(it.get("created_at"), datetime):
            it["created_at"] = int(it["created_at"].replace(tzinfo=timezone.utc).timestamp() * 1000)
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    if body.punched_at:
        ts_utc = datetime.fromtimestamp(body.punched_at / 1000, tz=timezone.utc)
    else:
        ts_utc = datetime.now(timezone.utc)
    ts_utc = ts_utc.replace(microsecond=0)
    ts_ist = ts_utc.astimezone(IST)
    ts = ts_utc.replace(tzinfo=None)

    d = ts_ist.date().isoformat()
    # R1: overnight shift — if IST punch time is earlier than shift_end,
    # the punch belongs to the shift that started the previous day.
    h_end, m_end = map(int, emp["shift_end"].split(":"))
    h_start, m_start = map(int, emp["shift_start"].split(":"))
    shift_is_overnight = (h_end, m_end) <= (h_start, m_start)
    if shift_is_overnight:
        ist_minutes = ts_ist.hour * 60 + ts_ist.minute
        shift_end_minutes = h_end * 60 + m_end
        if ist_minutes < shift_end_minutes:
            d = (ts_ist.date() - timedelta(days=1)).isoformat()

    if db.attendance_logs.find_one({"emp_code": body.emp_code, "date": d}):
        raise HTTPException(409, "already punched in for this date")

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts_ist, emp["shift_start"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    db.attendance_logs.insert_one(doc)
    doc.pop("_id", None)
    doc["punch_in"] = dt_to_ms(ts)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    if page < 1:
        raise HTTPException(422, "page must be >= 1")
    if page_size < 1 or page_size > 100:
        raise HTTPException(422, "page_size must be between 1 and 100")
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must be <= date_to")

    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip(skip)
        .limit(page_size)
    )
    for d in docs:
        if isinstance(d.get("punch_in"), datetime):
            d["punch_in"] = dt_to_ms(d["punch_in"])
        if isinstance(d.get("punch_out"), datetime):
            d["punch_out"] = dt_to_ms(d["punch_out"])
    return {"items": docs, "total": total, "page": page, "page_size": page_size}

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not doc:
        raise HTTPException(404, "attendance record not found")

    # Build the "after" state: start from current values
    new_status = body.status if body.status is not None else doc.get("status")
    if body.punch_in is not None:
        new_punch_in = ms_to_dt(body.punch_in).replace(microsecond=0)
    else:
        new_punch_in = doc.get("punch_in")
    if body.punch_out is not None:
        new_punch_out = ms_to_dt(body.punch_out).replace(microsecond=0)
    else:
        new_punch_out = doc.get("punch_out")

    # Rule: ABSENT/LEAVE clear punch times
    if new_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE cannot have punch times")
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence statuses require a punch_in
        if new_punch_in is None:
            raise HTTPException(422, "presence status requires punch_in")
        # punch_out must be after punch_in and within 24h
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 86400:
                raise HTTPException(422, "punch_out more than 24h after punch_in")

    # punch_in must stay on the record's attendance date (in IST)
    ist_in = new_punch_in.replace(tzinfo=timezone.utc).astimezone(IST) if new_punch_in else None
    if ist_in and ist_in.date().isoformat() != date:
        raise HTTPException(422, "punch_in not on the record's attendance date")

    # Recompute derived fields
    new_late = 0
    new_work = None
    new_ot = 0
    new_half = False
    if new_punch_in is not None:
        ist_punch_in = new_punch_in.replace(tzinfo=timezone.utc).astimezone(IST)
        new_late = compute_late_minutes(ist_punch_in, emp["shift_start"])
    if new_punch_in is not None and new_punch_out is not None:
        seconds = (new_punch_out - new_punch_in).total_seconds()
        new_work = round_half_up(seconds / 3600, 2)
        new_half = new_work < 4.50
        # overtime
        ist_out = new_punch_out.replace(tzinfo=timezone.utc).astimezone(IST)
        h_end, m_end = map(int, emp["shift_end"].split(":"))
        shift_end_ist = datetime.fromisoformat(date).replace(
            hour=h_end, minute=m_end, tzinfo=IST
        )
        if emp["shift_end"] <= emp["shift_start"]:
            shift_end_ist = shift_end_ist + timedelta(days=1)
        ot_raw = (ist_out - shift_end_ist).total_seconds() / 60
        new_ot = int(ot_raw) if ot_raw >= 30 else 0
        if new_ot < 0:
            new_ot = 0

    # Compute what actually changed
    changes = {}
    def diff(field, old, new):
        if old != new:
            changes[field] = {"from": old, "to": new}

    diff("status", doc.get("status"), new_status)
    diff("punch_in", doc.get("punch_in"), new_punch_in)
    diff("punch_out", doc.get("punch_out"), new_punch_out)
    diff("late_minutes", doc.get("late_minutes", 0), new_late)
    diff("work_hours", doc.get("work_hours"), new_work)
    diff("overtime_minutes", doc.get("overtime_minutes", 0), new_ot)
    diff("half_day", doc.get("half_day", False), new_half)

    if not changes:
        raise HTTPException(422, "no changes in request")

    # Append audit trail entry (using $push so concurrent edits don't lose history)
    history_entry = {
        "at": datetime.now(timezone.utc).replace(tzinfo=None),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    result = db.attendance_logs.update_one(
        {"_id": doc["_id"]},
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "late_minutes": new_late,
                "work_hours": new_work,
                "overtime_minutes": new_ot,
                "half_day": new_half,
            },
            "$push": {"history": history_entry},
        },
    )
    if result.modified_count == 0:
        raise HTTPException(409, "concurrent modification")

    updated = db.attendance_logs.find_one({"_id": doc["_id"]}, {"_id": 0})
    # serialize for API
    if isinstance(updated.get("punch_in"), datetime):
        updated["punch_in"] = dt_to_ms(updated["punch_in"])
    if isinstance(updated.get("punch_out"), datetime):
        updated["punch_out"] = dt_to_ms(updated["punch_out"])
    for h in updated.get("history", []):
        if isinstance(h.get("at"), datetime):
            h["at"] = dt_to_ms(h["at"])
        # convert nested date values in changes
        for field, change in (h.get("changes") or {}).items():
            if field in ("punch_in", "punch_out"):
                for k in ("from", "to"):
                    if isinstance(change.get(k), datetime):
                        change[k] = dt_to_ms(change[k])
    return updated

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    # Validate month format YYYY-MM
    try:
        year, mon = map(int, month.split("-"))
        if not (1 <= mon <= 12):
            raise ValueError
    except Exception:
        raise HTTPException(422, "month must be YYYY-MM")

    # Look up the employee (404 if missing)
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # Compute the month's first and last day
    first_day = f"{year:04d}-{mon:02d}-01"
    last_day_num = calendar.monthrange(year, mon)[1]
    last_day = f"{year:04d}-{mon:02d}-{last_day_num:02d}"

    # Aggregation over attendance_logs
    pipeline = [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "is_weekday": {
                "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                        [2, 3, 4, 5, 6]]
            }
        }},
        {"$group": {
            "_id": None,
            "present_days": {
                "$sum": {
                    "$cond": [
                        {"$and": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            "$is_weekday"
                        ]},
                        {"$cond": ["$half_day", 0.5, 1]},
                        0
                    ]
                }
            },
            "leave_days": {
                "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    if result:
        agg = result[0]
        present_days = float(agg["present_days"])
        leave_days = int(agg["leave_days"])
        late_count = int(agg["late_count"])
        total_late = int(agg["total_late_minutes"])
        total_ot = int(agg["total_overtime_minutes"])
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    # working_days: count Mon-Fri from max(joined_on, month_start) to month_end
    joined = date_cls.fromisoformat(emp["joined_on"])
    month_start = date_cls(year, mon, 1)
    month_end = date_cls(year, mon, last_day_num)
    start = max(joined, month_start)

    working_days = 0
    d = start
    while d <= month_end:
        if d.weekday() < 5:
            working_days += 1
        d = d.fromordinal(d.toordinal() + 1)

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up((present_days / working_days) * 100, 4)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary(month: str, department: Optional[str] = None):
    try:
        year, mon = map(int, month.split("-"))
        if not (1 <= mon <= 12):
            raise ValueError
    except Exception:
        raise HTTPException(422, "month must be YYYY-MM")

    first_day = f"{year:04d}-{mon:02d}-01"
    last_day_num = calendar.monthrange(year, mon)[1]
    last_day = f"{year:04d}-{mon:02d}-{last_day_num:02d}"

    emp_filter = {"joined_on": {"$lte": last_day}}
    if department:
        emp_filter["department"] = department
    emps = list(db.employees.find(emp_filter, {"_id": 0, "emp_code": 1, "department": 1}))
    if not emps:
        return {"month": month, "items": []}

    emp_codes = [e["emp_code"] for e in emps]

    log_pipeline = [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": first_day, "$lte": last_day}}},
        {"$addFields": {
            "is_weekday": {
                "$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, [2, 3, 4, 5, 6]]
            }
        }},
        {"$group": {
            "_id": "$emp_code",
            "present_days": {
                "$sum": {"$cond": [
                    {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]}, "$is_weekday"]},
                    {"$cond": ["$half_day", 0.5, 1]},
                    0
                ]}
            },
            "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "work_hours_sum": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$work_hours", None]}
                ]},
                "$work_hours",
                0
            ]}},
            "work_hours_count": {"$sum": {"$cond": [
                {"$and": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$ne": ["$work_hours", None]}
                ]},
                1,
                0
            ]}},
        }},
    ]

    log_rows = list(db.attendance_logs.aggregate(log_pipeline))
    log_by_emp = {r["_id"]: r for r in log_rows}

    dept_agg = defaultdict(lambda: {
        "headcount": 0,
        "present_days": 0.0,
        "work_sum": 0.0,
        "work_count": 0,
        "late_count": 0,
        "total_late_minutes": 0,
        "leave_count": 0,
        "on_duty_count": 0,
    })

    for emp in emps:
        d = emp["department"]
        dept_agg[d]["headcount"] += 1
        r = log_by_emp.get(emp["emp_code"])
        if not r:
            continue
        dept_agg[d]["present_days"] += float(r["present_days"])
        dept_agg[d]["work_sum"] += float(r["work_hours_sum"])
        dept_agg[d]["work_count"] += int(r["work_hours_count"])
        dept_agg[d]["late_count"] += int(r["late_count"])
        dept_agg[d]["total_late_minutes"] += int(r["total_late_minutes"])
        dept_agg[d]["leave_count"] += int(r["leave_count"])
        dept_agg[d]["on_duty_count"] += int(r["on_duty_count"])

    items = []
    for dept_name in sorted(dept_agg.keys()):
        v = dept_agg[dept_name]
        avg_wh = None
        if v["work_count"] > 0:
            avg_wh = round_half_up(v["work_sum"] / v["work_count"], 2)
        items.append({
            "department": dept_name,
            "headcount": v["headcount"],
            "present_days": v["present_days"],
            "avg_work_hours": avg_wh,
            "late_count": v["late_count"],
            "total_late_minutes": v["total_late_minutes"],
            "leave_count": v["leave_count"],
            "on_duty_count": v["on_duty_count"],
        })

    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: str, limit: int = 10, department: Optional[str] = None):
    try:
        year, mon = map(int, month.split("-"))
        if not (1 <= mon <= 12):
            raise ValueError
    except Exception:
        raise HTTPException(422, "month must be YYYY-MM")

    if limit < 1 or limit > 50:
        raise HTTPException(422, "limit must be between 1 and 50")

    first_day = f"{year:04d}-{mon:02d}-01"
    last_day_num = calendar.monthrange(year, mon)[1]
    last_day = f"{year:04d}-{mon:02d}-{last_day_num:02d}"

    emp_codes = None
    if department:
        emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        emp_codes = [e["emp_code"] for e in emps]
        if not emp_codes:
            return {"month": month, "items": []}

    match = {"date": {"$gte": first_day, "$lte": last_day}}
    if emp_codes is not None:
        match["emp_code"] = {"$in": emp_codes}

    pipeline = [
        {"$match": match},
        {"$group": {
            "_id": "$emp_code",
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
        }},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$setWindowFields": {
            "sortBy": {"total_late_minutes": -1},
            "output": {"rank": {"$rank": {}}}
        }},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
    ]

    rows = list(db.attendance_logs.aggregate(pipeline))

    codes = [r["_id"] for r in rows]
    emp_lookup = {e["emp_code"]: e for e in db.employees.find(
        {"emp_code": {"$in": codes}},
        {"_id": 0, "emp_code": 1, "name": 1, "department": 1}
    )}

    items = []
    for r in rows:
        emp = emp_lookup.get(r["_id"])
        if not emp:
            continue
        items.append({
            "rank": r["rank"],
            "emp_code": r["_id"],
            "name": emp["name"],
            "department": emp["department"],
            "total_late_minutes": r["total_late_minutes"],
            "late_count": r["late_count"],
        })

    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    start: str = QueryParam(..., alias="from"),
    end: str = QueryParam(..., alias="to"),
):
    try:
        d_from = date_cls.fromisoformat(start)
        d_to = date_cls.fromisoformat(end)
    except Exception:
        raise HTTPException(422, "from and to must be YYYY-MM-DD")

    if d_to < d_from:
        raise HTTPException(422, "to must be >= from")
    if (d_to - d_from).days > 91:
        raise HTTPException(422, "range exceeds 92 days")

    all_emps = list(db.employees.find(
        {"department": department},
        {"_id": 0, "emp_code": 1, "joined_on": 1}
    ))
    if not all_emps:
        raise HTTPException(404, "department not found")

    emp_codes = [e["emp_code"] for e in all_emps]

    pipeline = [
        {"$match": {
            "emp_code": {"$in": emp_codes},
            "date": {"$gte": start, "$lte": end},
        }},
        {"$group": {
            "_id": "$date",
            "present_count": {
                "$sum": {"$cond": [
                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                    {"$cond": ["$half_day", 0.5, 1]},
                    0
                ]}
            },
            "late_count": {
                "$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}
            },
        }},
    ]
    rows = list(db.attendance_logs.aggregate(pipeline))
    by_date = {r["_id"]: r for r in rows}

    items = []
    for date_str in date_range(start, end):
        d = date_cls.fromisoformat(date_str)
        is_working = d.weekday() < 5

        hc = sum(1 for e in all_emps if e["joined_on"] <= date_str)

        row = by_date.get(date_str)
        present_count = float(row["present_count"]) if row else 0.0
        late_count = int(row["late_count"]) if row else 0

        if is_working and hc > 0:
            attendance_rate = round_half_up((present_count / hc), 4)
        else:
            attendance_rate = None

        items.append({
            "date": date_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": present_count,
            "late_count": late_count,
            "attendance_rate": attendance_rate,
            "moving_avg_7d": None,
        })

    for i, it in enumerate(items):
        window = items[max(0, i - 6): i + 1]
        non_null = [w["attendance_rate"] for w in window if w["attendance_rate"] is not None]
        if non_null:
            avg = sum(non_null) / len(non_null)
            it["moving_avg_7d"] = round_half_up(avg, 4)

    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    start: Optional[str] = QueryParam(None, alias="from"),
    end: Optional[str] = QueryParam(None, alias="to"),
    page: int = 1,
    page_size: int = 20,
):
    if endpoint == "attendance_list":
        # Same query as /attendance
        q = {}
        if emp_code: q["emp_code"] = emp_code
        if date_from or date_to:
            q["date"] = {}
            if date_from: q["date"]["$gte"] = date_from
            if date_to:   q["date"]["$lte"] = date_to
        if status: q["status"] = status
        skip = (page - 1) * page_size
        cursor = (
            db.attendance_logs.find(q, {"_id": 0})
            .sort([("date", -1), ("emp_code", 1)])
            .skip(skip)
            .limit(page_size)
        )
        explain = cursor.explain()
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain))}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        year, mon = map(int, month.split("-"))
        first_day = f"{year:04d}-{mon:02d}-01"
        last_day_num = calendar.monthrange(year, mon)[1]
        last_day = f"{year:04d}-{mon:02d}-{last_day_num:02d}"
        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": None, "total_late": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
        ]
        result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(result))}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        year, mon = map(int, month.split("-"))
        last_day_num = calendar.monthrange(year, mon)[1]
        last_day = f"{year:04d}-{mon:02d}-{last_day_num:02d}"
        q = {"joined_on": {"$lte": last_day}}
        if department:
            q["department"] = department
        explain = db.employees.find(q, {"_id": 0, "emp_code": 1}).explain()
        return {"endpoint": endpoint, "collection": "employees",
                "explain": json.loads(json_util.dumps(explain))}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        year, mon = map(int, month.split("-"))
        first_day = f"{year:04d}-{mon:02d}-01"
        last_day_num = calendar.monthrange(year, mon)[1]
        last_day = f"{year:04d}-{mon:02d}-{last_day_num:02d}"
        pipeline = [
            {"$match": {"date": {"$gte": first_day, "$lte": last_day}}},
            {"$group": {"_id": "$emp_code", "total": {"$sum": {"$ifNull": ["$late_minutes", 0]}}}},
        ]
        result = db.command("aggregate", "attendance_logs", pipeline=pipeline, explain=True)
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(result))}

    elif endpoint == "department_trend":
        if not department or not start or not end:
            raise HTTPException(422, "department, from, to required")
        emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1}))
        codes = [e["emp_code"] for e in emps]
        q = {"emp_code": {"$in": codes}, "date": {"$gte": start, "$lte": end}}
        explain = db.attendance_logs.find(q).explain()
        return {"endpoint": endpoint, "collection": "attendance_logs",
                "explain": json.loads(json_util.dumps(explain))}

    else:
        raise HTTPException(422, "unknown endpoint")

