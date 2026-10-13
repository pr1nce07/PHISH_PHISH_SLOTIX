"""Solver, preflight, auditor and scoring tests (acceptance criteria 13.1 - 13.13)."""
import copy

from app import models as m
from app import services
from app.solver import auditor, engine
from app.solver.engine import SolveControl, solve
from app.solver.preflight import diagnose, preflight
from app.solver.problem import Slot, apply_scenario, match_slots
from app.solver.score import score
from tests.conftest import T, World


def keys(findings):
    return {f["rule_key"] for f in findings if f["severity"] == "error"}


def test_reference_example_schedules_everything(world):
    p = world.reference().problem()
    assert not auditor.errors(preflight(p))
    res = solve(p, time_limit=10)
    assert res.ok
    theory = [s for s in res.slots if s.assignment_id == world.theory["theory"].id]
    pracs = [s for s in res.slots if s.assignment_id in (world.p1["practical"].id, world.p2["practical"].id)]
    assert len(theory) == 3 and len(pracs) == 2
    assert all(s.end - s.start == 60 for s in theory) and all(s.end - s.start == 120 for s in pracs)
    assert all(s.room_id == world.lab.id for s in pracs)
    assert auditor.audit(p, res.slots) == []  # independent re-check: no hard violation at all
    assert res.objective == score(p, res.slots)["total"]  # solver objective == independent scorer


def test_overlaps_are_detected(world):
    w = world.reference()
    other = w.subject(w.cohort, "CSAC", w.fac, theory=1)
    p = w.problem()
    a, b = w.theory["theory"].id, other["theory"].id
    clash = [Slot(a, 0, T(10, 30), T(11, 30), w.cls.id, id=1), Slot(b, 0, T(10, 30), T(11, 30), w.cls.id, id=2)]
    found = keys(auditor.audit(p, clash, check_requirements=False))
    assert {"faculty_overlap", "room_overlap", "group_overlap"} <= found
    res = solve(p, time_limit=10)
    assert res.ok and not keys(auditor.audit(p, res.slots))


def test_parent_child_and_descendant_overlap(world):
    w = world.reference()
    f2 = w.faculty("X2")
    sub_batch = w.group("Batch A - half", 10, w.b1, "batch")  # two levels below the cohort
    deep = w.subject(sub_batch, "DEEP", f2, prac=1, feats=["forensic_workstations"])
    p = w.problem()
    assert p.related(w.cohort.id, sub_batch.id) and p.related(w.b1.id, sub_batch.id)
    assert not p.related(w.b1.id, w.b2.id) and not p.related(w.b2.id, sub_batch.id)
    whole = Slot(w.theory["theory"].id, 1, T(10, 30), T(11, 30), w.cls.id, id=1)
    batch = Slot(w.p1["practical"].id, 1, T(10, 30), T(12, 30), w.lab.id, id=2)
    assert "group_overlap" in keys(auditor.slot_issues(p, whole, [batch]))
    grand = Slot(deep["practical"].id, 1, T(10, 30), T(12, 30), w.lab.id, id=3)
    assert "group_overlap" in keys(auditor.slot_issues(p, whole, [grand]))
    res = solve(p, time_limit=10)
    assert res.ok and "group_overlap" not in keys(auditor.audit(p, res.slots))


def test_capacity_19_cannot_host_20(world):
    w = World()
    lab = w.room("LAB19", 19, "lab")
    w.room("CR", 40)
    f = w.faculty("A")
    c = w.group("C", 40)
    b = w.group("B", 20, c, "batch")
    pr = w.subject(b, "S", f, prac=1)
    p = w.problem()
    pf = preflight(p)
    assert "room_capacity" in keys(pf)
    assert any(x["evidence"].get("expected") == 20 and x["evidence"].get("available") == 19 for x in pf)
    assert not solve(p, time_limit=5).ok
    assert "room_capacity" in keys(auditor.slot_issues(p, Slot(pr["practical"].id, 0, T(10, 30), T(12, 30), lab.id), []))


def test_room_type_and_features(world):
    w = World()
    plain = w.room("LAB-PLAIN", 30, "lab")
    cr = w.room("CR", 40)
    f = w.faculty("A")
    g = w.group("G", 20)
    pr = w.subject(g, "S", f, prac=1, feats=["forensic_workstations"])
    p = w.problem()
    assert "room_suitability" in keys(preflight(p))
    aid = pr["practical"].id
    assert "room_suitability" in keys(auditor.slot_issues(p, Slot(aid, 0, T(10, 30), T(12, 30), plain.id), []))  # missing feature
    assert "room_suitability" in keys(auditor.slot_issues(p, Slot(aid, 0, T(10, 30), T(12, 30), cr.id), []))  # wrong type
    good = w.room("LAB-F", 30, "lab", ["forensic_workstations"])
    p = w.problem()
    res = solve(p, time_limit=5)
    assert res.ok and res.slots[0].room_id == good.id


