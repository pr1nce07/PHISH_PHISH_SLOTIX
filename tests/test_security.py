"""Authorisation, session, injection, immutability and workflow tests (acceptance criteria 13 - security)."""
import threading
import time

import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import text

from app import clerk_auth
from app import models as m
from app.config import settings
from tests.conftest import PW, T, Api


@pytest.fixture
def env(world):
    """Reference data, three roles, and one published timetable."""
    w = world.reference()
    f2 = w.faculty("OTHER")
    other_cohort = w.group("Other class", 30)
    w.subject(other_cohort, "OTH", f2, theory=2)
    w.user("admin", "admin")
    w.user("fac", "faculty", faculty_id=w.fac.id)
    w.user("fac2", "faculty", faculty_id=f2.id)
    w.user("stu", "student", student_group_id=w.b1.id)
    admin = Api("admin")
    run = admin.post("/api/admin/runs", {"term_id": w.term.id, "time_limit": 10, "candidates": 2}).json()
    assert run["status"] == "completed", run
    versions = admin.get(f"/api/admin/runs/{run['id']}").json()["versions"]
    v = versions[0]
    assert admin.post(f"/api/admin/versions/{v['id']}/validate").json()["valid"]
    rv = admin.get(f"/api/admin/versions/{v['id']}").json()["version"]["row_version"]
    pub = admin.post(f"/api/admin/versions/{v['id']}/publish", {"row_version": rv})
    assert pub.status_code == 200, pub.text
    w.admin, w.pub_id, w.draft_id, w.f2 = admin, v["id"], versions[1]["id"], f2
    return w


def audit_actions(w):
    w.s.expire_all()
    return [(e.action, e.outcome) for e in w.s.query(m.AuditEvent)]


def test_anonymous_gets_nothing(env):
    anon = Api()
    for url in ("/api/timetable/published?term_id=1", "/api/terms", "/api/admin/versions", "/api/admin/data/rooms", "/api/admin/audit",
                "/api/notifications", "/api/faculty/requests"):
        assert anon.get(url).status_code == 401, url
    assert anon.post("/api/admin/data/rooms", {"code": "X"}).status_code == 401
    assert anon.c.delete("/api/admin/data/rooms/1").status_code == 401


def test_student_is_strictly_read_only_and_scoped(env):
    w, stu = env, Api("stu")
    rooms_before = w.s.query(m.Room).count()
    sid = w.s.query(m.TimetableSession).filter_by(version_id=w.draft_id).first().id
    attempts = [("POST", "/api/admin/data/rooms", {"department_id": w.dept.id, "code": "HACK", "name": "x", "capacity": 10}),
                ("PUT", f"/api/admin/data/rooms/{w.cls.id}", {"department_id": w.dept.id, "code": "HACK", "name": "x", "capacity": 10}),
                ("DELETE", f"/api/admin/data/rooms/{w.cls.id}", None),
                ("PATCH", f"/api/admin/sessions/{sid}", {"day_of_week": 0, "start_minute": 630, "room_id": w.cls.id, "row_version": 1}),
                ("POST", f"/api/admin/versions/{w.draft_id}/publish", {"row_version": 1}),
                ("POST", "/api/admin/runs", {"term_id": w.term.id}),
                ("POST", "/api/faculty/requests", {"term_id": w.term.id, "request_type": "leave", "reason": "because", "day_of_week": 1})]
    for method, url, body in attempts:
        r = stu.req(method, url, json=body)
        assert r.status_code == 403, (method, url, r.status_code)
    for method in ("POST", "PUT", "PATCH", "DELETE"):  # the published view has no mutation route at all
        assert stu.req(method, f"/api/timetable/published?term_id={w.term.id}", json={}).status_code == 405
    w.s.expire_all()
    assert w.s.query(m.Room).count() == rooms_before and w.s.get(m.Room, w.cls.id).code == "CR1"
    # drafts and admin data are invisible
    for url in (f"/api/admin/versions/{w.draft_id}", "/api/admin/versions", "/api/admin/audit", "/api/admin/data/faculty_availability", "/api/admin/rule-sets"):
        assert stu.get(url).status_code == 403
    # only the published version, and only the student's own class + batch
    data = stu.get(f"/api/timetable/published?term_id={w.term.id}").json()
    assert data["version"]["id"] == w.pub_id
    groups = {s["group_id"] for s in data["sessions"]}
    assert groups == {w.cohort.id, w.b1.id}
    assert "locked" not in data["sessions"][0] and "source" not in data["sessions"][0]
    csv = stu.get(f"/api/timetable/published/export.csv?term_id={w.term.id}").text
    assert "Batch B" not in csv and "Other class" not in csv and "Batch A" in csv  # export honours the same scope


