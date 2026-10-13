"""Soft-rule scoring. Pure Python and independent of the solver, so any timetable (generated,
repaired or manually edited) gets the same per-rule penalty breakdown. Hard violations are never
part of this score - they invalidate a candidate (see auditor.py)."""
from __future__ import annotations

import math
from collections import defaultdict
from itertools import permutations

from .problem import Problem, Slot
from .rules import REGISTRY

Place = tuple[int, int, int]  # (day, start period index, room id)
STRENGTH = {"low": 1, "normal": 2, "high": 4}


def self_pref_cost(problem: Problem, prefs: dict, day: int, idxs) -> int:
    """Raw penalty of teaching these periods given a faculty member's own submitted preferences."""
    marks, last = prefs["marks"], len(problem.periods) - 1
    has_preferred = "prefer" in marks.values()
    n = 0
    for q in idxs:
        mark = marks.get(f"{day}-{problem.periods[q].start}")
        if mark == "avoid":
            n += 3
        elif has_preferred and mark != "prefer":
            n += 1
        if (prefs["avoid_first"] and q == 0) or (prefs["avoid_last"] and q == last):
            n += 2
    return n * STRENGTH.get(prefs["strength"], 2)


def soft(problem: Problem, key: str):
    r = problem.rule(key)
    return r if r and not r.is_hard and r.weight > 0 else None


def change_cost(new: Place, old: Place | None) -> int:
    if old is None or new == old:
        return 0
    if new[:2] == old[:2]:
        return 2  # room change only
    return 5 + (2 if new[0] != old[0] else 0) + min(3, abs(new[1] - old[1]))


