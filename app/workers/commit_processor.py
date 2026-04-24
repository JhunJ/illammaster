"""
커밋 처리 백그라운드 태스크: DWG/DXF -> 변환 -> 파싱 -> PostGIS 적재 -> changeset -> status 갱신.
"""
import logging
from pathlib import Path

from sqlalchemy.orm import Session
from geoalchemy2.elements import WKTElement

from app.db.session import SessionLocal

from app.models import Commit, File, Entity, BlockDef, BlockInsert, BlockAttr
from app.services.storage import get_full_path
from app.services.oda_converter import dwg_to_dxf
from app.services.dxf_parser import parse_dxf
from app.utils.geom import wkt_to_2d, ensure_polygon_rings_closed

logger = logging.getLogger(__name__)


def process_commit(commit_id: int) -> None:
    db = SessionLocal()
    try:
        commit = db.query(Commit).filter(Commit.id == commit_id).first()
        if not commit:
            logger.error("Commit %s not found", commit_id)
            return
        if commit.status not in ("PENDING", "PROCESSING"):
            logger.info("Commit %s already processed: %s", commit_id, commit.status)
            return
        if commit.status == "PENDING":
            commit.status = "PROCESSING"
            _set_progress(db, commit, "변환 중…")
            db.commit()
            db.refresh(commit)
            logger.info("Commit %s PROCESSING (convert/parse/persist)", commit_id)

        file = db.query(File).filter(File.id == commit.file_id).first()
        if not file:
            _fail_commit(db, commit_id, "File not found")
            return

        full_path = get_full_path(file.storage_path)
        if not full_path.exists():
            _fail_commit(db, commit_id, f"File not on disk: {full_path}")
            return

        settings = (commit.settings or {}) if commit.settings else {}
        dxf_path: Path | None = None
        dxf_bytes: bytes

        suffix = full_path.suffix.upper()
        if suffix == ".DWG":
            _set_progress(db, commit, "DWG → DXF 변환 중…")
            db.commit()
            dxf_path = dwg_to_dxf(full_path)
            if dxf_path is None:
                _fail_commit(
                    db,
                    commit_id,
                    "DWG 변환 실패: ODA File Converter(ODA_FC_PATH) 또는 LibreDWG(dwg2dxf)를 설치하거나, CAD에서 DXF로 저장 후 DXF 파일을 업로드하세요.",
                )
                return
            dxf_bytes = dxf_path.read_bytes()
        elif suffix == ".DXF":
            dxf_bytes = full_path.read_bytes()
        else:
            _fail_commit(db, commit_id, f"Unsupported file type: {suffix}")
            return

        _set_progress(db, commit, "DXF 파싱 중…")
        db.commit()
        try:
            entities, block_defs, block_inserts, block_attrs, layer_colors = parse_dxf(dxf_bytes, settings)
        except Exception as e:
            logger.exception("DXF parse error for commit %s: %s", commit_id, e)
            _fail_commit(db, commit_id, f"DXF parse error: {e}")
            return

        _set_progress(db, commit, "DB 저장 중…")
        db.commit()
        try:
            temp_key_to_insert_id = _persist_blocks(db, commit_id, block_defs, block_inserts, block_attrs)
            _persist_entities(db, commit_id, entities, temp_key_to_insert_id)
        except Exception as e:
            logger.exception("Persist error for commit %s: %s", commit_id, e)
            db.rollback()
            err_detail = f"{type(e).__name__}: {e}"
            _fail_commit(db, commit_id, f"Persist error: {err_detail}")
            return

        commit.settings = {**(commit.settings or {}), "layer_colors": layer_colors}
        commit.status = "READY"
        commit.error_message = None
        _set_progress(db, commit, None)
        db.commit()
        logger.info("Commit %s READY", commit_id)
    except Exception as e:
        logger.exception("process_commit failed: %s", e)
        try:
            db.rollback()
            _fail_commit(db, commit_id, str(e))
        except Exception:
            pass
    finally:
        db.close()


def _set_progress(db: Session, commit: Commit, message: str | None) -> None:
    commit.progress_message = message


def _fail_commit(db: Session, commit_id: int, error_message: str) -> None:
    c = db.get(Commit, commit_id)
    if c:
        c.status = "FAILED"
        c.error_message = error_message
        c.progress_message = None
        db.commit()
    logger.error("Commit %s FAILED: %s", commit_id, error_message)


BULK_ENTITY_BATCH = 10_000


def _normalize_temp_insert_key(raw: object) -> int | str | None:
    if raw is None:
        return None
    if isinstance(raw, bool):
        return int(raw)
    if isinstance(raw, int):
        return raw
    if isinstance(raw, str):
        s = raw.strip()
        if not s:
            return None
        try:
            return int(s)
        except ValueError:
            return s
    try:
        return int(raw)
    except (TypeError, ValueError):
        return raw