def test_faculty_boundaries_and_idor(env):
    w, fac, fac2 = env, Api("fac"), Api("fac2")
    assert fac.post("/api/admin/data/subjects", {}).status_code == 403
    assert fac.post("/api/admin/rule-sets", {"name": "x"}).status_code == 403
    assert fac.get("/api/admin/audit").status_code == 403
    assert fac.post("/api/admin/users", {"username": "evil", "full_name": "e", "password": PW, "roles": ["admin"]}).status_code == 403
    r = fac.post("/api/faculty/requests", {"term_id": w.term.id, "request_type": "leave", "reason": "Conference travel", "day_of_week": 2})
    assert r.status_code == 201
    rid = r.json()["id"]
    assert r.json()["status"] == "pending"
    # another faculty member cannot read, cancel or decide it by changing the id
    assert fac2.get(f"/api/faculty/requests/{rid}").status_code == 404
    assert fac2.post(f"/api/faculty/requests/{rid}/cancel").status_code == 404
    assert fac2.post(f"/api/admin/requests/{rid}/decision", {"decision": "approved"}).status_code == 403
    assert fac.post(f"/api/admin/requests/{rid}/decision", {"decision": "approved"}).status_code == 403  # no self-approval
    assert fac2.get("/api/faculty/requests").json() == []
    # mass assignment of protected fields is rejected outright
    bad = fac.post("/api/faculty/requests", {"term_id": w.term.id, "request_type": "leave", "reason": "Conference travel", "day_of_week": 2,
                                             "status": "approved", "faculty_id": w.f2.id, "reviewer_user_id": 1})
    assert bad.status_code == 422
    # a time-change request must reference one of the requester's own published sessions
    theirs = next(s for s in w.admin.get(f"/api/admin/versions/{w.pub_id}").json()["sessions"] if s["faculty_id"] == w.f2.id)
    r = fac.post("/api/faculty/requests", {"term_id": w.term.id, "request_type": "time_change", "reason": "Please move", "session_id": theirs["id"]})
    assert r.status_code == 403
    # sessions and publication are admin-only
    assert fac.post(f"/api/admin/sessions/{theirs['id']}/lock", {"locked": True, "row_version": 1}).status_code == 403
    assert fac.post(f"/api/admin/versions/{w.draft_id}/publish", {"row_version": 1}).status_code == 403
    acts = audit_actions(w)
    assert ("security.forbidden", "denied") in acts and ("request.create", "denied") in acts
    dump = " ".join(str(e.detail) for e in w.s.query(m.AuditEvent))
    assert PW not in dump and "tt_session" not in dump


def test_request_approval_never_edits_published_timetable(env):
    w, fac = env, Api("fac")
    before = w.admin.get(f"/api/admin/versions/{w.pub_id}").json()["sessions"]
    day = next(s["day_of_week"] for s in before if s["faculty_id"] == w.fac.id)
    rid = fac.post("/api/faculty/requests", {"term_id": w.term.id, "request_type": "leave", "reason": "Medical leave", "day_of_week": day}).json()["id"]
    out = w.admin.post(f"/api/admin/requests/{rid}/decision", {"decision": "approved", "note": "ok", "apply_unavailability": True}).json()
    assert out["request"]["status"] == "approved" and out["affected_sessions"]
    assert w.admin.get(f"/api/admin/versions/{w.pub_id}").json()["sessions"] == before  # untouched
    assert any("approved" in n["message"] for n in fac.get("/api/notifications").json())
    # controlled repair: a new draft where nothing is left on the leave day, old version still published
    run = w.admin.post("/api/admin/runs", {"term_id": w.term.id, "kind": "repair", "base_version_id": w.pub_id, "time_limit": 10}).json()
    assert run["status"] == "completed", run
    new = w.admin.get(f"/api/admin/runs/{run['id']}").json()["versions"][0]
    assert new["status"] == "draft" and new["summary"]["moved"] >= len(out["affected_sessions"])
    sess = w.admin.get(f"/api/admin/versions/{new['id']}").json()["sessions"]
    assert not [s for s in sess if s["faculty_id"] == w.fac.id and s["day_of_week"] == day]
    assert w.s.query(m.TimetableVersion).filter_by(status="published").one().id == w.pub_id


