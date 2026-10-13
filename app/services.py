"""Timetable services: run execution (background worker), persistence, validation, analytics and
explanations. Routers stay thin and call into this module."""
from __future__ import annotations

import copy
import logging
import threading
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from . import models as m
from .config import settings
from .db import SessionLocal
from .solver import auditor
from .solver.engine import SolveControl, solve
from .solver.preflight import diagnose, preflight
from .solver.problem import DAY_NAMES, Problem, Slot, apply_scenario, fmt, load_problem, match_slots
from .solver.rules import CORE_HARD, default_rules
from .solver.score import placement_costs, score

log = logging.getLogger("timetable")

SYNC_RUNS = False  # tests flip this to run jobs inline
_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="solver")
_controls: dict[int, SolveControl] = {}
_lock = threading.Lock()


# ---- helpers ----------------------------------------------------------------------------------
def default_rule_set(db: Session) -> m.RuleSet:
    rs = db.scalars(select(m.RuleSet).where(m.RuleSet.active.is_(True)).order_by(m.RuleSet.id)).first()
    if rs is None:
        rs = m.RuleSet(name="Default rules")
        db.add(rs)
        db.flush()
        for r in default_rules():
            db.add(m.RuleDefinition(rule_set_id=rs.id, **r))
        db.flush()
    return rs


def version_slots(db: Session, version_id: int) -> list[Slot]:
    return [Slot(s.teaching_assignment_id, s.day_of_week, s.start_minute, s.end_minute, s.room_id, s.locked, s.id, s.source)
            for s in db.scalars(select(m.TimetableSession).where(m.TimetableSession.version_id == version_id)
                                .order_by(m.TimetableSession.day_of_week, m.TimetableSession.start_minute, m.TimetableSession.id))]


def problem_for_version(db: Session, v: m.TimetableVersion) -> Problem:
    run = db.get(m.TimetableRun, v.run_id) if v.run_id else None
    rs_id = run.rule_set_id if run else default_rule_set(db).id
    p = load_problem(db, v.term_id, rs_id)
    if v.is_sandbox and v.scenario:
        apply_scenario(p, v.scenario)
    return p


def save_findings(db: Session, findings: list[dict], run_id: int | None = None, version_id: int | None = None) -> None:
    for f in findings:
        db.add(m.ConflictFinding(run_id=run_id, version_id=version_id, **f))


def finding_rows(db: Session, **where) -> list[dict]:
    q = select(m.ConflictFinding)
    for k, v in where.items():
        q = q.where(getattr(m.ConflictFinding, k) == v)
    order = {"error": 0, "warning": 1, "info": 2}
    rows = [{"id": f.id, "rule_key": f.rule_key, "severity": f.severity, "category": f.category, "message": f.message,
             "entities": f.entities, "evidence": f.evidence, "suggestions": f.suggestions} for f in db.scalars(q)]
    return sorted(rows, key=lambda r: (order.get(r["severity"], 3), r["id"]))


def refresh_version(db: Session, v: m.TimetableVersion, problem: Problem | None = None) -> list[dict]:
    """Re-audit persisted sessions independently and store the findings + soft score."""
    problem = problem or problem_for_version(db, v)
    slots = version_slots(db, v.id)
    findings = auditor.audit(problem, slots)
    base = version_slots(db, v.base_version_id) if v.base_version_id and v.summary.get("kind") in ("repair", "whatif") else None
    sc = score(problem, slots, base)
    v.score, v.score_breakdown = sc["total"], sc
    db.execute(delete(m.ConflictFinding).where(m.ConflictFinding.version_id == v.id, m.ConflictFinding.run_id.is_(None)))
    save_findings(db, findings, version_id=v.id)
    return findings


