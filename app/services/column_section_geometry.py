"""
기둥 일람 SECTION 칸 단면도: 원(주근)·직선(띠근) 기하를 읽고 MAIN BAR 텍스트와 대조.

- 단면 주근 박스 가까이의 가로·세로 치수선(LINE)과 순수 숫자 TEXT(500, 700 등)로 B·H(mm)를 추정해
  행의 width_mm·depth_mm에 반영한다(치수선과 TEXT가 어긋나면 치수선 길이를 우선).
  도면의 DIMENSION 전개 LINE/TEXT는 props.from_dimension 으로 표시되며, 주근 클러스터·외곽 집계에서는 제외한다
  (치수선이 단면 기하와 섞여 검색 박스가 잘리는 것을 방지). 일반 LINE/TEXT는 그대로 사용한다.
- Top-Bot / Left-Right: 직사각 배치에서 상·하변 주근 수와 좌·우변(모서리 제외) 주근 수.
  표기 예: 4-D19, 3-D19 → (4+3)*2 = 14 본.
- 가로 띠근 → X-Tie Bar, 세로 띠근 → Y-Tie Bar (외곽 사각형에 붙은 선은 제외).
- 겹치는 원(중심 근접)은 하나로 병합.
- 검색은 느슨한 bbox로 1차 적재 후, 주근 중심의 중앙선(median)·간격 기반 클러스터로 단면 범위를 좁혀 분석한다.
  (클러스터 선택 시 1점 스파이크가 앵커에 더 가깝다고 전체 주근 덩어리를 이기지 않도록, 최소 개수 이상만 후보로 삼는다.)
- 닫힌 LWPOLYLINE 직사각 외곽(주근 원보다 바깥) 중 주근을 모두 포함하는 것 중 면적이 가장 큰 것을 단면 B×H(mm)로 추정한다.
"""
from __future__ import annotations

import math
import re
from typing import Any, Optional

from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope
from geoalchemy2.shape import to_shape
from sqlalchemy import and_, or_
from sqlalchemy.orm import Session
from shapely.geometry import LineString, Point, box as shapely_box

try:
    from shapely import from_wkt as shapely_from_wkt
except ImportError:
    from shapely.wkt import loads as shapely_from_wkt

from app.models import BlockDef, BlockInsert, Entity
from app.utils.geom import transform_block_wkt_to_world

_RE_MAIN_BAR = re.compile(
    r"(\d+)\s*-\s*(?:U?HD|SHD|D)\s*(\d+)",
    re.I,
)
_RE_PURE_SECTION_DIM_MM = re.compile(r"^\s*(\d{2,4})\s*$")


def _compact_header_for_match(s: str | None) -> str:
    """CAD 라벨 '형 태'·'크 기'·키 slug '형_태__1' 등을 비교용으로 압축."""
    return re.sub(r"[\s_\-]", "", (s or "").strip())


def header_row_is_section_geometry_anchor(label: str | None, key: str | None) -> bool:
    """
    단면도(블록·원)가 붙는 행 — 영문 SECTION 외 한국 일람의 '형태' 등.
    schedule_extraction 의 field_headers / 템플릿 행과 공통으로 사용한다.
    """
    lab = str(label or "")
    ku = str(key or "").upper()
    lu = lab.upper()
    if "SECTION" in lu or "SECTION" in ku:
        return True
    cl = _compact_header_for_match(lab).lower()
    ck = _compact_header_for_match(key).lower()
    if "형태" in cl or "형태" in ck:
        return True
    if "단면" in cl or "단면" in ck:
        return True
    if re.fullmatch(r"TYPE", (lab or "").strip(), flags=re.I):
        return True
    return False


def parse_main_bar_spec(text: str | None) -> tuple[int | None, int | None]:
    """'14-D19' → (14, 19)."""
    if not text:
        return None, None
    m = _RE_MAIN_BAR.search(str(text).strip())
    if not m:
        return None, None
    return int(m.group(1)), int(m.group(2))


def _median_float(vals: list[float]) -> float:
    """schedule_extraction._median_float 와 동일(순환 import 방지용)."""
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    m = n // 2
    return float(s[m]) if n % 2 else 0.5 * (s[m - 1] + s[m])


def _merge_points(points: list[tuple[float, float]], tol: float) -> list[tuple[float, float]]:
    """근접 점을 하나로(순서 유지, 대표는 군집 평균)."""
    if not points:
        return []
    pts = list(points)
    out: list[tuple[float, float]] = []
    used = [False] * len(pts)
    tol2 = tol * tol
    for i, p in enumerate(pts):
        if used[i]:
            continue
        sx, sy, n = p[0], p[1], 1
        used[i] = True
        for j in range(i + 1, len(pts)):
            if used[j]:
                continue
            q = pts[j]
            dx, dy = q[0] - p[0], q[1] - p[1]
            if dx * dx + dy * dy <= tol2:
                sx += q[0]
                sy += q[1]
                n += 1
                used[j] = True
        out.append((sx / n, sy / n))
    return out


def _polyline_as_rebar_circle(et: str, shp) -> tuple[tuple[float, float], float] | None:
    """
    폴리라인으로만 그린 작은 원(주근 단면) — 닫힌 형태·종횡비로 원형에 가깝게 보일 때만.
    """
    if et not in ("LWPOLYLINE", "POLYLINE"):
        return None
    gt = getattr(shp, "geom_type", "")
    if gt != "LineString":
        return None
    coords = list(shp.coords)
    if len(coords) < 10:
        return None
    x0, y0 = coords[0][0], coords[0][1]
    xn, yn = coords[-1][0], coords[-1][1]
    gap = math.hypot(x0 - xn, y0 - yn)
    xs = [c[0] for c in coords]
    ys = [c[1] for c in coords]
    bx0, bx1, by0, by1 = min(xs), max(xs), min(ys), max(ys)
    bw, bh = bx1 - bx0, by1 - by0
    span = max(bw, bh, 1e-6)
    if gap > max(2.5, span * 0.04):
        return None
    if bw < 1e-6 or bh < 1e-6:
        return None
    ar = bw / bh
    if ar < 0.62 or ar > 1.58:
        return None
    cx = sum(xs) / len(xs)
    cy = sum(ys) / len(ys)
    r = 0.25 * (bw + bh)
    return (cx, cy), r


def _centroid_and_radius_from_shape(shape) -> tuple[tuple[float, float] | None, float | None]:
    """CIRCLE 근사 POLYGON/LINESTRING → 중심·반지름 추정."""
    try:
        c = shape.centroid
        xy = (float(c.x), float(c.y))
    except Exception:
        return None, None
    try:
        b = shape.bounds
        w, h = b[2] - b[0], b[3] - b[1]
        r = 0.25 * (w + h)
        if r < 1e-6:
            r = None
    except Exception:
        r = None
    return xy, r


def _load_shapes_in_bbox(
    db: Session,
    commit_id: int,
    bbox: tuple[float, float, float, float],
    types: tuple[str, ...] = (
        "CIRCLE",
        "LINE",
        "LWPOLYLINE",
        "POLYLINE",
        "ARC",
        "POINT",
        "ELLIPSE",
    ),
) -> list[tuple[str, Any, str | None]]:
    """(entity_type, shapely geometry, layer) — bbox와 교차하는 것만."""
    minx, miny, maxx, maxy = bbox
    bpoly = shapely_box(minx, miny, maxx, maxy)
    env = ST_MakeEnvelope(minx, miny, maxx, maxy, 0)
    out: list[tuple[str, Any, str | None]] = []
    q = (
        db.query(Entity)
        .filter(
            Entity.commit_id == commit_id,
            Entity.entity_type.in_(types),
            Entity.geom.isnot(None),
            ST_Intersects(Entity.geom, env),
        )
    )
    for ent in q.all():
        ent_props = ent.props if isinstance(ent.props, dict) else {}
        if ent_props.get("from_dimension"):
            continue
        try:
            shp = to_shape(ent.geom)
        except Exception:
            continue
        try:
            if not shp.intersects(bpoly):
                continue
        except Exception:
            continue
        out.append((ent.entity_type, shp, ent.layer))
    return out


def _block_base_xy(block_def: BlockDef) -> tuple[float, float]:
    if block_def.base_point is None:
        return 0.0, 0.0
    try:
        shp = to_shape(block_def.base_point)
        return float(shp.x), float(shp.y)
    except Exception:
        return 0.0, 0.0


def _insert_xy(bi: BlockInsert) -> tuple[float, float] | None:
    if bi.insert_point is None:
        return None
    try:
        shp = to_shape(bi.insert_point)
        return float(shp.x), float(shp.y)
    except Exception:
        return None


def _load_shapes_from_block_definitions(
    db: Session,
    commit_id: int,
    bbox: tuple[float, float, float, float],
    block_inserts_cached: list[BlockInsert] | None = None,
) -> list[tuple[str, Any, str | None]]:
    """
    블록 정의(props.entities)에만 있거나, INSERT 전개가 DB에 안 남은 경우 대비.
    각 INSERT의 삽입점·스케일·회전으로 블록 로컬 WKT → 월드 좌표 변환 후 bbox와 교차하는 도형만.
    """
    minx, miny, maxx, maxy = bbox
    bpoly = shapely_box(minx, miny, maxx, maxy)
    span = max(maxx - minx, maxy - miny, 1.0)
    pad = span * 0.5 + 30000.0
    out: list[tuple[str, Any, str | None]] = []

    defs_rows = db.query(BlockDef).filter(BlockDef.commit_id == commit_id).all()
    defs_by_id = {int(d.id): d for d in defs_rows if d.id is not None}
    defs_by_name = {str(d.name): d for d in defs_rows}

    inserts = block_inserts_cached
    if inserts is None:
        inserts = db.query(BlockInsert).filter(BlockInsert.commit_id == commit_id).all()

    for bi in inserts:
        ins = _insert_xy(bi)
        if ins is None:
            continue
        ix, iy = ins
        if ix > maxx + pad or ix < minx - pad or iy > maxy + pad or iy < miny - pad:
            continue

        bd: BlockDef | None = None
        if bi.block_def_id is not None:
            try:
                bd = defs_by_id.get(int(bi.block_def_id))
            except (TypeError, ValueError):
                bd = None
        if bd is None and bi.block_name:
            bd = defs_by_name.get(str(bi.block_name).strip())
        if bd is None:
            continue

        props = bd.props if isinstance(bd.props, dict) else {}
        entities_list = props.get("entities")
        if not entities_list or not isinstance(entities_list, list):
            continue

        bx, by = _block_base_xy(bd)
        rot = float(bi.rotation or 0.0)
        sx = float(bi.scale_x) if bi.scale_x is not None else 1.0
        sy = float(bi.scale_y) if bi.scale_y is not None else 1.0

        for item in entities_list:
            if not isinstance(item, dict):
                continue
            raw_wkt = item.get("geom_wkt")
            if not raw_wkt:
                continue
            et = str(item.get("entity_type") or "").strip().upper() or "LINE"
            if et not in (
                "CIRCLE",
                "LINE",
                "LWPOLYLINE",
                "POLYLINE",
                "ARC",
                "POINT",
                "ELLIPSE",
            ):
                continue
            item_props = item.get("props") if isinstance(item.get("props"), dict) else {}
            if item_props.get("from_dimension"):
                continue
            layer = item.get("layer")
            try:
                twkt = transform_block_wkt_to_world(
                    str(raw_wkt),
                    bx,
                    by,
                    ix,
                    iy,
                    sx,
                    sy,
                    rot,
                )
            except Exception:
                twkt = None
            if not twkt:
                continue
            try:
                shp = shapely_from_wkt(twkt)
            except Exception:
                continue
            try:
                if not shp.intersects(bpoly):
                    continue
            except Exception:
                continue
            out.append((et, shp, str(layer) if layer is not None else None))

    return out


def _dedupe_circle_shapes_spatially(
    shapes: list[tuple[str, Any, str | None]],
    _bbox: tuple[float, float, float, float],
) -> tuple[list[tuple[str, Any, str | None]], dict[str, Any]]:
    """
    entities + 블록 정의에서 같은 원이 두 번 들어오는 경우(좌표가 거의 동일) 1개로 합친다.
    리스트 앞쪽(엔티티 테이블)을 같은 클러스터에서 우선 유지한다.
    중복 허용 거리는 검색 bbox가 아니라 주근 중심 점군의 크기로만 정해 인접 주근과 합쳐지지 않게 한다.
    """
    indexed: list[tuple[float, float, tuple[str, Any, str | None]]] = []
    rest: list[tuple[str, Any, str | None]] = []
    for et, shp, ly in shapes:
        if et in ("CIRCLE", "ARC"):
            xy, _ = _centroid_and_radius_from_shape(shp)
            if xy:
                indexed.append((xy[0], xy[1], (et, shp, ly)))
            continue
        gt = getattr(shp, "geom_type", "")
        if et == "ELLIPSE" and gt == "LineString":
            xy, _ = _centroid_and_radius_from_shape(shp)
            if xy:
                indexed.append((xy[0], xy[1], (et, shp, ly)))
            continue
        if et in ("LWPOLYLINE", "POLYLINE") and gt == "LineString":
            pr = _polyline_as_rebar_circle(et, shp)
            if pr:
                xy, _r = pr
                indexed.append((xy[0], xy[1], (et, shp, ly)))
                continue
            rest.append((et, shp, ly))
            continue
        if et == "POINT" and gt == "Point":
            try:
                indexed.append((float(shp.x), float(shp.y), (et, shp, ly)))
            except Exception:
                pass
            continue
        if gt == "LineString" and et == "LINE":
            rest.append((et, shp, ly))

    n = len(indexed)
    if n == 0:
        return rest, {
            "circle_spatial_dedup_removed": 0,
            "spatial_dup_tol": 0.0,
            "circle_shapes_before_dedup": 0,
            "circle_shapes_after_dedup": 0,
        }

    xs_i = [indexed[i][0] for i in range(n)]
    ys_i = [indexed[i][1] for i in range(n)]
    cloud_span = max(max(xs_i) - min(xs_i), max(ys_i) - min(ys_i), 1.0)
    # 이중 적재: 보통 수치 오차 수준. 인접 주근 간격은 cloud_span의 훨씬 작은 비율
    dup_tol = max(2.0, min(20.0, cloud_span / 90.0, cloud_span * 0.018))
    dt2 = dup_tol * dup_tol

    if n <= 1:
        only_item = indexed[0][2]
        return [only_item] + rest, {
            "circle_spatial_dedup_removed": 0,
            "spatial_dup_tol": round(dup_tol, 3),
            "circle_shapes_before_dedup": n,
            "circle_shapes_after_dedup": n,
        }

    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        pi, pj = find(i), find(j)
        if pi != pj:
            parent[pi] = pj

    for i in range(n):
        ax, ay = indexed[i][0], indexed[i][1]
        for j in range(i + 1, n):
            bx, by = indexed[j][0], indexed[j][1]
            dx, dy = ax - bx, ay - by
            if dx * dx + dy * dy <= dt2:
                union(i, j)

    members: dict[int, list[int]] = {}
    for i in range(n):
        r = find(i)
        members.setdefault(r, []).append(i)

    kept_circles: list[tuple[str, Any, str | None]] = []
    removed = 0
    for _root, inds in members.items():
        inds.sort()
        kept_circles.append(indexed[inds[0]][2])
        removed += len(inds) - 1

    stats = {
        "circle_spatial_dedup_removed": removed,
        "spatial_dup_tol": round(dup_tol, 3),
        "circle_shapes_before_dedup": n,
        "circle_shapes_after_dedup": len(kept_circles),
    }
    return kept_circles + rest, stats


