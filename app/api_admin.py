"""Administrator API. The whole router is gated by `require_admin` (default deny); each handler
additionally validates the objects it is asked to touch."""
from __future__ import annotations

import csv
import io
from datetime import datetime

from fastapi import APIRouter, Body, Depends, HTTPException, Query, Request, Response
from pydantic import ValidationError
from sqlalchemy import func, select, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from . import models as m
from . import schemas as sc
from . import services as svc
from .db import get_db
from .security import (CurrentUser, audit, hash_password, password_problem, rate_limit, require_admin, revoke_sessions,
                       user_roles)
from .solver import auditor
from .solver.preflight import preflight
from .solver.problem import DAY_NAMES, Slot, apply_scenario, fmt, load_problem
from .solver.rules import REGISTRY, default_rules, registry_public, validate_rule_config

router = APIRouter(prefix="/api/admin", dependencies=[Depends(require_admin)])

RESOURCES: dict[str, tuple[type, type[sc.Strict], str]] = {
    "departments": (m.Department, sc.DepartmentIn, "code"),
    "academic_terms": (m.AcademicTerm, sc.TermIn, "name"),
    "programmes": (m.Programme, sc.ProgrammeIn, "code"),
    "student_groups": (m.StudentGroup, sc.GroupIn, "name"),
    "faculty_profiles": (m.FacultyProfile, sc.FacultyIn, "name"),
    "subjects": (m.Subject, sc.SubjectIn, "code"),
    "teaching_assignments": (m.TeachingAssignment, sc.AssignmentIn, "id"),
    "rooms": (m.Room, sc.RoomIn, "code"),
    "room_features": (m.RoomFeature, sc.RoomFeatureIn, "code"),
    "working_days": (m.WorkingDay, sc.WorkingDayIn, "day_of_week"),
    "period_slots": (m.PeriodSlot, sc.PeriodIn, "start_minute"),
    "scheduled_breaks": (m.ScheduledBreak, sc.BreakIn, "start_minute"),
    "faculty_availability": (m.FacultyAvailability, sc.AvailabilityIn, "id"),
    "fixed_commitments": (m.FixedCommitment, sc.FixedIn, "id"),
}
FILTERS = ("term_id", "department_id", "room_id", "faculty_id", "subject_id", "student_group_id", "programme_id")


def row(obj) -> dict:
    out = {}
    for c in obj.__table__.columns:
        v = getattr(obj, c.name)
        out[c.name] = v.isoformat() + "Z" if isinstance(v, datetime) else v
    return out


def get_or_404(db: Session, model, id_: int):
    obj = db.get(model, id_)
    if obj is None:
        raise HTTPException(404, "Not found.")
    return obj


def parse(schema, body):
    try:
        return schema.model_validate(body)
    except ValidationError as exc:
        raise HTTPException(422, [{"field": ".".join(str(p) for p in e["loc"]), "message": e["msg"]} for e in exc.errors()])


def resource(name: str):
    if name not in RESOURCES:
        raise HTTPException(404, "Unknown resource.")
    return RESOURCES[name]


def cross_check(db: Session, name: str, data: dict, existing_id: int | None) -> None:
    """Relationship rules that a single-table constraint cannot express."""
    if name == "student_groups" and data.get("parent_group_id"):
        parent = db.get(m.StudentGroup, data["parent_group_id"])
        if parent is None or parent.term_id != data["term_id"]:
            raise HTTPException(422, "The parent group must exist in the same term.")
        seen, cur = set(), parent
        while cur is not None:
            if cur.id == existing_id or cur.id in seen:
                raise HTTPException(422, "A group cannot be its own ancestor.")
            seen.add(cur.id)
            cur = db.get(m.StudentGroup, cur.parent_group_id) if cur.parent_group_id else None
    if name == "subjects":
        g = db.get(m.StudentGroup, data["student_group_id"])
        if g is None or g.term_id != data["term_id"]:
            raise HTTPException(422, "The student group must belong to the selected term.")
    if name == "fixed_commitments" and data.get("teaching_assignment_id"):
        a = db.get(m.TeachingAssignment, data["teaching_assignment_id"])
        if a is None or a.faculty_id != data["faculty_id"]:
            raise HTTPException(422, "The fixed session's faculty member must be the one assigned to that subject.")


def commit(db: Session) -> None:
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "The change conflicts with existing data (duplicate value, missing reference, or the record is still in use).")


# ---- generic master data ----------------------------------------------------------------------
@router.get("/data/{name}")
def list_rows(name: str, request: Request, db: Session = Depends(get_db)):
    model, _, order = resource(name)
    q = select(model)
    for f in FILTERS:
        v = request.query_params.get(f)
        if v is not None and hasattr(model, f):
            if not v.isdigit():
                raise HTTPException(422, f"{f} must be a number.")
            q = q.where(getattr(model, f) == int(v))
    return [row(o) for o in db.scalars(q.order_by(getattr(model, order)).limit(5000))]


@router.post("/data/{name}", status_code=201)
def create_row(name: str, request: Request, body: dict = Body(...), db: Session = Depends(get_db),
               user: CurrentUser = Depends(require_admin)):
    model, schema, _ = resource(name)
    data = parse(schema, body).model_dump()
    cross_check(db, name, data, None)
    obj = model(**data)
    db.add(obj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "The change conflicts with existing data (duplicate value or missing reference).")
    audit(db, user, f"{name}.create", name, obj.id, data, request)
    commit(db)
    return row(obj)


