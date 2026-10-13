"""Deterministic input validation before the solver runs, plus infeasibility diagnostics.

Preflight findings are facts computed from the inputs. Diagnostics after a proven-infeasible solve
are clearly labelled: CP-SAT does not hand back "the" cause, so we test constraint families and
compute one maximal partial placement, and say so honestly.
"""
from __future__ import annotations

from collections import defaultdict

from .auditor import audit, finding, slot_issues
from .engine import RELAXABLE, SolveControl, solve
from .problem import DAY_NAMES, Problem, Slot, fmt


def _pf(key, msg, **kw):
    kw.setdefault("category", "preflight")
    return finding(key, msg, **kw)


def preflight(problem: Problem, locked: list[Slot] | None = None) -> list[dict]:
    out: list[dict] = []
    p = problem
    for iss in p.issues:
        out.append(_pf(iss["rule_key"], iss["message"], entities=iss.get("entities"), suggestions=iss.get("suggestions")))
    if not p.days:
        out.append(_pf("within_hours", "No working days are configured for this term.", suggestions=["Add working days."]))
    if not p.periods:
        out.append(_pf("within_hours", "No teaching periods are configured for this term.", suggestions=["Add period slots."]))
    for a, b in zip(p.periods, p.periods[1:]):
        if b.start < a.end:
            out.append(_pf("within_hours", f"Periods {fmt(a.start)}-{fmt(a.end)} and {fmt(b.start)}-{fmt(b.end)} overlap.",
                           suggestions=["Fix the period definitions."]))
    for per in p.periods:
        for name, d, a, b in p.breaks:
            if a < per.end and per.start < b:
                out.append(_pf("break_overlap", f"Period {fmt(per.start)}-{fmt(per.end)} overlaps the break '{name}' "
                                                f"({fmt(a)}-{fmt(b)}{'' if d is None else ' on ' + DAY_NAMES[d]}); it can never be used"
                                                f"{'' if d is None else ' on that day'}.",
                               severity="warning", suggestions=["Adjust the period or the break."]))
    for g in p.groups.values():
        kids = [c for c in p.groups.values() if c.parent_id == g.id and c.group_type == "batch"]
        if kids and sum(c.strength for c in kids) != g.strength:
            out.append(_pf("valid_assignment", f"Batches of {g.name} total {sum(c.strength for c in kids)} students but the class strength is {g.strength}.",
                           severity="warning", entities={"group_id": g.id}))
    if not p.needs and not p.issues:
        out.append(_pf("weekly_requirement", "There are no subject offerings with weekly requirements to schedule.",
                       suggestions=["Add subjects, weekly requirements and teaching assignments."]))
    if not p.days or not p.periods:
        return out

    slots_total = len(p.days) * len(p.periods)
    seen_assign: set[int] = set()
    fac_need: dict[int, int] = defaultdict(int)
    fac_subj: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    grp_need: dict[int, int] = defaultdict(int)
    type_need: dict[str, int] = defaultdict(int)
    for n in p.needs:
        fac_need[n.faculty_id] += n.length
        fac_subj[n.faculty_id][n.subject_id] += n.length
        grp_need[n.group_id] += n.length
        if n.room_type:
            type_need[n.room_type] += n.length
        if n.assignment_id in seen_assign:
            continue
        seen_assign.add(n.assignment_id)
        sub, g = p.subjects[n.subject_id], p.groups[n.group_id]
        ent = {"subject_id": sub.id, "group_id": g.id, "faculty_id": n.faculty_id, "assignment_id": n.assignment_id}
        what = f"{sub.code} {n.session_type} for {g.name}"
        active = [r for r in p.rooms.values() if r.active]
        typed = [r for r in active if not n.room_type or r.room_type == n.room_type]
        feat = [r for r in typed if set(n.features) <= set(r.features)]
        fits = [r for r in feat if r.capacity >= g.strength]
        if not typed:
            out.append(_pf("room_suitability", f"{what} requires a room of type '{n.room_type}', but no active room of that type exists.",
                           entities=ent, evidence={"required_type": n.room_type, "available_types": sorted({r.room_type for r in active})},
                           suggestions=[f"Add a '{n.room_type}' room or change the required room type of {sub.code}."]))
        elif not feat:
            out.append(_pf("room_suitability", f"{what} requires feature(s) {', '.join(n.features)}, but no '{n.room_type or 'any'}' room has all of them.",
                           entities=ent, evidence={"required_features": n.features},
                           suggestions=["Add the feature to a suitable room or relax the subject's required features."]))
        elif not fits:
            best = max(r.capacity for r in feat)
            out.append(_pf("room_capacity", f"{what}: the group has {g.strength} students but the largest suitable room seats {best}.",
                           entities=ent, evidence={"expected": g.strength, "available": best},
                           suggestions=["Add a larger suitable room, or split the group into smaller batches."]))
        if not any(p.span(q, n.length) for q in range(len(p.periods))):
            out.append(_pf("consecutive_periods", f"{what} needs {n.length} consecutive period(s), but the timetable has no such unbroken block.",
                           entities=ent, evidence={"expected": n.length},
                           suggestions=["Add adjacent periods or reduce the periods per session."]))
        elif not p.time_options(n):
            f = p.faculty.get(n.faculty_id)
            out.append(_pf("faculty_availability",
                           f"{what} needs {n.length} consecutive period(s), but {f.name if f else 'the assigned faculty member'} has no "
                           f"available block of that length on any working day.",
                           entities=ent, evidence={"expected": n.length, "available": 0},
                           suggestions=["Extend the faculty member's availability or assign another faculty member."]))

    for fid, need in fac_need.items():
        f = p.faculty.get(fid)
        if f is None:
            continue
        free = sum(1 for d in p.days for per in p.periods
                   if not p.break_hit(d, per.start, per.end) and not p.faculty_issue(fid, d, per.start, per.end))
        subs = fac_subj[fid]
        ent = {"faculty_id": fid, "subject_ids": sorted(subs)}
        if need > free:
            if len(subs) == 1:
                sub = p.subjects[next(iter(subs))]
                msg = (f"Subject {sub.code} requires {need} weekly period(s), but its assigned faculty member {f.name} "
                       f"is available for only {free} period(s).")
            else:
                parts = ", ".join(f"{p.subjects[s].code}: {n}" for s, n in sorted(subs.items()))
                msg = f"{f.name} must teach {need} periods per week ({parts}), but is available for only {free} period(s)."
            out.append(_pf("faculty_availability", msg, entities=ent, evidence={"expected": need, "available": free},
                           suggestions=[f"Increase {f.name}'s availability by at least {need - free} period(s).",
                                        "Reduce the weekly requirement or assign another faculty member."]))
        if f.max_week and need > f.max_week:
            out.append(_pf("faculty_workload", f"{f.name} is assigned {need} periods per week, above the weekly limit of {f.max_week}.",
                           entities=ent, evidence={"expected": need, "available": f.max_week},
                           suggestions=["Raise the weekly limit or reassign some sessions."]))
        if f.max_day and need > f.max_day * len(p.days):
            out.append(_pf("faculty_workload", f"{f.name} is assigned {need} periods per week, but {f.max_day} per day over "
                                               f"{len(p.days)} days allows only {f.max_day * len(p.days)}.",
                           entities=ent, evidence={"expected": need, "available": f.max_day * len(p.days)}))
    for g in p.leaf_groups():
        need = sum(grp_need.get(c, 0) for c in [g] + p.ancestors(g))
        if need > slots_total:
            out.append(_pf("group_overlap", f"{p.groups[g].name} needs {need} periods per week (including whole-class sessions), "
                                            f"but the week only has {slots_total} periods.",
                           entities={"group_id": g}, evidence={"expected": need, "available": slots_total},
                           suggestions=["Reduce weekly requirements or add periods/working days."]))
    for rtype, need in type_need.items():
        cap = slots_total * sum(1 for r in p.rooms.values() if r.active and r.room_type == rtype)
        if cap and need > cap:
            out.append(_pf("room_overlap", f"Sessions needing a '{rtype}' total {need} periods per week, but those rooms offer only {cap}.",
                           evidence={"expected": need, "available": cap}, suggestions=[f"Add another '{rtype}' room."]))

    for fx in p.fixed:
        ent = {"fixed_commitment_id": fx.id, "faculty_id": fx.faculty_id}
        where = f"'{fx.title}' ({DAY_NAMES[fx.day]} {fmt(fx.start)}-{fmt(fx.end)})"
        if fx.assignment_id is None:
            continue
        a = p.assignments.get(fx.assignment_id)
        if a is None:
            out.append(_pf("fixed_commitment", f"Fixed commitment {where} refers to a teaching assignment outside this term.", entities=ent))
            continue
        want, length = p.required(a.id)
        idxs = p.indices(fx.start, fx.end)
        if fx.day not in p.days or idxs is None:
            out.append(_pf("fixed_commitment", f"Fixed commitment {where} is outside the working calendar or not aligned to periods.", entities=ent))
        elif want == 0 or len(idxs) != length:
            out.append(_pf("fixed_commitment", f"Fixed commitment {where} spans {len(idxs)} period(s), but the subject requires "
                                               f"{length} period(s) per session ({want} session(s) per week).", entities=ent))
        else:
            probe = Slot(a.id, fx.day, fx.start, fx.end, fx.room_id if fx.room_id is not None else -1)
            for f in audit(p, [probe], check_requirements=False):
                if fx.room_id is None and f["rule_key"] in ("room_suitability", "room_capacity"):
                    continue
                out.append(_pf("fixed_commitment", f"Fixed commitment {where} is itself invalid: {f['message']}", entities=ent))
    pins = [Slot(fx.assignment_id, fx.day, fx.start, fx.end, fx.room_id if fx.room_id is not None else -fx.id, id=None)
            for fx in p.fixed if fx.assignment_id in p.assignments]
    for f in audit(p, pins, check_requirements=False):
        if f["rule_key"] in ("faculty_overlap", "room_overlap", "group_overlap"):
            out.append(_pf("fixed_commitment", "Two fixed commitments contradict each other: " + f["message"], entities=f["entities"]))
    out.extend(check_locked(p, locked or []))
    return out