def test_csrf_and_cross_origin(env):
    c = Api("admin")
    body = {"department_id": env.dept.id, "code": "NEW", "name": "n", "capacity": 10}
    assert c.c.post("/api/admin/data/rooms", json=body).status_code == 403  # no CSRF token
    assert c.c.post("/api/admin/data/rooms", json=body, headers={"X-CSRF-Token": "wrong"}).status_code == 403
    assert c.post("/api/admin/data/rooms", body, headers={"Origin": "https://evil.example"}).status_code == 403
    assert c.post("/api/admin/data/rooms", body).status_code == 201
    r = c.c.post("/api/auth/login", json={"username": "admin", "password": PW})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert "content-security-policy" in {k.lower() for k in r.headers} and r.headers["x-frame-options"] == "DENY"


def test_login_hardening(env):
    anon = Api()
    for payload in ("' OR '1'='1' --", "admin'; DROP TABLE app_users; --"):
        assert anon.post("/api/auth/login", {"username": payload, "password": payload}).status_code == 401
    assert env.s.query(m.AppUser).count() == 4
    a = anon.post("/api/auth/login", {"username": "admin", "password": "wrong-password"})
    b = anon.post("/api/auth/login", {"username": "nobody", "password": "wrong-password"})
    assert a.status_code == b.status_code == 401 and a.json() == b.json()  # no account enumeration
    for _ in range(settings.login_max_attempts):
        anon.post("/api/auth/login", {"username": "fac", "password": "bad"})
    assert anon.post("/api/auth/login", {"username": "fac", "password": PW}).status_code == 429  # throttled
    # logout revokes the server-side session
    c = Api("stu")
    assert c.get("/api/auth/me").status_code == 200
    assert c.post("/api/auth/logout").status_code == 200
    assert c.get("/api/auth/me").status_code == 401
    assert ("auth.login", "failed") in audit_actions(env)


def test_malformed_and_injection_input_fails_safely(env):
    a, w = env.admin, env
    r = a.post("/api/admin/data/rooms", {"department_id": w.dept.id, "code": "R'); DROP TABLE rooms;--", "name": "<script>alert(1)</script>", "capacity": 10})
    assert r.status_code == 422  # code pattern rejects it
    r = a.post("/api/admin/data/rooms", {"department_id": w.dept.id, "code": "R9", "name": "Robert'); DROP TABLE rooms;--", "capacity": 10})
    assert r.status_code == 201 and w.s.query(m.Room).count() >= 3  # stored as plain text, table intact
    for bad in ({"department_id": w.dept.id, "code": "N", "name": "n", "capacity": -5}, {"department_id": "x", "code": "N", "name": "n", "capacity": 5},
                {"department_id": w.dept.id, "code": "N", "name": "n", "capacity": 5, "id": 1, "is_admin": True}):
        assert a.post("/api/admin/data/rooms", bad).status_code == 422
    assert a.post("/api/admin/data/period_slots", {"term_id": w.term.id, "idx": 9, "start_minute": 900, "end_minute": 800}).status_code == 422
    assert a.post("/api/admin/data/rooms", {"department_id": 99999, "code": "N", "name": "n", "capacity": 5}).status_code == 409
    assert a.get("/api/admin/data/no_such_table").status_code == 404
    assert a.get("/api/admin/data/rooms?term_id=1%20OR%201=1").status_code in (200, 422)
    assert a.req("PUT", f"/api/admin/rule-sets/{w.rs.id}/rules/__import__", json={"enabled": True, "is_hard": False, "weight": 1, "params": {}}).status_code == 422
    assert a.req("PUT", f"/api/admin/rule-sets/{w.rs.id}/rules/room_fit", json={"enabled": True, "is_hard": True, "weight": 1, "params": {}}).status_code == 422
    r = a.post("/api/admin/runs", {"term_id": w.term.id, "kind": "whatif", "base_version_id": w.pub_id, "scenario": [{"type": "run_code", "code": "import os"}]})
    assert r.status_code == 422
    # CSV import: formulas rejected, dry run saves nothing
    n = w.s.query(m.Room).count()
    r = a.post("/api/admin/data/rooms/import", {"csv": f"department_id,code,name,capacity\n{w.dept.id},Z1,=HYPERLINK(\"http://evil\"),10\n", "dry_run": False}).json()
    assert r["errors"] and r["imported"] == 0
    r = a.post("/api/admin/data/rooms/import", {"csv": f"department_id,code,name,capacity\n{w.dept.id},Z1,Zed,10\n", "dry_run": True}).json()
    assert r["valid"] == 1 and r["imported"] == 0
    w.s.expire_all()
    assert w.s.query(m.Room).count() == n
    err = a.post("/api/admin/data/rooms", {"department_id": 99999, "code": "N", "name": "n", "capacity": 5}).text
    assert "sqlite" not in err.lower() and "traceback" not in err.lower() and "INSERT" not in err


