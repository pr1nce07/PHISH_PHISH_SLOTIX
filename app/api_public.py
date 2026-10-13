"""Authentication plus the faculty and student (read-only) APIs.

Students only ever reach GET endpoints here, and every query is scoped on the server to the
published version and to the student's own group family."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from . import clerk_auth
from . import models as m
from . import schemas as sc
from . import services as svc
from .api_admin import get_or_404, request_row, timetable_csv
from .config import settings
from .db import get_db
from .solver.problem import load_problem
from .security import (COOKIE, CurrentUser, audit, client_ip, create_session, current_user, current_user_optional, dummy_verify,
                       hash_password, limiter, password_problem, rate_limit, require_faculty, revoke_sessions, user_roles,
                       verify_password)

router = APIRouter(prefix="/api")
BAD_LOGIN = "Invalid username or password."


def me_payload(user: CurrentUser) -> dict:
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "roles": sorted(user.roles),
            "faculty_id": user.faculty_id, "student_group_id": user.student_group_id, "csrf_token": user.csrf_token}


def start_session(db: Session, user: m.AppUser, response: Response) -> dict:
    token, csrf = create_session(db, user)  # fresh session id on every login (no fixation)
    response.set_cookie(COOKIE, token, max_age=settings.session_minutes * 60, httponly=True, secure=settings.cookie_secure,
                        samesite="strict", path="/")
    return {"id": user.id, "username": user.username, "full_name": user.full_name, "roles": sorted(user_roles(db, user.id)),
            "faculty_id": user.faculty_id, "student_group_id": user.student_group_id, "csrf_token": csrf}


@router.get("/auth/config")
def auth_config():
    """Public, non-secret settings the login page needs."""
    return {"clerk_publishable_key": settings.clerk_publishable_key if settings.clerk_enabled else None,
            "clerk_frontend_api": settings.clerk_frontend_api or None, "local_login": settings.local_login,
            "org_name": settings.org_name}


@router.post("/auth/clerk")
def clerk_login(body: sc.ClerkLoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    """Exchange a verified Clerk session token for an application session.
    Default deny: only identities an administrator has pre-registered (by e-mail) get in."""
    if not limiter.hit(f"clerk-ip:{client_ip(request)}", 30, 300):
        raise HTTPException(429, "Too many sign-in attempts. Please wait a few minutes and try again.")

    def deny(reason: str, status: int = 403, msg: str = "This account is not registered for the timetable system. "
                                                        "Ask the coordinator to add your e-mail address."):
        audit(db, None, "auth.clerk_login", detail={"reason": reason}, request=request, outcome="denied")
        db.commit()
        raise HTTPException(status, msg)

    try:
        claims = clerk_auth.verify_session_token(body.token)
    except clerk_auth.ClerkError as exc:
        deny(str(exc), 401, "Sign-in could not be verified. Please sign in again.")
    sub = claims["sub"]
    user = db.scalars(select(m.AppUser).where(m.AppUser.external_subject == sub)).first()
    if user is None:  # first sign-in: link by a VERIFIED e-mail that an admin registered beforehand
        try:
            emails = clerk_auth.verified_emails(sub)
        except clerk_auth.ClerkError as exc:
            deny(str(exc), 503, "Sign-in is temporarily unavailable. Please try again later.")
        user = db.scalars(select(m.AppUser).where(func.lower(m.AppUser.email).in_(emails), m.AppUser.external_subject.is_(None))).first() if emails else None
        if user is None:
            deny("no matching registered e-mail")
        user.external_subject, user.auth_provider = sub, "clerk"
    if not user.active:
        deny("inactive account")
    if settings.require_admin_mfa and "admin" in user_roles(db, user.id) and not clerk_auth.has_second_factor(claims):
        deny("admin without second factor", 403, "Administrators must sign in with two-step verification (authenticator app). "
                                                 "Enable it in your account security settings and sign in again.")
    user.mfa_enabled = clerk_auth.has_second_factor(claims)
    out = start_session(db, user, response)
    audit(db, user, "auth.clerk_login", "app_users", user.id, {"mfa": user.mfa_enabled}, request)
    db.commit()
    return out


@router.post("/auth/login")
def login(body: sc.LoginIn, request: Request, response: Response, db: Session = Depends(get_db)):
    if not settings.local_login:
        raise HTTPException(404, "Not found.")
    ip, uname = client_ip(request), body.username.lower()
    win = settings.login_window_seconds
    key = f"login:{ip}:{uname}"
    if not limiter.hit(f"login-ip:{ip}", settings.login_max_attempts * 10, win) or limiter.blocked(key, settings.login_max_attempts, win):
        audit(db, None, "auth.login", detail={"username": uname, "reason": "rate_limited"}, request=request, outcome="denied")
        db.commit()
        raise HTTPException(429, "Too many sign-in attempts. Please wait a few minutes and try again.")
    user = db.scalars(select(m.AppUser).where(m.AppUser.username == uname)).first()
    now = m.utcnow()
    ok = False
    if user is None or not user.active or user.auth_provider != "local" or (user.locked_until and user.locked_until > now):
        dummy_verify(body.password)
    else:
        ok = verify_password(body.password, user.password_hash)
    if not ok:
        limiter.hit(key, settings.login_max_attempts, win)
        if user is not None:
            user.failed_logins += 1
            if user.failed_logins >= settings.login_max_attempts * 2:
                user.locked_until, user.failed_logins = now + timedelta(minutes=15), 0
        audit(db, None, "auth.login", "app_users", user.id if user else None, {"username": uname}, request, outcome="failed")
        db.commit()
        raise HTTPException(401, BAD_LOGIN)  # same answer whether or not the account exists
    user.failed_logins, user.locked_until = 0, None
    out = start_session(db, user, response)
    audit(db, user, "auth.login", "app_users", user.id, {}, request)
    db.commit()
    return out


@router.post("/auth/logout")
def logout(request: Request, response: Response, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    db.execute(m.AuthSession.__table__.delete().where(m.AuthSession.token_hash == user.token_hash))
    audit(db, user, "auth.logout", request=request)
    db.commit()
    response.delete_cookie(COOKIE, path="/")
    return {"ok": True}


@router.get("/auth/me")
def me(request: Request):
    user = current_user_optional(request)
    if user is None:
        raise HTTPException(401, "Authentication required.")
    return me_payload(user)


@router.post("/auth/change-password", dependencies=[Depends(rate_limit("pwchange", 10, 3600))])
def change_password(body: sc.PasswordChangeIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    u = db.get(m.AppUser, user.id)
    if not verify_password(body.current_password, u.password_hash):
        audit(db, user, "auth.password_change", request=request, outcome="failed")
        db.commit()
        raise HTTPException(403, "The current password is incorrect.")
    if (msg := password_problem(body.new_password)):
        raise HTTPException(422, msg)
    u.password_hash = hash_password(body.new_password)
    revoke_sessions(db, u.id, keep=user.token_hash)  # sign out every other device
    audit(db, user, "auth.password_change", request=request)
    db.commit()
    return {"ok": True}


# ---- scoped timetable reads -------------------------------------------------------------------
def group_family(db: Session, group_id: int | None) -> set[int]:
    if group_id is None:
        return set()
    g = db.get(m.StudentGroup, group_id)
    if g is None:
        return set()
    parent = {x.id: x.parent_group_id for x in db.scalars(select(m.StudentGroup).where(m.StudentGroup.term_id == g.term_id))}

    def chain(i):
        out, seen = [], set()
        while i is not None and i not in seen:
            out.append(i)
            seen.add(i)
            i = parent.get(i)
        return out

    mine = set(chain(group_id))
    return mine | {i for i in parent if group_id in chain(i)}


def published(db: Session, term_id: int) -> m.TimetableVersion | None:
    return db.scalars(select(m.TimetableVersion).where(m.TimetableVersion.term_id == term_id, m.TimetableVersion.status == "published",
                                                       m.TimetableVersion.is_sandbox.is_(False))).first()


def scoped_sessions(db: Session, user: CurrentUser, term_id: int) -> tuple[m.TimetableVersion, list[dict]]:
    pub = published(db, term_id)
    if pub is None:
        raise HTTPException(404, "No timetable has been published for this term yet.")
    rows = svc.session_rows(db, pub.id)
    if user.is_admin or "faculty" in user.roles:
        for r in rows:
            r["mine"] = r["faculty_id"] == user.faculty_id
    elif "student" in user.roles:
        fam = group_family(db, user.student_group_id)
        rows = [r for r in rows if r["group_id"] in fam]
    else:
        rows = []
    for r in rows:  # internal bookkeeping stays server-side
        r.pop("source", None)
        r.pop("locked", None)
    return pub, rows


@router.get("/terms")
def terms(db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    out = []
    own_term = None
    own_dept = None
    if not user.is_admin and "faculty" not in user.roles:
        g = db.get(m.StudentGroup, user.student_group_id) if user.student_group_id else None
        own_term = g.term_id if g else -1
    elif "faculty" in user.roles and user.faculty_id:
        prof = db.get(m.FacultyProfile, user.faculty_id)
        own_dept = prof.department_id if prof else None
    for t in db.scalars(select(m.AcademicTerm).order_by(m.AcademicTerm.id.desc())):
        has_pub = published(db, t.id) is not None
        # faculty also see active terms of their own department before publication, so they can submit preferences
        upcoming = own_dept is not None and t.active and t.department_id == own_dept
        if user.is_admin or upcoming or (has_pub and (own_term is None or own_term == t.id)):
            out.append({"id": t.id, "name": t.name, "department_id": t.department_id, "active": t.active, "published": has_pub})
    return out


@router.get("/timetable/published")
def published_timetable(term_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    pub, rows = scoped_sessions(db, user, term_id)
    shown = {r["group_id"] for r in rows}
    fam = set().union(*(group_family(db, g) for g in shown)) if shown else set()
    groups = [{"id": g.id, "name": g.name, "parent_group_id": g.parent_group_id}
              for g in db.scalars(select(m.StudentGroup).where(m.StudentGroup.term_id == term_id)) if g.id in fam or g.id in shown]
    if "student" in user.roles and not (user.is_admin or "faculty" in user.roles):
        groups = [g for g in groups if g["id"] in group_family(db, user.student_group_id)]
    return {"version": {"id": pub.id, "label": pub.label, "published_at": pub.published_at and pub.published_at.isoformat() + "Z"},
            "calendar": svc.term_calendar(db, term_id), "sessions": rows, "groups": groups}


@router.get("/timetable/published/export.csv", dependencies=[Depends(rate_limit("export", 60, 3600))])
def published_csv(term_id: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    pub, rows = scoped_sessions(db, user, term_id)  # the export uses exactly the same scope as the screen
    audit(db, user, "timetable.export", "timetable_versions", pub.id, {"rows": len(rows)}, request)
    db.commit()
    return timetable_csv(rows, f"timetable-{pub.id}.csv")


# ---- faculty requests -------------------------------------------------------------------------
def own_faculty(user: CurrentUser) -> int:
    if user.faculty_id is None:
        raise HTTPException(403, "Your account is not linked to a faculty profile.")
    return user.faculty_id


@router.get("/faculty/requests")
def my_requests(db: Session = Depends(get_db), user: CurrentUser = Depends(require_faculty)):
    fid = own_faculty(user)
    return [request_row(db, r) for r in db.scalars(select(m.FacultyRequest).where(m.FacultyRequest.faculty_id == fid)
                                                   .order_by(m.FacultyRequest.id.desc()).limit(200))]


@router.get("/faculty/requests/{id_}")
def my_request(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_faculty)):
    r = db.get(m.FacultyRequest, id_)
    if r is None or r.faculty_id != own_faculty(user):  # same answer for "missing" and "not yours" (no id probing)
        raise HTTPException(404, "Not found.")
    return request_row(db, r)


@router.post("/faculty/requests", status_code=201, dependencies=[Depends(rate_limit("requests", 20, 3600))])
def create_request(body: sc.RequestIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_faculty)):
    fid = own_faculty(user)
    get_or_404(db, m.AcademicTerm, body.term_id)
    label = None
    if body.session_id is not None:
        pub = published(db, body.term_id)
        row = next((s for s in svc.session_rows(db, pub.id) if s["id"] == body.session_id), None) if pub else None
        if row is None or row["faculty_id"] != fid:
            audit(db, user, "request.create", "faculty_requests", None, {"session_id": body.session_id}, request, outcome="denied")
            db.commit()
            raise HTTPException(403, "You can only raise requests for your own published sessions.")
        label = f"{row['subject_code']} {row['session_type']} · {row['group_name']} · {svc.DAY_NAMES[row['day_of_week']]} {svc.fmt(row['start_minute'])}"
    r = m.FacultyRequest(term_id=body.term_id, faculty_id=fid, requester_user_id=user.id, session_label=label,
                         **body.model_dump(exclude={"term_id"}))
    db.add(r)
    db.flush()
    audit(db, user, "request.create", "faculty_requests", r.id, {"type": body.request_type}, request)
    db.commit()
    return request_row(db, r)


@router.post("/faculty/requests/{id_}/cancel")
def cancel_request(id_: int, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_faculty)):
    r = db.get(m.FacultyRequest, id_)
    if r is None or r.faculty_id != own_faculty(user):
        raise HTTPException(404, "Not found.")
    if r.status != "pending":
        raise HTTPException(409, "Only pending requests can be cancelled.")
    r.status = "cancelled"
    audit(db, user, "request.cancel", "faculty_requests", id_, {}, request)
    db.commit()
    return request_row(db, r)


# ---- faculty: my preferences (soft constraints, faculty role only) ------------------------------
def _slot_key(day: int, start: int) -> str:
    return f"{day}-{start}"


@router.get("/faculty/preferences")
def my_preferences(term_id: int, db: Session = Depends(get_db), user: CurrentUser = Depends(require_faculty)):
    """The signed-in faculty member's own preferences. There is no id parameter to tamper with."""
    fid = own_faculty(user)
    get_or_404(db, m.AcademicTerm, term_id)
    rs = svc.default_rule_set(db)
    db.commit()
    problem = load_problem(db, term_id, rs.id)
    f = problem.faculty.get(fid)
    row = db.scalars(select(m.FacultyPreference).where(m.FacultyPreference.faculty_id == fid, m.FacultyPreference.term_id == term_id)).first()
    # only "can I teach here at all" is exposed - never the reason or anyone's notes
    unavailable = [_slot_key(d, p.start) for d in problem.days for p in problem.periods
                   if problem.break_hit(d, p.start, p.end) or problem.faculty_issue(fid, d, p.start, p.end)]
    pub = published(db, term_id)
    return {
        "calendar": svc.term_calendar(db, term_id), "unavailable": unavailable,
        "marks": dict(row.slot_marks or {}) if row else {},
        "avoid_first": row.avoid_first if row else False, "avoid_last": row.avoid_last if row else False,
        "spread_evenly": row.spread_evenly if row else True, "strength": row.strength if row else "normal",
        "notes": (row.notes or "") if row else "",
        "submitted_at": row.submitted_at.isoformat() + "Z" if row and row.submitted_at else None,
        "info": {"subjects_assigned": len({a.subject_id for a in problem.assignments.values() if a.faculty_id == fid}),
                 "max_periods_per_day": f.max_day if f else None, "max_periods_per_week": f.max_week if f else None},
        "report": svc.preference_report(problem, svc.version_slots(db, pub.id), fid) if pub else None,
    }


