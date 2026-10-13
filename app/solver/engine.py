"""CP-SAT timetable model (Google OR-Tools).

Hard constraints are model constraints or domain filters; soft rules only ever appear in the
objective, so no preference weight can buy a hard violation.
"""
from __future__ import annotations

import math
import time
from collections import defaultdict
from dataclasses import dataclass, field

from ortools.sat.python import cp_model

from .problem import Need, Problem, Slot
from .score import STRENGTH, placement_costs, soft

RELAXABLE = {
    "faculty_overlap": "faculty double-booking",
    "room_overlap": "room double-booking",
    "group_overlap": "student-group overlaps",
    "faculty_availability": "faculty availability / unavailable slots",
    "workload": "faculty daily/weekly workload limits",
    "room_capacity": "room seating capacity",
    "room_suitability": "room type / required features",
    "custom_rules": "additional configured hard rules",
    "fixed": "fixed teaching commitments",
    "locked": "locked sessions",
}


class SolveControl:
    """Lets another thread cancel a running solve."""

    def __init__(self) -> None:
        self.solver: cp_model.CpSolver | None = None
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True
        if self.solver is not None:
            self.solver.stop_search()


@dataclass
class SolveResult:
    status: str  # OPTIMAL | FEASIBLE | INFEASIBLE | MODEL_INVALID | UNKNOWN
    slots: list[Slot] = field(default_factory=list)
    placements: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    objective: int | None = None
    wall: float = 0.0
    unassigned: list[str] = field(default_factory=list)
    empty_domains: list[str] = field(default_factory=list)
    note: str = ""

    @property
    def ok(self) -> bool:
        return self.status in ("OPTIMAL", "FEASIBLE")