def session_rows(db: Session, version_id: int) -> list[dict]:
    q = (select(m.TimetableSession, m.TeachingAssignment, m.Subject, m.StudentGroup, m.FacultyProfile, m.Room)
         .join(m.TeachingAssignment, m.TimetableSession.teaching_assignment_id == m.TeachingAssignment.id)
         .join(m.Subject, m.TeachingAssignment.subject_id == m.Subject.id)
         .join(m.StudentGroup, m.Subject.student_group_id == m.StudentGroup.id)
         .join(m.FacultyProfile, m.TeachingAssignment.faculty_id == m.FacultyProfile.id)
         .join(m.Room, m.TimetableSession.room_id == m.Room.id)
         .where(m.TimetableSession.version_id == version_id)
         .order_by(m.TimetableSession.day_of_week, m.TimetableSession.start_minute))
    return [{"id": s.id, "assignment_id": a.id, "subject_id": sub.id, "subject_code": sub.code, "subject_name": sub.name,
             "session_type": a.session_type, "group_id": g.id, "group_name": g.name, "programme_id": g.programme_id,
             "semester": g.semester, "faculty_id": f.id, "faculty_name": f.name, "faculty_code": f.code, "room_id": r.id,
             "room_code": r.code, "room_type": r.room_type, "day_of_week": s.day_of_week, "start_minute": s.start_minute,
             "end_minute": s.end_minute, "period_count": s.period_count, "locked": s.locked, "source": s.source}
            for s, a, sub, g, f, r in db.execute(q)]


def term_calendar(db: Session, term_id: int) -> dict:
    return {
        "days": sorted(d.day_of_week for d in db.scalars(select(m.WorkingDay).where(m.WorkingDay.term_id == term_id, m.WorkingDay.active.is_(True)))),
        "periods": [{"idx": i, "start_minute": p.start_minute, "end_minute": p.end_minute, "label": p.label}
                    for i, p in enumerate(sorted(db.scalars(select(m.PeriodSlot).where(m.PeriodSlot.term_id == term_id)), key=lambda p: p.start_minute))],
        "breaks": [{"name": b.name, "day_of_week": b.day_of_week, "start_minute": b.start_minute, "end_minute": b.end_minute}
                   for b in db.scalars(select(m.ScheduledBreak).where(m.ScheduledBreak.term_id == term_id))],
    }


def version_row(v: m.TimetableVersion) -> dict:
    return {"id": v.id, "term_id": v.term_id, "run_id": v.run_id, "base_version_id": v.base_version_id, "label": v.label,
            "status": v.status, "is_sandbox": v.is_sandbox, "scenario": v.scenario, "row_version": v.row_version, "score": v.score,
            "score_breakdown": v.score_breakdown, "summary": v.summary, "created_at": v.created_at.isoformat() + "Z",
            "validated_at": v.validated_at and v.validated_at.isoformat() + "Z",
            "published_at": v.published_at and v.published_at.isoformat() + "Z"}


def run_row(db: Session, r: m.TimetableRun, full: bool = False) -> dict:
    end = r.finished_at or m.utcnow()
    out = {"id": r.id, "term_id": r.term_id, "rule_set_id": r.rule_set_id, "rule_set_version": r.rule_set_version, "kind": r.kind,
           "status": r.status, "solver_status": r.solver_status, "input_hash": r.input_hash, "params": r.params, "summary": r.summary,
           "created_at": r.created_at.isoformat() + "Z", "started_at": r.started_at and r.started_at.isoformat() + "Z",
           "finished_at": r.finished_at and r.finished_at.isoformat() + "Z",
           "elapsed_seconds": round((end - r.started_at).total_seconds(), 1) if r.started_at else 0}
    if full:
        out["findings"] = finding_rows(db, run_id=r.id)
        out["versions"] = [version_row(v) for v in db.scalars(select(m.TimetableVersion).where(m.TimetableVersion.run_id == r.id)
                                                              .order_by(m.TimetableVersion.id))]
    return out