@router.put("/data/{name}/{id_}")
def update_row(name: str, id_: int, request: Request, body: dict = Body(...), db: Session = Depends(get_db),
               user: CurrentUser = Depends(require_admin)):
    model, schema, _ = resource(name)
    obj = get_or_404(db, model, id_)
    data = parse(schema, body).model_dump()
    cross_check(db, name, data, id_)
    changed = {k: v for k, v in data.items() if getattr(obj, k) != v}
    for k, v in changed.items():
        setattr(obj, k, v)
    audit(db, user, f"{name}.update", name, id_, changed, request)
    commit(db)
    return row(obj)


@router.delete("/data/{name}/{id_}", status_code=204)
def delete_row(name: str, id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    model, _, _ = resource(name)
    obj = get_or_404(db, model, id_)
    db.delete(obj)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "This record is still referenced by other data (for example a timetable) and cannot be deleted. "
                                 "Deactivate it instead.")
    audit(db, user, f"{name}.delete", name, id_, {}, request)
    commit(db)
    return Response(status_code=204)


def csv_safe(v) -> str:
    """Neutralise spreadsheet formula injection on export."""
    s = "" if v is None else ";".join(map(str, v)) if isinstance(v, list) else str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") and not s.lstrip("-").replace(".", "", 1).isdigit() else s


def csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(header)
    for r in rows:
        w.writerow([csv_safe(v) for v in r])
    return Response(buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@router.get("/data/{name}/export.csv", dependencies=[Depends(rate_limit("export", 60, 3600))])
def export_rows(name: str, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    model, schema, order = resource(name)
    fields = list(schema.model_fields)
    rows = [[getattr(o, f) for f in fields] for o in db.scalars(select(model).order_by(getattr(model, order)).limit(20000))]
    audit(db, user, f"{name}.export", name, None, {"rows": len(rows)}, request)
    db.commit()
    return csv_response(f"{name}.csv", fields, rows)


@router.post("/data/{name}/import")
def import_rows(name: str, request: Request, body: sc.ImportIn, db: Session = Depends(get_db),
                user: CurrentUser = Depends(require_admin)):
    """Validated CSV import: every row goes through the same schema as the API. Nothing is saved
    unless every row is valid and dry_run is false. Formulas are rejected, never evaluated."""
    model, schema, _ = resource(name)
    reader = csv.DictReader(io.StringIO(body.csv))
    fields = set(schema.model_fields)
    if not reader.fieldnames or set(reader.fieldnames) - fields:
        raise HTTPException(422, f"Unexpected column(s). Allowed columns: {', '.join(sorted(fields))}")
    errors, clean = [], []
    for n, raw in enumerate(reader, start=2):
        if n > 2001:
            errors.append({"row": n, "errors": ["Too many rows (limit 2000)."]})
            break
        rec = {}
        for k, v in raw.items():
            v = (v or "").strip()
            if v == "":
                continue
            if v[0] in "=+@\t\r" or (v[0] == "-" and not v[1:].replace(".", "", 1).isdigit()):
                errors.append({"row": n, "errors": [f"{k}: values starting with = + - @ are rejected (possible spreadsheet formula)."]})
                continue
            rec[k] = [x.strip() for x in v.split(";") if x.strip()] if k.endswith("_features") else v
        try:
            data = schema.model_validate(rec).model_dump()
            cross_check(db, name, data, None)
            clean.append(data)
        except ValidationError as exc:
            errors.append({"row": n, "errors": [f"{'.'.join(str(p) for p in e['loc'])}: {e['msg']}" for e in exc.errors()]})
        except HTTPException as exc:
            errors.append({"row": n, "errors": [str(exc.detail)]})
    result = {"rows": len(clean) + len(errors), "valid": len(clean), "errors": errors[:200], "imported": 0, "dry_run": body.dry_run}
    if errors or body.dry_run:
        return result
    try:
        for data in clean:
            db.add(model(**data))
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Import aborted: a row duplicates existing data or references a missing record. Nothing was saved.")
    audit(db, user, f"{name}.import", name, None, {"rows": len(clean)}, request)
    commit(db)
    result["imported"] = len(clean)
    return result


# ---- rule sets --------------------------------------------------------------------------------
def ruleset_row(db: Session, rs: m.RuleSet) -> dict:
    rules = {r.rule_key: r for r in db.scalars(select(m.RuleDefinition).where(m.RuleDefinition.rule_set_id == rs.id))}
    return {**row(rs), "rules": [{"rule_key": k, "enabled": r.enabled, "is_hard": r.is_hard, "weight": r.weight, "params": r.params}
                                 for k in REGISTRY if (r := rules.get(k))]}


@router.get("/rules/registry")
def rule_registry():
    return registry_public()


@router.get("/rule-sets")
def rule_sets(db: Session = Depends(get_db)):
    svc.default_rule_set(db)
    svc.ensure_rule_rows(db)
    db.commit()
    return [ruleset_row(db, rs) for rs in db.scalars(select(m.RuleSet).order_by(m.RuleSet.id))]


@router.post("/rule-sets", status_code=201)
def create_rule_set(body: sc.RuleSetIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    rs = m.RuleSet(name=body.name)
    db.add(rs)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "A rule set with this name already exists.")
    src = {r.rule_key: r for r in db.scalars(select(m.RuleDefinition).where(m.RuleDefinition.rule_set_id == body.copy_from))} if body.copy_from else {}
    for d in default_rules():
        if d["rule_key"] in src:
            s = src[d["rule_key"]]
            d = validate_rule_config(s.rule_key, s.enabled, s.is_hard, s.weight, s.params or {})
        db.add(m.RuleDefinition(rule_set_id=rs.id, **d))
    audit(db, user, "rule_set.create", "rule_sets", rs.id, {"name": body.name, "copy_from": body.copy_from}, request)
    commit(db)
    return ruleset_row(db, rs)


@router.patch("/rule-sets/{id_}")
def patch_rule_set(id_: int, body: sc.RuleSetPatch, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_admin)):
    rs = get_or_404(db, m.RuleSet, id_)
    if body.name is not None:
        rs.name = body.name
    if body.active is not None:
        rs.active = body.active
    audit(db, user, "rule_set.update", "rule_sets", id_, body.model_dump(exclude_none=True), request)
    commit(db)
    return ruleset_row(db, rs)


@router.put("/rule-sets/{id_}/rules/{key}")
def set_rule(id_: int, key: str, body: sc.RuleConfigIn, request: Request, db: Session = Depends(get_db),
             user: CurrentUser = Depends(require_admin)):
    rs = get_or_404(db, m.RuleSet, id_)
    try:
        cfg = validate_rule_config(key, body.enabled, body.is_hard, body.weight, body.params)
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    r = db.scalars(select(m.RuleDefinition).where(m.RuleDefinition.rule_set_id == id_, m.RuleDefinition.rule_key == key)).first()
    if r is None:
        r = m.RuleDefinition(rule_set_id=id_, rule_key=key)
        db.add(r)
    r.enabled, r.is_hard, r.weight, r.params = cfg["enabled"], cfg["is_hard"], cfg["weight"], cfg["params"]
    rs.version += 1
    audit(db, user, "rule_set.rule_update", "rule_sets", id_, cfg, request)
    commit(db)
    return ruleset_row(db, rs)


# ---- users ------------------------------------------------------------------------------------
def user_row(db: Session, u: m.AppUser) -> dict:
    return {"id": u.id, "username": u.username, "full_name": u.full_name, "email": u.email, "active": u.active,
            "roles": sorted(user_roles(db, u.id)), "faculty_id": u.faculty_id, "student_group_id": u.student_group_id,
            "auth_provider": u.auth_provider, "mfa_enabled": u.mfa_enabled}


def set_roles(db: Session, u: m.AppUser, roles: list[str]) -> None:
    db.execute(text("DELETE FROM app_user_roles WHERE user_id = :u"), {"u": u.id})
    for r in db.scalars(select(m.AppRole).where(m.AppRole.code.in_(set(roles)))):
        db.add(m.AppUserRole(user_id=u.id, role_id=r.id))


@router.get("/users")
def users(db: Session = Depends(get_db)):
    return [user_row(db, u) for u in db.scalars(select(m.AppUser).order_by(m.AppUser.username))]


@router.post("/users", status_code=201)
def create_user(body: sc.UserIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    if body.password is None and not body.email:
        raise HTTPException(422, "Give an e-mail address (for Clerk sign-in) or a password.")
    if body.password is not None and (msg := password_problem(body.password)):
        raise HTTPException(422, msg)
    u = m.AppUser(username=body.username.lower(), full_name=body.full_name, email=body.email.lower() if body.email else None,
                  password_hash=hash_password(body.password) if body.password else None,
                  faculty_id=body.faculty_id, student_group_id=body.student_group_id)
    db.add(u)
    try:
        db.flush()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Username or e-mail already exists, or a linked record is missing.")
    set_roles(db, u, body.roles)
    audit(db, user, "user.create", "app_users", u.id, {"username": u.username, "roles": body.roles}, request)
    commit(db)
    return user_row(db, u)


@router.patch("/users/{id_}")
def patch_user(id_: int, body: sc.UserPatch, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    u = get_or_404(db, m.AppUser, id_)
    if id_ == user.id and (body.active is False or (body.roles is not None and "admin" not in body.roles)):
        raise HTTPException(409, "You cannot deactivate yourself or remove your own administrator role.")
    if body.password is not None:
        if (msg := password_problem(body.password)):
            raise HTTPException(422, msg)
        u.password_hash, u.failed_logins, u.locked_until = hash_password(body.password), 0, None
    if body.full_name is not None:
        u.full_name = body.full_name
    if body.email is not None and body.email.lower() != (u.email or ""):
        u.email, u.external_subject = body.email.lower(), None  # a new e-mail must be re-linked at next Clerk sign-in
    if body.active is not None:
        u.active = body.active
    if body.clear_links:
        u.faculty_id = u.student_group_id = None
    if body.faculty_id is not None:
        u.faculty_id = body.faculty_id
    if body.student_group_id is not None:
        u.student_group_id = body.student_group_id
    if body.roles is not None:
        set_roles(db, u, body.roles)
    if body.password is not None or body.roles is not None or body.active is False:
        revoke_sessions(db, u.id, keep=user.token_hash if id_ == user.id else None)  # privilege change => re-login
    audit(db, user, "user.update", "app_users", id_, body.model_dump(exclude_none=True), request)
    commit(db)
    return user_row(db, u)


# ---- preflight and runs -----------------------------------------------------------------------
@router.post("/preflight")
def run_preflight(body: sc.PreflightIn, db: Session = Depends(get_db)):
    get_or_404(db, m.AcademicTerm, body.term_id)
    rs = get_or_404(db, m.RuleSet, body.rule_set_id) if body.rule_set_id else svc.default_rule_set(db)
    db.commit()
    p = load_problem(db, body.term_id, rs.id)
    f = preflight(p)
    return {"ok": not auditor.errors(f), "findings": f, "sessions_required": len(p.needs), "input_hash": p.snapshot_hash()}


@router.post("/runs", status_code=202, dependencies=[Depends(rate_limit("runs", 30, 3600))])
def create_run(body: sc.RunIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    get_or_404(db, m.AcademicTerm, body.term_id)
    rs = get_or_404(db, m.RuleSet, body.rule_set_id) if body.rule_set_id else svc.default_rule_set(db)
    if body.base_version_id:
        base = get_or_404(db, m.TimetableVersion, body.base_version_id)
        if base.term_id != body.term_id:
            raise HTTPException(422, "The base version belongs to another term.")
    elif body.kind != "generate":
        raise HTTPException(422, "Repair and what-if runs need a base version.")
    if body.kind != "whatif" and body.scenario and not body.sandbox:
        raise HTTPException(422, "Scenario changes are only allowed in a sandbox (what-if) run.")
    scenario = [c.model_dump(exclude_none=True) for c in body.scenario]
    try:
        apply_scenario(load_problem(db, body.term_id, rs.id), scenario)  # validate against real ids before queueing
    except ValueError as exc:
        raise HTTPException(422, str(exc))
    if svc.queued_count(db) >= svc.settings.max_queued_runs:
        raise HTTPException(429, "Too many generation jobs are queued. Wait for one to finish or cancel it.")
    run = m.TimetableRun(term_id=body.term_id, rule_set_id=rs.id, rule_set_version=rs.version, requested_by=user.id, kind=body.kind,
                         params={"time_limit": body.time_limit, "candidates": body.candidates, "base_version_id": body.base_version_id,
                                 "scenario": scenario, "sandbox": body.sandbox or body.kind == "whatif"})
    db.add(run)
    db.flush()
    audit(db, user, "run.create", "timetable_runs", run.id, {"kind": body.kind, "term_id": body.term_id, "rule_set": rs.id}, request)
    db.commit()
    svc.submit_run(run.id)
    db.expire_all()
    return svc.run_row(db, db.get(m.TimetableRun, run.id))


@router.get("/runs")
def list_runs(term_id: int | None = None, db: Session = Depends(get_db)):
    q = select(m.TimetableRun).order_by(m.TimetableRun.id.desc()).limit(50)
    if term_id:
        q = q.where(m.TimetableRun.term_id == term_id)
    return [svc.run_row(db, r) for r in db.scalars(q)]


@router.get("/runs/{id_}")
def get_run(id_: int, db: Session = Depends(get_db)):
    return svc.run_row(db, get_or_404(db, m.TimetableRun, id_), full=True)


@router.post("/runs/{id_}/cancel")
def cancel_run(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    run = get_or_404(db, m.TimetableRun, id_)
    if run.status not in ("queued", "running"):
        raise HTTPException(409, "This run has already finished.")
    svc.cancel_run(id_)
    audit(db, user, "run.cancel", "timetable_runs", id_, {}, request)
    db.commit()
    return {"ok": True}


# ---- versions ---------------------------------------------------------------------------------
def editable(v: m.TimetableVersion) -> None:
    if v.status not in ("draft", "validated"):
        raise HTTPException(409, "Published and archived timetables are immutable. Clone the version to make changes.")


def bump(db: Session, v: m.TimetableVersion, expected: int) -> None:
    """Optimistic concurrency: the edit only goes through if nobody else changed the version meanwhile."""
    res = db.execute(update(m.TimetableVersion).where(m.TimetableVersion.id == v.id, m.TimetableVersion.row_version == expected)
                     .values(row_version=expected + 1, status="draft", validated_at=None))
    if res.rowcount != 1:
        db.rollback()
        raise HTTPException(409, "This timetable was changed by someone else. Reload it and try again.")
    db.refresh(v)


def version_payload(db: Session, v: m.TimetableVersion) -> dict:
    return {"version": svc.version_row(v), "sessions": svc.session_rows(db, v.id), "calendar": svc.term_calendar(db, v.term_id),
            "findings": svc.finding_rows(db, version_id=v.id),
            "groups": [{"id": g.id, "name": g.name, "parent_group_id": g.parent_group_id}
                       for g in db.scalars(select(m.StudentGroup).where(m.StudentGroup.term_id == v.term_id))]}


@router.get("/versions")
def list_versions(term_id: int | None = None, db: Session = Depends(get_db)):
    q = select(m.TimetableVersion).order_by(m.TimetableVersion.id.desc()).limit(200)
    if term_id:
        q = q.where(m.TimetableVersion.term_id == term_id)
    counts = dict(db.execute(select(m.TimetableSession.version_id, func.count()).group_by(m.TimetableSession.version_id)).all())
    return [{**svc.version_row(v), "session_count": counts.get(v.id, 0)} for v in db.scalars(q)]


@router.get("/versions/compare")
def compare_versions(ids: str = Query(..., max_length=60, pattern=r"^\d+(,\d+){0,4}$"), db: Session = Depends(get_db)):
    vs = [get_or_404(db, m.TimetableVersion, int(i)) for i in ids.split(",")]
    if len({v.term_id for v in vs}) > 1:
        raise HTTPException(422, "Versions from different terms cannot be compared.")
    out, first, problem = [], None, None
    for v in vs:
        problem = svc.problem_for_version(db, v)
        slots = svc.version_slots(db, v.id)
        found = auditor.audit(problem, slots)
        an = svc.analytics(problem, slots)
        sc_ = svc.score(problem, slots, None)
        first = first if first is not None else slots
        out.append({"version": svc.version_row(v), "hard_errors": len(auditor.errors(found)), "valid": not auditor.errors(found),
                    "score": sc_["total"], "rules": sc_["rules"], "sessions": len(slots),
                    "moved_vs_first": len(svc.diff_slots(problem, first, slots)) if first is not slots else 0,
                    "avg_room_utilisation": round(sum(r["utilisation"] for r in an["rooms"]) / max(1, len(an["rooms"]))),
                    "max_faculty_day": max((max(f["per_day"].values()) for f in an["faculty"]), default=0),
                    "unused_seat_periods": an["unused_seat_periods"]})
    best = min((o for o in out if o["valid"]), key=lambda o: o["score"], default=None)
    return {"candidates": out, "best_version_id": best and best["version"]["id"],
            "note": "Lower score is better. Hard errors invalidate a candidate regardless of score."}


@router.get("/versions/{id_}")
def get_version(id_: int, db: Session = Depends(get_db)):
    return version_payload(db, get_or_404(db, m.TimetableVersion, id_))


@router.get("/versions/{id_}/analytics")
def version_analytics(id_: int, db: Session = Depends(get_db)):
    v = get_or_404(db, m.TimetableVersion, id_)
    return svc.analytics(svc.problem_for_version(db, v), svc.version_slots(db, v.id))


@router.post("/versions/{id_}/validate")
def validate_version(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    editable(v)
    errs = auditor.errors(svc.refresh_version(db, v))
    v.status, v.validated_at = ("draft", None) if errs else ("validated", m.utcnow())
    v.row_version += 1
    audit(db, user, "version.validate", "timetable_versions", id_, {"errors": len(errs)}, request, outcome="ok" if not errs else "failed")
    db.commit()
    return {"valid": not errs, **version_payload(db, v)}


@router.post("/versions/{id_}/publish")
def publish_version(id_: int, body: sc.PublishIn, request: Request, db: Session = Depends(get_db),
                    user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    if v.is_sandbox:
        raise HTTPException(409, "A what-if sandbox can never be published. Clone it into a normal draft first.")
    if v.status != "validated":
        raise HTTPException(409, "Only a validated timetable can be published. Run validation first.")
    # inputs may have changed since validation -> audit again right before publishing
    errs = auditor.errors(svc.refresh_version(db, v))
    if errs or not svc.version_slots(db, v.id):
        v.status, v.validated_at = "draft", None
        audit(db, user, "version.publish", "timetable_versions", id_, {"errors": len(errs)}, request, outcome="failed")
        db.commit()
        raise HTTPException(409, "The timetable no longer passes validation and was returned to draft. Review the findings.")
    try:
        db.execute(update(m.TimetableVersion).where(m.TimetableVersion.term_id == v.term_id, m.TimetableVersion.status == "published")
                   .values(status="archived"))
        res = db.execute(update(m.TimetableVersion)
                         .where(m.TimetableVersion.id == id_, m.TimetableVersion.status == "validated",
                                m.TimetableVersion.row_version == body.row_version, m.TimetableVersion.is_sandbox.is_(False))
                         .values(status="published", published_at=m.utcnow(), published_by=user.id, row_version=body.row_version + 1))
        if res.rowcount != 1:
            db.rollback()
            raise HTTPException(409, "This timetable changed while you were publishing. Reload and try again.")
        audit(db, user, "version.publish", "timetable_versions", id_, {"term_id": v.term_id}, request)
        people = {s["faculty_id"] for s in svc.session_rows(db, id_)}
        groups = {g.id for g in db.scalars(select(m.StudentGroup).where(m.StudentGroup.term_id == v.term_id))}
        for u in db.scalars(select(m.AppUser).where(m.AppUser.active.is_(True))):
            if u.faculty_id in people or u.student_group_id in groups:
                db.add(m.Notification(user_id=u.id, message=f"A new timetable has been published: {v.label}."))
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(409, "Another version was published at the same time. Reload and try again.")
    db.refresh(v)
    return version_payload(db, v)


@router.post("/versions/{id_}/archive")
def archive_version(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    if v.is_sandbox or v.status == "archived":
        raise HTTPException(409, "This version cannot be archived.")
    v.status = "archived"
    v.row_version += 1
    audit(db, user, "version.archive", "timetable_versions", id_, {}, request)
    db.commit()
    return svc.version_row(v)


@router.post("/versions/{id_}/clone", status_code=201)
def clone_version(id_: int, body: sc.CloneIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    src = get_or_404(db, m.TimetableVersion, id_)
    v = svc.clone_version(db, src, body.label or f"Copy of {src.label}"[:120], user.id, sandbox=body.sandbox)
    svc.refresh_version(db, v)
    audit(db, user, "version.clone", "timetable_versions", v.id, {"from": id_, "sandbox": body.sandbox}, request)
    db.commit()
    return version_payload(db, v)


@router.delete("/versions/{id_}", status_code=204)
def delete_version(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    editable(v)
    db.delete(v)
    audit(db, user, "version.delete", "timetable_versions", id_, {"label": v.label}, request)
    commit(db)
    return Response(status_code=204)


def timetable_csv(sessions: list[dict], name: str) -> Response:
    rows = [[DAY_NAMES[s["day_of_week"]], fmt(s["start_minute"]), fmt(s["end_minute"]), s["subject_code"], s["subject_name"],
             s["session_type"], s["group_name"], s["faculty_name"], s["room_code"]] for s in sessions]
    return csv_response(name, ["Day", "Start", "End", "Subject code", "Subject", "Type", "Group", "Faculty", "Room"], rows)


@router.get("/versions/{id_}/export.csv", dependencies=[Depends(rate_limit("export", 60, 3600))])
def export_version(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    audit(db, user, "version.export", "timetable_versions", id_, {}, request)
    db.commit()
    return timetable_csv(svc.session_rows(db, id_), f"timetable-version-{v.id}.csv")


# ---- sessions (safe manual editor) ------------------------------------------------------------
def _session_ctx(db: Session, id_: int):
    s = get_or_404(db, m.TimetableSession, id_)
    v = db.get(m.TimetableVersion, s.version_id)
    return s, v


def _apply_edit(db: Session, v: m.TimetableVersion, new_slots: list[Slot], touched: Slot | None, expected: int, user, request,
                action: str, mutate) -> dict:
    problem = svc.problem_for_version(db, v)
    before = auditor.audit(problem, svc.version_slots(db, v.id))
    introduced = auditor.new_errors(before, auditor.audit(problem, new_slots))
    if touched is not None:
        introduced = [f for f in introduced if f["rule_key"] != "weekly_requirement" or action == "session.create"]
    if introduced:
        audit(db, user, action, "timetable_versions", v.id, {"rejected": [f["rule_key"] for f in introduced]}, request, outcome="rejected")
        db.commit()
        raise HTTPException(409, {"message": "The change was rejected because it would break mandatory constraints.",
                                  "conflicts": [{"rule_key": f["rule_key"], "message": f["message"]} for f in introduced[:20]]})
    bump(db, v, expected)
    mutate(problem)
    db.flush()
    svc.refresh_version(db, v, problem)
    audit(db, user, action, "timetable_versions", v.id, {}, request)
    db.commit()
    return version_payload(db, v)


@router.patch("/sessions/{id_}")
def move_session(id_: int, body: sc.SessionMove, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    s, v = _session_ctx(db, id_)
    editable(v)
    if s.locked:
        raise HTTPException(409, "This session is locked. Unlock it before moving it.")
    slots = svc.version_slots(db, v.id)
    cur = next(x for x in slots if x.id == id_)
    problem = svc.problem_for_version(db, v)
    start_idx = next((p.idx for p in problem.periods if p.start == body.start_minute), None)
    span = problem.span(start_idx, problem.required(cur.assignment_id)[1] or s.period_count) if start_idx is not None else None
    if span is None:
        raise HTTPException(409, {"message": "The change was rejected because it would break mandatory constraints.",
                                  "conflicts": [{"rule_key": "consecutive_periods",
                                                 "message": "The session does not fit into consecutive periods starting at that time."}]})
    moved = Slot(cur.assignment_id, body.day_of_week, span[0], span[1], body.room_id, False, id_, "manual")

    def mutate(problem):
        s.day_of_week, s.start_minute, s.end_minute, s.room_id, s.source = moved.day, moved.start, moved.end, moved.room_id, "manual"
        s.period_count = len(problem.indices(moved.start, moved.end) or [0])

    return _apply_edit(db, v, [x for x in slots if x.id != id_] + [moved], moved, body.row_version, user, request, "session.move", mutate)


@router.post("/versions/{id_}/sessions", status_code=201)
def create_session(id_: int, body: sc.SessionCreate, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    editable(v)
    problem = svc.problem_for_version(db, v)
    if body.teaching_assignment_id not in problem.assignments:
        raise HTTPException(422, "Unknown teaching assignment for this term.")
    start_idx = next((p.idx for p in problem.periods if p.start == body.start_minute), None)
    span = problem.span(start_idx, problem.required(body.teaching_assignment_id)[1]) if start_idx is not None else None
    if span is None:
        raise HTTPException(409, {"message": "The session does not fit into consecutive periods starting at that time.", "conflicts": []})
    new = Slot(body.teaching_assignment_id, body.day_of_week, span[0], span[1], body.room_id, False, -1, "manual")
    slots = svc.version_slots(db, v.id)

    def mutate(problem):
        db.add(m.TimetableSession(version_id=v.id, teaching_assignment_id=new.assignment_id, room_id=new.room_id, day_of_week=new.day,
                                  start_minute=new.start, end_minute=new.end, period_count=len(problem.indices(new.start, new.end)),
                                  source="manual"))

    return _apply_edit(db, v, slots + [new], new, body.row_version, user, request, "session.create", mutate)


@router.delete("/sessions/{id_}")
def delete_session(id_: int, request: Request, row_version: int = Query(..., ge=1), db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_admin)):
    s, v = _session_ctx(db, id_)
    editable(v)
    if s.locked:
        raise HTTPException(409, "This session is locked. Unlock it before deleting it.")
    bump(db, v, row_version)
    db.delete(s)
    db.flush()
    svc.refresh_version(db, v)  # the draft now misses a required session and cannot be validated until it is re-added
    audit(db, user, "session.delete", "timetable_versions", v.id, {"session_id": id_}, request)
    db.commit()
    return version_payload(db, v)


@router.post("/sessions/{id_}/lock")
def lock_session(id_: int, body: sc.LockIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    s, v = _session_ctx(db, id_)
    editable(v)
    status, validated_at = v.status, v.validated_at
    bump(db, v, body.row_version)
    v.status, v.validated_at = status, validated_at  # locking does not change the schedule, keep validation state
    s.locked = body.locked
    audit(db, user, "session.lock" if body.locked else "session.unlock", "timetable_versions", v.id, {"session_id": id_}, request)
    db.commit()
    return version_payload(db, v)


@router.post("/versions/{id_}/lock-all")
def lock_all(id_: int, body: sc.LockIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_admin)):
    v = get_or_404(db, m.TimetableVersion, id_)
    editable(v)
    status, validated_at = v.status, v.validated_at
    bump(db, v, body.row_version)
    v.status, v.validated_at = status, validated_at
    db.execute(update(m.TimetableSession).where(m.TimetableSession.version_id == id_).values(locked=body.locked))
    audit(db, user, "version.lock_all" if body.locked else "version.unlock_all", "timetable_versions", id_, {}, request)
    db.commit()
    return version_payload(db, v)


@router.get("/sessions/{id_}/options")
def session_options(id_: int, db: Session = Depends(get_db)):
    s, v = _session_ctx(db, id_)
    problem = svc.problem_for_version(db, v)
    slots = svc.version_slots(db, v.id)
    return {"options": svc.slot_options(problem, slots, next(x for x in slots if x.id == id_)),
            "rooms": [{"id": r.id, "code": r.code, "capacity": r.capacity, "room_type": r.room_type} for r in problem.rooms.values()]}


@router.get("/sessions/{id_}/explain")
def session_explain(id_: int, db: Session = Depends(get_db)):
    s, v = _session_ctx(db, id_)
    problem = svc.problem_for_version(db, v)
    slots = svc.version_slots(db, v.id)
    return svc.explain_slot(problem, slots, next(x for x in slots if x.id == id_))


# ---- faculty request inbox --------------------------------------------------------------------
def request_row(db: Session, r: m.FacultyRequest, admin: bool = True) -> dict:
    f = db.get(m.FacultyProfile, r.faculty_id)
    out = {"id": r.id, "term_id": r.term_id, "faculty_id": r.faculty_id, "faculty_name": f.name if f else None,
           "request_type": r.request_type, "reason": r.reason, "session_id": r.session_id, "session_label": r.session_label,
           "leave_from": r.leave_from, "leave_to": r.leave_to, "day_of_week": r.day_of_week,
           "proposed_day_of_week": r.proposed_day_of_week, "proposed_start_minute": r.proposed_start_minute, "status": r.status,
           "decision_note": r.decision_note, "created_at": r.created_at.isoformat() + "Z",
           "reviewed_at": r.reviewed_at and r.reviewed_at.isoformat() + "Z"}
    return out


@router.get("/requests")
def list_requests(status: str | None = Query(None, pattern="^(pending|approved|rejected|cancelled)$"), db: Session = Depends(get_db)):
    q = select(m.FacultyRequest).order_by(m.FacultyRequest.id.desc()).limit(500)
    if status:
        q = q.where(m.FacultyRequest.status == status)
    return [request_row(db, r) for r in db.scalars(q)]


@router.post("/requests/{id_}/decision")
def decide_request(id_: int, body: sc.DecisionIn, request: Request, db: Session = Depends(get_db),
                   user: CurrentUser = Depends(require_admin)):
    """Approval records a decision and (optionally) a controlled input change. It never edits a
    published timetable: the coordinator then runs a repair, validates and publishes a new version."""
    r = get_or_404(db, m.FacultyRequest, id_)
    if r.status != "pending":
        raise HTTPException(409, "This request has already been decided.")
    r.status, r.decision_note, r.reviewer_user_id, r.reviewed_at = body.decision, body.note, user.id, m.utcnow()
    affected: list[dict] = []
    pub = db.scalars(select(m.TimetableVersion).where(m.TimetableVersion.term_id == r.term_id, m.TimetableVersion.status == "published")).first()
    if body.decision == "approved":
        if pub:
            affected = [s for s in svc.session_rows(db, pub.id) if s["faculty_id"] == r.faculty_id and (
                (r.request_type == "leave" and s["day_of_week"] == r.day_of_week) or (r.request_type == "time_change" and s["id"] == r.session_id))]
        if r.request_type == "leave" and body.apply_unavailability:
            db.add(m.FacultyAvailability(faculty_id=r.faculty_id, term_id=r.term_id, day_of_week=r.day_of_week, start_minute=0,
                                         end_minute=1440, kind="unavailable", note=f"Approved leave request #{r.id}"))
    db.add(m.Notification(user_id=r.requester_user_id,
                          message=f"Your {r.request_type.replace('_', ' ')} request #{r.id} was {body.decision}."
                                  + (f" Note: {body.note[:200]}" if body.note else "")))
    audit(db, user, f"request.{body.decision}", "faculty_requests", id_, {"apply_unavailability": body.apply_unavailability}, request)
    db.commit()
    return {"request": request_row(db, r), "published_version_id": pub.id if pub else None, "affected_sessions": affected,
            "next_step": "Run a repair from the published version to produce a new draft, validate it and publish it."
            if affected else "No published sessions are affected."}


# ---- faculty preferences (read-only for the coordinator) ---------------------------------------
@router.get("/faculty-preferences")
def faculty_preferences(term_id: int, db: Session = Depends(get_db)):
    """What faculty submitted under "My preferences". Only the faculty member can change them."""
    get_or_404(db, m.AcademicTerm, term_id)
    rs = svc.default_rule_set(db)
    db.commit()
    problem = load_problem(db, term_id, rs.id)
    pub = db.scalars(select(m.TimetableVersion).where(m.TimetableVersion.term_id == term_id, m.TimetableVersion.status == "published")).first()
    slots = svc.version_slots(db, pub.id) if pub else None
    out = []
    for p in db.scalars(select(m.FacultyPreference).where(m.FacultyPreference.term_id == term_id, m.FacultyPreference.submitted_at.is_not(None))):
        f = db.get(m.FacultyProfile, p.faculty_id)
        marks = p.slot_marks or {}
        out.append({"faculty_id": p.faculty_id, "faculty_name": f.name if f else None,
                    "preferred_slots": sum(v == "prefer" for v in marks.values()), "avoid_slots": sum(v == "avoid" for v in marks.values()),
                    "marks": marks, "avoid_first": p.avoid_first, "avoid_last": p.avoid_last, "spread_evenly": p.spread_evenly,
                    "strength": p.strength, "notes": p.notes, "submitted_at": p.submitted_at.isoformat() + "Z",
                    "report": svc.preference_report(problem, slots, p.faculty_id) if slots is not None else None})
    return sorted(out, key=lambda r: r["faculty_name"] or "")


# ---- audit and dashboard ----------------------------------------------------------------------
@router.get("/audit")
def audit_log(limit: int = Query(200, ge=1, le=1000), action: str | None = Query(None, max_length=60), db: Session = Depends(get_db)):
    q = select(m.AuditEvent).order_by(m.AuditEvent.id.desc()).limit(limit)
    if action:
        q = q.where(m.AuditEvent.action.like(action.replace("%", "").replace("_", r"\_") + "%", escape="\\"))
    return [row(e) for e in db.scalars(q)]


@router.get("/dashboard")
def dashboard(term_id: int, db: Session = Depends(get_db)):
    get_or_404(db, m.AcademicTerm, term_id)
    rs = svc.default_rule_set(db)
    db.commit()
    problem = load_problem(db, term_id, rs.id)
    pf = preflight(problem)
    versions = list(db.scalars(select(m.TimetableVersion).where(m.TimetableVersion.term_id == term_id, m.TimetableVersion.is_sandbox.is_(False))))
    pub = next((v for v in versions if v.status == "published"), None)
    an = svc.analytics(problem, svc.version_slots(db, pub.id)) if pub else None
    conflicts = db.scalar(select(func.count()).select_from(m.ConflictFinding).join(
        m.TimetableVersion, m.ConflictFinding.version_id == m.TimetableVersion.id).where(
        m.TimetableVersion.term_id == term_id, m.TimetableVersion.status.in_(("draft", "validated")),
        m.ConflictFinding.severity == "error", m.ConflictFinding.run_id.is_(None))) or 0
    runs = [svc.run_row(db, r) for r in db.scalars(select(m.TimetableRun).where(m.TimetableRun.term_id == term_id)
                                                   .order_by(m.TimetableRun.id.desc()).limit(5))]
    return {
        "published": svc.version_row(pub) if pub else None,
        "counts": {"drafts": sum(v.status == "draft" for v in versions), "validated": sum(v.status == "validated" for v in versions),
                   "archived": sum(v.status == "archived" for v in versions), "sessions_required": len(problem.needs),
                   "groups": len(problem.groups), "subjects": len(problem.subjects), "rooms": len(problem.rooms),
                   "faculty": len({a.faculty_id for a in problem.assignments.values()})},
        "pending_requests": db.scalar(select(func.count()).select_from(m.FacultyRequest).where(
            m.FacultyRequest.term_id == term_id, m.FacultyRequest.status == "pending")) or 0,
        "unresolved_conflicts": conflicts, "data_quality": pf, "recent_runs": runs, "analytics": an,
    }
