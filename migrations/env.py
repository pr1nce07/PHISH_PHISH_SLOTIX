"""Alembic environment. The database URL comes from TT_DATABASE_URL (see app/config.py)."""
from alembic import context

from app import db
from app.models import Base

target_metadata = Base.metadata

with db.engine.connect() as connection:
    context.configure(connection=connection, target_metadata=target_metadata, render_as_batch=True)
    with context.begin_transaction():
        context.run_migrations()
    connection.commit()