def _load_combined_shapes_in_bbox(
    db: Session,
    commit_id: int,
    bbox: tuple[float, float, float, float],
    *,
    include_block_definitions: bool = True,
    block_inserts_cached: list[BlockInsert] | None = None,
) -> tuple[list[tuple[str, Any, str | None]], dict[str, Any]]:
    from_entities = _load_shapes_in_bbox(db, commit_id, bbox)
    if not include_block_definitions:
        merged, st = _dedupe_circle_shapes_spatially(from_entities, bbox)
        return merged, {"entity_table": len(from_entities), "block_definitions": 0, **st}
    from_blocks = _load_shapes_from_block_definitions(
        db, commit_id, bbox, block_inserts_cached=block_inserts_cached
    )
    combined = from_entities + from_blocks
    merged, st = _dedupe_circle_shapes_spatially(combined, bbox)
    return merged, {
        "entity_table": len(from_entities),
        "block_definitions": len(from_blocks),
        **st,
    }


def _median_sorted(vals: list[float]) -> float:
    n = len(vals)
    if n == 0:
        return 0.0
    mid = n // 2
    if n % 2:
        return vals[mid]
    return 0.5 * (vals[mid - 1] + vals[mid])


def _edge_band_from_sorted_coords(sorted_unique: list[float], span: float) -> float:
    """행·열 간격(gap) 중앙값으로 모서리 띠 두께 — 고정 비율보다 단면 밀도에 적응."""
    if len(sorted_unique) < 2:
        return max(span * 0.055, 3.0)
    gaps = [sorted_unique[i + 1] - sorted_unique[i] for i in range(len(sorted_unique) - 1)]
    gaps.sort()
    med_gap = _median_sorted(gaps)
    return max(med_gap * 0.42, span * 0.022, 3.0)


def _count_bars_rectangular(
    centers: list[tuple[float, float]],
    edge_tol_frac: float = 0.055,
) -> dict[str, Any]:
    """
    직사각 단면 가정: 상·하변(모서리 포함) = top/bot 수, 좌·우(모서리 제외) = left/right 수.
    모서리 띠 두께는 고정 비율 대신, 주근 y(또는 x) 좌표 간격의 중앙값으로 잡는다.
    """
    _ = edge_tol_frac  # 하위 호환용 인자(과거 고정 비율)
    if len(centers) < 4:
        return {
            "top_bot": None,
            "left_right": None,
            "top_count": None,
            "bottom_count": None,
            "left_side_count": None,
            "right_side_count": None,
            "total_unique": len(centers),
        }
    xs = [p[0] for p in centers]
    ys = [p[1] for p in centers]
    minx, maxx, miny, maxy = min(xs), max(xs), min(ys), max(ys)
    w, h = maxx - minx, maxy - miny
    ux = sorted({round(x, 4) for x in xs})
    uy = sorted({round(y, 4) for y in ys})
    tol_y = _edge_band_from_sorted_coords(uy, h)
    tol_x = _edge_band_from_sorted_coords(ux, w)
    # 한 줄도 못 잡을 때만 bbox 비율 폴백
    if tol_y <= 1e-6:
        tol_y = max(0.055 * min(w, h), 3.0)
    if tol_x <= 1e-6:
        tol_x = max(0.055 * min(w, h), 3.0)

    def on_top(p: tuple[float, float]) -> bool:
        return p[1] >= maxy - tol_y

    def on_bot(p: tuple[float, float]) -> bool:
        return p[1] <= miny + tol_y

    def on_left(p: tuple[float, float]) -> bool:
        return p[0] <= minx + tol_x

    def on_right(p: tuple[float, float]) -> bool:
        return p[0] >= maxx - tol_x

    top_c = sum(1 for p in centers if on_top(p))
    bot_c = sum(1 for p in centers if on_bot(p))
    left_side = sum(1 for p in centers if on_left(p) and not on_top(p) and not on_bot(p))
    right_side = sum(1 for p in centers if on_right(p) and not on_top(p) and not on_bot(p))

    # 한 변 기준 표시값(대칭일 때만 단일 숫자)
    tb = top_c if top_c == bot_c else None
    lr = left_side if left_side == right_side else None
    # 모서리는 상·하변에만 포함, 좌우 변은 모서리 제외 → 네 구역 합 = 전체 주근 수
    geom_total = top_c + bot_c + left_side + right_side
    sym_pair = None
    if tb is not None and lr is not None:
        sym_pair = 2 * (tb + lr)

    return {
        "top_bot": tb,
        "left_right": lr,
        "top_count": top_c,
        "bottom_count": bot_c,
        "left_side_count": left_side,
        "right_side_count": right_side,
        "total_unique": len(centers),
        "geom_main_total": geom_total,
        "symmetry_check_pair": sym_pair,
        "top_bottom_symmetric": top_c == bot_c,
        "left_right_symmetric": left_side == right_side,
        "edge_tol_y": round(tol_y, 4),
        "edge_tol_x": round(tol_x, 4),
    }


def _line_to_segments(ln: LineString) -> list[LineString]:
    c = list(ln.coords)
    if len(c) < 2:
        return []
    out: list[LineString] = []
    for i in range(len(c) - 1):
        out.append(LineString([c[i], c[i + 1]]))
    return out


def _cluster_count(sorted_vals: list[float], merge_tol: float) -> int:
    if not sorted_vals:
        return 0
    n = 1
    last = sorted_vals[0]
    for v in sorted_vals[1:]:
        if v - last > merge_tol:
            n += 1
            last = v
    return n


def _median_unsorted(vals: list[float]) -> float:
    if not vals:
        return 0.0
    return _median_sorted(sorted(vals))


def _median_gap_unique(sorted_unique: list[float]) -> float:
    if len(sorted_unique) < 2:
        return 0.0
    gaps = [
        sorted_unique[i + 1] - sorted_unique[i]
        for i in range(len(sorted_unique) - 1)
    ]
    gaps.sort()
    return _median_sorted(gaps)


def _uf_find(parent: list[int], i: int) -> int:
    while parent[i] != i:
        parent[i] = parent[parent[i]]
        i = parent[i]
    return i


def _uf_union(parent: list[int], i: int, j: int) -> None:
    ri, rj = _uf_find(parent, i), _uf_find(parent, j)
    if ri != rj:
        parent[ri] = rj


def _cluster_points_by_eps(
    points: list[tuple[float, float]],
    eps: float,
) -> list[list[tuple[float, float]]]:
    """유클리드 거리 eps 이내를 같은 클러스터(Union-Find)."""
    n = len(points)
    if n == 0:
        return []
    parent = list(range(n))
    e2 = eps * eps
    for i in range(n):
        xi, yi = points[i]
        for j in range(i + 1, n):
            xj, yj = points[j]
            dx, dy = xi - xj, yi - yj
            if dx * dx + dy * dy <= e2:
                _uf_union(parent, i, j)
    buckets: dict[int, list[int]] = {}
    for i in range(n):
        r = _uf_find(parent, i)
        buckets.setdefault(r, []).append(i)
    return [[points[i] for i in idxs] for idxs in buckets.values()]


def _merge_beam_vertical_twin_rebar_clusters(
    clusters: list[list[tuple[float, float]]],
    y_ref: float,
    strip_xc: float,
    gx: float,
    gy: float,
    *,
    min_cluster: int,
    meta: dict[str, Any],
    max_centroid_dy: float | None = None,
) -> list[list[tuple[float, float]]]:
    """
    보 단면: 상부근·하부근 원이 세로로 떨어져 한 eps 클러스터가 둘로 갈라질 때,
    y_ref(형태·단면 앵커)를 세로로 가로지르는 두 덩어리를 한 단면으로 합친다.
    인접 층 두 단면이 y_ref 양쪽에 있으면 동일 조건이 성립할 수 있어,
    두 덩어리 **중심 간 세로 거리**가 한 칸 단면 범위를 넘으면 합치지 않는다.
    """
    if len(clusters) < 2 or gy <= 1e-6:
        return clusters

    def _mean_xy(cl: list[tuple[float, float]]) -> tuple[float, float]:
        n = len(cl)
        return sum(p[0] for p in cl) / n, sum(p[1] for p in cl) / n

    # 한 세트(상·하부근)로 볼 수 있는 최대 세로 분리 — 층 간(수천 단위) twin 오인 방지
    cap_dy = min(1420.0, max(560.0, gy * 32.0 + gx * 14.0))
    if max_centroid_dy is not None:
        cap_dy = min(cap_dy, float(max_centroid_dy))
    meta["beam_twin_merge_max_centroid_dy"] = round(cap_dy, 4)

    big = [c for c in clusters if len(c) >= max(3, min_cluster - 1)]
    if len(big) < 2:
        return clusters

    best_ca: list[tuple[float, float]] | None = None
    best_cb: list[tuple[float, float]] | None = None
    best_score = -1
    for i, ci in enumerate(big):
        for cj in big[i + 1 :]:
            ax, ay = _mean_xy(ci)
            bx, by = _mean_xy(cj)
            if abs(ax - bx) > max(gx * 5.5, 240.0):
                continue
            hi_y = max(ay, by)
            lo_y = min(ay, by)
            if not (lo_y - gy * 4.0 <= y_ref <= hi_y + gy * 4.0):
                continue
            if hi_y - lo_y > cap_dy:
                continue
            score = len(ci) + len(cj)
            if score > best_score:
                best_score = score
                best_ca, best_cb = ci, cj
    if best_ca is None or best_cb is None:
        return clusters
    ca, cb = best_ca, best_cb
    merged = ca + cb
    rest = [c for c in clusters if c is not ca and c is not cb]
    meta["beam_twin_rebar_cluster_merge"] = True
    meta["beam_twin_cluster_sizes"] = [len(ca), len(cb)]
    return [merged] + rest