def _add_version(db: Session, run: m.TimetableRun | None, term_id: int, label: str, slots: list[Slot], *, sandbox=False,
                 scenario=None, base_version_id=None, summary=None, user_id=None) -> m.TimetableVersion:
    v = m.TimetableVersion(term_id=term_id, run_id=run.id if run else None, label=label, is_sandbox=sandbox, scenario=scenario or [],
                           base_version_id=base_version_id, summary=summary or {}, created_by=user_id)
    db.add(v)
    db.flush()
    for s in slots:
        db.add(m.TimetableSession(version_id=v.id, teaching_assignment_id=s.assignment_id, room_id=s.room_id, day_of_week=s.day,
                                  start_minute=s.start, end_minute=s.end, period_count=max(1, round((s.end - s.start) / 60)),
                                  locked=s.locked, source=s.source))
    db.flush()
    return v


def clone_version(db: Session, src: m.TimetableVersion, label: str, user_id: int, sandbox: bool = False) -> m.TimetableVersion:
    """Restore/revert and sandbox duplication both create a NEW draft; history is never rewritten."""
    v = _add_version(db, None, src.term_id, label, version_slots(db, src.id), sandbox=sandbox, base_version_id=src.id,
                     summary={"kind": "clone", "cloned_from": src.id}, user_id=user_id)
    v.run_id = src.run_id
    if sandbox:
        v.scenario = list(src.scenario or [])
    return v


def fix_period_counts(db: Session, version_id: int, problem: Problem) -> None:
    for s in db.scalars(select(m.TimetableSession).where(m.TimetableSession.version_id == version_id)):
        idxs = problem.indices(s.start_minute, s.end_minute)
        if idxs:
            s.period_count = len(idxs)


def describe(problem: Problem, s: Slot | None) -> str:
    if s is None:
        return "—"
    r = problem.rooms.get(s.room_id)
    return f"{DAY_NAMES[s.day][:3]} {fmt(s.start)}-{fmt(s.end)} · {r.code if r else '?'}"


def diff_slots(problem: Problem, before: list[Slot], after: list[Slot]) -> list[dict]:
    bmap, bleft = match_slots(problem, copy.deepcopy(before))
    amap, _ = match_slots(problem, copy.deepcopy(after))
    # pair identical placements first so only true moves are reported
    out = []
    needs = {n.key: n for n in problem.needs}
    by_a: dict[int, tuple[list, list]] = defaultdict(lambda: ([], []))
    for k, s in bmap.items():
        by_a[needs[k].assignment_id][0].append(s)
    for k, s in amap.items():
        by_a[needs[k].assignment_id][1].append(s)
    sig = lambda s: (s.day, s.start, s.end, s.room_id)  # noqa: E731
    for aid, (old, new) in by_a.items():
        for s in list(new):
            hit = next((o for o in old if sig(o) == sig(s)), None)
            if hit:
                old.remove(hit)
                new.remove(s)
        a = problem.assignments[aid]
        sub = problem.subjects[a.subject_id]
        label = f"{sub.code} {a.session_type} ({problem.groups[sub.group_id].name})"
        for i in range(max(len(old), len(new))):
            o, n = (old[i] if i < len(old) else None), (new[i] if i < len(new) else None)
            kind = "added" if o is None else "removed" if n is None else ("room" if (o.day, o.start) == (n.day, n.start) else "moved")
            out.append({"session": label, "change": kind, "before": describe(problem, o), "after": describe(problem, n),
                        "faculty_id": a.faculty_id, "group_id": sub.group_id})
    for s in bleft:
        out.append({"session": problem.slot_label(s), "change": "removed", "before": describe(problem, s), "after": "—"})
    return out


# ---- run execution ----------------------------------------------------------------------------
def queued_count(db: Session) -> int:
    return db.scalar(select(func.count()).select_from(m.TimetableRun).where(m.TimetableRun.status.in_(("queued", "running")))) or 0


def submit_run(run_id: int) -> None:
    control = SolveControl()
    with _lock:
        _controls[run_id] = control
    if SYNC_RUNS:
        execute_run(run_id, control)
    else:
        _executor.submit(execute_run, run_id, control)


