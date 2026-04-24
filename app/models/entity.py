from datetime import datetime

from geoalchemy2 import Geometry
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB

from app.db.base import Base


class Entity(Base):
    __tablename__ = "entities"

    id = Column(Integer, primary_key=True, index=True)
    commit_id = Column(Integer, ForeignKey("commits.id"), nullable=False)
    entity_type = Column(String(64), nullable=False, index=True)
    layer = Column(String(255), nullable=True, index=True)
    color = Column(Integer, nullable=True, index=True)
    linetype = Column(String(255), nullable=True)
    geom = Column(Geometry(geometry_type="GEOMETRY", srid=0), nullable=True)
    centroid = Column(Geometry(geometry_type="POINT", srid=0), nullable=True)
    bbox = Column(Geometry(geometry_type="POLYGON", srid=0), nullable=True)
    props = Column(JSONB, nullable=True)
    fingerprint = Column(String(128), nullable=True, index=True)
    block_insert_id = Column(Integer, ForeignKey("block_inserts.id"), nullable=True, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