def _bbox_from_centerline_cluster(
    points: list[tuple[float, float]],
    strip_xc: float,
    y_ref: float,
    loose_hw: float,
    loose_hh: float,
    *,
    min_cluster: int = 4,
    segment_y_span: float | None = None,
    strip_x_half_exclusive: float | None = None,
    expected_main_bars: int | None = None,
    anchor_y_bounds: tuple[float, float] | None = None,
    beam_like_section: bool = False,
) -> tuple[tuple[float, float, float, float] | None, dict[str, Any]]:
    """
    주근 중심들의 중앙선(median x, median y)을 기준으로,
    중앙 간격(median gap)으로 링크 거리를 잡아 근접 클러스터링한 뒤,
    스트립 중심·행 앵커(y_ref)에 가장 맞는 덩어리의 bbox(+패딩)를 반환한다.
    """
    meta: dict[str, Any] = {"mode": "centerline_cluster"}
    if len(points) < min_cluster:
        meta["reason"] = "too_few_points"
        return None, meta

    if strip_x_half_exclusive is not None and strip_x_half_exclusive > 80.0:
        meta["strip_x_half_exclusive"] = round(strip_x_half_exclusive, 4)
    if expected_main_bars is not None and expected_main_bars >= 6:
        meta["expected_main_bars"] = int(expected_main_bars)

    xband = loose_hw * 0.93
    filtered = [p for p in points if abs(p[0] - strip_xc) <= xband]
    if len(filtered) >= min_cluster:
        work = filtered
        meta["x_band_applied"] = True
        meta["x_band"] = round(xband, 4)
    else:
        work = list(points)
        meta["x_band_applied"] = False

    # 템플릿 행 세로 구간(부재 한 칸) 밖의 주근은 제외 — 인접 행(ΔY 수천) 단면이
    # 넓은 anchor_band + union-find 로 한 덩어리가 되어 dy:3000+ 잘못 매칭되는 것을 막는다.
    if anchor_y_bounds is not None and len(anchor_y_bounds) >= 2:
        try:
            y_lo_b = float(anchor_y_bounds[0])
            y_hi_b = float(anchor_y_bounds[1])
        except (TypeError, ValueError):
            y_lo_b, y_hi_b = None, None
        if (
            y_lo_b is not None
            and y_hi_b is not None
            and y_hi_b > y_lo_b + 40.0
        ):
            span_b = y_hi_b - y_lo_b
            margin_b = max(260.0, min(1400.0, span_b * 0.20 + 240.0))
            meta["anchor_y_bounds_world"] = [round(y_lo_b, 4), round(y_hi_b, 4)]
            meta["row_y_bounds_margin"] = round(margin_b, 2)
            y_row_pts = [
                p for p in work
                if (y_lo_b - margin_b) <= p[1] <= (y_hi_b + margin_b)
            ]
            if len(y_row_pts) >= min_cluster:
                work = y_row_pts
                meta["row_y_bounds_filter"] = True
            else:
                meta["row_y_bounds_filter"] = False
                meta["row_y_bounds_pts_below_min"] = len(y_row_pts)

    twin_merge_max_dy: float | None = None
    if beam_like_section:
        _twin_caps: list[float] = []
        if anchor_y_bounds is not None and len(anchor_y_bounds) >= 2:
            try:
                _spw = float(anchor_y_bounds[1]) - float(anchor_y_bounds[0])
                if _spw > 80.0:
                    _twin_caps.append(min(1500.0, max(520.0, _spw * 0.88 + 380.0)))
            except (TypeError, ValueError):
                pass
        if segment_y_span is not None and float(segment_y_span) > 80.0:
            _twin_caps.append(
                min(1500.0, max(520.0, float(segment_y_span) * 0.90 + 400.0))
            )
        if _twin_caps:
            twin_merge_max_dy = min(_twin_caps)
            meta["beam_twin_merge_cap_from_row_window"] = round(twin_merge_max_dy, 4)

    def _cluster_mean_x(cl: list[tuple[float, float]]) -> float:
        return sum(p[0] for p in cl) / len(cl)

    def _x_allow_tiers() -> list[float | None]:
        if strip_x_half_exclusive is None or strip_x_half_exclusive <= 40.0:
            return [None]
        h = strip_x_half_exclusive
        # 이웃 열 클러스터(dx≈2h)는 마지막에 None 으로 다시 허용하지 않는다.
        return [h * 1.06, h * 1.26, h * 1.48, h * 1.68, h * 1.86]

    def _pick_best_cluster_x(clist: list[list[tuple[float, float]]], need: int):
        tiers = _x_allow_tiers()
        for ti, allow in enumerate(tiers):
            qual: list[list[tuple[float, float]]] = []
            for cl in clist:
                if len(cl) < need:
                    continue
                if allow is None:
                    qual.append(cl)
                else:
                    if abs(_cluster_mean_x(cl) - strip_xc) <= allow:
                        qual.append(cl)
            if qual:
                meta["cluster_x_allow_tier"] = ti
                meta["cluster_x_allow"] = None if allow is None else round(allow, 4)
                return min(qual, key=sort_key)
        qual_any = [cl for cl in clist if len(cl) >= need]
        if not qual_any:
            return None
        meta["cluster_x_allow_tier"] = "x_nearest"
        meta["cluster_x_allow"] = "min_abs_dx"
        return min(
            qual_any,
            key=lambda cl: (abs(_cluster_mean_x(cl) - strip_xc), sort_key(cl)),
        )

    # 같은 열에 층이 위·아래로 쌓인 경우, 앵커와 먼 층의 주근이 union-find 로 이어지지 않게
    anchor_band = min(loose_hh * 0.88, max(3200.0, loose_hh * 0.38))
    # 보 세로: segment_y_span 으로 anchor_band 를 키우면 row 창이 넓을 때 인접 층 원이 한 밴드에 들어와
    # twin merge·클러스터가 층을 가로지르는 경우가 많다 → 기둥 경로만 확장한다.
    if (
        not beam_like_section
        and segment_y_span is not None
        and segment_y_span > 120.0
    ):
        anchor_band = max(
            anchor_band,
            min(segment_y_span * 0.62 + 900.0, loose_hh * 0.94),
        )
    meta["anchor_band"] = round(anchor_band, 2)
    if segment_y_span is not None:
        meta["segment_y_span"] = round(segment_y_span, 2)
    # 행 Y창 필터가 좁게 먹힌 뒤에도 segment_y_span 이 과대면 anchor_band 가 6000+ 로 커져
    # 인접 층 주근이 한 클러스터로 붙는다 → 앵커 창 폭에 맞춰 상한을 둔다.
    if (
        meta.get("row_y_bounds_filter")
        and isinstance(meta.get("anchor_y_bounds_world"), (list, tuple))
        and len(meta["anchor_y_bounds_world"]) >= 2
    ):
        try:
            aw0, aw1 = float(meta["anchor_y_bounds_world"][0]), float(meta["anchor_y_bounds_world"][1])
            span_aw = aw1 - aw0
            if span_aw > 80.0 and span_aw < 5200.0:
                mg = float(meta.get("row_y_bounds_margin") or 400.0)
                cap_band = max(440.0, span_aw * 0.58 + mg * 1.05)
                anchor_band = min(anchor_band, cap_band)
                meta["anchor_band"] = round(anchor_band, 2)
                meta["anchor_band_capped_to_row_window"] = True
        except (TypeError, ValueError):
            pass
    banded = [p for p in work if abs(p[1] - y_ref) <= anchor_band]
    if len(banded) >= min_cluster:
        work = banded
        meta["y_anchor_band_applied"] = True
        meta["y_anchor_band"] = round(anchor_band, 2)
    else:
        meta["y_anchor_band_applied"] = False

    if strip_x_half_exclusive is not None and strip_x_half_exclusive > 50.0:
        h = strip_x_half_exclusive
        narrowed: list[tuple[float, float]] | None = None
        for mul in (1.62, 1.78, 1.98, 2.22, 2.48):
            cand = [p for p in work if abs(p[0] - strip_xc) <= h * mul]
            if len(cand) >= min_cluster:
                narrowed = cand
                meta["circle_prefilter_x_mul"] = round(mul, 4)
                break
        if narrowed is not None:
            work = narrowed

    xs = [p[0] for p in work]
    ys = [p[1] for p in work]
    cx = _median_unsorted(xs)
    cy = _median_unsorted(ys)
    meta["centerline_x"] = round(cx, 4)
    meta["centerline_y"] = round(cy, 4)

    ux = sorted({round(x, 4) for x in xs})
    uy = sorted({round(y, 4) for y in ys})
    gx = _median_gap_unique(ux)
    gy = _median_gap_unique(uy)
    if gx <= 1e-6:
        gx = max(loose_hw * 0.028, 12.0)
    if gy <= 1e-6:
        gy = max(loose_hh * 0.028, 12.0)
    meta["median_gap_x"] = round(gx, 4)
    meta["median_gap_y"] = round(gy, 4)

    eps_link = max(gx, gy) * 1.12
    if beam_like_section and anchor_y_bounds is not None and len(anchor_y_bounds) >= 2:
        try:
            _aw0, _aw1 = float(anchor_y_bounds[0]), float(anchor_y_bounds[1])
            _span_aw = _aw1 - _aw0
            if _span_aw > 80.0:
                _eps_cap = max(
                    min(gx, gy) * 1.22 + 10.0,
                    gx * 1.18,
                )
                _eps_cap = min(
                    max(_eps_cap, _span_aw * 0.36 + 110.0),
                    _span_aw * 0.58 + 230.0,
                )
                _eps_cap = max(52.0, _eps_cap)
                if eps_link > _eps_cap:
                    meta["cluster_eps_beam_row_cap"] = round(_eps_cap, 4)
                    eps_link = _eps_cap
        except (TypeError, ValueError):
            pass
    meta["cluster_eps"] = round(eps_link, 4)

    clusters = _cluster_points_by_eps(work, eps_link)
    if not clusters:
        meta["reason"] = "no_clusters"
        return None, meta
    if beam_like_section:
        clusters = _merge_beam_vertical_twin_rebar_clusters(
            clusters,
            y_ref,
            strip_xc,
            gx,
            gy,
            min_cluster=min_cluster,
            meta=meta,
            max_centroid_dy=twin_merge_max_dy,
        )

    exp = expected_main_bars

    def _filter_clusters_by_mainbar_expect(
        clist: list[list[tuple[float, float]]],
    ) -> list[list[tuple[float, float]]]:
        if exp is None or exp < 8:
            return clist
        floor_n = max(min_cluster, int(exp * 0.42))
        filtered_c = [c for c in clist if len(c) >= floor_n]
        if len(filtered_c) >= 1:
            meta.setdefault("cluster_prefilter_min_pts", floor_n)
            return filtered_c
        return clist

    clusters = _filter_clusters_by_mainbar_expect(clusters)

    def sort_key(cl: list[tuple[float, float]]) -> tuple[int, int, float, int, float, float, int]:
        """
        같은 스트립에 위·아래 층 단면이 나란히 있을 때,
        '주근 개수 많음'보다 SECTION 행 앵커(y_ref)에 세로로 가까운 덩어리를 먼저 고른다.
        (예: -2층 18본 vs -1층 14본이 한 열에 있으면, 큰 쪽만 고르면 층이 뒤바뀐다.)
        한 단면이 주근 간격 때문에 위·아래 두 덩어리로 쪼개질 때는, 앵커 Y를 세로로 포함하고
        세로 폭이 큰 덩어리를 우선한다(하단 라벨만 보고 앵커가 밀린 경우 보정).
        표의 MAIN BAR 본수가 있으면 스플린터(4점 등)보다 그에 가까운 덩어리를 우선한다.
        """
        n = len(cl)
        cxb = sum(p[0] for p in cl) / n
        cyb = sum(p[1] for p in cl) / n
        miny_c = min(p[1] for p in cl)
        maxy_c = max(p[1] for p in cl)
        span_y = maxy_c - miny_c
        tol_y = max(120.0, loose_hh * 0.018)
        contains = 1 if (miny_c - tol_y) <= y_ref <= (maxy_c + tol_y) else 0
        dy = abs(cyb - y_ref)
        dx = abs(cxb - strip_xc)
        exact = 0 if (exp is not None and exp >= 6 and n == exp) else 1
        bar_fit = abs(n - exp) if exp is not None and exp >= 6 else 0
        # dy를 span_y보다 앞에 두어, 인접 층이 한 클러스터로 붙어 y_ref를 세로로
        # '포함'만 하고 중심이 멀어진 경우(-span_y만으로는 큰 덩어리가 이김)를 줄인다.
        return (exact, -contains, bar_fit, dy, -span_y, dx, -n)

    # 이전: min(clusters) → 1점짜리가 y_ref에 더 가깝다는 이유로 12점 덩어리보다 이김 →
    # best_cluster_too_small 후 느슨 창 전체 분석 → 외곽·검색박스 폭주(25200×17820 등).
    clusters_active: list[list[tuple[float, float]]] = clusters
    best = _pick_best_cluster_x(clusters_active, min_cluster)
    if best is None:
        eps2 = max(eps_link * 1.75, max(gx, gy) * 0.55)
        if beam_like_section and anchor_y_bounds is not None and len(anchor_y_bounds) >= 2:
            try:
                _aw0, _aw1 = float(anchor_y_bounds[0]), float(anchor_y_bounds[1])
                _span_aw = _aw1 - _aw0
                if _span_aw > 80.0:
                    _eps2_cap = min(
                        _span_aw * 0.74 + 280.0,
                        max(eps_link * 1.38, gy * 22.0 + 200.0),
                    )
                    eps2 = min(eps2, max(_eps2_cap, eps_link * 1.04))
                    meta["cluster_eps_retry_beam_cap"] = round(_eps2_cap, 4)
            except (TypeError, ValueError):
                pass
        clusters2 = _cluster_points_by_eps(work, eps2)
        if beam_like_section and clusters2:
            clusters2 = _merge_beam_vertical_twin_rebar_clusters(
                clusters2,
                y_ref,
                strip_xc,
                gx,
                gy,
                min_cluster=min_cluster,
                meta=meta,
                max_centroid_dy=twin_merge_max_dy,
            )
        clusters2 = _filter_clusters_by_mainbar_expect(clusters2)
        meta["cluster_eps_retry"] = round(eps2, 4)
        meta["cluster_count_retry"] = len(clusters2)
        b2 = _pick_best_cluster_x(clusters2, min_cluster)
        if b2 is not None:
            best = b2
            clusters_active = clusters2
            eps_link = eps2
    if best is None and len(work) >= min_cluster:
        best = list(work)
        meta["reason"] = "fallback_y_band_points"
    elif best is None:
        for need2 in (3, 2):
            best = _pick_best_cluster_x(clusters_active, need2)
            if best is not None:
                meta["reason"] = f"relaxed_min_cluster_{need2}"
                break
    if best is None:
        meta["reason"] = "best_cluster_too_small"
        meta["cluster_size"] = max((len(c) for c in clusters_active), default=0)
        return None, meta

    meta["cluster_size"] = len(best)
    _cap_prev = 56
    meta["cluster_member_centers_preview"] = [
        [round(p[0], 4), round(p[1], 4)] for p in best[:_cap_prev]
    ]
    if len(best) > _cap_prev:
        meta["cluster_member_centers_preview_truncated"] = len(best)
    _cxb = sum(p[0] for p in best) / len(best)
    _cyb = sum(p[1] for p in best) / len(best)
    meta["cluster_centroid"] = [round(_cxb, 4), round(_cyb, 4)]
    meta["cluster_dy_to_anchor"] = round(abs(_cyb - y_ref), 4)
    minx = min(p[0] for p in best)
    maxx = max(p[0] for p in best)
    miny = min(p[1] for p in best)
    maxy = max(p[1] for p in best)

    pad = max(gx, gy) * 0.68 + 40.0
    pad = min(pad, max(loose_hw, loose_hh) * 0.48)

    bbox = (minx - pad, miny - pad, maxx + pad, maxy + pad)
    meta["cluster_bbox"] = [round(bbox[i], 4) for i in range(4)]
    return bbox, meta


