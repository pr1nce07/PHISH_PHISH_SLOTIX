"""Plain-data snapshot of everything the scheduler needs, decoupled from the ORM.

The same `Problem` object feeds the preflight validator, the CP-SAT model, the independent
auditor, the scorer and the what-if sandbox (which mutates a copy, never the database).
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from .. import models as m
from .rules import REGISTRY, validate_rule_config

DAY_NAMES = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"]


def fmt(minute: int) -> str:
    return f"{minute // 60:02d}:{minute % 60:02d}"


@dataclass
class Period:
    idx: int
    start: int
    end: int
    label: str


@dataclass
class GroupD:
    id: int
    name: str
    parent_id: int | None
    strength: int
    group_type: str


@dataclass
class RoomD:
    id: int
    code: str
    name: str
    room_type: str
    capacity: int
    features: list[str]
    active: bool


@dataclass
class FacultyD:
    id: int
    code: str
    name: str
    active: bool
    max_day: int | None
    max_week: int | None
    available: list[tuple[int, int, int]] = field(default_factory=list)
    unavailable: list[tuple[int, int, int]] = field(default_factory=list)
    preferred: list[tuple[int, int, int]] = field(default_factory=list)
    # submitted self-service preferences: {"marks": {"day-start": "prefer"|"avoid"}, "avoid_first", "avoid_last", "spread", "strength"}
    prefs: dict | None = None


@dataclass
class SubjectD:
    id: int
    code: str
    name: str
    group_id: int
    # session_type -> {"sessions": n, "periods": n, "room_type": str|None, "features": [codes]}
    req: dict[str, dict]


@dataclass
class AssignD:
    id: int
    subject_id: int
    faculty_id: int
    session_type: str


@dataclass
class FixedD:
    id: int
    faculty_id: int
    assignment_id: int | None
    day: int
    start: int
    end: int
    room_id: int | None
    title: str


@dataclass
class RuleD:
    key: str
    enabled: bool
    is_hard: bool
    weight: int
    params: dict


@dataclass
class Need:
    """One session that must be placed."""
    key: str
    assignment_id: int
    index: int
    subject_id: int
    session_type: str
    group_id: int
    faculty_id: int
    length: int
    room_type: str | None
    features: list[str]


@dataclass
class Slot:
    """One placed session (minutes are authoritative; period indexes are derived)."""
    assignment_id: int
    day: int
    start: int
    end: int
    room_id: int
    locked: bool = False
    id: int | None = None
    source: str = "generated"


@dataclass
class Problem:
    term_id: int
    days: list[int]
    periods: list[Period]
    breaks: list[tuple[str, int | None, int, int]]
    groups: dict[int, GroupD]
    rooms: dict[int, RoomD]
    faculty: dict[int, FacultyD]
    subjects: dict[int, SubjectD]
    assignments: dict[int, AssignD]
    fixed: list[FixedD]
    rules: dict[str, RuleD]
    issues: list[dict] = field(default_factory=list)
    needs: list[Need] = field(default_factory=list)

    # ---- structure helpers -------------------------------------------------
    def build_needs(self) -> None:
        by_key = {(a.subject_id, a.session_type): a for a in self.assignments.values()}
        self.needs = []
        for s in sorted(self.subjects.values(), key=lambda x: x.id):
            for stype, r in s.req.items():
                a = by_key.get((s.id, stype))
                if r["sessions"] <= 0 or a is None:
                    continue
                for k in range(r["sessions"]):
                    self.needs.append(Need(f"{a.id}:{k}", a.id, k, s.id, stype, s.group_id, a.faculty_id,
                                           r["periods"], r["room_type"], list(r["features"])))

    def required(self, assignment_id: int) -> tuple[int, int]:
        """(sessions per week, periods per session) for an assignment; (0, 0) when unknown."""
        a = self.assignments.get(assignment_id)
        s = self.subjects.get(a.subject_id) if a else None
        if not s:
            return 0, 0
        r = s.req[a.session_type]
        return r["sessions"], r["periods"]

    def ancestors(self, gid: int) -> list[int]:
        out, seen, cur = [], {gid}, self.groups.get(gid)
        while cur and cur.parent_id is not None and cur.parent_id not in seen:
            out.append(cur.parent_id)
            seen.add(cur.parent_id)
            cur = self.groups.get(cur.parent_id)
        return out

    def related(self, g1: int, g2: int) -> bool:
        """True when the two groups share students (same group, or ancestor/descendant at any depth)."""
        return g1 == g2 or g1 in self.ancestors(g2) or g2 in self.ancestors(g1)

    def family(self, gid: int) -> set[int]:
        return {g for g in self.groups if self.related(g, gid)}

    def leaf_groups(self) -> list[int]:
        parents = {g.parent_id for g in self.groups.values()}
        return [g for g in self.groups if g not in parents]

    # ---- time helpers ------------------------------------------------------
    def span(self, p: int, length: int) -> tuple[int, int] | None:
        """(start, end) minutes of `length` consecutive periods from index p, or None if not contiguous."""
        if p < 0 or length <= 0 or p + length > len(self.periods):
            return None
        for i in range(p, p + length - 1):
            if self.periods[i].end != self.periods[i + 1].start:
                return None
        return self.periods[p].start, self.periods[p + length - 1].end

    def indices(self, start: int, end: int) -> list[int] | None:
        """Period indexes exactly covering [start, end), or None when not aligned/consecutive."""
        i = next((p.idx for p in self.periods if p.start == start), None)
        if i is None:
            return None
        out = []
        while i < len(self.periods):
            out.append(i)
            p = self.periods[i]
            if p.end == end:
                return out
            if p.end > end or i + 1 >= len(self.periods) or self.periods[i + 1].start != p.end:
                return None
            i += 1
        return None

    def break_hit(self, day: int, start: int, end: int) -> str | None:
        for name, d, a, b in self.breaks:
            if (d is None or d == day) and a < end and start < b:
                return name
        return None

    # ---- eligibility helpers (shared by preflight, engine and auditor) -----
    def faculty_issue(self, fid: int, day: int, start: int, end: int) -> str | None:
        f = self.faculty.get(fid)
        if f is None:
            return "the assigned faculty member does not exist"
        if not f.active:
            return f"{f.name} is inactive"
        for d, a, b in f.unavailable:
            if d == day and a < end and start < b:
                return f"{f.name} is unavailable on {DAY_NAMES[day]} {fmt(a)}-{fmt(b)}"
        if f.available:
            wins = sorted((a, b) for d, a, b in f.available if d == day)
            merged: list[list[int]] = []
            for a, b in wins:
                if merged and a <= merged[-1][1]:
                    merged[-1][1] = max(merged[-1][1], b)
                else:
                    merged.append([a, b])
            if not any(a <= start and end <= b for a, b in merged):
                return f"{f.name} is not available on {DAY_NAMES[day]} {fmt(start)}-{fmt(end)}"
        for fx in self.fixed:
            if fx.assignment_id is None and fx.faculty_id == fid and fx.day == day and fx.start < end and start < fx.end:
                return f"{f.name} has the fixed commitment '{fx.title}' on {DAY_NAMES[day]} {fmt(fx.start)}-{fmt(fx.end)}"
        return None

    def room_issues(self, group_id: int, room_type: str | None, features: list[str], room_id: int) -> list[tuple[str, str]]:
        r = self.rooms.get(room_id)
        if r is None:
            return [("room_suitability", "the room does not exist in this department")]
        g = self.groups.get(group_id)
        out = []
        if not r.active:
            out.append(("room_suitability", f"room {r.code} is not available (inactive)"))
        if room_type and r.room_type != room_type:
            out.append(("room_suitability", f"room {r.code} is a '{r.room_type}', but a '{room_type}' is required"))
        missing = sorted(set(features) - set(r.features))
        if missing:
            out.append(("room_suitability", f"room {r.code} lacks required feature(s): {', '.join(missing)}"))
        if g and r.capacity < g.strength:
            out.append(("room_capacity", f"room {r.code} seats {r.capacity}, but {g.name} has {g.strength} students"))
        return out

    def blocked_slot(self, day: int, idxs: list[int]) -> bool:
        r = self.rule("no_teaching_slot")
        return bool(r and (r.params.get("day") is None or r.params["day"] == day) and r.params["period_idx"] in idxs)

    def time_options(self, need: Need, relax: frozenset = frozenset()) -> list[tuple[int, int]]:
        """Feasible (day, start period index) pairs ignoring other sessions."""
        out = []
        for d in self.days:
            for p in range(len(self.periods)):
                sp = self.span(p, need.length)
                if sp is None or self.break_hit(d, *sp):
                    continue
                if "faculty_availability" not in relax and self.faculty_issue(need.faculty_id, d, *sp):
                    continue
                if "custom_rules" not in relax and self.blocked_slot(d, list(range(p, p + need.length))):
                    continue
                out.append((d, p))
        return out

    def room_options(self, need: Need, relax: frozenset = frozenset()) -> list[int]:
        out = []
        for rid in sorted(self.rooms):
            issues = self.room_issues(need.group_id, need.room_type, need.features, rid)
            if "room_capacity" in relax:
                issues = [i for i in issues if i[0] != "room_capacity"]
            if "room_suitability" in relax:
                issues = [i for i in issues if i[0] != "room_suitability" or "inactive" in i[1]]
            if not issues:
                out.append(rid)
        return out

    def rule(self, key: str) -> RuleD | None:
        r = self.rules.get(key)
        return r if r and r.enabled else None

    # ---- labels ------------------------------------------------------------
    def need_label(self, n: Need) -> str:
        s, g = self.subjects[n.subject_id], self.groups.get(n.group_id)
        return f"{s.code} {n.session_type} #{n.index + 1} for {g.name if g else '?'}"

    def slot_label(self, s: Slot) -> str:
        a = self.assignments.get(s.assignment_id)
        sub = self.subjects.get(a.subject_id) if a else None
        g = self.groups.get(sub.group_id) if sub else None
        return (f"{sub.code if sub else '?'} {a.session_type if a else ''} ({g.name if g else '?'}) "
                f"{DAY_NAMES[s.day][:3]} {fmt(s.start)}-{fmt(s.end)}")

    def snapshot_hash(self) -> str:
        data = asdict(self)
        data.pop("needs", None)
        return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()


def load_problem(db: Session, term_id: int, rule_set_id: int | None) -> Problem:
    term = db.get(m.AcademicTerm, term_id)
    if term is None:
        raise LookupError("term not found")
    issues: list[dict] = []
    days = sorted(d.day_of_week for d in db.scalars(select(m.WorkingDay).where(
        m.WorkingDay.term_id == term_id, m.WorkingDay.active.is_(True))))
    slots = sorted(db.scalars(select(m.PeriodSlot).where(m.PeriodSlot.term_id == term_id)), key=lambda p: p.start_minute)
    periods = [Period(i, p.start_minute, p.end_minute, p.label or f"P{i + 1}") for i, p in enumerate(slots)]
    breaks = [(b.name, b.day_of_week, b.start_minute, b.end_minute)
              for b in db.scalars(select(m.ScheduledBreak).where(m.ScheduledBreak.term_id == term_id))]
    groups = {g.id: GroupD(g.id, g.name, g.parent_group_id, g.strength, g.group_type)
              for g in db.scalars(select(m.StudentGroup).where(m.StudentGroup.term_id == term_id))}
    feats: dict[int, list[str]] = {}
    for f in db.scalars(select(m.RoomFeature)):
        feats.setdefault(f.room_id, []).append(f.code)
    rooms = {r.id: RoomD(r.id, r.code, r.name, r.room_type, r.capacity, sorted(feats.get(r.id, [])), r.active)
             for r in db.scalars(select(m.Room).where(m.Room.department_id == term.department_id))}
    faculty = {f.id: FacultyD(f.id, f.code, f.name, f.active, f.max_periods_per_day, f.max_periods_per_week)
               for f in db.scalars(select(m.FacultyProfile))}
    for av in db.scalars(select(m.FacultyAvailability).where(m.FacultyAvailability.term_id == term_id)):
        if av.faculty_id in faculty:
            getattr(faculty[av.faculty_id], av.kind).append((av.day_of_week, av.start_minute, av.end_minute))
    for pr in db.scalars(select(m.FacultyPreference).where(m.FacultyPreference.term_id == term_id,
                                                           m.FacultyPreference.submitted_at.is_not(None))):
        if pr.faculty_id in faculty:
            faculty[pr.faculty_id].prefs = {"marks": dict(pr.slot_marks or {}), "avoid_first": pr.avoid_first, "avoid_last": pr.avoid_last,
                                            "spread": pr.spread_evenly, "strength": pr.strength}
    subjects: dict[int, SubjectD] = {}
    for s in db.scalars(select(m.Subject).where(m.Subject.term_id == term_id, m.Subject.active.is_(True))):
        if s.student_group_id not in groups:
            issues.append({"rule_key": "valid_assignment", "message": f"Subject {s.code} refers to a student group outside this term.",
                           "entities": {"subject_id": s.id}})
            continue
        subjects[s.id] = SubjectD(s.id, s.code, s.name, s.student_group_id, {
            "theory": {"sessions": s.theory_sessions_per_week, "periods": s.theory_periods_per_session,
                       "room_type": s.theory_required_room_type, "features": list(s.theory_required_features or [])},
            "practical": {"sessions": s.practical_sessions_per_week, "periods": s.practical_periods_per_session,
                          "room_type": s.practical_required_room_type, "features": list(s.practical_required_features or [])},
        })
    assignments = {}
    if subjects:
        for a in db.scalars(select(m.TeachingAssignment).where(m.TeachingAssignment.subject_id.in_(subjects))):
            assignments[a.id] = AssignD(a.id, a.subject_id, a.faculty_id, a.session_type)
    have = {(a.subject_id, a.session_type) for a in assignments.values()}
    for s in subjects.values():
        for stype, r in s.req.items():
            if r["sessions"] > 0 and (s.id, stype) not in have:
                issues.append({"rule_key": "valid_assignment",
                               "message": f"Subject {s.code} ({groups[s.group_id].name}) needs {r['sessions']} {stype} session(s) "
                                          f"per week but has no faculty assigned for {stype}.",
                               "entities": {"subject_id": s.id, "group_id": s.group_id},
                               "suggestions": [f"Add a {stype} teaching assignment for {s.code}."]})
    fixed = [FixedD(f.id, f.faculty_id, f.teaching_assignment_id, f.day_of_week, f.start_minute, f.end_minute, f.room_id, f.title)
             for f in db.scalars(select(m.FixedCommitment).where(m.FixedCommitment.term_id == term_id))]
    rules: dict[str, RuleD] = {}
    if rule_set_id is not None:
        for r in db.scalars(select(m.RuleDefinition).where(m.RuleDefinition.rule_set_id == rule_set_id)):
            if r.rule_key not in REGISTRY:
                continue  # unknown rule types are never executed
            try:
                c = validate_rule_config(r.rule_key, r.enabled, r.is_hard, r.weight, r.params or {})
            except ValueError as exc:
                issues.append({"rule_key": "rule_config", "message": f"Rule '{r.rule_key}' has an invalid configuration: {exc}",
                               "entities": {"rule_key": r.rule_key}})
                continue
            rules[r.rule_key] = RuleD(c["rule_key"], c["enabled"], c["is_hard"], c["weight"], c["params"])
    prob = Problem(term_id, days, periods, breaks, groups, rooms, faculty, subjects, assignments, fixed, rules, issues)
    prob.build_needs()
    return prob


SCENARIO_TYPES = ("faculty_unavailable", "room_unavailable", "strength_change", "extra_sessions", "rule_override")


def apply_scenario(p: Problem, scenario: list[dict]) -> list[str]:
    """Mutate an in-memory Problem for a what-if sandbox. Returns human-readable descriptions.
    Raises ValueError on anything that is not an allow-listed, well-formed change."""
    notes = []
    for ch in scenario or []:
        t = ch.get("type")
        if t == "faculty_unavailable":
            f = p.faculty.get(ch.get("faculty_id"))
            if f is None:
                raise ValueError("unknown faculty_id in scenario")
            day = ch.get("day")
            if day is not None and day not in range(7):
                raise ValueError("day must be 0-6")
            for d in ([day] if day is not None else p.days):
                f.unavailable.append((d, 0, 1440))
            notes.append(f"{f.name} unavailable on {DAY_NAMES[day] if day is not None else 'all days'}")
        elif t == "room_unavailable":
            r = p.rooms.get(ch.get("room_id"))
            if r is None:
                raise ValueError("unknown room_id in scenario")
            r.active = False
            notes.append(f"Room {r.code} unavailable")
        elif t == "strength_change":
            g = p.groups.get(ch.get("group_id"))
            s = ch.get("strength")
            if g is None or not isinstance(s, int) or not 1 <= s <= 5000:
                raise ValueError("strength_change needs a valid group_id and strength (1-5000)")
            notes.append(f"{g.name} strength {g.strength} → {s}")
            g.strength = s
        elif t == "extra_sessions":
            s = p.subjects.get(ch.get("subject_id"))
            st, delta = ch.get("session_type"), ch.get("delta")
            if s is None or st not in ("theory", "practical") or not isinstance(delta, int) or not -10 <= delta <= 10:
                raise ValueError("extra_sessions needs subject_id, session_type and delta (-10..10)")
            s.req[st]["sessions"] = max(0, s.req[st]["sessions"] + delta)
            notes.append(f"{s.code} {st} sessions/week {delta:+d}")
        elif t == "rule_override":
            c = validate_rule_config(ch.get("rule_key"), ch.get("enabled", True), ch.get("is_hard", False),
                                     ch.get("weight", 1), ch.get("params") or {})
            p.rules[c["rule_key"]] = RuleD(c["rule_key"], c["enabled"], c["is_hard"], c["weight"], c["params"])
            notes.append(f"Rule {c['rule_key']} → {'hard' if c['is_hard'] else 'weight ' + str(c['weight'])}"
                         f"{'' if c['enabled'] else ' (disabled)'}")
        else:
            raise ValueError(f"unsupported scenario type '{t}'")
    p.build_needs()
    return notes


def match_slots(problem: Problem, slots: list[Slot]) -> tuple[dict[str, Slot], list[Slot]]:
    """Pair existing sessions with the needs of their assignment (locked first, then by time).
    Returns ({need_key: slot}, leftovers that no longer correspond to any requirement)."""
    by_a: dict[int, list[Slot]] = {}
    for s in slots:
        by_a.setdefault(s.assignment_id, []).append(s)
    needs_by_a: dict[int, list[Need]] = {}
    for n in problem.needs:
        needs_by_a.setdefault(n.assignment_id, []).append(n)
    out, left = {}, []
    for aid, lst in by_a.items():
        lst.sort(key=lambda s: (not s.locked, s.day, s.start, s.room_id))
        ns = needs_by_a.get(aid, [])
        for i, s in enumerate(lst):
            if i < len(ns):
                out[ns[i].key] = s
            else:
                left.append(s)
    return out, left
