"""Typed, allow-listed rule registry.

Rules are configured with structured, validated parameters only. Nothing here (or anywhere else)
evaluates administrator-supplied code or expressions.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Mandatory hard constraints. They are always enforced and cannot be disabled, demoted or weighted.
CORE_HARD = {
    "within_hours": "Sessions must fall on a working day and exactly on configured periods.",
    "break_overlap": "Sessions must not overlap a scheduled break.",
    "faculty_availability": "Faculty teach only inside their availability and outside unavailable/fixed slots.",
    "faculty_overlap": "A faculty member cannot teach two sessions at the same time.",
    "room_overlap": "A room or laboratory hosts at most one session at a time.",
    "group_overlap": "A student group (including its parent class and its batches) attends one session at a time.",
    "room_capacity": "Room seating capacity must cover the strength of the class or batch.",
    "room_suitability": "The room must be active, of the required type and have every required feature.",
    "consecutive_periods": "A session occupies its required number of consecutive periods.",
    "weekly_requirement": "Every subject offering receives exactly its weekly theory and practical sessions.",
    "fixed_commitment": "Fixed teaching commitments stay in their slot (and room when given).",
    "locked_session": "Locked sessions do not move during regeneration or repair.",
    "faculty_workload": "Faculty daily/weekly teaching-period limits are respected.",
    "valid_assignment": "Every session needs a consistent subject/group/term/faculty assignment.",
}


@dataclass(frozen=True)
class Param:
    kind: str  # "int"
    minimum: int
    maximum: int
    default: int | None
    nullable: bool = False
    help: str = ""


@dataclass(frozen=True)
class RuleSpec:
    key: str
    name: str
    description: str
    modes: tuple[str, ...]  # subset of ("hard", "soft")
    default_enabled: bool = True
    default_hard: bool = False
    default_weight: int = 1
    params: dict[str, Param] = field(default_factory=dict)
    version: int = 1


_P = Param
REGISTRY: dict[str, RuleSpec] = {s.key: s for s in [
    RuleSpec("spread_subject_days", "Spread a subject across days",
             "Repeated sessions of the same subject should fall on different days.", ("soft", "hard"), True, False, 6),
    RuleSpec("student_gaps", "Avoid student gaps",
             "Penalises each extra block of teaching (a free gap between classes) in a group's day.", ("soft",), True, False, 4),
    RuleSpec("faculty_gaps", "Avoid faculty idle gaps",
             "Penalises each extra block of teaching in a faculty member's day.", ("soft",), True, False, 2),
    RuleSpec("balance_faculty_load", "Balance faculty load across days",
             "Penalises periods above a faculty member's even daily share.", ("soft",), True, False, 3),
    RuleSpec("faculty_preferred_slots", "Faculty preferred slots",
             "Penalises periods taught outside a faculty member's preferred windows.", ("soft",), True, False, 3),
    RuleSpec("faculty_self_preferences", "Faculty self-service preferences",
             "Slots a faculty member marked 'avoid' or did not mark 'preferred', their first/last-period and "
             "spread-the-week requests, scaled by the strength they chose (low 1x, normal 2x, high 4x).", ("soft",), True, False, 2),
    RuleSpec("avoid_late_slots", "Avoid late slots", "Penalises periods at or after the given period index.",
             ("soft",), True, False, 1, {"from_idx": _P("int", 0, 30, 5, help="First period index considered late (0-based)")}),
    RuleSpec("avoid_early_slots", "Avoid early slots", "Penalises periods at or before the given period index.",
             ("soft",), False, False, 1, {"until_idx": _P("int", 0, 30, 0, help="Last period index considered early (0-based)")}),
    RuleSpec("practical_preferred_window", "Preferred laboratory window",
             "Penalises practical periods outside the preferred period-index window.", ("soft",), False, False, 2,
             {"from_idx": _P("int", 0, 30, 3), "to_idx": _P("int", 0, 30, 5)}),
    RuleSpec("room_fit", "Right-sized rooms", "Penalises unused seats (never overrides the capacity rule).",
             ("soft",), True, False, 1, {"seats_per_point": _P("int", 1, 500, 20, help="Unused seats per penalty point")}),
    RuleSpec("room_stability", "Keep a subject in one room",
             "Penalises each additional room used by the theory sessions of one subject offering.", ("soft",), True, False, 2),
    RuleSpec("max_faculty_consecutive", "Limit consecutive teaching periods",
             "A faculty member should not teach more than N periods in a row.", ("soft", "hard"), True, False, 4,
             {"max": _P("int", 1, 12, 3)}),
    RuleSpec("max_group_periods_per_day", "Daily period limit per student group",
             "A student group attends at most N periods per day.", ("hard",), False, True, 0, {"max": _P("int", 1, 20, 6)}),
    RuleSpec("no_teaching_slot", "Blocked teaching slot",
             "No session may use the given period (on one day, or on every day when day is empty).", ("hard",), False, True, 0,
             {"day": _P("int", 0, 6, None, nullable=True, help="0=Mon … 6=Sun, empty = every day"),
              "period_idx": _P("int", 0, 30, 0)}),
    RuleSpec("minimal_change", "Minimise changes during repair",
             "During repair/what-if, penalises every session moved away from the base timetable.", ("soft",), True, False, 40),
]}


def validate_rule_config(key: str, enabled: bool, is_hard: bool, weight: int, params: dict) -> dict:
    """Return a cleaned config or raise ValueError. Unknown keys/params are rejected (allow-list)."""
    spec = REGISTRY.get(key)
    if spec is None:
        raise ValueError(f"Unknown rule type '{key}'")
    if (is_hard and "hard" not in spec.modes) or (not is_hard and "soft" not in spec.modes):
        raise ValueError(f"Rule '{key}' cannot be {'hard' if is_hard else 'soft'}")
    if not isinstance(weight, int) or isinstance(weight, bool) or not 0 <= weight <= 1000:
        raise ValueError("weight must be an integer between 0 and 1000")
    if not isinstance(params, dict):
        raise ValueError("params must be an object")
    unknown = set(params) - set(spec.params)
    if unknown:
        raise ValueError(f"Unknown parameter(s): {', '.join(sorted(unknown))}")
    clean: dict = {}
    for name, p in spec.params.items():
        v = params.get(name, p.default)
        if v is None:
            if not p.nullable:
                raise ValueError(f"Parameter '{name}' is required")
        elif not isinstance(v, int) or isinstance(v, bool) or not p.minimum <= v <= p.maximum:
            raise ValueError(f"Parameter '{name}' must be an integer between {p.minimum} and {p.maximum}")
        clean[name] = v
    return {"rule_key": key, "enabled": bool(enabled), "is_hard": bool(is_hard), "weight": weight, "params": clean}


def default_rules() -> list[dict]:
    return [validate_rule_config(s.key, s.default_enabled, s.default_hard, s.default_weight,
                                 {k: p.default for k, p in s.params.items()}) for s in REGISTRY.values()]


def registry_public() -> dict:
    return {
        "core_hard": [{"rule_key": k, "description": v} for k, v in CORE_HARD.items()],
        "rules": [{
            "rule_key": s.key, "name": s.name, "description": s.description, "modes": list(s.modes), "version": s.version,
            "params": {k: {"min": p.minimum, "max": p.maximum, "default": p.default, "nullable": p.nullable, "help": p.help}
                       for k, p in s.params.items()},
        } for s in REGISTRY.values()],
    }