def _display_bbox_from_circle_centers(
    centers: list[tuple[float, float]],
) -> tuple[float, float, float, float] | None:
    """
    캔버스·디버그 표시용 사각형 — 조회용 느슨 bbox(세로 수만 단위) 대신 주근 중심만 감싼 최소 영역.
    """
    if len(centers) < 1:
        return None
    xs = [p[0] for p in centers]
    ys = [p[1] for p in centers]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    span = max(maxx - minx, maxy - miny, 1.0)
    pad = max(span * 0.1, 45.0)
    return (minx - pad, miny - pad, maxx + pad, maxy + pad)


def _filter_shapes_by_bbox(
    shapes: list[tuple[str, Any, str | None]],
    bbox: tuple[float, float, float, float],
) -> list[tuple[str, Any, str | None]]:
    minx, miny, maxx, maxy = bbox
    bpoly = shapely_box(minx, miny, maxx, maxy)
    out: list[tuple[str, Any, str | None]] = []
    for et, shp, ly in shapes:
        try:
            if shp.intersects(bpoly):
                out.append((et, shp, ly))
        except Exception:
            continue
    return out


def _closed_polyline_gap_xy(coords: list[tuple[float, ...]]) -> float:
    if len(coords) < 2:
        return 1e12
    x0, y0 = float(coords[0][0]), float(coords[0][1])
    xn, yn = float(coords[-1][0]), float(coords[-1][1])
    return math.hypot(x0 - xn, y0 - yn)


def _lwpoly_as_outline_rectangle(et: str, shp: LineString) -> tuple[float, float, float, float] | None:
    """
    닫힌 폴리라인 중 축에 평행한 직사각 외곽선(콘크리트·후프 외곽 등).
    작은 원형 주근 폴리라인은 제외한다.
    """
    if et not in ("LWPOLYLINE", "POLYLINE"):
        return None
    if getattr(shp, "geom_type", "") != "LineString":
        return None
    if _polyline_as_rebar_circle(et, shp):
        return None
    coords = list(shp.coords)
    if len(coords) < 5:
        return None
    b = shp.bounds
    minx, miny, maxx, maxy = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    w, h = maxx - minx, maxy - miny
    if w < 8.0 or h < 8.0:
        return None
    ar = w / h if h else 999.0
    if ar < 0.05 or ar > 22.0:
        return None
    span = max(w, h, 1e-6)
    gap = _closed_polyline_gap_xy(coords)
    if gap > max(4.0, span * 0.035):
        return None
    tol = max(w, h) * 0.075
    for xy in coords[:-1]:
        x, y = float(xy[0]), float(xy[1])
        on_edge = min(abs(x - minx), abs(x - maxx)) < tol or min(abs(y - miny), abs(y - maxy)) < tol
        if not on_edge:
            return None
    return (minx, miny, maxx, maxy)


def _merged_centers_inside_rect(
    merged: list[tuple[float, float]],
    bb: tuple[float, float, float, float],
    eps: float,
) -> bool:
    minx, miny, maxx, maxy = bb
    for px, py in merged:
        if not (minx + eps <= px <= maxx - eps and miny + eps <= py <= maxy - eps):
            return False
    return True


def _collect_outline_rectangle_candidates(
    shapes: list[tuple[str, Any, str | None]],
) -> list[tuple[tuple[float, float, float, float], str]]:
    out: list[tuple[tuple[float, float, float, float], str]] = []
    seen: set[tuple[float, float, float, float]] = set()
    for et, shp, _ly in shapes:
        gt = getattr(shp, "geom_type", "")
        if et in ("LWPOLYLINE", "POLYLINE") and gt == "LineString":
            bb = _lwpoly_as_outline_rectangle(et, shp)
            if bb is None:
                continue
            key = tuple(round(bb[i], 2) for i in range(4))
            if key in seen:
                continue
            seen.add(key)
            out.append((bb, et))
    return out


def _pick_section_outline_size_mm(
    candidates: list[tuple[tuple[float, float, float, float], str]],
    merged: list[tuple[float, float]],
    merge_tol: float,
) -> tuple[int | None, int | None, dict[str, Any]]:
    """
    주근 중심보다 한 겹 바깥의 닫힌 직사각형 중, 주근을 모두 포함하는 것들 중
    면적이 가장 큰 것을 단면 외곽(흰색 박스)으로 본다(후프보다 큰 콘크리트 외곽 우선).
    """
    meta: dict[str, Any] = {"candidate_rects": len(candidates)}
    if not candidates or len(merged) < 2:
        meta["reason"] = "no_candidates_or_bars"
        return None, None, meta

    cxmin = min(p[0] for p in merged)
    cxmax = max(p[0] for p in merged)
    cymin = min(p[1] for p in merged)
    cymax = max(p[1] for p in merged)
    cbw, cbh = cxmax - cxmin, cymax - cymin
    cspan = max(cbw, cbh, 1.0)
    eps = max(0.8, merge_tol * 0.18)
    min_inset_need = max(0.55, merge_tol * 0.1, cspan * 0.002)

    scored: list[tuple[float, tuple[float, float, float, float], str]] = []

    for bb, src in candidates:
        mx0, my0, mx1, my1 = bb
        if not _merged_centers_inside_rect(merged, bb, eps):
            continue
        w, h = mx1 - mx0, my1 - my0
        if w <= cbw + 0.5 or h <= cbh + 0.5:
            continue
        inset_l = cxmin - mx0
        inset_r = mx1 - cxmax
        inset_b = cymin - my0
        inset_t = my1 - cymax
        min_inset = min(inset_l, inset_r, inset_b, inset_t)
        if min_inset < min_inset_need:
            continue
        if w > max(cbw * 12.0, cspan * 9.0, 25000.0) or h > max(cbh * 12.0, cspan * 9.0, 25000.0):
            continue
        scored.append((w * h, bb, src))

    if not scored:
        # 외곽 여유가 작은 도면: 포함·크기만으로 최대 면적 후보
        for bb, src in candidates:
            if not _merged_centers_inside_rect(merged, bb, eps):
                continue
            mx0, my0, mx1, my1 = bb
            w, h = mx1 - mx0, my1 - my0
            if w <= cbw or h <= cbh:
                continue
            if w > max(cbw * 15.0, 30000.0) or h > max(cbh * 15.0, 30000.0):
                continue
            scored.append((w * h, bb, src))

    if not scored:
        meta["reason"] = "no_valid_outline"
        return None, None, meta

    scored.sort(key=lambda t: -t[0])
    area, best_bb, src = scored[0]
    mx0, my0, mx1, my1 = best_bb
    w = mx1 - mx0
    h = my1 - my0
    meta["picked_area"] = round(area, 2)
    meta["picked_bbox"] = [round(best_bb[i], 4) for i in range(4)]
    meta["picked_source"] = src
    meta["bar_bbox"] = [round(x, 4) for x in (cxmin, cymin, cxmax, cymax)]
    return int(round(w)), int(round(h)), meta


def _pick_beam_stem_section_outline_mm(
    merged: list[tuple[float, float]],
    merge_tol: float,
) -> tuple[int | None, int | None, dict[str, Any]]:
    """
    T·I 형 보: B는 아래쪽 주근 띠(하부 돌출) 가로 범위(+피복), H는 주근 세로 범위(+피복).
    하부 가로폭이 상부·전체보다 뚜렷이 좁을 때만 적용하고, 직사각에 가깝으면 None.
    """
    meta: dict[str, Any] = {"mode": "beam_stem", "pick": "stem_band"}
    if not merged or len(merged) < 2:
        meta["reason"] = "too_few_merged"
        return None, None, meta
    xs = [p[0] for p in merged]
    ys = [p[1] for p in merged]
    minx_b, maxx_b = min(xs), max(xs)
    miny_b, maxy_b = min(ys), max(ys)
    span_y = maxy_b - miny_b
    span_x = maxx_b - minx_b
    if span_y < 14.0 or span_x < 14.0:
        meta["reason"] = "span_too_small"
        return None, None, meta
    y_cut = miny_b + 0.40 * span_y
    bot = [p for p in merged if p[1] <= y_cut]
    if len(bot) < 2:
        bot = [p for p in merged if p[1] <= miny_b + 0.55 * span_y]
    if len(bot) < 2:
        bot = list(merged)
    stem_x0, stem_x1 = min(p[0] for p in bot), max(p[0] for p in bot)
    stem_w = stem_x1 - stem_x0
    stem_frac = stem_w / max(span_x, 1e-6)
    meta["stem_width_ratio"] = round(float(stem_frac), 4)
    y_mid = miny_b + 0.48 * span_y
    top = [p for p in merged if p[1] >= y_mid]
    tw = span_x
    if len(top) >= 2:
        tw = max(p[0] for p in top) - min(p[0] for p in top)
    meta["top_rebar_width"] = round(float(tw), 4)
    if stem_frac >= 0.88 and stem_w >= 0.90 * tw:
        meta["reason"] = "rectangular_like"
        return None, None, meta
    cover = max(merge_tol * 2.6, span_y * 0.062, 22.0)
    w_mm = int(round(stem_w + 2.0 * cover))
    h_mm = int(round(span_y + 2.0 * cover))
    bx0 = stem_x0 - cover
    bx1 = stem_x1 + cover
    by0 = miny_b - cover
    by1 = maxy_b + cover
    meta["picked_bbox"] = [round(bx0, 4), round(by0, 4), round(bx1, 4), round(by1, 4)]
    meta["picked_source"] = "beam_stem_rebar_band"
    meta["bar_bbox"] = [round(minx_b, 4), round(miny_b, 4), round(maxx_b, 4), round(maxy_b, 4)]
    return w_mm, h_mm, meta


def _collect_circles_lines_radii(
    shapes: list[tuple[str, Any, str | None]],
) -> tuple[list[tuple[float, float]], list[float], list[LineString]]:
    """analyze_section_geometry와 동일 규칙으로 원 중심·반지름·선 목록 추출."""
    circle_centers: list[tuple[float, float]] = []
    radii: list[float] = []
    lines_out: list[LineString] = []
    for et, shp, _ly in shapes:
        gt = getattr(shp, "geom_type", "")
        if et == "CIRCLE":
            xy, r = _centroid_and_radius_from_shape(shp)
            if xy:
                circle_centers.append(xy)
                if r:
                    radii.append(r)
            continue
        if et == "ARC":
            xy, r = _centroid_and_radius_from_shape(shp)
            if xy:
                circle_centers.append(xy)
                if r:
                    radii.append(r)
            continue
        if et == "ELLIPSE" and gt == "LineString":
            xy, r = _centroid_and_radius_from_shape(shp)
            if xy:
                circle_centers.append(xy)
                if r:
                    radii.append(r)
            continue
        if et == "POINT" and gt == "Point":
            try:
                circle_centers.append((float(shp.x), float(shp.y)))
                radii.append(2.0)
            except Exception:
                pass
            continue
        if et in ("LWPOLYLINE", "POLYLINE") and gt == "LineString":
            pr = _polyline_as_rebar_circle(et, shp)
            if pr:
                xy, r = pr
                circle_centers.append((xy[0], xy[1]))
                if r:
                    radii.append(r)
                continue
            lines_out.append(LineString(list(shp.coords)))
            continue
        if gt == "LineString" and et == "LINE":
            lines_out.append(LineString(list(shp.coords)))
    return circle_centers, radii, lines_out


def _count_mid_cross_ties(
    lines: list[LineString],
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
) -> tuple[int, int]:
    """
    후프(닫힌 사각 외곽)·상하좌우 변에 붙은 선은 제외하고,
    단면 내부를 가로·세로로 가로지르는 선만 X/Y 타이바로 집계한다.
    세그먼트별로 판별하며, y(또는 x)가 다른 여러 개는 각각 1개로 센다.
    """
    w = maxx - minx
    h = maxy - miny
    if w <= 1.0 or h <= 1.0:
        return 0, 0
    hoop_y = max(0.045 * h, 6.0)
    hoop_x = max(0.045 * w, 6.0)

    x_mids: list[float] = []
    y_mids: list[float] = []
    for ln in lines:
        for seg in _line_to_segments(ln):
            b = seg.bounds
            bx0, by0, bx1, by1 = b[0], b[1], b[2], b[3]
            bw = bx1 - bx0
            bh = by1 - by0
            mys = 0.5 * (by0 + by1)
            mxs = 0.5 * (bx0 + bx1)

            # 긴 가로선: 외곽 상·하 변(후프) 제외
            if bw >= 0.40 * w and bh <= max(0.20 * h, bw * 0.48, 2.0):
                if mys > miny + hoop_y and mys < maxy - hoop_y:
                    x_mids.append(mys)
                continue

            # 긴 세로선: 좌·우 외곽 변 제외
            if bh >= 0.40 * h and bw <= max(0.20 * w, bh * 0.48, 2.0):
                if mxs > minx + hoop_x and mxs < maxx - hoop_x:
                    y_mids.append(mxs)

    x_mids.sort()
    y_mids.sort()
    merge_y = max(0.055 * h, 7.0)
    merge_x = max(0.055 * w, 7.0)
    nx = _cluster_count(x_mids, merge_y)
    ny = _cluster_count(y_mids, merge_x)
    return min(nx, 8), min(ny, 8)


def _parse_pure_section_dimension_mm(text: str | None) -> int | None:
    """단면 치수 전용: `500`, ` 700 ` 처럼 숫자만 있는 문자열(mm)."""
    if not text:
        return None
    m = _RE_PURE_SECTION_DIM_MM.match(str(text).strip())
    if not m:
        return None
    v = int(m.group(1))
    if 100 <= v <= 4000:
        return v
    return None