def cancel_run(run_id: int) -> bool:
    with _lock:
        c = _controls.get(run_id)
    if c:
        c.cancel()
    return c is not None


def recover_stale_runs() -> None:
    """Runs left queued/running by a previous process can never finish - mark them failed."""
    with SessionLocal() as db:
        for r in db.scalars(select(m.TimetableRun).where(m.TimetableRun.status.in_(("queued", "running")))):
            r.status, r.finished_at = "failed", m.utcnow()
            r.summary = {**(r.summary or {}), "message": "The server restarted before this run finished."}
        db.commit()


def execute_run(run_id: int, control: SolveControl | None = None) -> None:
    control = control or SolveControl()
    with SessionLocal() as db:
        run = db.get(m.TimetableRun, run_id)
        if run is None or run.status != "queued":
            return
        run.status, run.started_at = "running", m.utcnow()
        db.commit()
        try:
            _execute(db, run, control)
            db.commit()
        except Exception:  # noqa: BLE001 - never leak internals to the client; details go to the server log
            log.exception("run %s failed", run_id)
            db.rollback()
            run = db.get(m.TimetableRun, run_id)
            run.status, run.solver_status = "failed", run.solver_status or "ERROR"
            run.summary = {"message": "The generation job failed with an internal error. See the server log."}
        finally:
            run.finished_at = m.utcnow()
            db.commit()
            with _lock:
                _controls.pop(run_id, None)