def check_locked(problem: Problem, locked: list[Slot]) -> list[dict]:
    """Locked sessions never move. If changed inputs make one invalid we report it instead of moving it."""
    out = []
    for f in audit(problem, locked, check_requirements=False):
        out.append(finding("locked_session", "A locked session is incompatible with the current inputs: " + f["message"],
                           category="preflight", entities=f["entities"], evidence={"violated_rule": f["rule_key"]},
                           suggestions=["Unlock the session so it can be rescheduled, or revert the input change."]))
    count: dict[int, int] = defaultdict(int)
    for s in locked:
        count[s.assignment_id] += 1
    for aid, n in count.items():
        want, _ = problem.required(aid)
        if n > want:
            out.append(finding("locked_session", f"{n} locked session(s) exist for an assignment that now requires only {want} per week.",
                               category="preflight", entities={"assignment_id": aid},
                               suggestions=["Unlock or delete the surplus locked session(s)."]))
    return out


def diagnose(problem: Problem, locked: dict[str, Slot], time_each: float = 3.0, workers: int = 4,
             control: SolveControl | None = None) -> list[dict]:
    """Evidence for a proven-infeasible model. Honest about what is and is not established."""
    out: list[dict] = []
    relevant = [k for k in RELAXABLE if not (k == "locked" and not locked) and not (k == "fixed" and not any(f.assignment_id for f in problem.fixed))]
    culprits = []
    for fam in relevant:
        if control and control.cancelled:
            break
        res = solve(problem, time_limit=time_each, locked=locked, relax=frozenset({fam}), use_objective=False, workers=workers)
        if res.ok:
            culprits.append(fam)
    if culprits:
        for fam in culprits:
            out.append(finding(fam if fam not in ("workload", "fixed", "locked", "custom_rules") else
                               {"workload": "faculty_workload", "fixed": "fixed_commitment", "locked": "locked_session",
                                "custom_rules": "custom_rules"}[fam],
                               f"The timetable becomes feasible if the rule family '{RELAXABLE[fam]}' is ignored, so these constraints "
                               f"are part of the conflict.", category="diagnostic",
                               evidence={"test": "single constraint-family relaxation", "family": fam},
                               suggestions=[{"faculty_overlap": "Assign some sessions to another faculty member.",
                                             "room_overlap": "Add a suitable room/laboratory.",
                                             "group_overlap": "Reduce the weekly load of the affected class or add periods.",
                                             "faculty_availability": "Extend faculty availability.",
                                             "workload": "Raise faculty workload limits.",
                                             "room_capacity": "Add a larger room or split the group.",
                                             "room_suitability": "Add a room of the required type/features.",
                                             "custom_rules": "Disable or loosen an additional hard rule.",
                                             "fixed": "Move or remove a fixed commitment.",
                                             "locked": "Unlock one or more sessions."}[fam]]))
    else:
        out.append(finding("infeasible", "No single constraint family explains the conflict; several constraints combine to make the "
                                         "requirements impossible.", category="diagnostic",
                           evidence={"test": "single constraint-family relaxation", "families_tested": relevant}))
    if control and control.cancelled:
        return out
    part = solve(problem, time_limit=max(time_each, 5.0), locked=locked, optional=True, workers=workers)
    if part.ok and part.unassigned:
        placed = part.slots
        needs = {n.key: n for n in problem.needs}
        for key in part.unassigned:
            n = needs[key]
            sub, g, f = problem.subjects[n.subject_id], problem.groups[n.group_id], problem.faculty.get(n.faculty_id)
            times, rooms = problem.time_options(n), problem.room_options(n)
            reasons: dict[str, int] = defaultdict(int)
            for d, q in times:
                st, en = problem.span(q, n.length)
                probe = Slot(n.assignment_id, d, st, en, rooms[0] if rooms else -1)
                keys = {x["rule_key"] for x in slot_issues(problem, probe, placed)}
                keys.discard("room_overlap")
                if rooms and not keys:
                    free = False
                    for r in rooms:
                        probe.room_id = r
                        if not any(o.room_id == r and o.day == d and o.start < en and st < o.end for o in placed):
                            free = True
                            break
                    keys = set() if free else {"room_overlap"}
                for k in keys or {"workload_or_combined_limits"}:
                    reasons[k] += 1
            out.append(finding("weekly_requirement",
                               f"Unassigned: {problem.need_label(n)} ({n.length} period(s), {f.name if f else 'no faculty'}). "
                               f"In the best partial timetable found, its {len(times)} possible time slot(s) are blocked by: "
                               + (", ".join(f"{k.replace('_', ' ')} ×{v}" for k, v in sorted(reasons.items())) or "no eligible slot") + ".",
                               category="diagnostic",
                               entities={"subject_id": sub.id, "group_id": g.id, "faculty_id": n.faculty_id, "assignment_id": n.assignment_id},
                               evidence={"possible_time_slots": len(times), "eligible_rooms": len(rooms), "blocked_by": dict(reasons),
                                         "note": "One maximal partial placement; a different combination of sessions could be left out instead."},
                               suggestions=["Free one of the blocking resources for this session, or reduce the requirement."]))
    return out