def solve(problem: Problem, *, time_limit: float = 20.0, locked: dict[str, Slot] | None = None,
          base: dict[str, Slot] | None = None, avoid: list[dict[str, tuple[int, int, int]]] | None = None,
          min_distinct: int = 1, relax: frozenset = frozenset(), optional: bool = False, use_objective: bool = True,
          control: SolveControl | None = None, workers: int = 4, seed: int = 1, max_vars: int = 400_000) -> SolveResult:
    t0 = time.monotonic()
    locked, base = locked or {}, base or {}
    needs = problem.needs
    model = cp_model.CpModel()
    X: dict[str, dict[tuple[int, int, int], cp_model.IntVar]] = {}
    sched: dict[str, cp_model.IntVar] = {}
    fac_cov: dict[tuple[int, int, int], list] = defaultdict(list)
    room_cov: dict[tuple[int, int, int], list] = defaultdict(list)
    grp_cov: dict[tuple[int, int, int], list] = defaultdict(list)
    by_assign: dict[int, list[Need]] = defaultdict(list)
    empty: list[str] = []
    nvars = 0

    def place_of(s: Slot, n: Need):
        idxs = problem.indices(s.start, s.end)
        return (s.day, idxs[0], s.room_id) if idxs and len(idxs) == n.length else None

    base_place = {k: place_of(s, n) for n in needs if (s := base.get(n.key)) for k in [n.key]}

    for n in needs:
        by_assign[n.assignment_id].append(n)
        if n.key in locked and "locked" not in relax:
            pl = place_of(locked[n.key], n)
            cands = [pl] if pl else []
        else:
            rooms = problem.room_options(n, relax)
            cands = [(d, p, r) for d, p in problem.time_options(n, relax) for r in rooms]
        nvars += len(cands)
        if nvars > max_vars:
            return SolveResult("MODEL_INVALID", note=f"Model too large (> {max_vars} placement variables). Reduce the input size.",
                               wall=time.monotonic() - t0)
        if not cands:
            empty.append(n.key)
        X[n.key] = {}
        for d, p, r in cands:
            v = model.new_bool_var(f"x_{n.key}_{d}_{p}_{r}")
            X[n.key][(d, p, r)] = v
            for q in range(p, p + n.length):
                fac_cov[(n.faculty_id, d, q)].append(v)
                room_cov[(r, d, q)].append(v)
                grp_cov[(n.group_id, d, q)].append(v)
        if optional:
            sched[n.key] = model.new_bool_var(f"s_{n.key}")
            model.add(sum(X[n.key].values()) == sched[n.key])
        elif cands:
            model.add_exactly_one(X[n.key].values())

    if empty and not optional:
        return SolveResult("INFEASIBLE", empty_domains=empty, wall=time.monotonic() - t0,
                           note="At least one session has no eligible day/time/room at all.")

    if "faculty_overlap" not in relax:
        for lits in fac_cov.values():
            if len(lits) > 1:
                model.add_at_most_one(lits)
    if "room_overlap" not in relax:
        for lits in room_cov.values():
            if len(lits) > 1:
                model.add_at_most_one(lits)
    NP = len(problem.periods)
    chains = {g: [g] + problem.ancestors(g) for g in problem.groups}

    def group_lits(g: int, d: int, q: int) -> list:
        return [v for c in chains[g] for v in grp_cov.get((c, d, q), ())]

    if "group_overlap" not in relax:
        # a group and every ancestor share students -> at most one session per period along the chain
        for g in problem.groups:
            for d in problem.days:
                for q in range(NP):
                    lits = group_lits(g, d, q)
                    if len(lits) > 1:
                        model.add_at_most_one(lits)

    fac_ids = sorted({n.faculty_id for n in needs})
    if "workload" not in relax:
        for fid in fac_ids:
            f = problem.faculty.get(fid)
            if f and f.max_day:
                for d in problem.days:
                    lits = [v for q in range(NP) for v in fac_cov.get((fid, d, q), ())]
                    if lits:
                        model.add(sum(lits) <= f.max_day)
            if f and f.max_week:
                lits = [v for d in problem.days for q in range(NP) for v in fac_cov.get((fid, d, q), ())]
                if lits:
                    model.add(sum(lits) <= f.max_week)

    if "fixed" not in relax:
        for fx in problem.fixed:
            if fx.assignment_id is None:
                continue
            idxs = problem.indices(fx.start, fx.end)
            lits = [v for n in by_assign.get(fx.assignment_id, []) for (d, p, r), v in X[n.key].items()
                    if idxs and d == fx.day and p == idxs[0] and len(idxs) == n.length and (fx.room_id is None or r == fx.room_id)]
            if lits:
                model.add_at_least_one(lits)
            elif not optional:
                return SolveResult("INFEASIBLE", wall=time.monotonic() - t0,
                                   note=f"Fixed commitment '{fx.title}' cannot be placed in its slot.")

    hard_spread = (r := problem.rule("spread_subject_days")) and r.is_hard and "custom_rules" not in relax
    if "custom_rules" not in relax:
        r = problem.rule("max_group_periods_per_day")
        if r:
            for g in problem.leaf_groups():
                for d in problem.days:
                    lits = [v for q in range(NP) for v in group_lits(g, d, q)]
                    if lits:
                        model.add(sum(lits) <= r.params["max"])
        r = problem.rule("max_faculty_consecutive")
        if r and r.is_hard:
            size = r.params["max"] + 1
            for fid in fac_ids:
                for d in problem.days:
                    for q in range(NP):
                        if problem.span(q, size):
                            lits = [v for i in range(size) for v in fac_cov.get((fid, d, q + i), ())]
                            if lits:
                                model.add(sum(lits) <= r.params["max"])
    day_count = {}
    for aid, ns in by_assign.items():
        if len(ns) > 1:
            for d in problem.days:
                lits = [v for n in ns for (dd, _, _), v in X[n.key].items() if dd == d]
                if lits:
                    day_count[(aid, d)] = lits
                    if hard_spread:
                        model.add(sum(lits) <= 1)

    # candidate diversity: differ from each earlier solution in at least `min_distinct` session times
    for prev in avoid or []:
        same = [v for n in needs if n.key in prev for (d, p, _), v in X[n.key].items() if (d, p) == prev[n.key][:2]]
        if same:
            model.add(sum(same) <= len([n for n in needs if n.key in prev]) - min_distinct)

    if optional:
        model.maximize(sum(sched.values()))
    elif use_objective:
        terms = []
        weights = {k: r.weight for k in ("faculty_preferred_slots", "faculty_self_preferences", "avoid_late_slots", "avoid_early_slots",
                                         "practical_preferred_window", "room_fit", "minimal_change") if (r := soft(problem, k))}
        for n in needs:
            bp = base_place.get(n.key)
            for (d, p, r), v in X[n.key].items():
                c = sum(weights[k] * val for k, val in placement_costs(
                    problem, n.faculty_id, n.group_id, n.session_type, d, p, n.length, r, bp).items())
                if c:
                    terms.append(c * v)
                if bp == (d, p, r):
                    model.add_hint(v, 1)
        if (r := soft(problem, "spread_subject_days")):
            for key, lits in day_count.items():
                ex = model.new_int_var(0, len(lits), f"spread_{key}")
                model.add(ex >= sum(lits) - 1)
                terms.append(r.weight * ex)
        if (r := soft(problem, "room_stability")):
            for aid, ns in by_assign.items():
                if len(ns) > 1 and ns[0].session_type == "theory":
                    per_room = defaultdict(list)
                    for n in ns:
                        for (_, _, rid), v in X[n.key].items():
                            per_room[rid].append(v)
                    used = []
                    for rid, lits in per_room.items():
                        u = model.new_bool_var(f"used_{aid}_{rid}")
                        for v in lits:
                            model.add_implication(v, u)
                        used.append(u)
                    terms.append(r.weight * (sum(used) - 1))

        def gap_terms(weight: int, name: str, busy_at) -> None:
            # penalty = (number of teaching blocks) - 1 on days with any teaching
            starts, total = [], []
            prev = None
            for q in range(NP):
                cur = busy_at(q)
                if cur:
                    st = model.new_bool_var(f"{name}_{q}")
                    model.add(st >= sum(cur) - (sum(prev) if prev else 0))
                    starts.append(st)
                    total.extend(cur)
                prev = cur
            if len(starts) > 1:
                anyb = model.new_bool_var(f"{name}_any")
                model.add(anyb <= sum(total))
                terms.append(weight * (sum(starts) - anyb))

        if (r := soft(problem, "student_gaps")) and "group_overlap" not in relax:
            for g in problem.leaf_groups():
                for d in problem.days:
                    gap_terms(r.weight, f"gg_{g}_{d}", lambda q, g=g, d=d: group_lits(g, d, q))
        if (r := soft(problem, "faculty_gaps")) and "faculty_overlap" not in relax:
            for fid in fac_ids:
                for d in problem.days:
                    gap_terms(r.weight, f"fg_{fid}_{d}", lambda q, fid=fid, d=d: fac_cov.get((fid, d, q), []))
        if (r := soft(problem, "max_faculty_consecutive")):
            size = r.params["max"] + 1
            for fid in fac_ids:
                for d in problem.days:
                    for q in range(NP):
                        if problem.span(q, size):
                            lits = [v for i in range(size) for v in fac_cov.get((fid, d, q + i), ())]
                            if len(lits) > r.params["max"]:
                                ex = model.new_bool_var(f"run_{fid}_{d}_{q}")
                                model.add(ex >= sum(lits) - r.params["max"])
                                terms.append(r.weight * ex)
        bal, own = soft(problem, "balance_faculty_load"), soft(problem, "faculty_self_preferences")
        if (bal or own) and problem.days:
            for fid in fac_ids:
                f = problem.faculty.get(fid)
                # a faculty member's own "spread my load across the week" request adds to the global balance rule
                coef = (bal.weight if bal else 0) + (own.weight * STRENGTH.get(f.prefs["strength"], 2)
                                                     if own and f and f.prefs and f.prefs["spread"] else 0)
                if not coef:
                    continue
                total = sum(n.length for n in needs if n.faculty_id == fid)
                target = math.ceil(total / len(problem.days))
                for d in problem.days:
                    lits = [v for q in range(NP) for v in fac_cov.get((fid, d, q), ())]
                    if len(lits) > target:
                        ex = model.new_int_var(0, NP, f"bal_{fid}_{d}")
                        model.add(ex >= sum(lits) - target)
                        terms.append(coef * ex)
        if terms:
            model.minimize(sum(terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = float(time_limit)
    solver.parameters.num_workers = max(1, workers)
    solver.parameters.random_seed = seed
    if control is not None:
        control.solver = solver
        if control.cancelled:
            return SolveResult("UNKNOWN", note="Cancelled before the search started.", wall=time.monotonic() - t0)
    status = solver.status_name(solver.solve(model))
    res = SolveResult(status, wall=time.monotonic() - t0, empty_domains=empty)
    if res.ok:
        for n in needs:
            hit = next((pl for pl, v in X[n.key].items() if solver.boolean_value(v)), None)
            if hit is None:
                res.unassigned.append(n.key)
                continue
            d, p, r = hit
            start, end = problem.span(p, n.length)
            lk = locked.get(n.key)
            res.placements[n.key] = hit
            res.slots.append(Slot(n.assignment_id, d, start, end, r, locked=lk is not None,
                                  source=lk.source if lk is not None else "generated"))
        if not optional and use_objective:
            res.objective = int(round(solver.objective_value))
    return res