def _execute(db: Session, run: m.TimetableRun, control: SolveControl) -> None:
    params = run.params or {}
    limit = float(min(settings.solver_max_seconds, max(1, params.get("time_limit", 20))))
    problem = load_problem(db, run.term_id, run.rule_set_id)
    notes = apply_scenario(problem, params.get("scenario") or [])
    run.input_hash = problem.snapshot_hash()
    base_v = db.get(m.TimetableVersion, params["base_version_id"]) if params.get("base_version_id") else None
    base_slots = version_slots(db, base_v.id) if base_v else []
    locked_slots = [s for s in base_slots if s.locked]
    repairing = run.kind in ("repair", "whatif")
    summary: dict = {"kind": run.kind, "scenario_notes": notes, "sessions_required": len(problem.needs), "time_limit": limit}
    if repairing and base_slots:
        summary["affected_before_repair"] = [f["message"] for f in auditor.errors(auditor.audit(problem, base_slots))][:100]

    pf = preflight(problem, locked_slots)
    save_findings(db, pf, run_id=run.id)
    if auditor.errors(pf):
        run.status, run.solver_status = "infeasible", "PREFLIGHT_FAILED"
        summary["message"] = "Input validation found blocking problems; the solver was not started."
        run.summary = summary
        return

    locked_map, _ = match_slots(problem, copy.deepcopy(locked_slots))
    base_map = match_slots(problem, copy.deepcopy(base_slots))[0] if repairing else {}
    kw = dict(locked=locked_map, base=base_map, workers=settings.solver_workers, max_vars=settings.solver_max_vars)
    res = solve(problem, time_limit=limit, control=control, **kw)
    run.solver_status = res.status
    summary["solve_seconds"] = round(res.wall, 2)

    if not res.ok:
        if control.cancelled:
            run.status = "cancelled"
            summary["message"] = "The run was cancelled before a result was found."
        elif res.status == "INFEASIBLE":
            run.status = "infeasible"
            summary["message"] = "The solver proved that no timetable satisfies all mandatory constraints."
            needs = {n.key: n for n in problem.needs}
            for key in res.empty_domains:
                save_findings(db, [auditor.finding("weekly_requirement", f"{problem.need_label(needs[key])} has no eligible "
                                                   "day/time/room combination at all.", category="proven",
                                                   entities={"assignment_id": needs[key].assignment_id, "subject_id": needs[key].subject_id})],
                              run_id=run.id)
            save_findings(db, [auditor.finding("infeasible", "Proven infeasible: the mandatory constraints cannot all be satisfied "
                                               "with the current inputs." + (f" {res.note}" if res.note else ""), category="proven")], run_id=run.id)
            save_findings(db, diagnose(problem, locked_map, workers=settings.solver_workers, control=control), run_id=run.id)
        elif res.status == "MODEL_INVALID":
            run.status = "failed"
            summary["message"] = res.note or "The solver rejected the model (technical problem, not a scheduling conflict)."
            save_findings(db, [auditor.finding("model_invalid", summary["message"], category="technical")], run_id=run.id)
        else:
            run.status = "unknown"
            summary["message"] = (f"No conclusive result within {limit:.0f} s. This is NOT a proof that the timetable is impossible - "
                                  "try a longer time limit or simplify the inputs.")
            save_findings(db, [auditor.finding("solver_timeout", summary["message"], severity="warning", category="timeout")], run_id=run.id)
        run.summary = summary
        return

    results = [res]
    movable = len(problem.needs) - len(locked_map)
    want = int(min(3, max(1, params.get("candidates", 1)))) if run.kind == "generate" else 1
    distinct = max(1, -(-movable * 15 // 100))
    while len(results) < want and movable > 0 and not control.cancelled:
        alt = solve(problem, time_limit=min(limit, 15.0), avoid=[r.placements for r in results], min_distinct=distinct,
                    control=control, seed=len(results) + 1, **kw)
        if not alt.ok:
            break
        results.append(alt)

    sandbox = bool(params.get("sandbox")) or run.kind == "whatif"
    made = []
    for i, r in enumerate(results):
        label = {"generate": f"Run #{run.id} · candidate {i + 1}", "repair": f"Repair #{run.id}", "whatif": f"What-if #{run.id}"}[run.kind]
        vs = {"kind": run.kind, "solver_status": r.status,
              "optimality": ("feasible, optimality not proven" if r.status != "OPTIMAL" else "proven optimal" if i == 0
                             else "best among timetables that differ from the earlier candidates"),
              "scenario_notes": notes}
        if repairing:
            vs["diff"] = diff_slots(problem, base_slots, r.slots)
            vs["moved"] = len(vs["diff"])
        v = _add_version(db, run, run.term_id, label, r.slots, sandbox=sandbox, scenario=params.get("scenario") or [],
                         base_version_id=base_v.id if base_v else None, summary=vs, user_id=run.requested_by)
        fix_period_counts(db, v.id, problem)
        db.flush()
        found = refresh_version(db, v, problem)  # independent re-check of what was actually persisted
        if auditor.errors(found):
            log.error("solver output failed the independent audit: %s", auditor.errors(found)[:3])
            run.status = "failed"
            summary["message"] = "Internal consistency check failed: the generated candidate did not pass the independent audit."
            run.summary = summary
            return
        made.append({"version_id": v.id, "score": v.score, "solver_status": r.status, "moved": vs.get("moved")})
    run.status = "completed"
    summary["candidates"] = made
    summary["message"] = (f"{len(made)} valid candidate timetable(s) generated."
                          + ("" if res.status == "OPTIMAL" else " Optimality was not proven within the time limit."))
    run.summary = summary


# ---- explanations and analytics ---------------------------------------------------------------
def slot_options(problem: Problem, slots: list[Slot], target: Slot) -> list[dict]:
    """For every possible start slot: is it valid for this session, in which rooms, and if not, why not."""
    a = problem.assignments.get(target.assignment_id)
    sub = problem.subjects.get(a.subject_id) if a else None
    if a is None or sub is None:
        return []
    req = sub.req[a.session_type]
    others = [s for s in slots if s.id != target.id]
    before = auditor.audit(problem, slots)
    static_ok = [r.id for r in problem.rooms.values() if not problem.room_issues(sub.group_id, req["room_type"], req["features"], r.id)]
    out = []
    for d in problem.days:
        for q in range(len(problem.periods)):
            sp = problem.span(q, req["periods"])
            if sp is None:
                continue
            probe = Slot(a.id, d, sp[0], sp[1], -1, id=target.id)
            reasons = [f["message"] for f in auditor.slot_issues(problem, probe, others)
                       if f["rule_key"] not in ("room_suitability", "room_capacity", "room_overlap")]
            rooms = []
            if not reasons:
                busy = {o.room_id for o in others if o.day == d and o.start < sp[1] and sp[0] < o.end}
                rooms = [r for r in static_ok if r not in busy]
                if not rooms:
                    reasons.append("No suitable room is free: " + ("every room of the required type/capacity is occupied."
                                                                    if static_ok else "no room meets the type/feature/capacity requirements."))
                else:
                    probe.room_id = rooms[0]
                    extra = auditor.new_errors(before, auditor.audit(problem, others + [probe]))
                    extra = [f for f in extra if f["rule_key"] != "weekly_requirement"]
                    if extra:
                        reasons, rooms = [f["message"] for f in extra], []
            out.append({"day_of_week": d, "start_minute": sp[0], "end_minute": sp[1], "ok": bool(rooms), "room_ids": rooms,
                        "reasons": reasons[:6], "current": (d, sp[0]) == (target.day, target.start)})
    return out


def explain_slot(problem: Problem, slots: list[Slot], target: Slot) -> dict:
    a = problem.assignments.get(target.assignment_id)
    sub = problem.subjects.get(a.subject_id) if a else None
    if a is None or sub is None:
        return {"hard": [], "soft": [], "violations": []}
    g, f, r = problem.groups[sub.group_id], problem.faculty.get(a.faculty_id), problem.rooms.get(target.room_id)
    req = sub.req[a.session_type]
    idxs = problem.indices(target.start, target.end) or []
    issues = auditor.slot_issues(problem, target, [s for s in slots if s.id != target.id])
    bad = {i["rule_key"] for i in issues}
    facts = {
        "within_hours": f"{DAY_NAMES[target.day]} {fmt(target.start)}-{fmt(target.end)} lies on configured periods of a working day.",
        "break_overlap": "It does not touch any scheduled break.",
        "faculty_availability": f"{f.name if f else 'Faculty'} is available in this window.",
        "faculty_overlap": f"{f.name if f else 'Faculty'} teaches nothing else at this time.",
        "room_overlap": f"Room {r.code if r else '?'} is free at this time.",
        "group_overlap": f"{g.name} (and its parent class / batches) has no other session at this time.",
        "room_capacity": f"Room {r.code if r else '?'} seats {r.capacity if r else '?'} ≥ {g.strength} students.",
        "room_suitability": f"Room type '{r.room_type if r else '?'}' matches the requirement '{req['room_type'] or 'any'}'"
                            + (f" with features {', '.join(req['features'])}." if req["features"] else "."),
        "consecutive_periods": f"It occupies {len(idxs)} consecutive period(s); {req['periods']} required.",
    }
    hard = [{"rule_key": k, "ok": k not in bad, "text": t, "description": CORE_HARD[k]} for k, t in facts.items()]
    if target.locked:
        hard.append({"rule_key": "locked_session", "ok": True, "text": "This session is locked and will not move on regeneration.",
                     "description": CORE_HARD["locked_session"]})
    costs = placement_costs(problem, a.faculty_id, sub.group_id, a.session_type, target.day, idxs[0], len(idxs), target.room_id, None) if idxs else {}
    softs = [{"rule_key": k, "raw": v, "weight": problem.rules[k].weight, "penalty": v * problem.rules[k].weight} for k, v in costs.items()]
    return {"hard": hard, "soft": softs, "violations": [i["message"] for i in issues],
            "note": "Soft penalties listed here are the ones caused by this placement alone; day-level rules (gaps, balance, "
                    "spreading) are shown in the version's score breakdown."}


def analytics(problem: Problem, slots: list[Slot]) -> dict:
    heat: dict[tuple[int, int], int] = defaultdict(int)
    room_use: dict[int, int] = defaultdict(int)
    fac_day: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    grp_busy: dict[tuple[int, int], set] = defaultdict(set)
    unused = 0
    for s in slots:
        a = problem.assignments.get(s.assignment_id)
        idxs = problem.indices(s.start, s.end)
        if a is None or idxs is None:
            continue
        sub = problem.subjects[a.subject_id]
        for q in idxs:
            heat[(s.day, q)] += 1
        room_use[s.room_id] += len(idxs)
        fac_day[a.faculty_id][s.day] += len(idxs)
        grp_busy[(sub.group_id, s.day)].update(idxs)
        r, g = problem.rooms.get(s.room_id), problem.groups.get(sub.group_id)
        if r and g:
            unused += max(0, r.capacity - g.strength) * len(idxs)
    total = max(1, len(problem.days) * len(problem.periods))
    free = []
    for g in problem.leaf_groups():
        chain = [g] + problem.ancestors(g)
        busy = sum(len(set().union(*(grp_busy.get((c, d), set()) for c in chain))) for d in problem.days)
        free.append({"group_id": g, "name": problem.groups[g].name, "busy_periods": busy, "free_periods": total - busy})
    return {
        "heatmap": [{"day_of_week": d, "period_idx": q, "sessions": heat.get((d, q), 0)} for d in problem.days for q in range(len(problem.periods))],
        "rooms": sorted(({"room_id": r.id, "code": r.code, "room_type": r.room_type, "capacity": r.capacity, "periods_used": room_use.get(r.id, 0),
                          "utilisation": round(100 * room_use.get(r.id, 0) / total)} for r in problem.rooms.values() if r.active),
                        key=lambda x: -x["utilisation"]),
        "faculty": sorted(({"faculty_id": fid, "name": problem.faculty[fid].name, "total": sum(days.values()),
                            "per_day": {str(d): days.get(d, 0) for d in problem.days}, "max_day": problem.faculty[fid].max_day,
                            "max_week": problem.faculty[fid].max_week} for fid, days in fac_day.items() if fid in problem.faculty),
                          key=lambda x: -x["total"]),
        "groups": free, "unused_seat_periods": unused, "total_slots": total,
    }


# ---- faculty self-service preferences ----------------------------------------------------------
def preference_report(problem: Problem, slots: list[Slot], faculty_id: int) -> dict | None:
    """How well a timetable matches what one faculty member asked for (submitted preferences only)."""
    f = problem.faculty.get(faculty_id)
    pr = f.prefs if f else None
    if not pr:
        return None
    periods = in_pref = in_avoid = first_breach = last_breach = against = 0
    last_idx = len(problem.periods) - 1
    for s in slots:
        a = problem.assignments.get(s.assignment_id)
        if a is None or a.faculty_id != faculty_id:
            continue
        for q in problem.indices(s.start, s.end) or []:
            periods += 1
            mark = pr["marks"].get(f"{s.day}-{problem.periods[q].start}")
            fb, lb = pr["avoid_first"] and q == 0, pr["avoid_last"] and q == last_idx
            in_pref += mark == "prefer"
            in_avoid += mark == "avoid"
            first_breach += fb
            last_breach += lb
            against += mark == "avoid" or fb or lb  # a period that breaks two requests still counts once
    return {"periods": periods, "in_preferred": in_pref, "in_avoided": in_avoid, "first_breach": first_breach, "last_breach": last_breach,
            "honoured_pct": round(100 * (periods - against) / periods) if periods else 100}


def ensure_rule_rows(db: Session) -> None:
    """Rule types added in a later release get their default row in every existing rule set."""
    defaults = {r["rule_key"]: r for r in default_rules()}
    for rs in db.scalars(select(m.RuleSet)):
        have = set(db.scalars(select(m.RuleDefinition.rule_key).where(m.RuleDefinition.rule_set_id == rs.id)))
        for key in defaults.keys() - have:
            db.add(m.RuleDefinition(rule_set_id=rs.id, **defaults[key]))
    db.flush()