def test_manual_edit_validation_locking_and_concurrency(env):
    a, w = env.admin, env
    d = a.get(f"/api/admin/versions/{w.draft_id}").json()
    rv = d["version"]["row_version"]
    theory = [s for s in d["sessions"] if s["session_type"] == "theory" and s["faculty_id"] == w.fac.id]
    s1, s2 = theory[0], theory[1]
    clash = {"day_of_week": s2["day_of_week"], "start_minute": s2["start_minute"], "room_id": s1["room_id"]}
    r = a.req("PATCH", f"/api/admin/sessions/{s1['id']}", json={**clash, "row_version": rv})
    assert r.status_code == 409 and {c["rule_key"] for c in r.json()["detail"]["conflicts"]} >= {"faculty_overlap"}
    assert a.get(f"/api/admin/versions/{w.draft_id}").json()["sessions"] == d["sessions"]  # nothing saved
    opts = a.get(f"/api/admin/sessions/{s1['id']}/options").json()["options"]
    good = next(o for o in opts if o["ok"] and not o["current"])
    blocked = next(o for o in opts if not o["ok"])
    assert blocked["reasons"]
    move = {"day_of_week": good["day_of_week"], "start_minute": good["start_minute"], "room_id": good["room_ids"][0]}
    assert a.req("PATCH", f"/api/admin/sessions/{s1['id']}", json={**move, "row_version": rv + 7}).status_code == 409  # stale editor
    ok = a.req("PATCH", f"/api/admin/sessions/{s1['id']}", json={**move, "row_version": rv})
    assert ok.status_code == 200 and ok.json()["version"]["row_version"] == rv + 1
    assert not [f for f in ok.json()["findings"] if f["severity"] == "error"]
    rv += 1
    assert a.post(f"/api/admin/sessions/{s1['id']}/lock", {"locked": True, "row_version": rv}).status_code == 200
    assert a.req("PATCH", f"/api/admin/sessions/{s1['id']}", json={**move, "row_version": rv + 1}).status_code == 409  # locked
    # regenerate everything else: the locked session keeps its exact slot
    run = a.post("/api/admin/runs", {"term_id": w.term.id, "base_version_id": w.draft_id, "time_limit": 10}).json()
    new = a.get(f"/api/admin/runs/{run['id']}").json()["versions"][0]
    kept = [s for s in a.get(f"/api/admin/versions/{new['id']}").json()["sessions"] if s["locked"]]
    assert len(kept) == 1 and (kept[0]["day_of_week"], kept[0]["start_minute"], kept[0]["room_id"]) == (move["day_of_week"], move["start_minute"], move["room_id"])
    assert a.get(f"/api/admin/sessions/{s1['id']}/explain").json()["hard"]


