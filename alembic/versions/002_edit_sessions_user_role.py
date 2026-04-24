"""edit_sessions table and widen users.role

Revision ID: 002
Revises: 001
Create Date: 2026-04-23

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "002"
down_revision: Union[str, None] = "001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=64),
        type_=sa.String(length=255),
        existing_nullable=True,
    )
    op.create_table(
        "edit_sessions",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("commit_id", sa.Integer(), nullable=False),
        sa.Column("editor_user_id", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(), nullable=True),
        sa.Column("ended_at", sa.DateTime(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(["commit_id"], ["commits.id"]),
        sa.ForeignKeyConstraint(["editor_user_id"], ["users.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_edit_sessions_id", "edit_sessions", ["id"], unique=False)
    op.create_index("ix_edit_sessions_project_id", "edit_sessions", ["project_id"], unique=False)
    op.create_index("ix_edit_sessions_commit_id", "edit_sessions", ["commit_id"], unique=False)
    op.create_index("ix_edit_sessions_editor_user_id", "edit_sessions", ["editor_user_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_edit_sessions_editor_user_id", table_name="edit_sessions")
    op.drop_index("ix_edit_sessions_commit_id", table_name="edit_sessions")
    op.drop_index("ix_edit_sessions_project_id", table_name="edit_sessions")
    op.drop_index("ix_edit_sessions_id", table_name="edit_sessions")
    op.drop_table("edit_sessions")
    op.alter_column(
        "users",
        "role",
        existing_type=sa.String(length=255),
        type_=sa.String(length=64),
        existing_nullable=True,
    )
