"""
커밋 엔티티 → 2D 캔버스용 경량 프리뷰 페이로드 (WKT/쉐이프 기준).
"""
from __future__ import annotations

import json
import logging
import math
import re
import time
from pathlib import Path
from typing import Any

from geoalchemy2.shape import to_shape
from sqlalchemy import func, literal
from sqlalchemy.orm import Session

from app.models import BlockInsert, Commit, Entity
from app.services.cad_aci_palette import aci_to_css as _aci_to_css

logger = logging.getLogger(__name__)

# region agent log
_AGENT_DEBUG_LOG = Path(__file__).resolve().parents[2] / "debug-9c008d.log"


def _agent_debug_log(location: str, message: str, hypothesis_id: str, data: dict[str, Any]) -> None:
    try:
        line = json.dumps(
            {
                "sessionId": "9c008d",
                "timestamp": int(time.time() * 1000),
                "location": location,
                "message": message,
                "hypothesisId": hypothesis_id,
                "data": data,
            },
            ensure_ascii=False,
        )
        with open(_AGENT_DEBUG_LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


# endregion


def _sanitize_for_json(obj: Any) -> Any:
    """표준 JSON은 inf/nan 을 허용하지 않아 JSONResponse 500 방지."""
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else 0.0
    if isinstance(obj, (list, tuple)):
        return [_sanitize_for_json(x) for x in obj]
    if isinstance(obj, dict):
        return {k: _sanitize_for_json(v) for k, v in obj.items()}
    if isinstance(obj, (str, int, bool)) or obj is None:
        return obj
    try:
        import numpy as np

        if isinstance(obj, np.generic):
            return _sanitize_for_json(obj.item())
    except ImportError:
        pass
    return obj


DEFAULT_LIMIT = 60_000
# 양수 limit 지정 시 상한(의도치 않은 과다 요청 방지). 0·미지정은 전체 로드.
MAX_LIMIT = 50_000_000


def _packed_rgb_to_css(packed: int) -> str:
    r = (packed >> 16) & 255
    g = (packed >> 8) & 255
    b = packed & 255
    return f"#{r:02x}{g:02x}{b:02x}"


def cad_normalize_layer_lookup_key(name: str | None) -> str:
    """CAD Manage workspace `cadNormalizeLayerLookupKey` 와 동일 (레이어 트리·접두 제거)."""
    s = "" if name is None else str(name).strip()
    if not s:
        return ""
    s = re.sub(r"\s+", "", s).upper()
    k = s
    for sep in ("$", "|", "/", "\\"):
        idx = k.rfind(sep)
        if idx >= 0 and idx < len(k) - 1:
            k = k[idx + 1 :]
    return k


def _lookup_layer_color_cad(layer_colors: dict[str, Any] | None, layer: str | None) -> Any:
    """CAD Manage `cadResolveLayerColorValueFromMap` 와 동일한 키 매칭."""
    if not layer_colors or not isinstance(layer_colors, dict):
        return None
    name = (layer or "0").strip() or "0"
    if name in layer_colors and layer_colors[name] is not None:
        return layer_colors[name]
    want = cad_normalize_layer_lookup_key(name)
    if not want:
        return None
    for k, v in layer_colors.items():
        if v is None:
            continue
        try:
            if cad_normalize_layer_lookup_key(str(k)) == want:
                return v
        except Exception:
            continue
    return None


def _lookup_layer_color(layer_colors: dict[str, Any] | None, layer: str | None) -> Any:
    """하위 호환: CAD 정규화 조회."""
    return _lookup_layer_color_cad(layer_colors, layer)


def _dxf_color_value_to_pen(val: Any) -> int | None:
    """레이어/테이블 색 값 → CAD `getColor` 에 넣을 정수(ACI 또는 packed RGB)."""
    if val is None:
        return None
    if isinstance(val, dict) and val.get("color") is not None:
        return _dxf_color_value_to_pen(val.get("color"))
    if isinstance(val, str):
        s = val.strip()
        if s.startswith("#") and len(s) >= 7:
            hx = s[1:7]
            try:
                r = int(hx[0:2], 16)
                g = int(hx[2:4], 16)
                b = int(hx[4:6], 16)
                return (r << 16) | (g << 8) | b
            except ValueError:
                return None
        return None
    try:
        n = int(val)
    except (TypeError, ValueError):
        return None
    if n > 255:
        return n
    if 1 <= n <= 255:
        return n
    return None


def _dxf_color_value_to_css(val: Any) -> str | None:
    """레이어/엔티티 색: ACI, TrueColor(>255), 문자열 #rrggbb."""
    p = _dxf_color_value_to_pen(val)
    if p is None:
        return None
    return _pen_to_css(p)


def _pen_to_css(pen: int) -> str:
    """CAD Manage `getColor(aci)` 결과에 맞춘 CSS (0·256 → #e0e0e0)."""
    try:
        p = int(pen)
    except (TypeError, ValueError):
        return _aci_to_css(7)
    if p == 0 or p == 256:
        return "#e0e0e0"
    if p > 255:
        return _packed_rgb_to_css(p)
    if 1 <= p <= 255:
        return _aci_to_css(p)
    return _aci_to_css(7)


def _resolve_entity_pen(
    color: int | None,
    layer: str,
    layer_colors: dict[str, Any],
    props: dict[str, Any],
) -> int:
    """CAD Manage 화면과 동일한 최종 펜 정수 (TrueColor·color_raw·ByLayer·DB color 순).

    주의: DXF 그룹 62 값 256은 ByLayer이며, 255보다 큰 정수가 아니다.
    `raw > 255` 분기에 256이 들어가면 안 된다(256>255 가 참이 되어 ByLayer가 깨짐).
    """
    lay = (layer or "0").strip() or "0"
    raw = props.get("color_raw")
    tc420 = props.get("true_color")
    try:
        if tc420 is not None:
            tc420 = int(tc420)
    except (TypeError, ValueError):
        tc420 = None
    try:
        if raw is not None:
            raw = int(raw)
    except (TypeError, ValueError):
        raw = None

    # TrueColor(420) — 명시 RGB; ezdxf 로 62=256 과 420 동시인 경우도 있음 → 420 우선.
    if isinstance(tc420, int) and tc420 > 255:
        return tc420

    # ByLayer — 256은 packed RGB 가 아님(`raw > 255` 분기와 절대 섞이면 안 됨).
    if raw == 256 or color == 256:
        lc = _lookup_layer_color_cad(layer_colors, lay)
        p = _dxf_color_value_to_pen(lc)
        return p if p is not None else 7

    # 그룹 62에 packed RGB(드묾) 또는 색 객체에서 온 >255 값
    if isinstance(raw, int) and raw > 255:
        return raw

    if isinstance(raw, int) and 1 <= raw <= 255:
        return raw

    if raw == 0 and color == 0:
        return 0

    if isinstance(color, int) and color > 255:
        return color

    c = int(color) if color is not None else 7
    return c


def _resolve_entity_color_css(
    color: int | None,
    layer: str,
    layer_colors: dict[str, Any],
    props: dict[str, Any],
) -> str:
    """DXF color_raw / ByLayer(256) / TrueColor 반영 (pen 과 항상 일치)."""
    return _pen_to_css(_resolve_entity_pen(color, layer, layer_colors, props))


def _lineweight_to_px(lw: Any) -> float | None:
    """DXF lineweight(1/100 mm 단계 등) → 화면 선 두께 근사."""
    if lw is None:
        return None
    try:
        x = int(lw)
    except (TypeError, ValueError):
        return None
    if x < 0:
        return None
    return max(0.55, min(6.5, 0.55 + x / 45.0))


def _expand_geom(g) -> list[tuple[str, Any]]:
    """(kind, data) — kind: line|fill|dot , data: 좌표 리스트 또는 점."""
    out: list[tuple[str, Any]] = []
    gt = g.geom_type
    if gt == "Point":
        out.append(("dot", (float(g.x), float(g.y))))
    elif gt == "MultiPoint":
        for p in g.geoms:
            out.append(("dot", (float(p.x), float(p.y))))
    elif gt == "LineString":
        coords = [(float(x), float(y)) for x, y, *_ in g.coords]
        if len(coords) >= 2:
            out.append(("line", coords))
    elif gt == "LinearRing":
        coords = [(float(x), float(y)) for x, y, *_ in g.coords]
        if len(coords) >= 2:
            out.append(("line", coords))
    elif gt == "MultiLineString":
        for ls in g.geoms:
            coords = [(float(x), float(y)) for x, y, *_ in ls.coords]
            if len(coords) >= 2:
                out.append(("line", coords))
    elif gt == "Polygon":
        ext = [(float(x), float(y)) for x, y, *_ in g.exterior.coords]
        if len(ext) >= 2:
            out.append(("fill", ext))
        for hole in g.interiors:
            hc = [(float(x), float(y)) for x, y, *_ in hole.coords]
            if len(hc) >= 2:
                out.append(("line", hc))
    elif gt == "MultiPolygon":
        for poly in g.geoms:
            out.extend(_expand_geom(poly))
    elif gt == "GeometryCollection":
        for sub in g.geoms:
            out.extend(_expand_geom(sub))
    return out


def _bbox_points(coords: list[tuple[float, float]]) -> list[float]:
    if not coords:
        return [0.0, 0.0, 0.0, 0.0]
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    return [min(xs), min(ys), max(xs), max(ys)]


def _bounds_update(b: list[float], x: float, y: float) -> None:
    if b[0] > b[2]:
        b[0] = b[2] = x
        b[1] = b[3] = y
        return
    b[0] = min(b[0], x)
    b[1] = min(b[1], y)
    b[2] = max(b[2], x)
    b[3] = max(b[3], y)


def _block_id_to_name(db: Session, commit_id: int) -> dict[int, str]:
    rows = (
        db.query(BlockInsert.id, BlockInsert.block_name)
        .filter(BlockInsert.commit_id == commit_id)
        .all()
    )
    return {int(r.id): (r.block_name or "") for r in rows}


def _layers_index(
    db: Session, commit_id: int, layer_colors: dict[str, Any]
) -> list[dict[str, Any]]:
    # 동일한 SQL 표현식 객체를 써야 PostgreSQL GROUP BY 규칙을 만족한다(분리된 coalesce 는 500).
    layer_key = func.coalesce(Entity.layer, literal("0"))
    rows = (
        db.query(layer_key, func.count(Entity.id))
        .filter(Entity.commit_id == commit_id)
        .group_by(layer_key)
        .order_by(layer_key)
        .all()
    )
    out: list[dict[str, Any]] = []
    for name, cnt in rows:
        nm = str(name) if name is not None else "0"
        c = _lookup_layer_color(layer_colors, nm)
        sw = None
        if isinstance(c, str):
            sw = c
        elif isinstance(c, dict) and c.get("color"):
            sw = str(c["color"])
        elif c is not None:
            sw = _dxf_color_value_to_css(c)
        out.append({"name": nm, "entity_count": int(cnt), "color": sw})
    return out


def _label_text_height_world(props: dict[str, Any], et: str) -> float | None:
    """도면 좌표계 기준 텍스트 높이. MTEXT는 char_height 우선, TEXT/ATTRIB는 height."""
    if et == "MTEXT":
        raw = props.get("char_height")
        if raw is None:
            raw = props.get("height")
    else:
        raw = props.get("height")
        if raw is None:
            raw = props.get("char_height")
    if raw is None:
        return None
    try:
        h = float(raw)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(h) or h <= 0:
        return None
    return h


def _props_int(props: dict[str, Any], key: str, default: int = 0) -> int:
    try:
        v = props.get(key)
        if v is None:
            return int(default)
        return int(v)
    except (TypeError, ValueError):
        return int(default)


def _label_xy_world_from_point(props: dict[str, Any], sx: float, sy: float) -> tuple[float, float]:
    """Rhino `cadmanage_rhino` 와 동일: `text_align_*` 가 있으면 geom 포인트보다 우선."""
    try:
        tax = props.get("text_align_x")
        tay = props.get("text_align_y")
        if tax is not None and tay is not None:
            return float(tax), float(tay)
    except (TypeError, ValueError):
        pass
    return float(sx), float(sy)


def _blocks_index(db: Session, commit_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(BlockInsert.block_name, func.count(BlockInsert.id))
        .filter(BlockInsert.commit_id == commit_id)
        .group_by(BlockInsert.block_name)
        .order_by(BlockInsert.block_name)
        .all()
    )
    return [{"name": str(r[0] or ""), "insert_count": int(r[1])} for r in rows]


def build_viewer_preview(
    db: Session,
    commit_id: int,
    *,
    limit: int | None = None,
    simplify_tol: float = 0.0,
) -> dict[str, Any]:
    commit = db.query(Commit).filter(Commit.id == commit_id).first()
    if not commit:
        return {"error": "commit_not_found"}
    # limit 가 None, 0 이하 → SQL LIMIT 없이 커밋 전체 엔티티
    lim_cap: int | None
    if limit is None or int(limit) <= 0:
        lim_cap = None
    else:
        lim_cap = max(1, min(int(limit), MAX_LIMIT))

    q = db.query(Entity).filter(Entity.commit_id == commit_id).order_by(Entity.id)
    if lim_cap is not None:
        q = q.limit(lim_cap)
    layer_colors = (commit.settings or {}).get("layer_colors") or {}
    block_by_id = _block_id_to_name(db, commit_id)
    layers_index = _layers_index(db, commit_id, layer_colors)
    blocks_index = _blocks_index(db, commit_id)

    bounds = [float("inf"), float("inf"), float("-inf"), float("-inf")]
    vectors: list[dict[str, Any]] = []
    labels: list[dict[str, Any]] = []
    skipped_null_geom = 0
    to_shape_errors = 0
    _dbg_eids: set[int] = set()
    _dbg_entity_samples: list[dict[str, Any]] = []

    n = 0
    for ent in q:
        n += 1
        props = ent.props if isinstance(ent.props, dict) else {}
        et = ent.entity_type or ""

        if ent.geom is None:
            skipped_null_geom += 1
            continue
        try:
            shape = to_shape(ent.geom)
            if simplify_tol and simplify_tol > 0:
                try:
                    shape = shape.simplify(simplify_tol, preserve_topology=True)
                except Exception:
                    pass
        except Exception as e:
            to_shape_errors += 1
            logger.debug("to_shape skip id=%s: %s", ent.id, e)
            continue

        lay = (ent.layer or "0").strip() or "0"
        display_pen = _resolve_entity_pen(ent.color, lay, layer_colors, props)
        color_css = _pen_to_css(display_pen)
        cbl = props.get("color_bylayer")
        if cbl is None:
            cbl = ent.color == 256
        # region agent log
        if ent.id not in _dbg_eids and len(_dbg_eids) < 8:
            _dbg_eids.add(ent.id)
            _dbg_entity_samples.append(
                {
                    "eid": ent.id,
                    "etype": et,
                    "layer": lay,
                    "db_color": ent.color,
                    "color_raw": props.get("color_raw"),
                    "true_color": props.get("true_color"),
                    "cbl": cbl,
                    "pen": display_pen,
                    "color_css": color_css,
                }
            )
        # endregion
        lw_px = _lineweight_to_px(props.get("lineweight"))
        blk = None
        bid = int(ent.block_insert_id) if ent.block_insert_id is not None else None
        if ent.block_insert_id is not None:
            blk = block_by_id.get(int(ent.block_insert_id))

        if et in ("TEXT", "MTEXT", "ATTRIB"):
            txt = (props.get("text") or "").strip()
            if shape.geom_type == "Point":
                gx, gy = float(shape.x), float(shape.y)
                x, y = _label_xy_world_from_point(props, gx, gy)
                _bounds_update(bounds, x, y)
                th = _label_text_height_world(props, et)
                pad = max(2.0, (th or 2.0) * 0.6)
                bb = [x - pad, y - pad, x + pad, y + pad]
                label_row: dict[str, Any] = {
                    "x": x,
                    "y": y,
                    "text": txt[:500],
                    "layer": lay,
                    "etype": et,
                    "color": color_css,
                    "pen": display_pen,
                    "aci": ent.color,
                    "cbl": cbl,
                    "eid": ent.id,
                    "blk": blk,
                    "bid": bid,
                    "bb": bb,
                    "lw": lw_px,
                    "halign": _props_int(props, "halign", 0),
                    "valign": _props_int(props, "valign", 0),
                }
                if et == "MTEXT":
                    label_row["attachment_point"] = _props_int(props, "attachment_point", 1)
                    mla = props.get("mtext_line_aligns")
                    if isinstance(mla, (list, tuple)) and len(mla) > 0:
                        label_row["mtext_line_aligns"] = [str(x) if x is not None else "left" for x in mla]
                    try:
                        mw = props.get("mtext_width")
                        if mw is not None:
                            label_row["mtext_width"] = float(mw)
                    except (TypeError, ValueError):
                        pass
                    try:
                        lsfr = props.get("line_spacing_factor")
                        if lsfr is not None:
                            label_row["line_spacing_factor"] = float(lsfr)
                    except (TypeError, ValueError):
                        pass
                try:
                    rot_raw = props.get("rotation")
                    if rot_raw is not None:
                        rotv = float(rot_raw)
                        if math.isfinite(rotv):
                            label_row["rotation"] = rotv
                except (TypeError, ValueError):
                    pass
                if th is not None:
                    label_row["text_height"] = th
                labels.append(label_row)
            continue

        for kind, data in _expand_geom(shape):
            if kind == "dot":
                x, y = data
                _bounds_update(bounds, x, y)
                pad = 1e-6
                bb = [x - pad, y - pad, x + pad, y + pad]
                vectors.append(
                    {
                        "kind": "dot",
                        "xy": [x, y],
                        "layer": lay,
                        "etype": et,
                        "color": color_css,
                        "pen": display_pen,
                        "aci": ent.color,
                        "cbl": cbl,
                        "eid": ent.id,
                        "blk": blk,
                        "bid": bid,
                        "bb": bb,
                        "lw": lw_px,
                    }
                )
            elif kind == "line":
                path = data
                for x, y in path:
                    _bounds_update(bounds, x, y)
                bb = _bbox_points([(float(a[0]), float(a[1])) for a in path])
                vectors.append(
                    {
                        "kind": "line",
                        "path": path,
                        "layer": lay,
                        "etype": et,
                        "color": color_css,
                        "pen": display_pen,
                        "aci": ent.color,
                        "cbl": cbl,
                        "eid": ent.id,
                        "blk": blk,
                        "bid": bid,
                        "bb": bb,
                        "lw": lw_px,
                    }
                )
            elif kind == "fill":
                path = data
                for x, y in path:
                    _bounds_update(bounds, x, y)
                bb = _bbox_points([(float(a[0]), float(a[1])) for a in path])
                vectors.append(
                    {
                        "kind": "fill",
                        "path": path,
                        "layer": lay,
                        "etype": et,
                        "color": color_css,
                        "pen": display_pen,
                        "aci": ent.color,
                        "cbl": cbl,
                        "eid": ent.id,
                        "blk": blk,
                        "bid": bid,
                        "bb": bb,
                        "lw": lw_px,
                    }
                )

    if bounds[0] > bounds[2]:
        bounds = [0.0, 0.0, 1.0, 1.0]

    # region agent log
    try:
        pens = [v.get("pen") for v in vectors]
        uniq = list(dict.fromkeys(pens))[:40]
        _agent_debug_log(
            "viewer_preview.py:build_viewer_preview",
            "layer_colors_and_pen_distribution",
            "H1",
            {
                "commit_id": commit_id,
                "limit": lim_cap,
                "layer_colors_count": len(layer_colors),
                "layer_colors_keys_sample": list(layer_colors.keys())[:30],
                "vector_count": len(vectors),
                "unique_pen_count": len(set(pens)),
                "unique_pens_sample": uniq,
            },
        )
        _agent_debug_log(
            "viewer_preview.py:build_viewer_preview",
            "entity_prop_samples",
            "H3",
            {"commit_id": commit_id, "samples": _dbg_entity_samples},
        )
        _agent_debug_log(
            "viewer_preview.py:build_viewer_preview",
            "first_vectors_payload",
            "H5",
            {
                "commit_id": commit_id,
                "first5": [
                    {
                        "pen": v.get("pen"),
                        "color": v.get("color"),
                        "layer": v.get("layer"),
                        "aci": v.get("aci"),
                        "cbl": v.get("cbl"),
                    }
                    for v in vectors[:5]
                ],
            },
        )
    except Exception:
        pass
    # endregion

    return _sanitize_for_json(
        {
            "commit_id": commit_id,
            "commit_status": commit.status,
            "entity_count_loaded": n,
            "skipped_null_geom": skipped_null_geom,
            "to_shape_errors": to_shape_errors,
            "vector_count": len(vectors),
            "label_count": len(labels),
            "limit": lim_cap,
            "bounds": bounds,
            "layer_colors": layer_colors,
            "layers_index": layers_index,
            "blocks_index": blocks_index,
            "vectors": vectors,
            "labels": labels,
        }
    )