def test_publish_state_machine_and_immutability(env):
    a, w = env.admin, env
    rv = a.get(f"/api/admin/versions/{w.draft_id}").json()["version"]["row_version"]
    assert a.post(f"/api/admin/versions/{w.draft_id}/publish", {"row_version": rv}).status_code == 409  # not validated
    # an incomplete draft cannot be validated, hence never published
    sid = a.get(f"/api/admin/versions/{w.draft_id}").json()["sessions"][0]["id"]
    assert a.req("DELETE", f"/api/admin/sessions/{sid}?row_version={rv}").status_code == 200
    v = a.post(f"/api/admin/versions/{w.draft_id}/validate").json()
    assert v["valid"] is False and any(f["rule_key"] == "weekly_requirement" for f in v["findings"])
    assert a.post(f"/api/admin/versions/{w.draft_id}/publish", {"row_version": v["version"]["row_version"]}).status_code == 409
    # the published version is immutable through the API and in the database itself
    pub = a.get(f"/api/admin/versions/{w.pub_id}").json()
    ps = pub["sessions"][0]
    r = a.req("PATCH", f"/api/admin/sessions/{ps['id']}", json={"day_of_week": 0, "start_minute": 630, "room_id": ps["room_id"], "row_version": pub["version"]["row_version"]})
    assert r.status_code == 409
    assert a.req("DELETE", f"/api/admin/versions/{w.pub_id}").status_code == 409
    for sql in (f"UPDATE timetable_sessions SET day_of_week = 4 WHERE id = {ps['id']}", f"DELETE FROM timetable_sessions WHERE id = {ps['id']}",
                "UPDATE audit_events SET action = 'x'", "DELETE FROM audit_events"):
        with pytest.raises(Exception):
            w.s.execute(text(sql))
        w.s.rollback()
    # a sandbox can never be published
    sb = a.post(f"/api/admin/versions/{w.pub_id}/clone", {"sandbox": True}).json()["version"]
    a.post(f"/api/admin/versions/{sb['id']}/validate")
    rv2 = a.get(f"/api/admin/versions/{sb['id']}").json()["version"]["row_version"]
    assert a.post(f"/api/admin/versions/{sb['id']}/publish", {"row_version": rv2}).status_code == 409
    # the subject <-> group term trigger
    t2 = w.add(m.AcademicTerm(department_id=w.dept.id, name="T2"))
    with pytest.raises(Exception):
        w.s.execute(text("INSERT INTO subjects (department_id, term_id, student_group_id, code, name, subject_category, active, created_at,"
                         " theory_sessions_per_week, theory_periods_per_session, theory_required_features, practical_sessions_per_week,"
                         " practical_periods_per_session, practical_required_features) VALUES (:d, :t, :g, 'X', 'X', 'core', 1, '2026-01-01',"
                         " 0, 1, '[]', 0, 1, '[]')"), {"d": w.dept.id, "t": t2.id, "g": w.cohort.id})
    w.s.rollback()