def _entity_text_from_props(props: Any) -> str:
    if not isinstance(props, dict):
        return ""
    raw = str(props.get("text") or props.get("value") or "").strip()
    if "\n" in raw:
        raw = raw.split("\n", 1)[0].strip()
    return raw


def _entity_rotation_deg_from_props(props: Any) -> float | None:
    if not isinstance(props, dict):
        return None
    rot = props.get("rotation")
    try:
        return float(rot) if rot is not None else None
    except (TypeError, ValueError):
        return None


def _entity_xy_for_text_filter(ent: Entity) -> tuple[float, float] | None:
    if ent.centroid is not None:
        try:
            c = to_shape(ent.centroid)
            return float(c.x), float(c.y)
        except Exception:
            pass
    if ent.geom is not None:
        try:
            g = to_shape(ent.geom)
            c = g.centroid
            return float(c.x), float(c.y)
        except Exception:
            pass
    return None


def _expand_bbox_pad(
    bbox: tuple[float, float, float, float],
    pad: float,
) -> tuple[float, float, float, float]:
    minx, miny, maxx, maxy = bbox
    return (minx - pad, miny - pad, maxx + pad, maxy + pad)


def _load_dimension_text_samples_in_bbox(
    db: Session,
    commit_id: int,
    bbox: tuple[float, float, float, float],
) -> list[dict[str, Any]]:
    """단면 근처 TEXT/MTEXT/ATTRIB 중 순수 치수 숫자 후보."""
    minx, miny, maxx, maxy = bbox
    bpoly = shapely_box(minx, miny, maxx, maxy)
    env = ST_MakeEnvelope(minx, miny, maxx, maxy, 0)
    out: list[dict[str, Any]] = []
    spatial_or = or_(
        and_(Entity.bbox.isnot(None), ST_Intersects(Entity.bbox, env)),
        and_(
            Entity.bbox.is_(None),
            Entity.centroid.isnot(None),
            ST_Intersects(Entity.centroid, env),
        ),
        and_(
            Entity.bbox.is_(None),
            Entity.centroid.is_(None),
            Entity.geom.isnot(None),
            ST_Intersects(Entity.geom, env),
        ),
    )
    for ent in (
        db.query(Entity)
        .filter(
            Entity.commit_id == commit_id,
            Entity.entity_type.in_(("TEXT", "MTEXT", "ATTRIB")),
            spatial_or,
        )
        .all()
    ):
        xy = _entity_xy_for_text_filter(ent)
        if xy is None:
            continue
        try:
            pt = Point(xy[0], xy[1])
        except Exception:
            continue
        if ent.bbox is not None:
            try:
                eb = to_shape(ent.bbox)
                if not eb.intersects(bpoly):
                    continue
            except Exception:
                if not bpoly.intersects(pt):
                    continue
        else:
            try:
                if not bpoly.intersects(pt):
                    continue
            except Exception:
                continue
        props = ent.props if isinstance(ent.props, dict) else {}
        t = _entity_text_from_props(props)
        val = _parse_pure_section_dimension_mm(t)
        if val is None:
            continue
        out.append(
            {
                "x": xy[0],
                "y": xy[1],
                "text": t,
                "val": val,
                "rotation": _entity_rotation_deg_from_props(props),
            }
        )
    return out


def _infer_section_dimension_bh_mm(
    minx: float,
    miny: float,
    maxx: float,
    maxy: float,
    lines_out: list[LineString],
    text_samples: list[dict[str, Any]],
    *,
    outline_w_mm: int | None,
    outline_h_mm: int | None,
    beam_prefer_bottom_stem_b: bool = False,
) -> tuple[int | None, int | None, dict[str, Any]]:
    """
    주근 클러스터 bbox 기준으로 가로 치수선(B)·세로 치수선(H)과 순수 숫자 TEXT를 읽는다.
    도면 단위가 mm인 일람·단면도를 가정한다.
    """
    meta: dict[str, Any] = {}
    bar_w = maxx - minx
    bar_h = maxy - miny
    if bar_w < 8.0 or bar_h < 8.0:
        meta["reason"] = "bar_bbox_too_small"
        return None, None, meta

    mid_x = 0.5 * (minx + maxx)
    mid_y = 0.5 * (miny + maxy)
    # 주근 bbox만 기준으로 하면 out_x·out_y가 수~수십 단위로 줄어 좌측 세로 치수·TEXT(900 등)가 전부 탈락함
    edge_tol = max(88.0, 0.13 * max(bar_w, bar_h), 0.17 * min(bar_w, bar_h))
    out_y = max(32.0, 0.022 * bar_h, edge_tol * 0.55)
    out_x = max(32.0, 0.022 * bar_w, edge_tol * 0.55)

    b_line_vals: list[tuple[float, int]] = []
    h_line_vals: list[tuple[float, int]] = []

    for ln in lines_out:
        for seg in _line_to_segments(ln):
            c = list(seg.coords)
            if len(c) < 2:
                continue
            x0, y0 = float(c[0][0]), float(c[0][1])
            x1, y1 = float(c[1][0]), float(c[1][1])
            dx, dy = x1 - x0, y1 - y0
            seg_len = math.hypot(dx, dy)
            if seg_len < max(25.0, 0.12 * min(bar_w, bar_h)):
                continue
            mx, my = 0.5 * (x0 + x1), 0.5 * (y0 + y1)

            # 가로 치수선: 길이 ≈ 단면 가로폭, 세그먼트는 거의 수평, 단면 박스 바깥(위·아래)
            if (
                seg_len >= 0.28 * bar_w
                and seg_len <= 1.45 * bar_w + 160.0
                and abs(dy) <= max(4.5, 0.11 * seg_len)
            ):
                if abs(mx - mid_x) <= 0.58 * bar_w:
                    if my >= maxy - out_y * 0.55:
                        b_line_vals.append((abs(maxy - my), int(round(seg_len))))
                    elif my <= miny + out_y * 0.55:
                        b_line_vals.append((abs(miny - my), int(round(seg_len))))
            if beam_prefer_bottom_stem_b:
                mid_bot = miny + 0.24 * bar_h
                band = max(72.0, 0.34 * bar_h)
                if (
                    seg_len >= 0.26 * bar_w
                    and seg_len <= 1.55 * bar_w + 220.0
                    and abs(dy) <= max(5.5, 0.12 * seg_len)
                    and abs(mx - mid_x) <= 0.62 * bar_w
                    and abs(my - mid_bot) <= band
                ):
                    b_line_vals.append((abs(my - mid_bot), int(round(seg_len))))
            # 세로 치수선: 길이 ≈ 단면 세로, 세그먼트는 거의 수직, 좌·우 바깥
            if (
                seg_len >= 0.28 * bar_h
                and seg_len <= 1.45 * bar_h + 160.0
                and abs(dx) <= max(4.5, 0.11 * seg_len)
            ):
                if abs(my - mid_y) <= 0.58 * bar_h:
                    if mx <= minx + edge_tol:
                        h_line_vals.append((abs(minx - mx), int(round(seg_len))))
                    elif mx >= maxx - edge_tol:
                        h_line_vals.append((abs(mx - maxx), int(round(seg_len))))

    def _pick_dim(
        scored: list[tuple[float, int]],
        hint: int | None,
    ) -> int | None:
        if not scored:
            return None
        scored.sort(key=lambda t: t[0])
        if hint is None:
            return scored[0][1]
        best = min(scored, key=lambda t: (abs(t[1] - hint), t[0]))
        return best[1]

    b_from_line = _pick_dim(b_line_vals, outline_w_mm)
    h_from_line = _pick_dim(h_line_vals, outline_h_mm)

    b_text_scored: list[tuple[float, int]] = []
    h_text_scored: list[tuple[float, int]] = []
    for s in text_samples:
        cx, cy = float(s["x"]), float(s["y"])
        v = int(s["val"])
        if abs(cx - mid_x) <= 0.58 * bar_w:
            if cy >= maxy - out_y * 0.65:
                b_text_scored.append((abs(maxy - cy), v))
            elif cy <= miny + out_y * 0.65:
                b_text_scored.append((abs(cy - miny), v))
            elif beam_prefer_bottom_stem_b:
                mid_bot = miny + 0.24 * bar_h
                band = max(72.0, 0.36 * bar_h)
                if abs(cy - mid_bot) <= band:
                    b_text_scored.append((abs(cy - mid_bot), v))
        if abs(cy - mid_y) <= 0.58 * bar_h:
            if cx <= minx + edge_tol:
                h_text_scored.append((abs(minx - cx), v))
            elif cx >= maxx - edge_tol:
                h_text_scored.append((abs(cx - maxx), v))

    b_from_text = _pick_dim(b_text_scored, outline_w_mm)
    h_from_text = _pick_dim(h_text_scored, outline_h_mm)

    def _agree_line_text(line_v: int | None, text_v: int | None, tol: int = 6) -> int | None:
        if line_v is None:
            return text_v
        if text_v is None:
            return line_v
        if abs(line_v - text_v) <= tol:
            return int(round(0.5 * (line_v + text_v)))
        return line_v

    b_mm = _agree_line_text(b_from_line, b_from_text)
    h_mm = _agree_line_text(h_from_line, h_from_text)

    meta["b_from_line"] = b_from_line
    meta["h_from_line"] = h_from_line
    meta["b_from_text"] = b_from_text
    meta["h_from_text"] = h_from_text
    if b_mm is not None:
        meta["b_source"] = (
            "text+line"
            if b_from_text is not None and b_from_line is not None
            else ("text" if b_from_text is not None else "line")
        )
    if h_mm is not None:
        meta["h_source"] = (
            "text+line"
            if h_from_text is not None and h_from_line is not None
            else ("text" if h_from_text is not None else "line")
        )
    return b_mm, h_mm, meta


def analyze_section_geometry(
    shapes: list[tuple[str, Any, str | None]],
    main_bar_text: str | None,
    *,
    merge_tol_scale: float = 0.035,
    shape_sources: dict[str, Any] | None = None,
    beam_stem_outline: bool = False,
) -> dict[str, Any]:
    """도형 목록에서 주근·띠근 집계 및 텍스트와 비교."""
    circle_centers, radii, lines_out = _collect_circles_lines_radii(shapes)

    if not circle_centers:
        bad: dict[str, Any] = {
            "ok": False,
            "reason": "no_circles_in_region",
            "main_bar_text_count": parse_main_bar_spec(main_bar_text)[0],
            "debug_circle_count_raw": 0,
            "debug_circle_centers_merged": [],
        }
        if shape_sources:
            bad["shape_sources"] = shape_sources
        return bad

    xs = [p[0] for p in circle_centers]
    ys = [p[1] for p in circle_centers]
    span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
    merge_tol = max(merge_tol_scale * span, 3.0)
    merged = _merge_points(circle_centers, merge_tol)

    bar_info = _count_bars_rectangular(merged)
    minx, miny = min(p[0] for p in merged), min(p[1] for p in merged)
    maxx, maxy = max(p[0] for p in merged), max(p[1] for p in merged)

    outline_cands = _collect_outline_rectangle_candidates(shapes)
    sec_w, sec_h, outline_meta = _pick_section_outline_size_mm(outline_cands, merged, merge_tol)
    if beam_stem_outline:
        sw2, sh2, stem_meta = _pick_beam_stem_section_outline_mm(merged, merge_tol)
        if sw2 is not None and sh2 is not None:
            sec_w_cl = sec_w
            bar_w_mm = maxx - minx
            use_stem = True
            if sec_w_cl is not None:
                try:
                    cl_f = float(sec_w_cl)
                    if sw2 + 1 < int(max(280.0, 0.80 * cl_f)) and (cl_f - float(sw2)) > max(
                        60.0, 0.065 * cl_f
                    ):
                        use_stem = False
                except (TypeError, ValueError):
                    pass
            if use_stem and bar_w_mm >= 120.0:
                if float(sw2) < max(280.0, 0.78 * bar_w_mm) and (bar_w_mm - float(sw2)) > max(
                    90.0, 0.10 * bar_w_mm
                ):
                    use_stem = False
            if use_stem:
                sec_w, sec_h = sw2, sh2
                outline_meta = {
                    **(outline_meta if isinstance(outline_meta, dict) else {}),
                    **stem_meta,
                    "beam_stem_outline_override": True,
                }
            else:
                outline_meta = {
                    **(outline_meta if isinstance(outline_meta, dict) else {}),
                    **stem_meta,
                    "beam_stem_outline_skipped_narrow": True,
                    "beam_stem_width_mm_candidate": int(sw2),
                }

    x_tie, y_tie = _count_mid_cross_ties(lines_out, minx, miny, maxx, maxy)

    n_txt, d_txt = parse_main_bar_spec(main_bar_text)
    geom_n = bar_info.get("geom_main_total")

    match = None
    if geom_n is not None and n_txt is not None:
        match = geom_n == n_txt

    d_geom = None
    if radii:
        d_geom = int(round(2.0 * sum(radii) / len(radii)))

    d_eff = d_txt or d_geom
    tb, lr = bar_info.get("top_bot"), bar_info.get("left_right")
    top_bot_s = f"{tb}-D{d_eff}" if tb is not None and d_eff else ""
    left_right_s = f"{lr}-D{d_eff}" if lr is not None and d_eff else ""

    _preview_cap = 72
    _merged_preview = [[round(p[0], 4), round(p[1], 4)] for p in merged[:_preview_cap]]
    out: dict[str, Any] = {
        "ok": True,
        "merge_tolerance": round(merge_tol, 3),
        "merged_circle_count": len(merged),
        "top_bot_count": tb,
        "left_right_count": lr,
        "top_bot_label": top_bot_s,
        "left_right_label": left_right_s,
        "main_bar_count_geometry": geom_n,
        "main_bar_count_text": n_txt,
        "main_bar_diameter_text": d_txt,
        "main_bar_diameter_geometry_mm": d_geom,
        "main_bar_count_match": match,
        "symmetry_check_pair": bar_info.get("symmetry_check_pair"),
        "top_bottom_symmetric": bar_info.get("top_bottom_symmetric"),
        "left_right_symmetric": bar_info.get("left_right_symmetric"),
        "x_tie_bar_count": x_tie,
        "y_tie_bar_count": y_tie,
        "top_count": bar_info.get("top_count"),
        "bottom_count": bar_info.get("bottom_count"),
        "left_side_count": bar_info.get("left_side_count"),
        "right_side_count": bar_info.get("right_side_count"),
        "edge_tol_y_mm": bar_info.get("edge_tol_y"),
        "edge_tol_x_mm": bar_info.get("edge_tol_x"),
        "section_outline_width_mm": sec_w,
        "section_outline_depth_mm": sec_h,
        "section_outline_label": (
            f"{sec_w}×{sec_h}" if sec_w is not None and sec_h is not None else ""
        ),
        "section_outline_meta": outline_meta,
        "bar_cluster_bbox": [
            round(minx, 4),
            round(miny, 4),
            round(maxx, 4),
            round(maxy, 4),
        ],
        "debug_circle_count_raw": len(circle_centers),
        "debug_circle_centers_merged": _merged_preview,
        "debug_circle_centers_merged_truncated": len(merged) > _preview_cap,
    }
    if shape_sources:
        out["shape_sources"] = shape_sources
    return out


