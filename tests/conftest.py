import os

os.environ.setdefault("TT_SCRYPT_N", "1024")  # fast hashing in tests only
os.environ.setdefault("TT_ENV", "test")

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402
from sqlalchemy import select  # noqa: E402

from app import db as dbmod  # noqa: E402
from app import models as m  # noqa: E402
from app import services  # noqa: E402
from app.main import app  # noqa: E402
from app.security import hash_password, limiter  # noqa: E402
from app.solver.problem import load_problem  # noqa: E402

PW = "Correct-Horse-Battery-9"
T = lambda h, mi=0: h * 60 + mi  # noqa: E731


@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    engine = dbmod.init_engine(f"sqlite:///{(tmp_path / 't.db').as_posix()}")
    with engine.begin() as conn:
        m.install_schema(conn)
    services.SYNC_RUNS = True
    limiter.reset()
    yield
    engine.dispose()


class World:
    """Small builder for test data. Defaults reproduce the problem statement's example:
    M.Sc. DFIS Sem I, 40 students in two practical batches of 20, a subject with three theory
    periods per week and one two-period practical per batch per week."""

    def __init__(self, days=5, periods=((10, 30), (11, 30), (12, 30), (14, 30), (15, 30), (16, 30)), lunch=True):
        self.s = dbmod.SessionLocal()
        s = self.s
        n = s.query(m.Department).count() + 1
        self.dept = self.add(m.Department(code=f"D{n}", name=f"Dept {n}"))
        self.term = self.add(m.AcademicTerm(department_id=self.dept.id, name="T1"))
        self.prog = self.add(m.Programme(department_id=self.dept.id, code="MSC", name="M.Sc. DFIS"))
        for d in range(days):
            s.add(m.WorkingDay(term_id=self.term.id, day_of_week=d))
        for i, (h, mi) in enumerate(periods):
            s.add(m.PeriodSlot(term_id=self.term.id, idx=i, start_minute=T(h, mi), end_minute=T(h, mi) + 60))
        if lunch:
            s.add(m.ScheduledBreak(term_id=self.term.id, name="Lunch", start_minute=T(13, 30), end_minute=T(14, 30)))
        self.rs = services.default_rule_set(s)
        s.commit()

    def add(self, obj):
        self.s.add(obj)
        self.s.flush()
        return obj

    def room(self, code, cap, typ="classroom", feats=()):
        r = self.add(m.Room(department_id=self.dept.id, code=code, name=code, room_type=typ, capacity=cap))
        for f in feats:
            self.s.add(m.RoomFeature(room_id=r.id, code=f, name=f))
        self.s.commit()
        return r

    def faculty(self, code, **kw):
        f = self.add(m.FacultyProfile(department_id=self.dept.id, code=code, name=f"Prof {code}", **kw))
        self.s.commit()
        return f

    def group(self, name, strength, parent=None, typ="cohort"):
        g = self.add(m.StudentGroup(term_id=self.term.id, programme_id=self.prog.id, name=name, strength=strength,
                                    parent_group_id=parent.id if parent else None, group_type=typ))
        self.s.commit()
        return g

    def subject(self, group, code, fac, theory=0, prac=0, prac_len=2, feats=(), **kw):
        sub = self.add(m.Subject(department_id=self.dept.id, term_id=self.term.id, student_group_id=group.id, code=code, name=code,
                                 theory_sessions_per_week=theory, practical_sessions_per_week=prac,
                                 practical_periods_per_session=prac_len, practical_required_features=list(feats), **kw))
        out = {"subject": sub}
        for typ, n in (("theory", theory), ("practical", prac)):
            if n and fac is not None:
                out[typ] = self.add(m.TeachingAssignment(subject_id=sub.id, faculty_id=fac.id, session_type=typ))
        self.s.commit()
        return out

    def avail(self, fac, day, start, end, kind="available"):
        self.s.add(m.FacultyAvailability(faculty_id=fac.id, term_id=self.term.id, day_of_week=day, start_minute=start,
                                         end_minute=end, kind=kind))
        self.s.commit()

    def rule(self, key, **kw):
        r = self.s.scalars(select(m.RuleDefinition).where(m.RuleDefinition.rule_set_id == self.rs.id, m.RuleDefinition.rule_key == key)).one()
        for k, v in kw.items():
            setattr(r, k, v)
        self.s.commit()

    def problem(self):
        self.s.expire_all()
        return load_problem(self.s, self.term.id, self.rs.id)

    def reference(self):
        """The example from the problem statement."""
        self.cls = self.room("CR1", 45)
        self.lab = self.room("LAB1", 20, "lab", ["forensic_workstations"])
        self.fac = self.faculty("SR")
        self.cohort = self.group("MSc DFIS I", 40)
        self.b1 = self.group("Batch A", 20, self.cohort, "batch")
        self.b2 = self.group("Batch B", 20, self.cohort, "batch")
        self.theory = self.subject(self.cohort, "DFIS103", self.fac, theory=3)
        self.p1 = self.subject(self.b1, "DFIS103", self.fac, prac=1, feats=["forensic_workstations"])
        self.p2 = self.subject(self.b2, "DFIS103", self.fac, prac=1, feats=["forensic_workstations"])
        return self

    def user(self, username, role, **link):
        u = self.add(m.AppUser(username=username, full_name=username, password_hash=hash_password(PW), **link))
        rid = self.s.scalars(select(m.AppRole.id).where(m.AppRole.code == role)).one()
        self.s.add(m.AppUserRole(user_id=u.id, role_id=rid))
        self.s.commit()
        return u


@pytest.fixture
def world():
    w = World()
    yield w
    w.s.close()


class Api:
    """A logged-in client that sends the CSRF token like the real UI does."""

    def __init__(self, username=None):
        self.c = TestClient(app)
        self.csrf = ""
        if username:
            r = self.c.post("/api/auth/login", json={"username": username, "password": PW})
            assert r.status_code == 200, r.text
            self.csrf = r.json()["csrf_token"]

    def req(self, method, url, **kw):
        headers = kw.pop("headers", {})
        if method != "GET" and self.csrf:
            headers.setdefault("X-CSRF-Token", self.csrf)
        return self.c.request(method, url, headers=headers, **kw)

    def get(self, url, **kw):
        return self.req("GET", url, **kw)

    def post(self, url, json=None, **kw):
        return self.req("POST", url, json=json if json is not None else {}, **kw)


@pytest.fixture
def api():
    return Api