def test_concurrent_publish_leaves_exactly_one_published(env):
    a, w = env.admin, env
    ids = []
    for _ in range(2):
        c = a.post(f"/api/admin/versions/{w.pub_id}/clone", {"sandbox": False}).json()["version"]
        v = a.post(f"/api/admin/versions/{c['id']}/validate").json()
        assert v["valid"]
        ids.append((c["id"], v["version"]["row_version"]))
    clients, results = [Api("admin"), Api("admin")], []
    gate = threading.Barrier(2)

    def go(client, vid, rv):
        gate.wait()
        results.append(client.post(f"/api/admin/versions/{vid}/publish", {"row_version": rv}).status_code)

    threads = [threading.Thread(target=go, args=(c, vid, rv)) for c, (vid, rv) in zip(clients, ids)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    w.s.expire_all()
    assert 200 in results and all(r in (200, 409) for r in results)
    assert w.s.query(m.TimetableVersion).filter_by(term_id=w.term.id, status="published").count() == 1
    assert w.s.query(m.TimetableVersion).filter_by(id=w.pub_id).one().status == "archived"  # history kept, not deleted


def test_whatif_sandbox_does_not_touch_live_data(env):
    a, w = env.admin, env
    before = a.get(f"/api/admin/versions/{w.pub_id}").json()["sessions"]
    avail = w.s.query(m.FacultyAvailability).count()
    day = before[0]["day_of_week"]
    run = a.post("/api/admin/runs", {"term_id": w.term.id, "kind": "whatif", "base_version_id": w.pub_id, "time_limit": 10,
                                     "scenario": [{"type": "faculty_unavailable", "faculty_id": w.fac.id, "day": day}]}).json()
    assert run["status"] == "completed", run
    full = a.get(f"/api/admin/runs/{run['id']}").json()
    sb = full["versions"][0]
    assert sb["is_sandbox"] and sb["summary"]["diff"] and full["summary"]["affected_before_repair"]
    assert a.get(f"/api/admin/versions/{w.pub_id}").json()["sessions"] == before
    w.s.expire_all()
    assert w.s.query(m.FacultyAvailability).count() == avail
    cmp_ = a.get(f"/api/admin/versions/compare?ids={w.pub_id},{w.draft_id}").json()
    assert len(cmp_["candidates"]) == 2 and all(c["valid"] for c in cmp_["candidates"])
    assert a.get(f"/api/admin/versions/{w.pub_id}/analytics").json()["heatmap"]
    assert a.get(f"/api/admin/dashboard?term_id={w.term.id}").json()["published"]["id"] == w.pub_id


# ---- Clerk sign-in ----------------------------------------------------------------------------
@pytest.fixture
def clerk(monkeypatch):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    monkeypatch.setattr(settings, "clerk_publishable_key", "pk_test_x")
    monkeypatch.setattr(settings, "clerk_frontend_api", "unit-test.clerk.accounts.dev")
    monkeypatch.setattr(settings, "allowed_origins", ["http://testserver"])
    real = clerk_auth.verify_session_token
    monkeypatch.setattr(clerk_auth, "verify_session_token", lambda tok: real(tok, signing_key=key.public_key()))
    emails = {}
    monkeypatch.setattr(clerk_auth, "verified_emails", lambda sub: emails.get(sub, []))

    def token(sub, signer=key, **over):
        now = int(time.time())
        claims = {"sub": sub, "iss": "https://unit-test.clerk.accounts.dev", "iat": now, "nbf": now - 5, "exp": now + 60,
                  "azp": "http://testserver", "fva": [1, -1], **over}
        return jwt.encode(claims, signer, algorithm="RS256")

    return token, emails


def test_clerk_login_is_default_deny_and_role_comes_from_our_db(env, clerk, monkeypatch):
    token, emails = clerk
    w, anon = env, Api()
    login = lambda t: anon.post("/api/auth/clerk", {"token": t})  # noqa: E731
    stranger = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    assert login(token("user_1", signer=stranger)).status_code == 401  # forged signature
    assert login(token("user_1", exp=int(time.time()) - 600)).status_code == 401  # expired
    assert login(token("user_1", azp="https://evil.example")).status_code == 401  # minted for another origin
    assert login(token("user_1", iss="https://other.clerk.accounts.dev")).status_code == 401
    emails["user_1"] = ["someone@example.org"]
    assert login(token("user_1")).status_code == 403  # valid identity, but not registered by an admin
    # an admin pre-registers the e-mail as faculty -> the Clerk identity gets exactly that role
    r = w.admin.post("/api/admin/users", {"username": "clerk.fac", "full_name": "Clerk Faculty", "email": "Someone@Example.org", "roles": ["faculty"], "faculty_id": w.fac.id})
    assert r.status_code == 201
    ok = login(token("user_1", role="admin", roles=["admin"]))  # claims in the token cannot grant roles
    assert ok.status_code == 200 and ok.json()["roles"] == ["faculty"]
    anon.csrf = ok.json()["csrf_token"]
    assert anon.get("/api/faculty/requests").status_code == 200 and anon.get("/api/admin/audit").status_code == 403
    assert w.s.query(m.AppUser).filter_by(username="clerk.fac").one().external_subject == "user_1"
    emails["user_2"] = ["someone@example.org"]
    assert Api().post("/api/auth/clerk", {"token": token("user_2")}).status_code == 403  # e-mail already bound to another identity
    # admins can be forced to use a second factor (authenticator app)
    w.admin.post("/api/admin/users", {"username": "clerk.admin", "full_name": "Clerk Admin", "email": "boss@example.org", "roles": ["admin"]})
    emails["user_9"] = ["boss@example.org"]
    monkeypatch.setattr(settings, "require_admin_mfa", True)
    assert Api().post("/api/auth/clerk", {"token": token("user_9", fva=[1, -1])}).status_code == 403
    assert Api().post("/api/auth/clerk", {"token": token("user_9", fva=[1, 1])}).status_code == 200
    # local password login can be switched off entirely
    monkeypatch.setattr(settings, "local_login", False)
    assert Api().post("/api/auth/login", {"username": "admin", "password": PW}).status_code == 404
