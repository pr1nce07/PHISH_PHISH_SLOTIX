"""Faculty "My preferences": faculty-only access, owner taken from the session, and the solver
weighing the preferences as soft constraints (never above a mandatory rule)."""
from app import models as m
from app import services
from app.solver import auditor
from app.solver.engine import solve
from app.solver.score import score
from tests.conftest import T, Api
from tests.test_security import env  # noqa: F401  (fixture: reference data, users, one published timetable)


def prefs(w, fac, marks, **kw):
    w.s.add(m.FacultyPreference(faculty_id=fac.id, term_id=w.term.id, slot_marks=marks, submitted_at=kw.pop("submitted_at", m.utcnow()), **kw))
    w.s.commit()


def test_preferences_are_faculty_only_and_owned_by_the_session(env):  # noqa: F811
    w, fac, fac2, stu = env, Api("fac"), Api("fac2"), Api("stu")
    url = f"/api/faculty/preferences?term_id={w.term.id}"
    body = {"term_id": w.term.id, "marks": [{"day_of_week": 0, "start_minute": T(10, 30), "mark": "avoid"},
                                            {"day_of_week": 1, "start_minute": T(11, 30), "mark": "prefer"}],
            "avoid_first": True, "avoid_last": False, "spread_evenly": True, "strength": "high", "notes": "Labs before lunch please."}
    # only the faculty role reaches these endpoints
    for client in (stu, w.admin):
        assert client.get(url).status_code == 403
        assert client.req("PUT", "/api/faculty/preferences", json=body).status_code == 403
    assert Api().get(url).status_code == 401
    # save and read back
    r = fac.req("PUT", "/api/faculty/preferences", json=body)
    assert r.status_code == 200 and r.json()["submitted_at"]
    mine = fac.get(url).json()
    assert mine["marks"] == {f"0-{T(10, 30)}": "avoid", f"1-{T(11, 30)}": "prefer"}
    assert mine["avoid_first"] and mine["strength"] == "high" and mine["notes"] == "Labs before lunch please."
    assert mine["report"] and mine["report"]["periods"] == 7 and mine["info"]["subjects_assigned"] == 3
    # the owner is always the signed-in faculty member: a forged faculty_id is rejected, and a colleague sees only their own (empty) form
    assert fac2.req("PUT", "/api/faculty/preferences", json={**body, "faculty_id": w.fac.id}).status_code == 422
    other = fac2.get(url).json()
    assert other["marks"] == {} and other["notes"] == "" and other["submitted_at"] is None
    w.s.expire_all()
    assert w.s.query(m.FacultyPreference).count() == 1 and w.s.query(m.FacultyPreference).one().faculty_id == w.fac.id
    # malformed input
    bad = [{**body, "marks": [{"day_of_week": 0, "start_minute": 1, "mark": "avoid"}]},  # not a teaching period
           {**body, "marks": [{"day_of_week": 6, "start_minute": T(10, 30), "mark": "avoid"}]},  # not a working day
           {**body, "marks": [{"day_of_week": 0, "start_minute": T(10, 30), "mark": "must"}]},
           {**body, "strength": "absolute"}, {**body, "notes": "x" * 1001}, {**body, "term_id": 9999}]
    assert [fac.req("PUT", "/api/faculty/preferences", json=b).status_code for b in bad] == [422, 422, 422, 422, 422, 404]
    # the coordinator can read what was submitted (read-only); nobody else can
    rows = w.admin.get(f"/api/admin/faculty-preferences?term_id={w.term.id}").json()
    assert len(rows) == 1 and rows[0]["faculty_id"] == w.fac.id and rows[0]["avoid_slots"] == 1 and rows[0]["notes"]
    assert fac.get(f"/api/admin/faculty-preferences?term_id={w.term.id}").status_code == 403
    assert stu.get(f"/api/admin/faculty-preferences?term_id={w.term.id}").status_code == 403
    # submitting never touches the published timetable
    assert w.s.query(m.TimetableVersion).filter_by(status="published").one().id == w.pub_id
    assert "avoid" not in str(stu.get(f"/api/timetable/published?term_id={w.term.id}").json())


def test_solver_weighs_submitted_preferences(world):
    w = world.reference()
    monday = {f"0-{p}": "avoid" for p in (T(10, 30), T(11, 30), T(12, 30), T(14, 30), T(15, 30), T(16, 30))}
    prefs(w, w.fac, monday, avoid_first=True, avoid_last=True, spread_evenly=True, strength="high")
    p = w.problem()
    assert p.faculty[w.fac.id].prefs["strength"] == "high"
    res = solve(p, time_limit=10)
    assert res.ok and auditor.audit(p, res.slots) == []
    assert all(s.day != 0 for s in res.slots)  # nothing on the avoided Monday
    assert all(s.start != T(10, 30) and s.end != T(17, 30) for s in res.slots)  # neither first nor last period
    assert res.objective == score(p, res.slots)["total"]  # engine and independent scorer agree
    rep = services.preference_report(p, res.slots, w.fac.id)
    assert rep == {"periods": 7, "in_preferred": 0, "in_avoided": 0, "first_breach": 0, "last_breach": 0, "honoured_pct": 100}


def test_preferences_never_override_mandatory_rules(world):
    w = world.reference()
    every = {f"{d}-{p}": "avoid" for d in range(5) for p in (T(10, 30), T(11, 30), T(12, 30), T(14, 30), T(15, 30), T(16, 30))}
    prefs(w, w.fac, every, strength="high")
    p = w.problem()
    res = solve(p, time_limit=10)
    assert res.ok and len(res.slots) == 5 and auditor.audit(p, res.slots) == []  # everything is still scheduled
    assert services.preference_report(p, res.slots, w.fac.id)["honoured_pct"] == 0
    assert res.objective == score(p, res.slots)["total"] > 0


def test_unsubmitted_or_disabled_preferences_are_ignored(world):
    w = world.reference()
    prefs(w, w.fac, {f"0-{T(10, 30)}": "avoid"}, submitted_at=None)
    assert w.problem().faculty[w.fac.id].prefs is None  # a draft row is not a submission
    row = w.s.query(m.FacultyPreference).one()
    row.submitted_at = m.utcnow()
    w.s.commit()
    w.rule("faculty_self_preferences", enabled=False)  # the coordinator can switch the rule off
    p = w.problem()
    res = solve(p, time_limit=10)
    assert "faculty_self_preferences" not in {r["rule_key"] for r in score(p, res.slots)["rules"]}
