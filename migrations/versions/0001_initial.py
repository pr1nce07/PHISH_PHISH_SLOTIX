"""Initial schema: all tables, constraints, indexes, triggers and the three application roles.

Schema confirmation:
  * weekly requirements are columns of `subjects` (no `weekly_requirements` table);
  * `subjects.student_group_id` is a FK to `student_groups`, with a term-consistency trigger;
  * room features are rows of `room_features` with a direct `room_id` FK (no mapping table).

Later schema changes must be added as new revisions (never by editing this file).
"""
from alembic import op

from app.models import Base, install_schema

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    install_schema(op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(op.get_bind())
