"""Initial PostGIS schema for Illammaster

Revision ID: 001
Revises:
Create Date: 2026-04-20

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS postgis")

    op.create_table(
        "users",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("email", sa.String(255), nullable=False),
        sa.Column("role", sa.String(64), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_users_id", "users", ["id"], unique=False)
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    op.create_table(
        "projects",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("code", sa.String(64), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("settings", JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_code", "projects", ["code"], unique=True)
    op.create_index("ix_projects_id", "projects", ["id"], unique=False)

    op.create_table(
        "files",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("original_filename", sa.String(512), nullable=False),
        sa.Column("storage_path", sa.String(1024), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=True),
        sa.Column("file_size", sa.BigInteger(), nullable=True),
        sa.Column("uploaded_by", sa.Integer(), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_files_id", "files", ["id"], unique=False)
    op.create_index("ix_files_sha256", "files", ["sha256"], unique=False)

    op.create_table(
        "commits",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("project_id", sa.Integer(), nullable=False),
        sa.Column("file_id", sa.Integer(), nullable=False),
        sa.Column("parent_commit_id", sa.Integer(), nullable=True),
        sa.Column("version_label", sa.String(255), nullable=True),
        sa.Column("branch_name", sa.String(64), nullable=True),
        sa.Column("assignee_name", sa.String(255), nullable=True),
        sa.Column("assignee_department", sa.String(255), nullable=True),
        sa.Column("change_notes", sa.Text(), nullable=True),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("created_by", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.Column("settings", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("progress_message", sa.String(255), nullable=True),
        sa.Column("class_pre", sa.String(32), nullable=True),
        sa.Column("class_major", sa.String(64), nullable=True),
        sa.Column("class_mid", sa.String(32), nullable=True),
        sa.Column("class_minor", sa.String(64), nullable=True),
        sa.Column("class_work_type", sa.String(64), nullable=True),
        sa.ForeignKeyConstraint(["file_id"], ["files.id"]),
        sa.ForeignKeyConstraint(["parent_commit_id"], ["commits.id"]),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_commits_id", "commits", ["id"], unique=False)
    op.create_index("ix_commits_status", "commits", ["status"], unique=False)

    op.create_table(
        "block_defs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commit_id", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_point", Geometry(geometry_type="POINT", srid=0), nullable=True),
        sa.Column("props", JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["commit_id"], ["commits.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("commit_id", "name", name="uq_block_defs_commit_name"),
    )
    op.create_index("ix_block_defs_id", "block_defs", ["id"], unique=False)
    op.create_index("ix_block_defs_name", "block_defs", ["name"], unique=False)

    op.create_table(
        "block_inserts",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commit_id", sa.Integer(), nullable=False),
        sa.Column("block_def_id", sa.Integer(), nullable=True),
        sa.Column("block_name", sa.String(255), nullable=False),
        sa.Column("layer", sa.String(255), nullable=True),
        sa.Column("color", sa.Integer(), nullable=True),
        sa.Column("insert_point", Geometry(geometry_type="POINT", srid=0), nullable=True),
        sa.Column("rotation", sa.Float(), nullable=True),
        sa.Column("scale_x", sa.Float(), nullable=True),
        sa.Column("scale_y", sa.Float(), nullable=True),
        sa.Column("scale_z", sa.Float(), nullable=True),
        sa.Column("transform", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("props", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fingerprint", sa.String(128), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["block_def_id"], ["block_defs.id"]),
        sa.ForeignKeyConstraint(["commit_id"], ["commits.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_block_inserts_id", "block_inserts", ["id"], unique=False)
    op.create_index("ix_block_inserts_block_name", "block_inserts", ["block_name"], unique=False)
    op.create_index("ix_block_inserts_layer", "block_inserts", ["layer"], unique=False)
    op.create_index("ix_block_inserts_fingerprint", "block_inserts", ["fingerprint"], unique=False)
    op.execute("CREATE INDEX ix_block_inserts_insert_point ON block_inserts USING GIST (insert_point)")

    op.create_table(
        "entities",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commit_id", sa.Integer(), nullable=False),
        sa.Column("entity_type", sa.String(64), nullable=False),
        sa.Column("layer", sa.String(255), nullable=True),
        sa.Column("color", sa.Integer(), nullable=True),
        sa.Column("linetype", sa.String(255), nullable=True),
        sa.Column("geom", Geometry(geometry_type="GEOMETRY", srid=0), nullable=True),
        sa.Column("centroid", Geometry(geometry_type="POINT", srid=0), nullable=True),
        sa.Column("bbox", Geometry(geometry_type="POLYGON", srid=0), nullable=True),
        sa.Column("props", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("fingerprint", sa.String(128), nullable=True),
        sa.Column("block_insert_id", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["block_insert_id"], ["block_inserts.id"]),
        sa.ForeignKeyConstraint(["commit_id"], ["commits.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_entities_id", "entities", ["id"], unique=False)
    op.create_index("ix_entities_commit_id", "entities", ["commit_id"], unique=False)
    op.create_index("ix_entities_entity_type", "entities", ["entity_type"], unique=False)
    op.create_index("ix_entities_layer", "entities", ["layer"], unique=False)
    op.create_index("ix_entities_color", "entities", ["color"], unique=False)
    op.create_index("ix_entities_fingerprint", "entities", ["fingerprint"], unique=False)
    op.create_index("ix_entities_block_insert_id", "entities", ["block_insert_id"], unique=False)
    op.execute("CREATE INDEX ix_entities_geom ON entities USING GIST (geom)")
    op.execute("CREATE INDEX ix_entities_centroid ON entities USING GIST (centroid)")
    op.execute("CREATE INDEX ix_entities_bbox ON entities USING GIST (bbox)")

    op.create_table(
        "block_attrs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("insert_id", sa.Integer(), nullable=False),
        sa.Column("tag", sa.String(255), nullable=False),
        sa.Column("value", sa.String(1024), nullable=True),
        sa.Column("props", JSONB(astext_type=sa.Text()), nullable=True),
        sa.ForeignKeyConstraint(["insert_id"], ["block_inserts.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_block_attrs_id", "block_attrs", ["id"], unique=False)

    op.create_table(
        "schedule_runs",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("commit_id", sa.Integer(), nullable=False),
        sa.Column("category", sa.String(32), nullable=False),
        sa.Column("rules_version", sa.String(32), nullable=True),
        sa.Column("config_snapshot", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("rows", JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("validation", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("geometry_hints", JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(["commit_id"], ["commits.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_schedule_runs_id", "schedule_runs", ["id"], unique=False)
    op.create_index("ix_schedule_runs_commit_id", "schedule_runs", ["commit_id"], unique=False)
    op.create_index("ix_schedule_runs_category", "schedule_runs", ["category"], unique=False)


def downgrade() -> None:
    op.drop_table("schedule_runs")
    op.drop_table("block_attrs")
    op.drop_table("entities")
    op.drop_table("block_inserts")
    op.drop_table("block_defs")
    op.drop_table("commits")
    op.drop_table("files")
    op.drop_table("projects")
    op.drop_table("users")