def _persist_entities(
    db: Session,
    commit_id: int,
    entities: list[dict],
    temp_key_to_insert_id: dict[int | str, int] | None = None,
) -> None:
    """엔티티 저장. (bulk_insert_mappings는 GeoAlchemy2 Geometry 직렬화 이슈로 add 사용)
    geom_wkt가 None이거나 빈/유효하지 않은 WKT인 엔티티는 스킵하고 로그."""
    skipped = 0
    for e in entities:
        g = wkt_to_2d(e.get("geom_wkt"))
        if not g or (isinstance(g, str) and not g.strip()):
            skipped += 1
            continue
        valid_prefixes = ("POINT", "LINESTRING", "POLYGON", "MULTI", "GEOMETRYCOLLECTION")
        if not g.upper().strip().startswith(valid_prefixes):
            logger.warning("Persist skip invalid geom_wkt prefix: %s", (g or "")[:80])
            skipped += 1
            continue
        if g.upper().strip().startswith(("POLYGON", "MULTIPOLYGON")):
            g = ensure_polygon_rings_closed(g) or g
        c = wkt_to_2d(e.get("centroid_wkt"))
        b = wkt_to_2d(e.get("bbox_wkt"))
        geom = WKTElement(g, srid=0)
        centroid = WKTElement(c, srid=0) if c else None
        bbox = WKTElement(b, srid=0) if b else None
        block_insert_id = None
        if temp_key_to_insert_id:
            temp_key = _normalize_temp_insert_key(e.get("_temp_insert_key"))
            if temp_key is None:
                temp_key = _normalize_temp_insert_key(e.get("temp_insert_key"))
            if temp_key is not None:
                block_insert_id = temp_key_to_insert_id.get(temp_key)
        try:
            db.add(Entity(
                commit_id=commit_id,
                entity_type=e["entity_type"],
                layer=e.get("layer"),
                color=e.get("color"),
                linetype=e.get("linetype"),
                geom=geom,
                centroid=centroid,
                bbox=bbox,
                props=e.get("props"),
                fingerprint=e.get("fingerprint"),
                block_insert_id=block_insert_id,
            ))
        except Exception as ex:
            logger.warning("Persist skip entity (geom parse error): %s", ex)
            skipped += 1
    if skipped:
        logger.info("_persist_entities: skipped %d of %d entities", skipped, len(entities))
    db.commit()


def _persist_blocks(
    db: Session,
    commit_id: int,
    block_defs: list[dict],
    block_inserts: list[dict],
    block_attrs: list[dict],
) -> dict[int | str, int]:
    # props.entities는 블록당 수만~수십만 WKT로 JSONB 한도 초과 가능 → 크기 제한 내에서만 저장
    ENTITY_LIMIT = 5000
    name_to_def_id: dict[str, int] = {}
    for bd in block_defs:
        if bd.get("name") in name_to_def_id:
            continue
        bp_raw = bd.get("base_point_wkt")
        bp = wkt_to_2d(bp_raw) if bp_raw else None
        if bp and (" Z" in bp.upper() or "Z(" in bp.upper()):
            bp = wkt_to_2d(bp) or bp
        base = WKTElement(bp, srid=0) if bp else None
        entities_in_def = (bd.get("props") or {}).get("entities") or []
        if entities_in_def and len(entities_in_def) <= ENTITY_LIMIT:
            props_for_db = {"entities": entities_in_def, "entity_count": len(entities_in_def)}
        else:
            props_for_db = {"entity_count": len(entities_in_def)}
        obj = BlockDef(commit_id=commit_id, name=bd["name"], base_point=base, props=props_for_db)
        db.add(obj)
        db.flush()
        name_to_def_id[bd["name"]] = obj.id
    db.commit()

    temp_key_to_insert_id: dict[int | str, int] = {}
    for bi in block_inserts:
        ip_raw = bi.get("insert_point_wkt")
        ip = wkt_to_2d(ip_raw) if ip_raw else None
        if ip and (" Z" in ip.upper() or "Z(" in ip.upper()):
            ip = wkt_to_2d(ip) or ip
        insert_pt = WKTElement(ip, srid=0) if ip else None
        block_def_id = name_to_def_id.get(bi["block_name"])
        obj = BlockInsert(
            commit_id=commit_id,
            block_def_id=block_def_id,
            block_name=bi["block_name"],
            layer=bi.get("layer"),
            color=bi.get("color"),
            insert_point=insert_pt,
            rotation=bi.get("rotation"),
            scale_x=bi.get("scale_x"),
            scale_y=bi.get("scale_y"),
            scale_z=bi.get("scale_z"),
            transform=bi.get("transform"),
            props=bi.get("props"),
            fingerprint=bi.get("fingerprint"),
        )
        db.add(obj)
        db.flush()
        temp_key = _normalize_temp_insert_key(bi.get("_temp_insert_key"))
        if temp_key is None:
            temp_key = _normalize_temp_insert_key(bi.get("temp_insert_key"))
        if temp_key is not None:
            temp_key_to_insert_id[temp_key] = obj.id
    db.commit()

    for ba in block_attrs:
        temp_key = _normalize_temp_insert_key(ba.get("_temp_insert_key"))
        if temp_key is None:
            temp_key = _normalize_temp_insert_key(ba.get("temp_insert_key"))
        insert_id = temp_key_to_insert_id.get(temp_key) if temp_key is not None else None
        if insert_id is None:
            continue
        db.add(BlockAttr(
            insert_id=insert_id,
            tag=ba["tag"],
            value=ba.get("value"),
            props=ba.get("props"),
        ))
    db.commit()
    return temp_key_to_insert_id