def test_practical_is_consecutive_and_never_crosses_break(world):
    p = world.reference().problem()
    res = solve(p, time_limit=10)
    for s in res.slots:
        idxs = p.indices(s.start, s.end)
        assert idxs is not None and not p.break_hit(s.day, s.start, s.end)
    aid = world.p1["practical"].id
    across = Slot(aid, 0, T(12, 30), T(15, 30), world.lab.id)  # 12:30-15:30 would swallow the lunch break
    assert {"within_hours", "break_overlap"} & keys(auditor.slot_issues(p, across, []))
    assert p.span(2, 2) is None  # period 3 and period 4 are separated by lunch -> not consecutive
    # a timetable whose periods are all isolated cannot host a 2-period practical
    w2 = World(periods=((9, 0), (11, 0), (13, 0)), lunch=False)
    w2.room("L", 30, "lab")
    w2.subject(w2.group("G", 20), "S", w2.faculty("A"), prac=1)
    assert "consecutive_periods" in keys(preflight(w2.problem()))


def test_weekly_requirements_exact(world):
    p = world.reference().problem()
    res = solve(p, time_limit=10)
    assert "weekly_requirement" in keys(auditor.audit(p, res.slots[:-1]))  # one session missing
    extra = copy.copy(res.slots[0])
    assert "weekly_requirement" in keys(auditor.audit(p, res.slots + [extra]))  # one surplus


def test_faculty_available_for_two_periods_but_three_required(world):
    w = World()
    w.room("CR", 50)
    f = w.faculty("A")
    w.avail(f, 0, T(10, 30), T(12, 30))  # only two periods in the whole week
    w.subject(w.group("G", 40), "DFIS103", f, theory=3)
    pf = preflight(w.problem())
    msg = next(x for x in pf if x["rule_key"] == "faculty_availability")
    assert "DFIS103 requires 3 weekly period(s)" in msg["message"] and "available for only 2 period(s)" in msg["message"]
    assert msg["evidence"] == {"expected": 3, "available": 2}
    assert not solve(w.problem(), time_limit=5).ok  # never "2 of 3 scheduled = success"


def test_fixed_and_locked_sessions_stay(world):
    w = world.reference()
    w.s.add(m.FixedCommitment(term_id=w.term.id, faculty_id=w.fac.id, teaching_assignment_id=w.theory["theory"].id, room_id=w.cls.id,
                              title="pinned", day_of_week=3, start_minute=T(15, 30), end_minute=T(16, 30)))
    w.s.commit()
    p = w.problem()
    res = solve(p, time_limit=10)
    assert any(s.assignment_id == w.theory["theory"].id and (s.day, s.start, s.room_id) == (3, T(15, 30), w.cls.id) for s in res.slots)
    # lock one practical, then regenerate with completely different preferences
    lab = next(s for s in res.slots if s.assignment_id == w.p1["practical"].id)
    lab.locked = True
    w.rule("avoid_early_slots", enabled=True, weight=500)
    w.rule("avoid_late_slots", enabled=False)
    p2 = w.problem()
    locked, _ = match_slots(p2, [lab])
    res2 = solve(p2, time_limit=10, locked=locked, seed=7)
    kept = next(s for s in res2.slots if s.assignment_id == w.p1["practical"].id)
    assert (kept.day, kept.start, kept.room_id, kept.locked) == (lab.day, lab.start, lab.room_id, True)
    # the locked slot becomes impossible -> reported, never silently moved
    w.avail(w.fac, lab.day, 0, 1440, "unavailable")
    pf = preflight(w.problem(), [lab])
    assert "locked_session" in keys(pf)


def test_impossible_inputs_report_all_causes(world):
    w = World()
    w.room("CR", 30)  # too small for the class of 40, and there is no lab at all
    f = w.faculty("A")
    w.avail(f, 0, T(10, 30), T(11, 30))
    c = w.group("C", 40)
    w.subject(c, "TH", f, theory=3)
    w.subject(w.group("B", 20, c, "batch"), "PR", f, prac=1)
    k = keys(preflight(w.problem()))
    assert {"room_capacity", "room_suitability", "faculty_availability"} <= k


def infeasible_world():
    """Passes every preflight bound but is still impossible: both lecturers need Monday period 1."""
    w = World(days=1, periods=((10, 30), (11, 30), (12, 30)), lunch=False)
    w.room("CR", 50)
    fa, fb = w.faculty("A"), w.faculty("B")
    w.avail(fa, 0, T(10, 30), T(12, 30))
    w.avail(fb, 0, T(10, 30), T(11, 30))
    g = w.group("G", 30)
    w.subject(g, "SA", fa, theory=2)
    w.subject(g, "SB", fb, theory=1)
    return w