@router.put("/faculty/preferences", dependencies=[Depends(rate_limit("prefs", 60, 3600))])
def save_preferences(body: sc.PreferencesIn, request: Request, db: Session = Depends(get_db), user: CurrentUser = Depends(require_faculty)):
    """Preferences are weighed by the solver on the next generation; saving never changes a timetable."""
    fid = own_faculty(user)
    get_or_404(db, m.AcademicTerm, body.term_id)
    cal = svc.term_calendar(db, body.term_id)
    valid = {(d, p["start_minute"]) for d in cal["days"] for p in cal["periods"]}
    marks: dict[str, str] = {}
    for mk in body.marks:
        if (mk.day_of_week, mk.start_minute) not in valid:
            raise HTTPException(422, "A marked slot is not a teaching period of this term.")
        marks[_slot_key(mk.day_of_week, mk.start_minute)] = mk.mark
    row = db.scalars(select(m.FacultyPreference).where(m.FacultyPreference.faculty_id == fid, m.FacultyPreference.term_id == body.term_id)).first()
    if row is None:
        row = m.FacultyPreference(faculty_id=fid, term_id=body.term_id)
        db.add(row)
    row.slot_marks, row.avoid_first, row.avoid_last = marks, body.avoid_first, body.avoid_last
    row.spread_evenly, row.strength, row.notes = body.spread_evenly, body.strength, body.notes or None
    row.submitted_at = row.updated_at = m.utcnow()
    audit(db, user, "preferences.submit", "faculty_preferences", fid,
          {"term_id": body.term_id, "preferred": sum(v == "prefer" for v in marks.values()),
           "avoid": sum(v == "avoid" for v in marks.values()), "strength": body.strength}, request)
    db.commit()
    return {"ok": True, "submitted_at": row.submitted_at.isoformat() + "Z"}


# ---- notifications ----------------------------------------------------------------------------
@router.get("/notifications")
def notifications(db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    return [{"id": n.id, "message": n.message, "read": n.read, "created_at": n.created_at.isoformat() + "Z"}
            for n in db.scalars(select(m.Notification).where(m.Notification.user_id == user.id).order_by(m.Notification.id.desc()).limit(100))]


@router.post("/notifications/read-all")
def read_all(db: Session = Depends(get_db), user: CurrentUser = Depends(current_user)):
    db.execute(update(m.Notification).where(m.Notification.user_id == user.id).values(read=True))
    db.commit()
    return {"ok": True}
