"""Explicit request DTOs. Every model forbids unknown fields, which blocks mass assignment of
protected attributes (role, status, published_by, row/audit metadata, ...)."""
from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from .models import AVAIL_KINDS, GROUP_TYPES, SESSION_TYPES


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    @field_validator("*", mode="after")
    @classmethod
    def _no_control_chars(cls, v):
        if isinstance(v, str) and any(ord(c) < 32 and c not in "\n\t" for c in v):
            raise ValueError("control characters are not allowed")
        return v


Code = Annotated[str, StringConstraints(min_length=1, max_length=30, pattern=r"^[A-Za-z0-9][A-Za-z0-9 _./&()\-]*$")]
Name = Annotated[str, StringConstraints(min_length=1, max_length=160)]
Short = Annotated[str, StringConstraints(min_length=1, max_length=80)]
TypeCode = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{1,29}$")]
Minute = Annotated[int, Field(ge=0, le=1440)]
Day = Annotated[int, Field(ge=0, le=6)]
Id = Annotated[int, Field(ge=1)]
DateStr = Annotated[str, StringConstraints(pattern=r"^\d{4}-\d{2}-\d{2}$")]
Email = Annotated[str, StringConstraints(max_length=160, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
Features = Annotated[list[TypeCode], Field(max_length=20)]


class Window(Strict):
    @model_validator(mode="after")
    def _order(self):
        if getattr(self, "start_minute", 0) >= getattr(self, "end_minute", 1):
            raise ValueError("start time must be before end time")
        return self


class DepartmentIn(Strict):
    code: Code
    name: Name


class TermIn(Strict):
    department_id: Id
    name: Short
    start_date: DateStr | None = None
    end_date: DateStr | None = None
    active: bool = True


class ProgrammeIn(Strict):
    department_id: Id
    code: Code
    name: Name


class GroupIn(Strict):
    term_id: Id
    programme_id: Id
    parent_group_id: Id | None = None
    name: Short
    semester: Annotated[int, Field(ge=1, le=12)] = 1
    section: Annotated[str, StringConstraints(max_length=20)] | None = None
    group_type: Literal[GROUP_TYPES] = "cohort"  # type: ignore[valid-type]
    strength: Annotated[int, Field(ge=1, le=5000)]


class FacultyIn(Strict):
    department_id: Id
    code: Code
    name: Name
    email: Email | None = None
    max_periods_per_day: Annotated[int, Field(ge=1, le=20)] | None = None
    max_periods_per_week: Annotated[int, Field(ge=1, le=100)] | None = None
    active: bool = True


class SubjectIn(Strict):
    department_id: Id
    term_id: Id
    student_group_id: Id
    code: Code
    name: Name
    subject_category: TypeCode = "core"
    active: bool = True
    theory_sessions_per_week: Annotated[int, Field(ge=0, le=20)] = 0
    theory_periods_per_session: Annotated[int, Field(ge=1, le=8)] = 1
    theory_required_room_type: TypeCode | None = "classroom"
    theory_required_features: Features = []
    practical_sessions_per_week: Annotated[int, Field(ge=0, le=20)] = 0
    practical_periods_per_session: Annotated[int, Field(ge=1, le=8)] = 2
    practical_required_room_type: TypeCode | None = "lab"
    practical_required_features: Features = []


class AssignmentIn(Strict):
    subject_id: Id
    faculty_id: Id
    session_type: Literal[SESSION_TYPES]  # type: ignore[valid-type]


class RoomIn(Strict):
    department_id: Id
    code: Code
    name: Name
    room_type: TypeCode = "classroom"
    capacity: Annotated[int, Field(ge=1, le=5000)]
    active: bool = True


class RoomFeatureIn(Strict):
    room_id: Id
    code: TypeCode
    name: Name


class WorkingDayIn(Strict):
    term_id: Id
    day_of_week: Day
    active: bool = True


class PeriodIn(Window):
    term_id: Id
    idx: Annotated[int, Field(ge=0, le=30)]
    start_minute: Minute
    end_minute: Minute
    label: Annotated[str, StringConstraints(max_length=40)] | None = None


class BreakIn(Window):
    term_id: Id
    name: Annotated[str, StringConstraints(min_length=1, max_length=60)]
    day_of_week: Day | None = None
    start_minute: Minute
    end_minute: Minute


class AvailabilityIn(Window):
    faculty_id: Id
    term_id: Id
    day_of_week: Day
    start_minute: Minute
    end_minute: Minute
    kind: Literal[AVAIL_KINDS] = "available"  # type: ignore[valid-type]
    note: Annotated[str, StringConstraints(max_length=200)] | None = None


class FixedIn(Window):
    term_id: Id
    faculty_id: Id
    teaching_assignment_id: Id | None = None
    room_id: Id | None = None
    title: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    day_of_week: Day
    start_minute: Minute
    end_minute: Minute


class ImportIn(Strict):
    csv: Annotated[str, StringConstraints(min_length=1, max_length=500_000)]
    dry_run: bool = True


# ---- rules / users ----------------------------------------------------------------------------
class RuleSetIn(Strict):
    name: Short
    copy_from: Id | None = None


class RuleSetPatch(Strict):
    name: Short | None = None
    active: bool | None = None


class RuleConfigIn(Strict):
    enabled: bool
    is_hard: bool
    weight: Annotated[int, Field(ge=0, le=1000)]
    params: dict[str, int | None] = {}


Role = Literal["admin", "faculty", "student"]
Username = Annotated[str, StringConstraints(min_length=3, max_length=60, pattern=r"^[A-Za-z0-9._-]+$")]
Password = Annotated[str, StringConstraints(min_length=1, max_length=200, strip_whitespace=False)]


class UserIn(Strict):
    username: Username
    full_name: Annotated[str, StringConstraints(min_length=1, max_length=120)]
    email: Email | None = None
    password: Password | None = None  # omit for accounts that sign in through Clerk (matched by e-mail)
    roles: Annotated[list[Role], Field(min_length=1, max_length=3)]
    faculty_id: Id | None = None
    student_group_id: Id | None = None


class UserPatch(Strict):
    full_name: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    email: Email | None = None
    active: bool | None = None
    password: Password | None = None
    roles: Annotated[list[Role], Field(min_length=1, max_length=3)] | None = None
    faculty_id: Id | None = None
    student_group_id: Id | None = None
    clear_links: bool = False


class LoginIn(Strict):
    username: Annotated[str, StringConstraints(min_length=1, max_length=160)]
    password: Password


class ClerkLoginIn(Strict):
    token: Annotated[str, StringConstraints(min_length=20, max_length=4000, pattern=r"^[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+$")]


class PasswordChangeIn(Strict):
    current_password: Password
    new_password: Password


# ---- runs / versions / sessions ---------------------------------------------------------------
class ScenarioChange(Strict):
    type: Literal["faculty_unavailable", "room_unavailable", "strength_change", "extra_sessions", "rule_override"]
    faculty_id: Id | None = None
    room_id: Id | None = None
    group_id: Id | None = None
    subject_id: Id | None = None
    day: Day | None = None
    strength: Annotated[int, Field(ge=1, le=5000)] | None = None
    session_type: Literal[SESSION_TYPES] | None = None  # type: ignore[valid-type]
    delta: Annotated[int, Field(ge=-10, le=10)] | None = None
    rule_key: Annotated[str, StringConstraints(max_length=60)] | None = None
    enabled: bool | None = None
    is_hard: bool | None = None
    weight: Annotated[int, Field(ge=0, le=1000)] | None = None
    params: dict[str, int | None] | None = None


class PreflightIn(Strict):
    term_id: Id
    rule_set_id: Id | None = None


class RunIn(Strict):
    term_id: Id
    rule_set_id: Id | None = None
    kind: Literal["generate", "repair", "whatif"] = "generate"
    base_version_id: Id | None = None
    time_limit: Annotated[int, Field(ge=1, le=300)] = 20
    candidates: Annotated[int, Field(ge=1, le=3)] = 1
    scenario: Annotated[list[ScenarioChange], Field(max_length=20)] = []
    sandbox: bool = False


class PublishIn(Strict):
    row_version: Annotated[int, Field(ge=1)]


class CloneIn(Strict):
    label: Annotated[str, StringConstraints(min_length=1, max_length=120)] | None = None
    sandbox: bool = False


class SessionMove(Strict):
    day_of_week: Day
    start_minute: Minute
    room_id: Id
    row_version: Annotated[int, Field(ge=1)]


class SessionCreate(SessionMove):
    teaching_assignment_id: Id


class LockIn(Strict):
    locked: bool
    row_version: Annotated[int, Field(ge=1)]


# ---- faculty requests -------------------------------------------------------------------------
class RequestIn(Strict):
    term_id: Id
    request_type: Literal["leave", "time_change"]
    reason: Annotated[str, StringConstraints(min_length=5, max_length=1000)]
    session_id: Id | None = None
    leave_from: DateStr | None = None
    leave_to: DateStr | None = None
    day_of_week: Day | None = None
    proposed_day_of_week: Day | None = None
    proposed_start_minute: Minute | None = None

    @model_validator(mode="after")
    def _shape(self):
        if self.request_type == "leave" and self.day_of_week is None:
            raise ValueError("a leave request needs the affected weekday")
        if self.request_type == "time_change" and self.session_id is None:
            raise ValueError("a time-change request must reference one of your sessions")
        if self.leave_from and self.leave_to and self.leave_from > self.leave_to:
            raise ValueError("leave_from must not be after leave_to")
        return self


class PrefMark(Strict):
    day_of_week: Day
    start_minute: Minute
    mark: Literal["prefer", "avoid"]


class PreferencesIn(Strict):
    """A faculty member's own soft preferences. There is deliberately no faculty_id field:
    the owner always comes from the signed-in session."""
    term_id: Id
    marks: Annotated[list[PrefMark], Field(max_length=300)] = []
    avoid_first: bool = False
    avoid_last: bool = False
    spread_evenly: bool = True
    strength: Literal["low", "normal", "high"] = "normal"
    notes: Annotated[str, StringConstraints(max_length=1000)] = ""


class DecisionIn(Strict):
    decision: Literal["approved", "rejected"]
    note: Annotated[str, StringConstraints(max_length=1000)] = ""
    apply_unavailability: bool = False
