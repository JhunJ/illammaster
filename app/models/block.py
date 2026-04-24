from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Column, DateTime, Float, ForeignKey, Integer, String, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class BlockDef(Base):
    __tablename__ = "block_defs"
    __table_args__ = (UniqueConstraint("commit_id", "name", name="uq_block_defs_commit_name"),)

    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(Integer, ForeignKey("commits.id"), nullable=False)
    name = Column(String(255), nullable=False, index=True)
    base_point = Column(Geometry(geometry_type="POINT", srid=0), nullable=True)
    props = Column(JSONB, nullable=True)


class BlockInsert(Base):
    __tablename__ = "block_inserts"

    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(Integer, ForeignKey("commits.id"), nullable=False)
    block_def_id = Column(Integer, ForeignKey("block_defs.id"), nullable=True)
    block_name = Column(String(255), nullable=False, index=True)
    layer = Column(String(255), nullable=True, index=True)
    color = Column(Integer, nullable=True)
    insert_point = Column(Geometry(geometry_type="POINT", srid=0), nullable=True)
    rotation = Column(Float, nullable=True)
    scale_x = Column(Float, nullable=True)
    scale_y = Column(Float, nullable=True)
    scale_z = Column(Float, nullable=True)
    transform = Column(JSONB, nullable=True)
    props = Column(JSONB, nullable=True)
    fingerprint = Column(String(128), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)


class BlockAttr(Base):
    __tablename__ = "block_attrs"

    id = Column(Integer, primary_key=True, index=True)
    insert_id = Column(Integer, ForeignKey("block_inserts.id"), nullable=False)
    tag = Column(String(255), nullable=False)
    value = Column(String(1024), nullable=True)
    props = Column(JSONB, nullable=True)
