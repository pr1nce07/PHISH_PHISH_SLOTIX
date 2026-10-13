"""Faculty self-service preferences: new `faculty_preferences` table, and the new soft rule
`faculty_self_preferences` added (with its default weight) to every existing rule set."""
import json

import sqlalchemy as sa
from alembic import op

from app.models import FacultyPreference
from app.solver.rules import default_rules

revision = "0002_faculty_preferences"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

RULE = "faculty_self_preferences"


def upgrade() -> None:
    bind = op.get_bind()
    # databases created after this release already got the table from the initial revision
    FacultyPreference.__table__.create(bind, checkfirst=True)
    cfg = next(r for r in default_rules() if r["rule_key"] == RULE)
    bind.execute(sa.text(
        "INSERT INTO rule_definitions (rule_set_id, rule_key, enabled, is_hard, weight, params) "
        "SELECT id, :key, :enabled, 0, :weight, :params FROM rule_sets "
        "WHERE id NOT IN (SELECT rule_set_id FROM rule_definitions WHERE rule_key = :key)"),
        {"key": RULE, "enabled": int(cfg["enabled"]), "weight": cfg["weight"], "params": json.dumps(cfg["params"])})


def downgrade() -> None:
    bind = op.get_bind()
    bind.execute(sa.text("DELETE FROM rule_definitions WHERE rule_key = :key"), {"key": RULE})
    FacultyPreference.__table__.drop(bind, checkfirst=True)
