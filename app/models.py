"""Database schema (SQLite).

Schema notes (mandatory design decisions):
  * Weekly teaching requirements live directly on `subjects` (a *subject offering* for one
    student group in one term). There is NO `weekly_requirements` table.
  * `subjects.student_group_id` is a foreign key to `student_groups`; a trigger guarantees the
    group belongs to the same academic term as the offering.
  * Room features live in `room_features` with a direct `room_id` FK. There is NO
    `room_feature_map` table.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, Boolean, CheckConstraint, DateTime, ForeignKey, Index, Integer, String, Text,
                        UniqueConstraint, text)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


def pk():
    return mapped_column(Integer, primary_key=True, autoincrement=True)


def fk(target: str, ondelete: str = "RESTRICT", nullable: bool = False, index: bool = True):
    return mapped_column(Integer, ForeignKey(target, ondelete=ondelete), nullable=nullable, index=index)


SESSION_TYPES = ("theory", "practical")
GROUP_TYPES = ("cohort", "section", "batch")
AVAIL_KINDS = ("available", "unavailable", "preferred")
VERSION_STATUSES = ("draft", "validated", "published", "archived")
RUN_STATUSES = ("queued", "running", "completed", "infeasible", "unknown", "failed", "cancelled")
REQUEST_TYPES = ("leave", "time_change")
REQUEST_STATUSES = ("pending", "approved", "rejected", "cancelled")


def _in(col: str, values) -> str:
    return f"{col} IN ({', '.join(repr(v) for v in values)})"


class Department(Base):
    __tablename__ = "departments"
    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(20), unique=True)
    name: Mapped[str] = mapped_column(String(120))


class AppRole(Base):
    __tablename__ = "app_roles"
    id: Mapped[int] = pk()
    code: Mapped[str] = mapped_column(String(20), unique=True)
    __table_args__ = (CheckConstraint(_in("code", ("admin", "faculty", "student"))),)


class AppUser(Base):
    __tablename__ = "app_users"
    id: Mapped[int] = pk()
    username: Mapped[str] = mapped_column(String(60), unique=True)
    full_name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(160), unique=True, nullable=True)
    password_hash: Mapped[str | None] = mapped_column(String(300), nullable=True)
    # Reserved for the upcoming Google sign-in / authenticator (TOTP) work.
    auth_provider: Mapped[str] = mapped_column(String(20), default="local")
    external_subject: Mapped[str | None] = mapped_column(String(200), nullable=True)
    mfa_secret_enc: Mapped[str | None] = mapped_column(String(300), nullable=True)
    mfa_enabled: Mapped[bool] = mapped_column(Boolean, default=False)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    faculty_id: Mapped[int | None] = fk("faculty_profiles.id", "SET NULL", nullable=True)
    student_group_id: Mapped[int | None] = fk("student_groups.id", "SET NULL", nullable=True)
    failed_logins: Mapped[int] = mapped_column(Integer, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AppUserRole(Base):
    __tablename__ = "app_user_roles"
    user_id: Mapped[int] = mapped_column(ForeignKey("app_users.id", ondelete="CASCADE"), primary_key=True)
    role_id: Mapped[int] = mapped_column(ForeignKey("app_roles.id", ondelete="CASCADE"), primary_key=True)


class AuthSession(Base):
    __tablename__ = "auth_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = fk("app_users.id", "CASCADE")
    csrf_token: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    last_seen: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime)


class AcademicTerm(Base):
    __tablename__ = "academic_terms"
    id: Mapped[int] = pk()
    department_id: Mapped[int] = fk("departments.id")
    name: Mapped[str] = mapped_column(String(80))
    start_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    end_date: Mapped[str | None] = mapped_column(String(10), nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("department_id", "name"),)


class Programme(Base):
    __tablename__ = "programmes"
    id: Mapped[int] = pk()
    department_id: Mapped[int] = fk("departments.id")
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(160))
    __table_args__ = (UniqueConstraint("department_id", "code"),)


class StudentGroup(Base):
    """Cohort/class, section or practical batch. Batches hang under their cohort via parent_group_id."""
    __tablename__ = "student_groups"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id")
    programme_id: Mapped[int] = fk("programmes.id")
    parent_group_id: Mapped[int | None] = fk("student_groups.id", "RESTRICT", nullable=True)
    name: Mapped[str] = mapped_column(String(80))
    semester: Mapped[int] = mapped_column(Integer, default=1)
    section: Mapped[str | None] = mapped_column(String(20), nullable=True)
    group_type: Mapped[str] = mapped_column(String(10), default="cohort")
    strength: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        UniqueConstraint("term_id", "name"),
        CheckConstraint("strength > 0"),
        CheckConstraint("semester BETWEEN 1 AND 12"),
        CheckConstraint(_in("group_type", GROUP_TYPES)),
        CheckConstraint("parent_group_id IS NULL OR parent_group_id <> id"),
    )


class FacultyProfile(Base):
    __tablename__ = "faculty_profiles"
    id: Mapped[int] = pk()
    department_id: Mapped[int] = fk("departments.id")
    code: Mapped[str] = mapped_column(String(20))
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str | None] = mapped_column(String(160), nullable=True)
    max_periods_per_day: Mapped[int | None] = mapped_column(Integer, nullable=True)
    max_periods_per_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (
        UniqueConstraint("department_id", "code"),
        CheckConstraint("max_periods_per_day IS NULL OR max_periods_per_day > 0"),
        CheckConstraint("max_periods_per_week IS NULL OR max_periods_per_week > 0"),
    )


class Subject(Base):
    """A subject OFFERING for one student group in one term, carrying its weekly requirements."""
    __tablename__ = "subjects"
    id: Mapped[int] = pk()
    department_id: Mapped[int] = fk("departments.id")
    term_id: Mapped[int] = fk("academic_terms.id")
    student_group_id: Mapped[int] = fk("student_groups.id")
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(160))
    subject_category: Mapped[str] = mapped_column(String(30), default="core")
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    theory_sessions_per_week: Mapped[int] = mapped_column(Integer, default=0)
    theory_periods_per_session: Mapped[int] = mapped_column(Integer, default=1)
    theory_required_room_type: Mapped[str | None] = mapped_column(String(30), nullable=True, default="classroom")
    theory_required_features: Mapped[list] = mapped_column(JSON, default=list)
    practical_sessions_per_week: Mapped[int] = mapped_column(Integer, default=0)
    practical_periods_per_session: Mapped[int] = mapped_column(Integer, default=2)
    practical_required_room_type: Mapped[str | None] = mapped_column(String(30), nullable=True, default="lab")
    practical_required_features: Mapped[list] = mapped_column(JSON, default=list)
    __table_args__ = (
        UniqueConstraint("term_id", "student_group_id", "code"),
        CheckConstraint("theory_sessions_per_week BETWEEN 0 AND 20"),
        CheckConstraint("practical_sessions_per_week BETWEEN 0 AND 20"),
        CheckConstraint("theory_periods_per_session BETWEEN 1 AND 8"),
        CheckConstraint("practical_periods_per_session BETWEEN 1 AND 8"),
    )


class TeachingAssignment(Base):
    """Who teaches which session type of a subject offering (not the weekly requirement itself)."""
    __tablename__ = "teaching_assignments"
    id: Mapped[int] = pk()
    subject_id: Mapped[int] = fk("subjects.id", "CASCADE")
    faculty_id: Mapped[int] = fk("faculty_profiles.id")
    session_type: Mapped[str] = mapped_column(String(10))
    __table_args__ = (UniqueConstraint("subject_id", "session_type"), CheckConstraint(_in("session_type", SESSION_TYPES)))


class Room(Base):
    __tablename__ = "rooms"
    id: Mapped[int] = pk()
    department_id: Mapped[int] = fk("departments.id")
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    room_type: Mapped[str] = mapped_column(String(30), default="classroom")
    capacity: Mapped[int] = mapped_column(Integer)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("department_id", "code"), CheckConstraint("capacity > 0"))


class RoomFeature(Base):
    """A feature that belongs to exactly one room (no mapping table)."""
    __tablename__ = "room_features"
    id: Mapped[int] = pk()
    room_id: Mapped[int] = fk("rooms.id", "CASCADE")
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(120))
    __table_args__ = (UniqueConstraint("room_id", "code"),)


class WorkingDay(Base):
    __tablename__ = "working_days"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id", "CASCADE")
    day_of_week: Mapped[int] = mapped_column(Integer)  # 0 = Monday
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("term_id", "day_of_week"), CheckConstraint("day_of_week BETWEEN 0 AND 6"))


class PeriodSlot(Base):
    __tablename__ = "period_slots"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id", "CASCADE")
    idx: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    label: Mapped[str | None] = mapped_column(String(40), nullable=True)
    __table_args__ = (
        UniqueConstraint("term_id", "idx"),
        UniqueConstraint("term_id", "start_minute"),
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute"),
        CheckConstraint("idx >= 0"),
    )


class ScheduledBreak(Base):
    __tablename__ = "scheduled_breaks"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id", "CASCADE")
    name: Mapped[str] = mapped_column(String(60))
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)  # NULL = every working day
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute"),
        CheckConstraint("day_of_week IS NULL OR day_of_week BETWEEN 0 AND 6"),
    )


class FacultyAvailability(Base):
    """kind=available: teaching allowed only inside these windows (none defined = all working hours);
    kind=unavailable: never teach here; kind=preferred: soft preference."""
    __tablename__ = "faculty_availability"
    id: Mapped[int] = pk()
    faculty_id: Mapped[int] = fk("faculty_profiles.id", "CASCADE")
    term_id: Mapped[int] = fk("academic_terms.id", "CASCADE")
    day_of_week: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(12), default="available")
    note: Mapped[str | None] = mapped_column(String(200), nullable=True)
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute"),
        CheckConstraint("day_of_week BETWEEN 0 AND 6"),
        CheckConstraint(_in("kind", AVAIL_KINDS)),
    )


class FacultyPreference(Base):
    """Self-service SOFT preferences, written only by the faculty member they belong to
    (one row per faculty member and term). slot_marks = {"<day>-<start_minute>": "prefer" | "avoid"}."""
    __tablename__ = "faculty_preferences"
    id: Mapped[int] = pk()
    faculty_id: Mapped[int] = fk("faculty_profiles.id", "CASCADE")
    term_id: Mapped[int] = fk("academic_terms.id", "CASCADE")
    slot_marks: Mapped[dict] = mapped_column(JSON, default=dict)
    avoid_first: Mapped[bool] = mapped_column(Boolean, default=False)
    avoid_last: Mapped[bool] = mapped_column(Boolean, default=False)
    spread_evenly: Mapped[bool] = mapped_column(Boolean, default=True)
    strength: Mapped[str] = mapped_column(String(10), default="normal")
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    __table_args__ = (UniqueConstraint("faculty_id", "term_id"), CheckConstraint(_in("strength", ("low", "normal", "high"))))


class FixedCommitment(Base):
    """With teaching_assignment_id: one session of that assignment is pinned to this slot (and room).
    Without it: the faculty member is simply busy (external commitment) during the window."""
    __tablename__ = "fixed_commitments"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id", "CASCADE")
    faculty_id: Mapped[int] = fk("faculty_profiles.id", "CASCADE")
    teaching_assignment_id: Mapped[int | None] = fk("teaching_assignments.id", "CASCADE", nullable=True)
    room_id: Mapped[int | None] = fk("rooms.id", "RESTRICT", nullable=True)
    title: Mapped[str] = mapped_column(String(120))
    day_of_week: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute"),
        CheckConstraint("day_of_week BETWEEN 0 AND 6"),
    )


class RuleSet(Base):
    __tablename__ = "rule_sets"
    id: Mapped[int] = pk()
    name: Mapped[str] = mapped_column(String(80), unique=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class RuleDefinition(Base):
    __tablename__ = "rule_definitions"
    id: Mapped[int] = pk()
    rule_set_id: Mapped[int] = fk("rule_sets.id", "CASCADE")
    rule_key: Mapped[str] = mapped_column(String(60))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    is_hard: Mapped[bool] = mapped_column(Boolean, default=False)
    weight: Mapped[int] = mapped_column(Integer, default=1)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    __table_args__ = (UniqueConstraint("rule_set_id", "rule_key"), CheckConstraint("weight BETWEEN 0 AND 1000"))


class TimetableRun(Base):
    __tablename__ = "timetable_runs"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id")
    rule_set_id: Mapped[int] = fk("rule_sets.id")
    rule_set_version: Mapped[int] = mapped_column(Integer, default=1)
    requested_by: Mapped[int | None] = fk("app_users.id", "SET NULL", nullable=True)
    kind: Mapped[str] = mapped_column(String(12), default="generate")
    status: Mapped[str] = mapped_column(String(12), default="queued")
    solver_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    input_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    params: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (CheckConstraint(_in("status", RUN_STATUSES)),
                      CheckConstraint(_in("kind", ("generate", "repair", "whatif"))))


class TimetableVersion(Base):
    __tablename__ = "timetable_versions"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id")
    run_id: Mapped[int | None] = fk("timetable_runs.id", "SET NULL", nullable=True)
    base_version_id: Mapped[int | None] = fk("timetable_versions.id", "SET NULL", nullable=True)
    label: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(12), default="draft")
    is_sandbox: Mapped[bool] = mapped_column(Boolean, default=False)
    scenario: Mapped[list] = mapped_column(JSON, default=list)
    row_version: Mapped[int] = mapped_column(Integer, default=1)
    score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    score_breakdown: Mapped[dict] = mapped_column(JSON, default=dict)
    summary: Mapped[dict] = mapped_column(JSON, default=dict)
    created_by: Mapped[int | None] = fk("app_users.id", "SET NULL", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    validated_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    published_by: Mapped[int | None] = fk("app_users.id", "SET NULL", nullable=True)
    __table_args__ = (
        CheckConstraint(_in("status", VERSION_STATUSES)),
        CheckConstraint("NOT (is_sandbox AND status IN ('published', 'archived'))"),
        # At most one live published version per term - enforced by the database itself.
        Index("uq_one_published_per_term", "term_id", unique=True, sqlite_where=text("status = 'published'")),
    )


class TimetableSession(Base):
    __tablename__ = "timetable_sessions"
    id: Mapped[int] = pk()
    version_id: Mapped[int] = fk("timetable_versions.id", "CASCADE")
    teaching_assignment_id: Mapped[int] = fk("teaching_assignments.id")
    room_id: Mapped[int] = fk("rooms.id")
    day_of_week: Mapped[int] = mapped_column(Integer)
    start_minute: Mapped[int] = mapped_column(Integer)
    end_minute: Mapped[int] = mapped_column(Integer)
    period_count: Mapped[int] = mapped_column(Integer)
    locked: Mapped[bool] = mapped_column(Boolean, default=False)
    source: Mapped[str] = mapped_column(String(12), default="generated")
    __table_args__ = (
        CheckConstraint("start_minute >= 0 AND end_minute <= 1440 AND start_minute < end_minute"),
        CheckConstraint("day_of_week BETWEEN 0 AND 6"),
        CheckConstraint("period_count > 0"),
        CheckConstraint(_in("source", ("generated", "manual", "fixed", "imported"))),
    )


class FacultyRequest(Base):
    __tablename__ = "faculty_requests"
    id: Mapped[int] = pk()
    term_id: Mapped[int] = fk("academic_terms.id")
    faculty_id: Mapped[int] = fk("faculty_profiles.id")
    requester_user_id: Mapped[int] = fk("app_users.id")
    request_type: Mapped[str] = mapped_column(String(12))
    reason: Mapped[str] = mapped_column(Text)
    session_id: Mapped[int | None] = fk("timetable_sessions.id", "SET NULL", nullable=True)
    session_label: Mapped[str | None] = mapped_column(String(200), nullable=True)
    leave_from: Mapped[str | None] = mapped_column(String(10), nullable=True)
    leave_to: Mapped[str | None] = mapped_column(String(10), nullable=True)
    day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposed_day_of_week: Mapped[int | None] = mapped_column(Integer, nullable=True)
    proposed_start_minute: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(12), default="pending")
    decision_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    reviewer_user_id: Mapped[int | None] = fk("app_users.id", "SET NULL", nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    __table_args__ = (CheckConstraint(_in("request_type", REQUEST_TYPES)), CheckConstraint(_in("status", REQUEST_STATUSES)))


class ConflictFinding(Base):
    __tablename__ = "conflict_findings"
    id: Mapped[int] = pk()
    run_id: Mapped[int | None] = fk("timetable_runs.id", "CASCADE", nullable=True)
    version_id: Mapped[int | None] = fk("timetable_versions.id", "CASCADE", nullable=True)
    rule_key: Mapped[str] = mapped_column(String(60))
    severity: Mapped[str] = mapped_column(String(10))  # error | warning | info
    category: Mapped[str] = mapped_column(String(20))  # preflight | proven | diagnostic | timeout | audit | suggestion
    message: Mapped[str] = mapped_column(Text)
    entities: Mapped[dict] = mapped_column(JSON, default=dict)
    evidence: Mapped[dict] = mapped_column(JSON, default=dict)
    suggestions: Mapped[list] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class AuditEvent(Base):
    """Append-only (UPDATE/DELETE are blocked by triggers)."""
    __tablename__ = "audit_events"
    id: Mapped[int] = pk()
    at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, index=True)
    actor_user_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    actor_name: Mapped[str | None] = mapped_column(String(60), nullable=True)
    action: Mapped[str] = mapped_column(String(60), index=True)
    entity: Mapped[str | None] = mapped_column(String(60), nullable=True)
    entity_id: Mapped[str | None] = mapped_column(String(40), nullable=True)
    outcome: Mapped[str] = mapped_column(String(10), default="ok")
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip: Mapped[str | None] = mapped_column(String(60), nullable=True)


class Notification(Base):
    __tablename__ = "notifications"
    id: Mapped[int] = pk()
    user_id: Mapped[int] = fk("app_users.id", "CASCADE")
    message: Mapped[str] = mapped_column(String(400))
    read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


_FROZEN = "(SELECT status FROM timetable_versions WHERE id = {ref}.version_id) IN ('published', 'archived')"

TRIGGERS = [
    # subjects.term_id must equal the term of its student group
    """CREATE TRIGGER IF NOT EXISTS trg_subject_term_ins BEFORE INSERT ON subjects
       WHEN (SELECT term_id FROM student_groups WHERE id = NEW.student_group_id) IS NOT NEW.term_id
       BEGIN SELECT RAISE(ABORT, 'subject term must match its student group term'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_subject_term_upd BEFORE UPDATE ON subjects
       WHEN (SELECT term_id FROM student_groups WHERE id = NEW.student_group_id) IS NOT NEW.term_id
       BEGIN SELECT RAISE(ABORT, 'subject term must match its student group term'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_group_term_upd BEFORE UPDATE OF term_id ON student_groups
       WHEN EXISTS (SELECT 1 FROM subjects WHERE student_group_id = NEW.id AND term_id IS NOT NEW.term_id)
       BEGIN SELECT RAISE(ABORT, 'group has subject offerings in another term'); END""",
    # audit log is append-only
    """CREATE TRIGGER IF NOT EXISTS trg_audit_no_update BEFORE UPDATE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_audit_no_delete BEFORE DELETE ON audit_events
       BEGIN SELECT RAISE(ABORT, 'audit_events is append-only'); END""",
    # sessions of published/archived versions are immutable
    f"""CREATE TRIGGER IF NOT EXISTS trg_sess_frozen_ins BEFORE INSERT ON timetable_sessions
       WHEN {_FROZEN.format(ref='NEW')}
       BEGIN SELECT RAISE(ABORT, 'published timetable versions are immutable'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS trg_sess_frozen_upd BEFORE UPDATE ON timetable_sessions
       WHEN {_FROZEN.format(ref='OLD')}
       BEGIN SELECT RAISE(ABORT, 'published timetable versions are immutable'); END""",
    f"""CREATE TRIGGER IF NOT EXISTS trg_sess_frozen_del BEFORE DELETE ON timetable_sessions
       WHEN {_FROZEN.format(ref='OLD')}
       BEGIN SELECT RAISE(ABORT, 'published timetable versions are immutable'); END""",
    """CREATE TRIGGER IF NOT EXISTS trg_version_no_delete BEFORE DELETE ON timetable_versions
       WHEN OLD.status IN ('published', 'archived')
       BEGIN SELECT RAISE(ABORT, 'published timetable history cannot be deleted'); END""",
]


def install_schema(conn) -> None:
    """Create all tables, indexes and triggers. Used by the Alembic initial migration and by tests."""
    Base.metadata.create_all(conn)
    for ddl in TRIGGERS:
        conn.execute(text(ddl))
    for code in ("admin", "faculty", "student"):
        conn.execute(text("INSERT OR IGNORE INTO app_roles (code) VALUES (:c)"), {"c": code})
