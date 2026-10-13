"""Independent hard-constraint auditor.

It re-checks persisted sessions from scratch using minutes and plain Python - it shares no
variables or model state with the CP-SAT engine, so it can catch a wrong solver model, a bad
manual edit, or master data that changed after a timetable was generated.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Iterable

from .problem import DAY_NAMES, Problem, Slot, fmt


def finding(rule_key: str, message: str, *, severity: str = "error", category: str = "audit",
            entities: dict | None = None, evidence: dict | None = None, suggestions: list[str] | None = None) -> dict:
    return {"rule_key": rule_key, "severity": severity, "category": category, "message": message,
            "entities": entities or {}, "evidence": evidence or {}, "suggestions": suggestions or []}


def _ctx(problem: Problem, s: Slot):
    a = problem.assignments.get(s.assignment_id)
    sub = problem.subjects.get(a.subject_id) if a else None
    return a, sub


def slot_issues(problem: Problem, s: Slot, others: Iterable[Slot], category: str = "audit") -> list[dict]:
    """Every hard-constraint problem of one placement, checked against `others`."""
    out: list[dict] = []
    a, sub = _ctx(problem, s)
    label = problem.slot_label(s)
    base = {"session_ids": [s.id], "day": s.day, "start_minute": s.start, "end_minute": s.end, "room_id": s.room_id}

    def add(key, msg, **kw):
        ent = dict(base)
        ent.update(kw.pop("entities", {}))
        out.append(finding(key, msg, category=category, entities=ent, **kw))

    if a is None or sub is None:
        add("valid_assignment", f"Session {label} refers to a teaching assignment that is missing or inactive.")
        return out
    base.update(faculty_id=a.faculty_id, subject_id=sub.id, group_id=sub.group_id, assignment_id=a.id)
    if s.day not in problem.days:
        add("within_hours", f"{label}: {DAY_NAMES[s.day]} is not a working day.")
    idxs = problem.indices(s.start, s.end)
    need_len = sub.req[a.session_type]["periods"]
    if idxs is None:
        add("within_hours", f"{label}: {fmt(s.start)}-{fmt(s.end)} does not line up with consecutive configured periods.")
    elif len(idxs) != need_len:
        add("consecutive_periods", f"{label}: occupies {len(idxs)} period(s) but a {a.session_type} session of {sub.code} "
                                   f"requires {need_len} consecutive period(s).", evidence={"expected": need_len, "actual": len(idxs)})
    brk = problem.break_hit(s.day, s.start, s.end)
    if brk:
        add("break_overlap", f"{label}: overlaps the scheduled break '{brk}'.")
    fi = problem.faculty_issue(a.faculty_id, s.day, s.start, s.end)
    if fi:
        add("faculty_availability", f"{label}: {fi}.", suggestions=["Extend the faculty member's availability or pick another slot."])
    for key, msg in problem.room_issues(sub.group_id, sub.req[a.session_type]["room_type"], sub.req[a.session_type]["features"], s.room_id):
        add(key, f"{label}: {msg}.")
    if idxs and problem.blocked_slot(s.day, idxs):
        add("no_teaching_slot", f"{label}: uses a period blocked by the rule 'Blocked teaching slot'.")
    for o in others:
        if o is s or o.day != s.day or not (o.start < s.end and s.start < o.end):
            continue
        oa, osub = _ctx(problem, o)
        if oa is None or osub is None:
            continue
        ent = {"session_ids": [s.id, o.id]}
        ol = problem.slot_label(o)
        if oa.faculty_id == a.faculty_id:
            f = problem.faculty.get(a.faculty_id)
            add("faculty_overlap", f"{f.name if f else 'Faculty'} is double-booked: {label} overlaps {ol}.", entities=ent)
        if o.room_id == s.room_id:
            r = problem.rooms.get(s.room_id)
            add("room_overlap", f"Room {r.code if r else s.room_id} is double-booked: {label} overlaps {ol}.", entities=ent)
        if problem.related(sub.group_id, osub.group_id):
            g1, g2 = problem.groups[sub.group_id], problem.groups[osub.group_id]
            who = g1.name if g1.id == g2.id else f"{g1.name} and {g2.name} share students"
            add("group_overlap", f"Student overlap ({who}): {label} overlaps {ol}.", entities=ent)
    return out


def _runs(problem: Problem, busy: set[int], size: int) -> int:
    """Number of time-contiguous windows of `size` periods that are fully busy."""
    return sum(1 for q in range(len(problem.periods)) if problem.span(q, size) and all(q + i in busy for i in range(size)))


def audit(problem: Problem, slots: list[Slot], check_requirements: bool = True) -> list[dict]:
    out: list[dict] = []
    for i, s in enumerate(slots):
        out.extend(slot_issues(problem, s, slots[i + 1:]))

    by_assign: dict[int, list[Slot]] = defaultdict(list)
    for s in slots:
        by_assign[s.assignment_id].append(s)

    if check_requirements:
        for iss in problem.issues:
            out.append(finding(iss["rule_key"], iss["message"], entities=iss.get("entities"), suggestions=iss.get("suggestions")))
        for a in problem.assignments.values():
            want, _ = problem.required(a.id)
            got = len(by_assign.get(a.id, []))
            if got != want:
                sub = problem.subjects[a.subject_id]
                g = problem.groups[sub.group_id]
                word = "missing" if got < want else "surplus"
                out.append(finding("weekly_requirement",
                                   f"{sub.code} {a.session_type} for {g.name}: {want} session(s) per week required, {got} scheduled "
                                   f"({abs(want - got)} {word}).",
                                   entities={"subject_id": sub.id, "group_id": g.id, "faculty_id": a.faculty_id, "assignment_id": a.id,
                                             "session_ids": [s.id for s in by_assign.get(a.id, [])]},
                                   evidence={"expected": want, "actual": got, "unassigned": max(0, want - got)}))
        for fx in problem.fixed:
            if fx.assignment_id is None:
                continue
            ok = any(s.day == fx.day and s.start == fx.start and s.end == fx.end and (fx.room_id is None or s.room_id == fx.room_id)
                     for s in by_assign.get(fx.assignment_id, []))
            if not ok:
                out.append(finding("fixed_commitment",
                                   f"Fixed commitment '{fx.title}' ({DAY_NAMES[fx.day]} {fmt(fx.start)}-{fmt(fx.end)}) is not honoured.",
                                   entities={"fixed_commitment_id": fx.id, "assignment_id": fx.assignment_id, "faculty_id": fx.faculty_id,
                                             "session_ids": [s.id for s in by_assign.get(fx.assignment_id, [])]}))

    # workload and configurable hard rules
    fac_busy: dict[tuple[int, int], set[int]] = defaultdict(set)
    grp_busy: dict[tuple[int, int], set[int]] = defaultdict(set)
    fac_sess: dict[int, list] = defaultdict(list)
    for s in slots:
        a, sub = _ctx(problem, s)
        idxs = problem.indices(s.start, s.end)
        if a is None or sub is None or idxs is None:
            continue
        fac_busy[(a.faculty_id, s.day)].update(idxs)
        grp_busy[(sub.group_id, s.day)].update(idxs)
        fac_sess[a.faculty_id].append(s.id)
    week: dict[int, int] = defaultdict(int)
    for (fid, day), busy in fac_busy.items():
        f = problem.faculty.get(fid)
        week[fid] += len(busy)
        if f and f.max_day and len(busy) > f.max_day:
            out.append(finding("faculty_workload", f"{f.name} teaches {len(busy)} periods on {DAY_NAMES[day]}; the daily limit is {f.max_day}.",
                               entities={"faculty_id": fid, "day": day, "session_ids": fac_sess[fid]},
                               evidence={"expected": f.max_day, "actual": len(busy)}))
        r = problem.rule("max_faculty_consecutive")
        if f and r and r.is_hard and _runs(problem, busy, r.params["max"] + 1):
            out.append(finding("max_faculty_consecutive",
                               f"{f.name} teaches more than {r.params['max']} consecutive periods on {DAY_NAMES[day]}.",
                               entities={"faculty_id": fid, "day": day, "session_ids": fac_sess[fid]}))
    for fid, total in week.items():
        f = problem.faculty.get(fid)
        if f and f.max_week and total > f.max_week:
            out.append(finding("faculty_workload", f"{f.name} teaches {total} periods per week; the weekly limit is {f.max_week}.",
                               entities={"faculty_id": fid, "session_ids": fac_sess[fid]}, evidence={"expected": f.max_week, "actual": total}))
    r = problem.rule("max_group_periods_per_day")
    if r and r.is_hard:
        for g in problem.leaf_groups():
            chain = [g] + problem.ancestors(g)
            for d in problem.days:
                n = sum(len(grp_busy.get((c, d), ())) for c in chain)
                if n > r.params["max"]:
                    out.append(finding("max_group_periods_per_day",
                                       f"{problem.groups[g].name} has {n} periods on {DAY_NAMES[d]}; the limit is {r.params['max']}.",
                                       entities={"group_id": g, "day": d}, evidence={"expected": r.params["max"], "actual": n}))
    r = problem.rule("spread_subject_days")
    if r and r.is_hard:
        for aid, lst in by_assign.items():
            days = [s.day for s in lst]
            if len(days) != len(set(days)) and aid in problem.assignments:
                sub = problem.subjects[problem.assignments[aid].subject_id]
                out.append(finding("spread_subject_days", f"{sub.code} has more than one session on the same day.",
                                   entities={"subject_id": sub.id, "session_ids": [s.id for s in lst]}))
    return out


def errors(findings: list[dict]) -> list[dict]:
    return [f for f in findings if f["severity"] == "error"]


def new_errors(before: list[dict], after: list[dict]) -> list[dict]:
    """Hard violations present after an edit that were not there before."""
    seen = {(f["rule_key"], f["message"]) for f in errors(before)}
    return [f for f in errors(after) if (f["rule_key"], f["message"]) not in seen]