def _estimate_strip_half_width(strip_infos: list[dict[str, Any]] | None) -> float:
    """
    세로 스트립(부재 열) x_center 간격으로 검색 폭을 줄여 이웃 단면·잡도형 혼입을 줄인다.
    """
    if not strip_infos:
        return 3200.0
    xs: list[float] = []
    for s in strip_infos:
        xc = s.get("x_center")
        if xc is None:
            continue
        try:
            xs.append(float(xc))
        except (TypeError, ValueError):
            continue
    xs.sort()
    if len(xs) < 2:
        return 3200.0
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    gaps.sort()
    med = gaps[len(gaps) // 2]
    min_gap = gaps[0]
    hw = max(1600.0, min(med * 0.42, 6000.0))
    # 인접 스트립이 가깝으면 느슨 창이 이웃 열까지 닿지 않게 상한을 낮춘다.
    if min_gap < 2400.0:
        hw = min(hw, max(min_gap * 0.58 + 820.0, 1280.0))
    return hw


def _exclusive_strip_x_half_width(
    strip_infos: list[dict[str, Any]],
    strip_index: int,
    loose_cap: float,
) -> float | None:
    """
    인접 스트립 x_center 사이의 중점까지 거리(열 전용 반폭) — 느슨 조회창이 겹쳐도
    클러스터링 시 이웃 열 주근이 섞이지 않게 한다.
    """
    if not strip_infos or strip_index < 0 or strip_index >= len(strip_infos):
        return None
    try:
        xc = float(strip_infos[strip_index].get("x_center"))
    except (TypeError, ValueError, KeyError):
        return None
    xs: list[float] = []
    for s in strip_infos:
        xv = s.get("x_center")
        if xv is None:
            continue
        try:
            xs.append(float(xv))
        except (TypeError, ValueError):
            continue
    xs.sort()
    xs_u: list[float] = []
    for x in xs:
        if not xs_u or abs(x - xs_u[-1]) > 3.0:
            xs_u.append(x)
    xs = xs_u
    if len(xs) < 2:
        return None
    if xc < xs[0] - 1e-6 or xc > xs[-1] + 1e-6:
        return None
    k = min(range(len(xs)), key=lambda i: abs(xs[i] - xc))
    if abs(xs[k] - xc) > max(25.0, 0.02 * (xs[-1] - xs[0])):
        return None
    left_mid = 0.5 * (xs[k - 1] + xs[k]) if k > 0 else xc - loose_cap
    right_mid = 0.5 * (xs[k] + xs[k + 1]) if k + 1 < len(xs) else xc + loose_cap
    half = min(xc - left_mid, right_mid - xc)
    if half <= 30.0:
        return None
    return min(loose_cap, max(half, 120.0))


def _norm_bbox4(b: tuple[float, ...] | list[float]) -> tuple[float, float, float, float]:
    x0, y0, x1, y1 = float(b[0]), float(b[1]), float(b[2]), float(b[3])
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


def _pick_viewport_y_clip_for_section(
    xc: float,
    y_anchor: float,
    boxes: list[tuple[float, float, float, float]],
    *,
    pad_y: float = 850.0,
) -> tuple[float, float] | None:
    """
    다중 표 선택 시 단면 DB 조회 Y범위를 '해당 표' 쪽으로 한정한다.
    xc·행 앵커 Y에 가장 맞는 선택 박스를 고른 뒤 Y만 약간 패딩한다.
    """
    if not boxes:
        return None
    normed = [_norm_bbox4(b) for b in boxes]
    x_hit = [b for b in normed if b[0] <= xc <= b[2]]
    cand = x_hit if x_hit else normed

    def y_score(b: tuple[float, float, float, float]) -> tuple[int, float]:
        _mx, my0, _mx2, my1 = b
        if my0 <= y_anchor <= my1:
            return (0, my1 - my0)
        if y_anchor < my0:
            return (1, float(my0 - y_anchor))
        return (1, float(y_anchor - my1))

    best = min(cand, key=y_score)
    _mx, my0, _mx2, my1 = best
    return (my0 - pad_y, my1 + pad_y)


def _clip_bbox_loose_y(
    bbox_loose: tuple[float, float, float, float],
    y0: float,
    y1: float,
    *,
    min_height: float = 520.0,
) -> tuple[float, float, float, float]:
    lo0, lo1, lo2, lo3 = bbox_loose
    ny1 = max(lo1, float(y0))
    ny2 = min(lo3, float(y1))
    if ny2 - ny1 < min_height:
        return bbox_loose
    return (lo0, ny1, lo2, ny2)


def _section_template_y(field_headers: list[dict[str, Any]] | None) -> float | None:
    if not field_headers:
        return None
    for h in field_headers:
        if header_row_is_section_geometry_anchor(h.get("label"), h.get("key")):
            try:
                return float(h["y"])
            except (TypeError, KeyError, ValueError):
                continue
    return None


def is_beam_vertical_table_row(r: dict[str, Any]) -> bool:
    """보 세로 블록(병합 행·폴백 레이아웃 포함) — 단면 enrich·클러스터 분기용."""
    bl = str(r.get("beam_layout") or "")
    br = str(r.get("beam_row_role") or "")
    if bl.startswith("vertical_blocks"):
        return True
    if br == "beam_vertical_block":
        return True
    mis = r.get("beam_vertical_merged_strip_indices")
    if isinstance(mis, list) and len(mis) > 0:
        return True
    return False


def _field_headers_y_median_gap(field_headers: list[dict[str, Any]] | None) -> float:
    """좌측 필드 헤더 Y 간격 중앙값 — 보 세로 단면 세로창 상한 추정."""
    if not field_headers:
        return 380.0
    ys: list[float] = []
    for h in field_headers:
        try:
            ys.append(float(h["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if len(ys) < 2:
        return 380.0
    ys_sorted = sorted(ys, reverse=True)
    gaps = [abs(ys_sorted[i] - ys_sorted[i + 1]) for i in range(len(ys_sorted) - 1)]
    if not gaps:
        return 380.0
    gaps.sort()
    mid = gaps[len(gaps) // 2]
    return max(220.0, float(mid))


def enrich_rows_column_section_geometry(
    db: Session,
    commit_id: int,
    rows: list[dict[str, Any]],
    field_headers: list[dict[str, Any]] | None,
    strip_infos: list[dict[str, Any]] | None,
    *,
    half_width: Optional[float] = None,
    half_height: Optional[float] = None,
    include_block_definitions: bool = True,
    selection_world_bboxes: list[tuple[float, float, float, float]] | None = None,
    strip_index_field: str = "column_strip_index",
    infer_table_dims: bool = True,
    beam_stem_outline: bool = False,
) -> None:
    """각 세로 스트립·SECTION 행 주변 bbox에서 단면 기하 분석 후 row에 `section_geometry` 주입.

    strip_index_field: 기둥은 `column_strip_index`, 보 세로 블록 합성 행은 `column_strip_index`에 스트립을 넣어 재사용.
    infer_table_dims: False이면 치수선으로 추정한 B·H를 행의 width_mm·depth_mm에 덮어쓰지 않음(보 일람용).
    beam_stem_outline: 보 세로일 때 T형 하부 띠 기준 B·H 외곽 추정 + 하부 치수선 가중.
    """
    y_sec = _section_template_y(field_headers)
    if y_sec is None or not strip_infos:
        for r in rows:
            r["section_geometry"] = {"ok": False, "reason": "no_section_y_or_strips"}
        return

    eff_hw = (
        float(half_width)
        if half_width is not None and float(half_width) > 0
        else _estimate_strip_half_width(strip_infos)
    )
    eff_hh = (
        float(half_height)
        if half_height is not None and float(half_height) > 0
        else 11000.0
    )

    block_inserts_cached: list[BlockInsert] | None = None
    if include_block_definitions:
        block_inserts_cached = (
            db.query(BlockInsert).filter(BlockInsert.commit_id == commit_id).all()
        )

    # (스트립 인덱스, 행 세로 중심)마다 별도 로드 — SECTION 헤더 Y 한 점만 쓰면 단면도가 아래·위 행에 있을 때 범위 밖으로 빠진다.
    shapes_by_key: dict[tuple[Any, ...], list[tuple[str, Any, str | None]]] = {}
    sources_by_key: dict[tuple[Any, ...], dict[str, Any]] = {}
    bbox_by_key: dict[tuple[Any, ...], tuple[float, float, float, float]] = {}

    clip_boxes: list[tuple[float, float, float, float]] = []
    if selection_world_bboxes:
        for b in selection_world_bboxes:
            if isinstance(b, (list, tuple)) and len(b) >= 4:
                try:
                    clip_boxes.append(_norm_bbox4((float(b[0]), float(b[1]), float(b[2]), float(b[3]))))
                except (TypeError, ValueError):
                    continue

    for r in rows:
        si = r.get(strip_index_field)
        if si is None or si < 0 or si >= len(strip_infos):
            r["section_geometry"] = {"ok": False, "reason": "bad_strip_index"}
            continue
        if not str(r.get("mark") or r.get("name") or r.get("member_label") or "").strip():
            # 보 세로: 표 맨 앞·구간만 있는 행(부호 없이 도면만)도 스트립 0 단면이 있으면 기하 분석을 돌린다.
            if str(r.get("beam_row_role") or "") != "beam_vertical_block":
                r["section_geometry"] = {"ok": False, "reason": "no_member_mark"}
                continue
        xc = strip_infos[si].get("x_center")
        if xc is None:
            r["section_geometry"] = {"ok": False, "reason": "no_x_center"}
            continue
        try:
            xc = float(xc)
        except (TypeError, ValueError):
            r["section_geometry"] = {"ok": False, "reason": "x_center_invalid"}
            continue

        # 병합 행도 beam_vertical_merged_strip_indices 로 보 세로로 취급 — T형 stem·느슨창 세로 제한 적용
        use_beam_stem = beam_stem_outline and is_beam_vertical_table_row(r)

        x_excl = _exclusive_strip_x_half_width(
            strip_infos,
            si,
            max(eff_hw * 2.8, 12000.0),
        )
        hw_load = eff_hw
        if x_excl is not None and x_excl > 50.0:
            hw_load = min(eff_hw, max(x_excl * 2.38, 500.0))

        y_center = float(y_sec)
        anchor_source = "section_template_y"
        # 형태(SECTION) 행 Y가 있으면 우선: 우측 스트립 중앙값(row_data)은 HOOP 등 아래쪽 텍스트에
        # 끌려 단면이 아래로 밀리는 경우가 많다. 부재명·단면은 보통 그보다 위에 있다.
        rsa = r.get("row_section_anchor_y")
        if rsa is not None:
            try:
                y_center = float(rsa)
                anchor_source = "template_section_label_y"
            except (TypeError, ValueError):
                pass
        rda = r.get("row_data_anchor_y")
        if anchor_source == "section_template_y" and rda is not None:
            try:
                y_center = float(rda)
                anchor_source = "row_data_strip_y"
            except (TypeError, ValueError):
                pass
        if anchor_source == "section_template_y":
            ry = r.get("row_y_mean")
            if ry is not None:
                try:
                    y_center = float(ry)
                    anchor_source = "row_y_mean"
                except (TypeError, ValueError):
                    pass

        # (si, round(y,1)) 만 쓰면 인접 행(앵커 Y가 비슷)이 같은 DB 조회·클러스터 결과를 공유해
        # 단면 박스가 중복·어긋나 보일 수 있음. 템플릿 세그먼트 Y구간이 있으면 그걸 캐시 키에 포함.
        bounds = r.get("row_data_anchor_y_bounds")
        anchor_y_bounds_arg: tuple[float, float] | None = None
        y_anchor = y_center
        segment_y_span: float | None = None
        beam_vb = is_beam_vertical_table_row(r)
        if isinstance(bounds, (list, tuple)) and len(bounds) >= 2:
            try:
                y_lo_f, y_hi_f = float(bounds[0]), float(bounds[1])
                segment_y_span = max(0.0, y_hi_f - y_lo_f)
                rsa_clamp = r.get("row_section_anchor_y")
                if (
                    beam_vb
                    and rsa_clamp is not None
                    and anchor_source == "template_section_label_y"
                    and segment_y_span > max(780.0, _field_headers_y_median_gap(field_headers) * 4.2)
                ):
                    yc = float(rsa_clamp)
                    med_g = _field_headers_y_median_gap(field_headers)
                    max_span = max(780.0, med_g * 4.2)
                    half = max_span * 0.5
                    y_lo_f = yc - half
                    y_hi_f = yc + half
                    nb = [round(y_lo_f, 4), round(y_hi_f, 4)]
                    bounds = nb
                    r["row_data_anchor_y_bounds"] = nb
                    segment_y_span = max(0.0, y_hi_f - y_lo_f)
                y_lo_k = round(float(bounds[0]), 2)
                y_hi_k = round(float(bounds[1]), 2)
                cache_key = (si, y_lo_k, y_hi_k, round(hw_load, 1))
                if segment_y_span > 80.0:
                    y_mid_seg = 0.5 * (y_lo_f + y_hi_f)
                    skew_tol = max(260.0, 0.24 * segment_y_span)
                    # 보 세로 + 좌측 형태 행 Y(row_section_anchor_y): 앵커는 이미 단면 행에 맞춰져 있다.
                    # row_data_anchor_y_bounds 는 형태~주근까지 넓어 y_mid_seg 가 상부근 쪽으로 가며
                    # 클러스터가 단면에서 벗어나는 경우가 많다 → 이 경우 스큐 보정을 하지 않는다.
                    if (
                        not beam_vb or anchor_source != "template_section_label_y"
                    ) and abs(y_center - y_mid_seg) > skew_tol:
                        y_anchor = y_mid_seg
            except (TypeError, ValueError):
                cache_key = (si, round(y_center, 4), round(hw_load, 1))
            try:
                anchor_y_bounds_arg = (float(bounds[0]), float(bounds[1]))
            except (TypeError, ValueError):
                anchor_y_bounds_arg = None
        else:
            cache_key = (si, round(y_center, 4), round(hw_load, 1))

        # 보 세로: 기둥 기본 eff_hh(11000)로 DB를 열면 인접 층·옆부재 원이 한꺼번에 들어와 클러스터가 무너진다.
        eff_hh_use = float(eff_hh)
        if beam_vb:
            if segment_y_span is not None and segment_y_span > 100.0:
                eff_hh_use = min(
                    eff_hh_use,
                    max(1280.0, float(segment_y_span) * 2.12 + 760.0),
                    4600.0,
                )
            else:
                med_row = _field_headers_y_median_gap(field_headers)
                eff_hh_use = min(eff_hh_use, max(1450.0, med_row * 13.5), 4600.0)

        yv_clip: tuple[float, float] | None = None
        if clip_boxes:
            yv_clip = _pick_viewport_y_clip_for_section(xc, y_anchor, clip_boxes)
            cache_key = (
                *cache_key,
                (round(yv_clip[0], 1), round(yv_clip[1], 1)),
            )

        if cache_key not in shapes_by_key:
            bbox_loose = (
                xc - hw_load,
                y_center - eff_hh_use,
                xc + hw_load,
                y_center + eff_hh_use,
            )
            if yv_clip is not None:
                bbox_loose = _clip_bbox_loose_y(bbox_loose, yv_clip[0], yv_clip[1])
            comb, src = _load_combined_shapes_in_bbox(
                db,
                commit_id,
                bbox_loose,
                include_block_definitions=include_block_definitions,
                block_inserts_cached=block_inserts_cached,
            )
            shapes_by_key[cache_key] = comb
            sources_by_key[cache_key] = src
            bbox_by_key[cache_key] = bbox_loose

        loose_shapes = shapes_by_key[cache_key]
        loose_bb = bbox_by_key[cache_key]

        main_txt = str(r.get("MAIN_BAR") or "").strip() or None
        n_main_expect, _ = parse_main_bar_spec(main_txt)

        centers_raw, _, _ = _collect_circles_lines_radii(loose_shapes)
        beam_like_section = is_beam_vertical_table_row(r)
        # 보 세로 스트립의 x_center 는 TEXT 삽입점 평균이라 치수·구간 라벨에 치우쳐 단면도보다 왼쪽일 수 있다.
        # 단면 Y구간 안 원 X 중앙값으로 앵커를 보정하되, Y만 맞고 X는 전부 넣으면 병합 행·인접 열 주근이 섞여
        # 한쪽 단면만 잡히거나 두 스트립이 동일 picked 가 되므로 **이 스트립 xc 근처**만 포함한다.
        xc_cluster = float(xc)
        if beam_like_section and centers_raw and anchor_y_bounds_arg and len(anchor_y_bounds_arg) >= 2:
            try:
                y_lo_f = float(anchor_y_bounds_arg[0])
                y_hi_f = float(anchor_y_bounds_arg[1])
                y_pad = max(180.0, (y_hi_f - y_lo_f) * 0.12)
                pts_y = [
                    p
                    for p in centers_raw
                    if (y_lo_f - y_pad) <= p[1] <= (y_hi_f + y_pad)
                ]
                base_gate = 520.0
                if x_excl is not None and x_excl > 50.0:
                    base_gate = max(base_gate, float(x_excl) * 1.22)
                base_gate = min(max(base_gate, 420.0), 1900.0)
                xs_band: list[float] = []
                for mul in (1.0, 1.32, 1.68, 2.05, 2.45):
                    gate = base_gate * mul
                    xs_band = [p[0] for p in pts_y if abs(p[0] - float(xc)) <= gate]
                    if len(xs_band) >= 8:
                        break
                if len(xs_band) >= 4:
                    xc_cluster = float(_median_float(xs_band))
            except (TypeError, ValueError):
                pass

        tight_bb, cl_meta = _bbox_from_centerline_cluster(
            centers_raw,
            xc_cluster,
            y_anchor,
            eff_hw,
            eff_hh_use,
            segment_y_span=segment_y_span,
            strip_x_half_exclusive=x_excl,
            expected_main_bars=n_main_expect,
            anchor_y_bounds=anchor_y_bounds_arg,
            beam_like_section=beam_like_section,
        )
        if beam_like_section and abs(xc_cluster - xc) > 18.0:
            cl_meta = {
                **cl_meta,
                "beam_circle_median_x": round(xc_cluster, 4),
                "beam_strip_text_mean_x": round(xc, 4),
            }
        shapes_for_analysis = loose_shapes
        # 클러스터 박스로 필터하면 주근이 4개 미만이면 분석은 느슨 bbox 전체를 쓴다.
        # 이때에도 search_bbox 를 좁은 tight_bb 로 두면 뷰어·오버레이만 일부로 보인다(치수선 등 노이즈 클러스터).
        display_cluster_bb = tight_bb
        if tight_bb is not None:
            filt = _filter_shapes_by_bbox(loose_shapes, tight_bb)
            cc2, _, _ = _collect_circles_lines_radii(filt)
            if len(cc2) >= 4:
                shapes_for_analysis = filt
            else:
                cl_meta = {**cl_meta, "fallback": "cluster_filter_removed_circles"}
                display_cluster_bb = None

        geo = analyze_section_geometry(
            shapes_for_analysis,
            main_txt,
            shape_sources=sources_by_key.get(cache_key),
            beam_stem_outline=use_beam_stem,
        )
        geo["section_y_anchor_source"] = anchor_source
        if rda is not None:
            try:
                geo["row_data_anchor_y"] = round(float(rda), 4)
            except (TypeError, ValueError):
                geo["row_data_anchor_y"] = None
        else:
            geo["row_data_anchor_y"] = None
        geo["loose_shape_count"] = len(loose_shapes)
        geo["centers_raw_count"] = len(centers_raw)
        geo["shapes_for_analysis_count"] = len(shapes_for_analysis)
        geo["search_bbox_loose"] = [round(loose_bb[i], 4) for i in range(4)]
        # 표시용 search_bbox: DB 조회 창(느슨)을 쓰면 세로로 비정상적으로 길게 그려짐.
        # 클러스터가 잡혔으면 그 박스(단, 필터 폐기 시에는 tight 를 쓰지 않음) → 주근 헐·느슨 창.
        if display_cluster_bb is not None:
            geo["search_bbox"] = [round(display_cluster_bb[i], 4) for i in range(4)]
            geo["search_bbox_display_mode"] = "cluster"
        else:
            disp = _display_bbox_from_circle_centers(centers_raw)
            if disp is not None:
                geo["search_bbox"] = [round(disp[i], 4) for i in range(4)]
                geo["search_bbox_display_mode"] = "circle_hull"
            else:
                geo["search_bbox"] = [round(loose_bb[i], 4) for i in range(4)]
                geo["search_bbox_display_mode"] = "full_loose_query"
        # 클러스터가 느슨 창 전체를 쓴 경우에도 보 세로는 표 한 칸 세로로 잘라 다른 층·부재 단면을 끌어오지 않게 한다.
        if (
            beam_like_section
            and isinstance(geo.get("search_bbox"), list)
            and len(geo["search_bbox"]) == 4
            and anchor_y_bounds_arg is not None
            and len(anchor_y_bounds_arg) >= 2
        ):
            try:
                sx0, sy0, sx1, sy1 = (float(geo["search_bbox"][i]) for i in range(4))
                yspan = abs(sy1 - sy0)
                med_g2 = _field_headers_y_median_gap(field_headers)
                y_cap = max(1050.0, min(3200.0, med_g2 * 9.8))
                try:
                    al_c, ah_c = float(anchor_y_bounds_arg[0]), float(anchor_y_bounds_arg[1])
                    row_sp = max(1.0, ah_c - al_c)
                    y_cap = min(y_cap, max(780.0, row_sp * 2.42 + 460.0))
                except (TypeError, ValueError):
                    pass
                if yspan > y_cap:
                    al, ah = float(anchor_y_bounds_arg[0]), float(anchor_y_bounds_arg[1])
                    pad_y = max(300.0, med_g2 * 1.1, (ah - al) * 0.22 + 180.0)
                    ny0 = max(min(sy0, sy1), al - pad_y)
                    ny1 = min(max(sy0, sy1), ah + pad_y)
                    if ny1 > ny0 + 200.0:
                        geo["search_bbox"] = [
                            round(sx0, 4),
                            round(ny0, 4),
                            round(sx1, 4),
                            round(ny1, 4),
                        ]
                        geo["beam_search_bbox_y_clamped"] = True
            except (TypeError, ValueError):
                pass
        if (
            beam_like_section
            and isinstance(cl_meta, dict)
            and isinstance(cl_meta.get("cluster_bbox"), (list, tuple))
            and len(cl_meta["cluster_bbox"]) == 4
            and anchor_y_bounds_arg is not None
            and len(anchor_y_bounds_arg) >= 2
        ):
            try:
                kx0, ky0, kx1, ky1 = (float(cl_meta["cluster_bbox"][i]) for i in range(4))
                al_r, ah_r = float(anchor_y_bounds_arg[0]), float(anchor_y_bounds_arg[1])
                med_r = _field_headers_y_median_gap(field_headers)
                pad_r = max(220.0, med_r * 0.78, (ah_r - al_r) * 0.13 + 110.0)
                cy_lo = al_r - pad_r
                cy_hi = ah_r + pad_r
                lo_y, hi_y = min(ky0, ky1), max(ky0, ky1)
                nlo = max(lo_y, cy_lo)
                nhi = min(hi_y, cy_hi)
                if nhi > nlo + 45.0:
                    cl_meta["cluster_bbox"] = [
                        round(kx0, 4),
                        round(nlo, 4),
                        round(kx1, 4),
                        round(nhi, 4),
                    ]
                    cl_meta["cluster_bbox_row_window_clamp"] = True
            except (TypeError, ValueError):
                pass
        geo["cluster_geometry"] = cl_meta
        if isinstance(cl_meta, dict) and cl_meta.get("reason"):
            geo["cluster_fail_reason"] = cl_meta["reason"]
        geo["search_half_width"] = round(eff_hw, 4)
        geo["search_half_width_load"] = round(hw_load, 4)
        geo["search_half_height"] = round(eff_hh_use, 4)
        geo["section_y_anchor"] = round(y_anchor, 4)
        geo["section_y_center_world"] = round(y_center, 4)
        geo["section_template_y"] = round(float(y_sec), 4)
        if yv_clip is not None:
            geo["viewport_y_clip"] = [round(yv_clip[0], 4), round(yv_clip[1], 4)]
        if geo.get("ok") and isinstance(geo.get("bar_cluster_bbox"), list) and len(geo["bar_cluster_bbox"]) == 4:
            try:
                bx0, by0, bx1, by1 = (float(x) for x in geo["bar_cluster_bbox"])
                pad_txt = max(220.0, 0.55 * max(bx1 - bx0, by1 - by0, 1.0))
                if use_beam_stem:
                    pad_txt = max(pad_txt, 400.0, 0.72 * max(bx1 - bx0, by1 - by0, 1.0))
                txt_bb = _expand_bbox_pad((bx0, by0, bx1, by1), pad_txt)
                dim_texts = _load_dimension_text_samples_in_bbox(db, commit_id, txt_bb)
                _, _, lines_dim = _collect_circles_lines_radii(shapes_for_analysis)
                b_dim_mm, h_dim_mm, dim_meta = _infer_section_dimension_bh_mm(
                    bx0,
                    by0,
                    bx1,
                    by1,
                    lines_dim,
                    dim_texts,
                    outline_w_mm=geo.get("section_outline_width_mm"),
                    outline_h_mm=geo.get("section_outline_depth_mm"),
                    beam_prefer_bottom_stem_b=use_beam_stem,
                )
                if dim_meta:
                    geo["section_dimension_bh_meta"] = dim_meta
                if b_dim_mm is not None:
                    geo["section_dimension_b_mm"] = int(b_dim_mm)
                    if infer_table_dims:
                        r["width_mm"] = int(b_dim_mm)
                if h_dim_mm is not None:
                    geo["section_dimension_h_mm"] = int(h_dim_mm)
                    if infer_table_dims:
                        r["depth_mm"] = int(h_dim_mm)
            except (TypeError, ValueError):
                pass
        # 주근 외곽 직사각이 치수선·숫자 TEXT 추정보다 짧게 잡힌 경우(예: 세로 820 vs 치수 900) 외곽 mm을 치수에 맞춰
        # 뷰어 '도면 외곽 vs 표' 비교가 외곽 기준으로만 이뤄지므로 여기서 정렬한다.
        if geo.get("ok"):
            try:
                gw0 = geo.get("section_outline_width_mm")
                gh0 = geo.get("section_outline_depth_mm")
                bw = geo.get("section_dimension_b_mm")
                hh = geo.get("section_dimension_h_mm")
                if bw is not None and gw0 is not None:
                    bi, gi = int(bw), int(gw0)
                    if bi != gi and abs(bi - gi) > max(25, int(0.05 * max(gi, 1))):
                        geo["section_outline_width_mm_geometry"] = gi
                        geo["section_outline_width_mm"] = bi
                if hh is not None and gh0 is not None:
                    hi, gi = int(hh), int(gh0)
                    if hi != gi and abs(hi - gi) > max(25, int(0.05 * max(gi, 1))):
                        geo["section_outline_depth_mm_geometry"] = gi
                        geo["section_outline_depth_mm"] = hi
                gw1 = geo.get("section_outline_width_mm")
                gh1 = geo.get("section_outline_depth_mm")
                if gw1 is not None and gh1 is not None:
                    geo["section_outline_label"] = f"{int(gw1)}×{int(gh1)}"
            except (TypeError, ValueError):
                pass
        gw = geo.get("section_outline_width_mm")
        gh = geo.get("section_outline_depth_mm")
        rw, rd = r.get("width_mm"), r.get("depth_mm")
        if (
            gw is not None
            and gh is not None
            and rw is not None
            and rd is not None
        ):
            try:
                iw, idp = int(gw), int(gh)
                irw, ird = int(rw), int(rd)
                tol = max(2, int(0.025 * max(iw, idp)))
                geo["section_outline_vs_table_size"] = (
                    abs(iw - irw) <= tol and abs(idp - ird) <= tol
                )
            except (TypeError, ValueError):
                pass
        r["section_geometry"] = geo


def _beam_vertical_row_drawing_dims_from_zones(r: dict[str, Any]) -> tuple[int | None, int | None]:
    """부위별 section_geometry 에서 도면 B·H(mm) 후보를 한 쌍으로 고른다."""
    zones = r.get("beam_section_geometry_zones") or []
    b_out: int | None = None
    h_out: int | None = None
    for z in zones:
        sg = z.get("section_geometry") or {}
        if not isinstance(sg, dict):
            continue
        b = sg.get("section_dimension_b_mm")
        if b is None:
            b = sg.get("section_outline_width_mm")
        h = sg.get("section_dimension_h_mm")
        if h is None:
            h = sg.get("section_outline_depth_mm")
        try:
            if b is not None:
                b_out = int(b)
            if h is not None:
                h_out = int(h)
        except (TypeError, ValueError):
            continue
        if b_out is not None and h_out is not None:
            break
    return b_out, h_out


def _inject_beam_vertical_drawing_dim_fields(rows: list[dict[str, Any]]) -> None:
    """피벗용 beam_field_key_order / cells 에 도면 B·H 행을 삽입한다."""
    for r in rows:
        if str(r.get("beam_row_role") or "") != "beam_vertical_block":
            continue
        ko = list(r.get("beam_field_key_order") or [])
        if not ko or "drawing_b_mm" in ko:
            continue
        b_v, h_v = _beam_vertical_row_drawing_dims_from_zones(r)
        if b_v is None and h_v is None:
            continue
        tit = dict(r.get("beam_field_titles") or {})
        cells = list(r.get("cells") or [])
        while len(cells) < len(ko):
            cells.append("")
        ins = len(ko)
        for i, k in enumerate(ko):
            if k == "depth_mm":
                ins = i + 1
                break
            if k == "width_mm" and "depth_mm" not in ko:
                ins = i + 1
                break
        ko.insert(ins, "drawing_b_mm")
        ko.insert(ins + 1, "drawing_h_mm")
        tit["drawing_b_mm"] = "도면 B(mm)"
        tit["drawing_h_mm"] = "도면 H(mm)"
        cells.insert(ins, str(b_v) if b_v is not None else "")
        cells.insert(ins + 1, str(h_v) if h_v is not None else "")
        r["beam_field_key_order"] = ko
        r["beam_field_titles"] = tit
        r["cells"] = cells
        if b_v is not None:
            r["drawing_b_mm"] = b_v
        if h_v is not None:
            r["drawing_h_mm"] = h_v


def _extend_beam_vertical_field_headers_dim(
    field_headers: list[dict[str, Any]],
    rows: list[dict[str, Any]],
) -> None:
    if not field_headers:
        return
    has_dim = False
    for r in rows:
        b_v, h_v = _beam_vertical_row_drawing_dims_from_zones(r)
        if b_v is not None or h_v is not None:
            has_dim = True
            break
    if not has_dim:
        return
    keys_existing = {str(h.get("key") or "") for h in field_headers}
    if "drawing_b_mm" in keys_existing:
        return
    ys: list[float] = []
    for h in field_headers:
        try:
            ys.append(float(h["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not ys:
        return
    ymax = max(ys)
    step = 360.0
    field_headers.append(
        {"key": "drawing_b_mm", "label": "도면 B(mm)", "y": round(ymax + step, 4)}
    )
    field_headers.append(
        {"key": "drawing_h_mm", "label": "도면 H(mm)", "y": round(ymax + step * 2.0, 4)}
    )


def enrich_rows_beam_vertical_section_geometry_zones(
    db: Session,
    commit_id: int,
    rows: list[dict[str, Any]],
    field_headers: list[dict[str, Any]] | None,
    strip_infos: list[dict[str, Any]] | None,
    *,
    half_width: Optional[float] = None,
    half_height: Optional[float] = None,
    include_block_definitions: bool = True,
    selection_world_bboxes: list[tuple[float, float, float, float]] | None = None,
) -> None:
    """
    보 세로 블록: 한 부재가 X 방향으로 여러 스트립(부위)을 가질 때 스트립마다 단면 bbox를 잡아 분석한다.
    결과는 `beam_section_geometry_zones`(부위별)와 호환용 `section_geometry`(첫 부위)에 넣는다.
    """
    if not rows:
        return
    if not field_headers or not strip_infos:
        for r in rows:
            r.setdefault("section_geometry", {"ok": False, "reason": "no_section_headers_or_strips"})
        return

    synth: list[dict[str, Any]] = []
    meta: list[tuple[int, int]] = []
    for ri, r in enumerate(rows):
        sis = r.get("beam_vertical_merged_strip_indices")
        if not isinstance(sis, list) or not sis:
            si0 = r.get("beam_strip_index")
            sis = [int(si0)] if si0 is not None else []
        idxs: list[int] = []
        for x in sis:
            try:
                si = int(x)
            except (TypeError, ValueError):
                continue
            if 0 <= si < len(strip_infos):
                idxs.append(si)
        idxs = sorted(set(idxs), key=lambda i: float(strip_infos[i].get("x_center") or 0.0))
        zmap = r.get("beam_vertical_zone_label_by_strip")
        mmap = r.get("beam_vertical_mark_by_strip")
        imap = r.get("beam_vertical_inferred_mark_by_strip")
        for si in idxs:
            r2 = dict(r)
            r2["column_strip_index"] = int(si)
            syb = r.get("beam_vertical_strip_row_y_bounds")
            if isinstance(syb, dict):
                pair = syb.get(str(int(si)))
                if pair is None:
                    try:
                        pair = syb.get(int(si))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        pair = None
                if isinstance(pair, (list, tuple)) and len(pair) >= 2:
                    try:
                        r2["row_data_anchor_y_bounds"] = [round(float(pair[0]), 4), round(float(pair[1]), 4)]
                    except (TypeError, ValueError):
                        pass
            say = r.get("beam_vertical_strip_section_anchor_y")
            if isinstance(say, dict):
                av = say.get(str(int(si)))
                if av is None:
                    try:
                        av = say.get(int(si))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        av = None
                if av is not None:
                    try:
                        r2["row_section_anchor_y"] = round(float(av), 4)
                    except (TypeError, ValueError):
                        pass
            if isinstance(zmap, dict):
                zl = zmap.get(str(int(si)))
                if zl is None:
                    try:
                        zl = zmap.get(int(si))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        zl = None
                if zl and str(zl).strip():
                    r2["beam_vertical_zone_display_label"] = str(zl).strip()
            # 단면 검색·치수: 열별 표에서 읽은 부호만 (레이아웃 추론 부재명과 분리)
            if isinstance(mmap, dict):
                mk = mmap.get(str(int(si)))
                if mk is None:
                    try:
                        mk = mmap.get(int(si))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        mk = None
                if mk and str(mk).strip():
                    mks = str(mk).strip()
                    r2["member_label"] = mks
                    r2["mark"] = mks
                    r2["name"] = mks
            if isinstance(imap, dict):
                ik = imap.get(str(int(si)))
                if ik is None:
                    try:
                        ik = imap.get(int(si))  # type: ignore[arg-type]
                    except (TypeError, ValueError):
                        ik = None
                if ik and str(ik).strip():
                    r2["beam_vertical_inferred_mark"] = str(ik).strip()
            synth.append(r2)
            meta.append((ri, int(si)))

    if not synth:
        for r in rows:
            r.setdefault("section_geometry", {"ok": False, "reason": "no_valid_beam_strip_indices"})
        return

    enrich_rows_column_section_geometry(
        db,
        commit_id,
        synth,
        field_headers,
        strip_infos,
        half_width=half_width,
        half_height=half_height,
        include_block_definitions=include_block_definitions,
        selection_world_bboxes=selection_world_bboxes,
        strip_index_field="column_strip_index",
        infer_table_dims=True,
        beam_stem_outline=True,
    )

    width_by_ri: dict[int, int] = {}
    depth_by_ri: dict[int, int] = {}
    for r2, (ri, _si) in zip(synth, meta):
        if ri not in width_by_ri:
            w = r2.get("width_mm")
            if isinstance(w, int):
                width_by_ri[ri] = w
        if ri not in depth_by_ri:
            d = r2.get("depth_mm")
            if isinstance(d, int):
                depth_by_ri[ri] = d

    by_ri: dict[int, list[dict[str, Any]]] = {}
    for r2, (ri, si) in zip(synth, meta):
        geo = r2.get("section_geometry") or {}
        xc = strip_infos[si].get("x_center") if 0 <= si < len(strip_infos) else None
        zlab = r2.get("beam_vertical_zone_display_label")
        sm_geo = str(r2.get("mark") or r2.get("member_label") or "").strip() or None
        sm_inf = str(r2.get("beam_vertical_inferred_mark") or "").strip() or None
        # 단면·부위 매칭은 표에서 읽은 부호 우선(추론은 보조)
        sm_disp = sm_geo or sm_inf
        by_ri.setdefault(ri, []).append(
            {
                "beam_strip_index": si,
                "zone_index": len(by_ri.get(ri, [])),
                "x_center": xc,
                "section_geometry": geo,
                "beam_vertical_zone_display_label": zlab,
                "beam_strip_display_mark": sm_disp,
            }
        )

    for ri, r in enumerate(rows):
        zones = by_ri.get(ri, [])
        r["beam_section_geometry_zones"] = zones
        if zones:
            r["section_geometry"] = zones[0]["section_geometry"]
        else:
            r.setdefault("section_geometry", {"ok": False, "reason": "beam_zone_geometry_empty"})
        if ri in width_by_ri:
            r["width_mm"] = width_by_ri[ri]
        if ri in depth_by_ri:
            r["depth_mm"] = depth_by_ri[ri]
        if ri in width_by_ri and ri in depth_by_ri:
            r["size_mm"] = [width_by_ri[ri], depth_by_ri[ri]]
            r["SIZE"] = f"{width_by_ri[ri]}x{depth_by_ri[ri]}"

    # 디버그·뷰어(navRowYBounds)는 부모 행의 row_data_anchor_y_bounds 를 읽는다. 합성 r2만 줄이면 표에 옛값이 남는다.
    for ri, r in enumerate(rows):
        ylos: list[float] = []
        yhis: list[float] = []
        for r2, (r2_ri, _) in zip(synth, meta):
            if r2_ri != ri:
                continue
            b = r2.get("row_data_anchor_y_bounds")
            if isinstance(b, (list, tuple)) and len(b) >= 2:
                try:
                    ylos.append(float(b[0]))
                    yhis.append(float(b[1]))
                except (TypeError, ValueError):
                    pass
        if ylos and yhis and field_headers is not None:
            lo0, hi0 = min(ylos), max(yhis)
            rsa0 = r.get("row_section_anchor_y")
            max_sp = max(780.0, _field_headers_y_median_gap(field_headers) * 4.2)
            if rsa0 is not None and hi0 - lo0 > max_sp:
                yc = float(rsa0)
                half = max_sp * 0.5
                lo0, hi0 = yc - half, yc + half
            r["row_data_anchor_y_bounds"] = [round(lo0, 4), round(hi0, 4)]

    if field_headers is not None:
        _extend_beam_vertical_field_headers_dim(field_headers, rows)
    _inject_beam_vertical_drawing_dim_fields(rows)
