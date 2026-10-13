"""Repeatable DEMO seed.  Run:  python -m app.seed  [--reset-passwords]

Everything created here is FICTITIOUS demo data, loosely modelled on the layout of the sample
timetables (names/codes that were clearly legible are reused only as realistic labels). It is not
official institutional data.

Demo passwords are generated randomly and written to data/demo_credentials.txt (git-ignored);
they are never hard-coded or printed.
"""
from __future__ import annotations

import secrets
import sys

from sqlalchemy import select

from . import models as m
from .config import DATA_DIR
from .db import SessionLocal
from .security import hash_password
from .services import default_rule_set

DEMO_DEPT = "SCSDF-DEMO"
T = lambda h, mi=0: h * 60 + mi  # noqa: E731


def gen_password() -> str:
    return "Tt-" + secrets.token_urlsafe(12) + "9a"


def seed(reset_passwords: bool = False) -> None:
    with SessionLocal() as db:
        dept = db.scalars(select(m.Department).where(m.Department.code == DEMO_DEPT)).first()
        if dept is None:
            _create(db)
            db.commit()
            print("Demo data created.")
            reset_passwords = True
        else:
            print("Demo data already present - nothing changed.")
        if reset_passwords:
            lines = ["# DEMO accounts - generated locally, do not commit\n"]
            for u in db.scalars(select(m.AppUser).where(m.AppUser.username.like("demo.%"))):
                pw = gen_password()
                u.password_hash, u.failed_logins, u.locked_until = hash_password(pw), 0, None
                lines.append(f"{u.username}\t{pw}\t({u.full_name})\n")
            db.commit()
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            (DATA_DIR / "demo_credentials.txt").write_text("".join(lines), encoding="utf-8")
            print(f"Demo credentials written to {DATA_DIR / 'demo_credentials.txt'}")