def placement_costs(problem: Problem, faculty_id: int, group_id: int, session_type: str, day: int, p: int, length: int,
                    room_id: int, base: Place | None) -> dict[str, int]:
    """Raw (unweighted) penalties that depend on a single placement only."""
    out: dict[str, int] = {}
    idxs = range(p, p + length)
    r = soft(problem, "faculty_preferred_slots")
    f = problem.faculty.get(faculty_id)
    if r and f and f.preferred:
        n = 0
        for q in idxs:
            per = problem.periods[q]
            if not any(d == day and a <= per.start and per.end <= b for d, a, b in f.preferred):
                n += 1
        out[r.key] = n
    r = soft(problem, "faculty_self_preferences")
    if r and f and f.prefs:
        out[r.key] = self_pref_cost(problem, f.prefs, day, idxs)
    r = soft(problem, "avoid_late_slots")
    if r:
        out[r.key] = sum(1 for q in idxs if q >= r.params["from_idx"])
    r = soft(problem, "avoid_early_slots")
    if r:
        out[r.key] = sum(1 for q in idxs if q <= r.params["until_idx"])
    r = soft(problem, "practical_preferred_window")
    if r and session_type == "practical":
        out[r.key] = sum(1 for q in idxs if not r.params["from_idx"] <= q <= r.params["to_idx"])
    r = soft(problem, "room_fit")
    room, g = problem.rooms.get(room_id), problem.groups.get(group_id)
    if r and room and g:
        out[r.key] = min(10, max(0, room.capacity - g.strength) // r.params["seats_per_point"])
    r = soft(problem, "minimal_change")
    if r and base is not None:
        out[r.key] = change_cost((day, p, room_id), base)
    return {k: v for k, v in out.items() if v}


def _blocks(busy: set[int]) -> int:
    return sum(1 for q in busy if q - 1 not in busy)


def score(problem: Problem, slots: list[Slot], base_slots: list[Slot] | None = None) -> dict:
    raw: dict[str, int] = defaultdict(int)
    placed = []  # (assign, subject, slot, idxs)
    for s in slots:
        a = problem.assignments.get(s.assignment_id)
        idxs = problem.indices(s.start, s.end)
        if a is None or idxs is None or a.subject_id not in problem.subjects:
            continue
        placed.append((a, problem.subjects[a.subject_id], s, idxs))

    base_by_a: dict[int, list[Place]] = defaultdict(list)
    for s in base_slots or []:
        idxs = problem.indices(s.start, s.end)
        if idxs:
            base_by_a[s.assignment_id].append((s.day, idxs[0], s.room_id))

    by_a: dict[int, list] = defaultdict(list)
    fac_busy: dict[tuple[int, int], set[int]] = defaultdict(set)
    grp_busy: dict[tuple[int, int], set[int]] = defaultdict(set)
    for a, sub, s, idxs in placed:
        by_a[a.id].append((s, idxs))
        fac_busy[(a.faculty_id, s.day)].update(idxs)
        grp_busy[(sub.group_id, s.day)].update(idxs)
        for k, v in placement_costs(problem, a.faculty_id, sub.group_id, a.session_type, s.day, idxs[0], len(idxs), s.room_id, None).items():
            raw[k] += v

    if soft(problem, "minimal_change") and base_slots is not None:
        for aid, lst in by_a.items():
            new = [(s.day, idxs[0], s.room_id) for s, idxs in lst]
            old = list(base_by_a.get(aid, []))
            for pl in list(new):  # unchanged sessions cost nothing
                if pl in old:
                    new.remove(pl)
                    old.remove(pl)
            old += [None] * (len(new) - len(old))
            if not new:
                continue
            if len(new) <= 6:
                best = min(sum(change_cost(n, o) for n, o in zip(new, perm)) for perm in set(permutations(old, len(new))))
            else:
                best = sum(change_cost(n, o) for n, o in zip(sorted(new), sorted(old, key=lambda x: x or (9, 99, 0))))
            raw["minimal_change"] += best

    if soft(problem, "spread_subject_days"):
        for aid, lst in by_a.items():
            per_day: dict[int, int] = defaultdict(int)
            for s, _ in lst:
                per_day[s.day] += 1
            raw["spread_subject_days"] += sum(c - 1 for c in per_day.values())
    if soft(problem, "room_stability"):
        for aid, lst in by_a.items():
            if problem.assignments[aid].session_type == "theory" and len(lst) > 1:
                raw["room_stability"] += len({s.room_id for s, _ in lst}) - 1
    if soft(problem, "student_gaps"):
        for g in problem.leaf_groups():
            chain = [g] + problem.ancestors(g)
            for d in problem.days:
                busy = set().union(*(grp_busy.get((c, d), set()) for c in chain))
                raw["student_gaps"] += max(0, _blocks(busy) - 1)
    r = soft(problem, "max_faculty_consecutive")
    for (fid, d), busy in fac_busy.items():
        if soft(problem, "faculty_gaps"):
            raw["faculty_gaps"] += max(0, _blocks(busy) - 1)
        if r:
            size = r.params["max"] + 1
            raw[r.key] += sum(1 for q in range(len(problem.periods)) if problem.span(q, size) and all(q + i in busy for i in range(size)))
    totals: dict[int, int] = defaultdict(int)
    for (fid, d), busy in fac_busy.items():
        totals[fid] += len(busy)
    if problem.days:
        for (fid, d), busy in fac_busy.items():
            excess = max(0, len(busy) - math.ceil(totals[fid] / len(problem.days)))
            if soft(problem, "balance_faculty_load"):
                raw["balance_faculty_load"] += excess
            fp = problem.faculty.get(fid)
            if soft(problem, "faculty_self_preferences") and fp and fp.prefs and fp.prefs["spread"]:
                raw["faculty_self_preferences"] += excess * STRENGTH.get(fp.prefs["strength"], 2)

    rows, total = [], 0
    for key, spec in REGISTRY.items():
        r = soft(problem, key)
        if not r or (key == "minimal_change" and base_slots is None):
            continue
        pen = raw.get(key, 0) * r.weight
        total += pen
        rows.append({"rule_key": key, "name": spec.name, "raw": raw.get(key, 0), "weight": r.weight, "penalty": pen})
    return {"total": total, "rules": rows}