def test_solver_statuses_are_mapped_precisely(world, monkeypatch):
    p = world.reference().problem()
    assert solve(p, time_limit=10).status in ("OPTIMAL", "FEASIBLE")
    assert solve(p, time_limit=10, max_vars=1).status == "MODEL_INVALID"
    c = SolveControl()
    c.cancel()
    assert solve(p, time_limit=10, control=c).status == "UNKNOWN"

    w = infeasible_world()
    p2 = w.problem()
    assert not auditor.errors(preflight(p2))
    res = solve(p2, time_limit=10)
    assert res.status == "INFEASIBLE"
    diag = diagnose(p2, {}, time_each=2)
    fams = {f["evidence"].get("family") for f in diag}
    assert fams & {"faculty_availability", "group_overlap"}
    assert any(f["rule_key"] == "weekly_requirement" and f["message"].startswith("Unassigned") for f in diag)

    # persistence/UI states for every solver outcome
    def run(kind_status):
        if kind_status:
            monkeypatch.setattr(services, "solve", lambda *a, **k: engine.SolveResult(kind_status, note="x"))
        r = m.TimetableRun(term_id=world.term.id, rule_set_id=world.rs.id, params={"time_limit": 5})
        world.s.add(r)
        world.s.commit()
        services.execute_run(r.id)
        world.s.expire_all()
        return world.s.get(m.TimetableRun, r.id)

    ok = run(None)
    assert ok.status == "completed" and ok.solver_status in ("OPTIMAL", "FEASIBLE") and ok.input_hash
    unknown = run("UNKNOWN")
    assert unknown.status == "unknown" and "NOT a proof" in unknown.summary["message"]
    invalid = run("MODEL_INVALID")
    assert invalid.status == "failed" and invalid.solver_status == "MODEL_INVALID"
    for r in (unknown, invalid):
        assert not world.s.query(m.TimetableVersion).filter_by(run_id=r.id).count()  # nothing presented as a timetable


def test_infeasible_run_creates_no_version_and_cannot_publish():
    w = infeasible_world()
    r = m.TimetableRun(term_id=w.term.id, rule_set_id=w.rs.id, params={"time_limit": 5})
    w.s.add(r)
    w.s.commit()
    services.execute_run(r.id)
    w.s.expire_all()
    r = w.s.get(m.TimetableRun, r.id)
    assert r.status == "infeasible" and r.solver_status == "INFEASIBLE"
    assert w.s.query(m.TimetableVersion).count() == 0
    cats = {f.category for f in w.s.query(m.ConflictFinding).filter_by(run_id=r.id)}
    assert {"proven", "diagnostic"} <= cats


def test_soft_weights_change_ranking_not_hard_validity(world):
    w = world.reference()
    w.rule("avoid_late_slots", enabled=True, weight=0, params={"from_idx": 3})
    p0 = w.problem()
    r0 = solve(p0, time_limit=10)
    w.rule("avoid_late_slots", enabled=True, weight=200, params={"from_idx": 3})
    p1 = w.problem()
    r1 = solve(p1, time_limit=10)
    assert r0.ok and r1.ok
    assert auditor.audit(p0, r0.slots) == [] and auditor.audit(p1, r1.slots) == []
    late = lambda p, slots: sum(1 for s in slots for q in p.indices(s.start, s.end) if q >= 3)  # noqa: E731
    assert late(p1, r1.slots) <= late(p0, r0.slots)
    assert late(p1, r1.slots) == 0  # 7 teaching periods fit comfortably in the morning slots
    # the same timetable simply scores differently under different weights
    assert score(p1, r0.slots)["total"] >= score(p1, r1.slots)["total"]
    assert r1.objective == score(p1, r1.slots)["total"]


def test_distinct_candidates_and_minimal_disruption_repair(world):
    w = world.reference()
    r = m.TimetableRun(term_id=w.term.id, rule_set_id=w.rs.id, params={"time_limit": 5, "candidates": 3})
    w.s.add(r)
    w.s.commit()
    services.execute_run(r.id)
    w.s.expire_all()
    versions = w.s.query(m.TimetableVersion).filter_by(run_id=r.id).all()
    assert len(versions) >= 2
    sigs = [frozenset((s.assignment_id, s.day, s.start) for s in services.version_slots(w.s, v.id)) for v in versions]
    assert len(set(sigs)) == len(sigs)  # genuinely different schedules, not copies
    p = w.problem()
    for v in versions:
        assert auditor.audit(p, services.version_slots(w.s, v.id)) == []

    base = services.version_slots(w.s, versions[0].id)
    day = base[0].day
    affected = [s for s in base if s.day == day]
    p2 = w.problem()
    apply_scenario(p2, [{"type": "faculty_unavailable", "faculty_id": w.fac.id, "day": day}])
    base_map, _ = match_slots(p2, copy.deepcopy(base))
    rep = solve(p2, time_limit=10, base=base_map)
    assert rep.ok and auditor.audit(p2, rep.slots) == []
    assert all(s.day != day for s in rep.slots)
    moved = services.diff_slots(p2, base, rep.slots)
    assert len(moved) == len(affected)  # only the sessions hit by the disruption moved
    assert rep.objective == score(p2, rep.slots, base)["total"]


def test_rule_registry_rejects_unknown_and_unsafe_config():
    from app.solver.rules import validate_rule_config
    import pytest
    for bad in [("__import__('os').system('x')", True, False, 1, {}), ("avoid_late_slots", True, False, 1, {"from_idx": "1; DROP"}),
                ("avoid_late_slots", True, True, 1, {"from_idx": 1}), ("room_fit", True, False, 5000, {}),
                ("room_fit", True, False, 1, {"evil": 1})]:
        with pytest.raises(ValueError):
            validate_rule_config(*bad)