def _create(db) -> None:
    add = lambda o: (db.add(o), db.flush(), o)[2]  # noqa: E731
    dept = add(m.Department(code=DEMO_DEPT, name="School of Cyber Security & Digital Forensics (DEMO)"))
    term = add(m.AcademicTerm(department_id=dept.id, name="DEMO Odd Semester 2026-27", start_date="2026-09-14", end_date="2026-12-18"))
    for d in range(5):
        db.add(m.WorkingDay(term_id=term.id, day_of_week=d))
    for i, (s, e) in enumerate([(T(10, 30), T(11, 30)), (T(11, 30), T(12, 30)), (T(12, 30), T(13, 30)),
                                (T(14, 30), T(15, 30)), (T(15, 30), T(16, 30)), (T(16, 30), T(17, 30))]):
        db.add(m.PeriodSlot(term_id=term.id, idx=i, start_minute=s, end_minute=e, label=f"P{i + 1}"))
    db.add(m.ScheduledBreak(term_id=term.id, name="Lunch Break", start_minute=T(13, 30), end_minute=T(14, 30)))

    rooms = {}
    for code, name, typ, cap, feats in [
        ("IFS-002", "Basement classroom IFS 002", "classroom", 70, ["projector"]),
        ("SEM-HALL", "Seminar Hall", "classroom", 120, ["projector"]),
        ("CR-101", "Classroom 101", "classroom", 45, ["projector"]),
        ("BL-208", "Computer Lab BL 208", "lab", 35, ["computers", "projector"]),
        ("BL-209", "Computer Lab BL 209", "lab", 35, ["computers"]),
        ("FL-110", "Forensics Lab 110", "lab", 24, ["computers", "forensic_workstations"]),
    ]:
        r = rooms[code] = add(m.Room(department_id=dept.id, code=code, name=name, room_type=typ, capacity=cap))
        for f in feats:
            db.add(m.RoomFeature(room_id=r.id, code=f, name=f.replace("_", " ").title()))

    fac = {}
    for code, name, mx in [("HAP", "Dr. Himesh A. Patel", 4), ("JT", "Dr. Jay Teraiya", 4), ("PS", "Dr. Parag Shukla", 4),
                           ("AT", "Dr. Ashutosh Tripathi", 4), ("PR", "Dr. Pruthvish Rajput", 4), ("PK", "Mr. Prakash Khasor", 4),
                           ("SR", "Mr. Sarang Rajvansh", 4), ("DD", "Mr. Dharmesh Dave", 4)]:
        fac[code] = add(m.FacultyProfile(department_id=dept.id, code=code, name=name, max_periods_per_day=mx, max_periods_per_week=18))

    bt = add(m.Programme(department_id=dept.id, code="BTECH-CSE-CS", name="B.Tech CSE (Cyber Security)"))
    ms = add(m.Programme(department_id=dept.id, code="MSC-DFIS", name="M.Sc. Digital Forensics & Information Security"))

    def cohort(prog, name, sem, strength, n_batches):
        c = add(m.StudentGroup(term_id=term.id, programme_id=prog.id, name=name, semester=sem, strength=strength))
        bs = [add(m.StudentGroup(term_id=term.id, programme_id=prog.id, parent_group_id=c.id, name=f"{name} - Batch {chr(65 + i)}",
                                 semester=sem, group_type="batch", strength=strength // n_batches)) for i in range(n_batches)]
        return c, bs

    bt3, bt3b = cohort(bt, "B.Tech CS Sem III", 3, 60, 2)
    ms1, ms1b = cohort(ms, "M.Sc. DFIS Sem I", 1, 40, 2)

    def offering(group, code, name, f, theory=0, prac=0, feats=()):
        s = add(m.Subject(department_id=dept.id, term_id=term.id, student_group_id=group.id, code=code, name=name,
                          theory_sessions_per_week=theory, practical_sessions_per_week=prac, practical_periods_per_session=2,
                          practical_required_features=list(feats)))
        out = {}
        if theory:
            out["theory"] = add(m.TeachingAssignment(subject_id=s.id, faculty_id=fac[f].id, session_type="theory"))
        if prac:
            out["practical"] = add(m.TeachingAssignment(subject_id=s.id, faculty_id=fac[f].id, session_type="practical"))
        return out

    # Convention: whole-class theory on the cohort offering; one practical offering per batch.
    dbms = offering(bt3, "DBMS", "Database Management Systems", "PS", theory=4)
    offering(bt3, "EMIII", "Engineering Mathematics - III", "HAP", theory=4)
    offering(bt3, "DSA", "Data Structures & Algorithms", "JT", theory=3)
    offering(bt3, "CPP", "Computer Programming with Python", "AT", theory=2)
    offering(bt3, "COM", "Computer Organization & Microprocessor", "PR", theory=3)
    offering(bt3, "ECS", "Essentials of Cyber Security", "PK", theory=3)
    for b in bt3b:
        offering(b, "DSA", "DSA Lab", "JT", prac=1, feats=["computers"])
        offering(b, "CPP", "CPP Lab", "AT", prac=1, feats=["computers"])
        offering(b, "COM", "COM Lab", "PR", prac=1, feats=["computers"])
    offering(ms1, "DFIS103", "Digital Forensics", "SR", theory=3)
    offering(ms1, "CSAC", "Cyber Security Audit & Compliance", "DD", theory=3)
    for b in ms1b:
        offering(b, "DFIS103", "Digital Forensics Practical", "SR", prac=1, feats=["forensic_workstations"])

    A = lambda f, d, s, e, kind, note=None: db.add(m.FacultyAvailability(  # noqa: E731
        faculty_id=fac[f].id, term_id=term.id, day_of_week=d, start_minute=s, end_minute=e, kind=kind, note=note))
    A("HAP", 4, T(14, 30), T(17, 30), "unavailable", "Research half-day")
    for d in range(5):
        A("PS", d, T(10, 30), T(13, 30), "preferred")
    A("SR", 0, T(10, 30), T(11, 30), "unavailable", "University committee")
    db.add(m.FixedCommitment(term_id=term.id, faculty_id=fac["PK"].id, title="Department meeting", day_of_week=2,
                             start_minute=T(16, 30), end_minute=T(17, 30)))
    db.add(m.FixedCommitment(term_id=term.id, faculty_id=fac["PS"].id, teaching_assignment_id=dbms["theory"].id, room_id=rooms["IFS-002"].id,
                             title="DBMS fixed Monday first period", day_of_week=0, start_minute=T(10, 30), end_minute=T(11, 30)))
    default_rule_set(db)

    roles = {r.code: r.id for r in db.scalars(select(m.AppRole))}

    def user(username, name, role, **link):
        u = add(m.AppUser(username=username, full_name=name, **link))
        db.add(m.AppUserRole(user_id=u.id, role_id=roles[role]))

    user("demo.admin", "Demo Coordinator", "admin")
    user("demo.faculty", "Dr. Parag Shukla (demo)", "faculty", faculty_id=fac["PS"].id)
    user("demo.faculty2", "Mr. Sarang Rajvansh (demo)", "faculty", faculty_id=fac["SR"].id)
    user("demo.student", "Demo Student (B.Tech III, Batch A)", "student", student_group_id=bt3b[0].id)
    user("demo.student2", "Demo Student (M.Sc. DFIS I, Batch B)", "student", student_group_id=ms1b[1].id)


if __name__ == "__main__":
    seed("--reset-passwords" in sys.argv)
