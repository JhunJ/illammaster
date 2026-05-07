"""
구조 일람표 추출: TEXT/MTEXT/ATTRIB 클러스터링 + 일람마스터 문서 기반 휴리스틱(D/@/X, 층, 두께).
블록(INSERT) 전개 시 문자가 ATTRIB 로만 DB에 남는 도면을 포함한다.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any, Callable

from geoalchemy2.functions import ST_Intersects, ST_MakeEnvelope
from geoalchemy2.shape import to_shape
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Entity

from app.services.column_section_geometry import (
    enrich_rows_beam_flat_section_geometry,
    # 보 추출은 flat 단면만 사용 — 세로 존 enrich는 하위 호환·테스트용으로만 유지
    enrich_rows_beam_vertical_section_geometry_zones,
    enrich_rows_column_section_geometry,
    header_row_is_section_geometry_anchor,
)

CATEGORY_WALL = "wall"
CATEGORY_BEAM = "beam"
CATEGORY_COLUMN = "column"
CATEGORY_SLAB = "slab"

# DB Entity.entity_type — 표·단면이 블록이면 INSERT 전개 결과가 ATTRIB 인 경우가 많음
TEXT_ENTITY_TYPES_FOR_EXTRACT = ("TEXT", "MTEXT", "ATTRIB")

SCHEDULE_CATEGORIES = (CATEGORY_WALL, CATEGORY_BEAM, CATEGORY_COLUMN, CATEGORY_SLAB)


@dataclass
class ExtractionConfig:
    """카테고리별 추출 설정 (JSON 직렬화와 동일 키)."""

    rules_version: str = "1.0"
    layer_include: list[str] = field(default_factory=list)
    layer_exclude: list[str] = field(default_factory=list)
    y_tolerance: float = 2.5
    min_row_texts: int = 1
    bbox_wkt: str | None = None
    """도면 좌표계 minx, miny, maxx, maxy — TEXT/MTEXT/ATTRIB 삽입점이 이 직사각형 안에 있을 때만 추출."""
    selection_bbox: tuple[float, float, float, float] | None = None
    """여러 표 블록(다중 페이지·도면틀 제외) — 비어 있지 않으면 selection_bbox 대신 이 목록으로 필터."""
    selection_bboxes: list[tuple[float, float, float, float]] = field(default_factory=list)
    wall_mode: str = "cad"
    building_tag: str | None = None
    slab_layout: str = "auto"
    """기둥: horizontal_table(가로 일람·부호행 피벗) | vertical_blocks(Y로 필드 구분·세로 블록=1행)."""
    column_layout: str = "horizontal_table"
    """세로 블록 간 최소 X 간격(도면 단위). None이면 자동 추정."""
    column_strip_gap: float | None = None
    """같은 블록 안에서 한 줄로 볼 Y 허용(세로 블록 모드)."""
    column_band_y_tol: float = 4.0
    # beam_layout: auto | horizontal_wide | vertical_blocks | flat — 보는 Y행 묶음에서 가로 표 인식을 먼저 시도.
    beam_layout: str = "auto"
    # beam_band_y_tol None이면 column_band_y_tol과 동일하게 사용
    beam_band_y_tol: float | None = None
    # 보 추출 시 기본 True — 빈 문자열 TEXT/ATTRIB 도 같은 Y행 묶음에 포함(extract_schedule에서 설정).
    include_empty_text_entities: bool = False
    # 보: 인접 얇은 행 병합 시 Y중심 최대 차(None이면 max(3.5*y_tolerance, 10)).
    beam_row_merge_y_max: float | None = None
    # 보: 병합 시 한 덩어리로 흡수할 인접 행의 최대 텍스트 수(None이면 4).
    beam_row_merge_max_glue: int | None = None
    # 보: 한 수평 묶음으로 합친 뒤 최대 텍스트 수(None이면 512 — 가로 긴 일람 대응).
    beam_row_merge_max_merged: int | None = None
    # 보: 가교 병합 시 끝 행 최대 텍스트 수(None이면 8 — 부호+소라벨 등).
    beam_row_bridge_max_endpoint: int | None = None
    # 보: 가교 병합 시 사이 행 텍스트 수 상한(None이면 1 — END 등 한 줄이 끼면 막음).
    beam_row_bridge_max_mid: int | None = None
    # 보: 가교 병합 반복 횟수(None이면 1 — 부호↔부호 한 번만 잇고 끝).
    beam_row_bridge_passes: int | None = None


def _entity_xy(ent: Entity) -> tuple[float, float] | None:
    g = ent.geom
    if g is None:
        return None
    try:
        shape = to_shape(g)
        if shape.geom_type == "Point":
            return float(shape.x), float(shape.y)
        c = shape.centroid
        return float(c.x), float(c.y)
    except Exception:
        return None


def _text_from_props(props: dict | None) -> str:
    if not props:
        return ""
    t = props.get("text") or props.get("plain_text")
    return (str(t) if t is not None else "").strip()


def _optional_positive_float(cfg: dict[str, Any], key: str) -> float | None:
    """설정에 양수가 있을 때만 사용. 없으면 None → 단면 기하에서 스트립 간격 등으로 자동 추정."""
    v = cfg.get(key)
    if v is None or v == "":
        return None
    try:
        x = float(v)
        return x if x > 0 else None
    except (TypeError, ValueError):
        return None


def _normalize_bbox(
    a: float, b: float, c: float, d: float
) -> tuple[float, float, float, float]:
    return (min(a, c), min(b, d), max(a, c), max(b, d))


def _point_in_bbox(xy: tuple[float, float], bbox: tuple[float, float, float, float]) -> bool:
    minx, miny, maxx, maxy = bbox
    x, y = xy
    return minx <= x <= maxx and miny <= y <= maxy


def _parse_selection_bboxes(raw: dict[str, Any]) -> list[tuple[float, float, float, float]]:
    raw_list = raw.get("selection_bboxes")
    out: list[tuple[float, float, float, float]] = []
    if not isinstance(raw_list, list):
        return out
    for item in raw_list:
        if isinstance(item, (list, tuple)) and len(item) >= 4:
            try:
                out.append(
                    _normalize_bbox(
                        float(item[0]),
                        float(item[1]),
                        float(item[2]),
                        float(item[3]),
                    )
                )
            except (TypeError, ValueError):
                continue
    return out


def _entity_passes_selection(
    xy: tuple[float, float], cfg: ExtractionConfig
) -> bool:
    if cfg.selection_bboxes:
        return any(_point_in_bbox(xy, b) for b in cfg.selection_bboxes)
    if cfg.selection_bbox is not None:
        return _point_in_bbox(xy, cfg.selection_bbox)
    return True


def _layer_ok(layer: str | None, cfg: ExtractionConfig) -> bool:
    ly = (layer or "").strip()
    if cfg.layer_exclude and any(re.fullmatch(p, ly, re.I) for p in cfg.layer_exclude):
        return False
    if not cfg.layer_include:
        return True
    return any(re.fullmatch(p, ly, re.I) for p in cfg.layer_include)


def load_text_entities(
    db: Session,
    commit_id: int,
    cfg: ExtractionConfig,
) -> list[dict[str, Any]]:
    """커밋의 TEXT/MTEXT/ATTRIB 엔티티를 좌표·문자열과 함께 로드(블록 속성 포함)."""
    q = db.query(Entity).filter(
        Entity.commit_id == commit_id,
        Entity.entity_type.in_(TEXT_ENTITY_TYPES_FOR_EXTRACT),
    )
    sel_boxes: list[tuple[float, float, float, float]] = []
    if cfg.selection_bboxes:
        sel_boxes.extend(cfg.selection_bboxes)
    if cfg.selection_bbox is not None:
        sel_boxes.append(cfg.selection_bbox)
    if sel_boxes:
        env_filters = []
        for bx in sel_boxes:
            mi, mj, ma, mb = _normalize_bbox(
                float(bx[0]),
                float(bx[1]),
                float(bx[2]),
                float(bx[3]),
            )
            env_filters.append(ST_Intersects(Entity.geom, ST_MakeEnvelope(mi, mj, ma, mb, 0)))
        q = q.filter(Entity.geom.isnot(None), or_(*env_filters))
    out: list[dict[str, Any]] = []
    for ent in q.all():
        if not _layer_ok(ent.layer, cfg):
            continue
        xy = _entity_xy(ent)
        if xy is None:
            continue
        text = _text_from_props(ent.props if isinstance(ent.props, dict) else None)
        if not text:
            if not cfg.include_empty_text_entities:
                continue
            text = ""
        item = {
            "id": ent.id,
            "layer": ent.layer,
            "text": text,
            "x": xy[0],
            "y": xy[1],
            "entity_type": ent.entity_type,
        }
        if not _entity_passes_selection(xy, cfg):
            continue
        out.append(item)
    return out


def cluster_rows(items: list[dict[str, Any]], y_tol: float) -> list[list[dict[str, Any]]]:
    """Y 좌표로 행 묶기 → 행 내 X 정렬."""
    if not items:
        return []
    sorted_y = sorted(items, key=lambda r: (-r["y"], r["x"]))
    rows: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    row_y: float | None = None
    for it in sorted_y:
        if row_y is None:
            row_y = it["y"]
            current.append(it)
            continue
        if abs(it["y"] - row_y) <= y_tol:
            current.append(it)
        else:
            current.sort(key=lambda r: r["x"])
            rows.append(current)
            current = [it]
            row_y = it["y"]
    if current:
        current.sort(key=lambda r: r["x"])
        rows.append(current)
    return rows


def beam_row_clusters_for_validation(
    rows_cluster: list[list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """
    보 Y-행 묶음( cluster_rows 결과 )을 뷰어 표시·디버그용으로 직렬화.
    글자가 없는 엔티티만 있는 행(empty_only)도 bbox·entity_ids 로 포함한다.
    """
    out: list[dict[str, Any]] = []
    for i, row in enumerate(rows_cluster):
        if not row:
            continue
        xs = [float(r["x"]) for r in row]
        ys = [float(r["y"]) for r in row]
        texts = [str(r.get("text") or "") for r in row]
        ne = sum(1 for t in texts if str(t).strip())
        out.append(
            {
                "row_index": i,
                "entity_ids": [r.get("id") for r in row],
                "texts": texts,
                "y_mean": round(sum(ys) / len(ys), 4),
                "bbox": [
                    round(min(xs), 4),
                    round(min(ys), 4),
                    round(max(xs), 4),
                    round(max(ys), 4),
                ],
                "non_empty_text_count": ne,
                "empty_only": ne == 0,
            }
        )
    return out


def _row_mean_y(row: list[dict[str, Any]]) -> float:
    ys = [float(r["y"]) for r in row]
    return sum(ys) / len(ys) if ys else 0.0


_RE_BEAM_MARK_LIKE = re.compile(r"^\s*[A-Z]{1,3}\s*\d{1,4}[A-Z]?\s*(?:\(.+\))?\s*$", re.I)
# 공백 제거 후 비교하는 헤더 토큰(한글 띄어쓰기/전각 공백 흔들림 대응)
_BEAM_HEADER_TOKENS = {
    "부호",
    "기호",
    "형태",
    "상부근",
    "하부근",
    "스트럽",
    "스터럽",
    "표피철근",
}


def _row_nonempty_texts(row: list[dict[str, Any]]) -> list[str]:
    out: list[str] = []
    for it in row:
        t = str(it.get("text") or "")
        # 전각/호환 문자를 통일해 헤더 토큰 판정이 흔들리지 않게
        try:
            import unicodedata

            t = unicodedata.normalize("NFKC", t)
        except Exception:
            pass
        t = t.strip()
        if t:
            out.append(re.sub(r"\s+", " ", t))
    return out


def _row_has_beam_mark_like(row: list[dict[str, Any]]) -> bool:
    texts = _row_nonempty_texts(row)
    if not texts:
        return False
    # 너무 공격적이면 오탐이 커지므로, mark-like는 영문+숫자 중심 패턴만 허용
    for t in texts:
        if _RE_BEAM_MARK_LIKE.match(t) and re.search(r"\d", t):
            return True
    return False


def _row_is_beam_header_like(row: list[dict[str, Any]]) -> bool:
    texts = _row_nonempty_texts(row)
    if not texts:
        return False
    # 전부가 헤더 토큰일 때만 헤더 행으로 본다(혼합이면 데이터로 취급)
    for t in texts:
        tt = re.sub(r"[\s\u00a0\u3000]+", "", t)
        if tt not in _BEAM_HEADER_TOKENS:
            return False
    return True


def _row_has_any_beam_header_token(row: list[dict[str, Any]]) -> bool:
    texts = _row_nonempty_texts(row)
    for t in texts:
        tt = re.sub(r"[\s\u00a0\u3000]+", "", t)
        if tt in _BEAM_HEADER_TOKENS:
            return True
    return False


def merge_beam_sparse_row_clusters(
    rows: list[list[dict[str, Any]]],
    y_tol: float,
    *,
    max_glue_row_items: int = 4,
    max_merged_row_items: int = 512,
    y_gap_max: float | None = None,
    block_header_mark_merge: bool = True,
) -> list[list[dict[str, Any]]]:
    """
    가로로 긴 한 줄이 Y만 조금씩 달라 `cluster_rows` 에 여러 얇은 행으로 쪼개진 경우,
    인접 행을 하나의 수평 묶음으로 이어 붙인다.

    - **흡수 조건**: 바로 아래(또는 위)로 붙일 행(`nxt`)의 텍스트 수가 `max_glue_row_items` 이하이고,
      Y중심 차가 `y_gap_max` 이하이며, 합친 총 개수가 `max_merged_row_items` 이하일 때만 병합.
    - 이미 넓게 묶인 `cur` 는 길이 제한까지 계속 병합 가능(가로 일람 전체 한 줄 대응).
    """
    rows = [r for r in rows if r]
    if len(rows) < 2:
        return rows
    gap = y_gap_max if y_gap_max is not None and y_gap_max > 0 else max(float(y_tol) * 3.5, 10.0)
    glue = max(1, int(max_glue_row_items))
    cap = max(glue * 2, int(max_merged_row_items))

    indexed = [(i, _row_mean_y(r), r) for i, r in enumerate(rows)]
    indexed.sort(key=lambda t: -t[1])
    sorted_rows = [t[2] for t in indexed]

    merged: list[list[dict[str, Any]]] = []
    cur = list(sorted_rows[0])
    cur_my = _row_mean_y(cur)

    for nxt in sorted_rows[1:]:
        n_my = _row_mean_y(nxt)
        if block_header_mark_merge:
            # "부호/형태/상·하부근/스트럽" 같은 헤더가 부호명(RG2 등)과 한 줄로 합쳐지는 것을 방지
            if (
                (_row_has_any_beam_header_token(cur) and _row_has_beam_mark_like(nxt))
                or (_row_has_any_beam_header_token(nxt) and _row_has_beam_mark_like(cur))
            ):
                merged.append(cur)
                cur = list(nxt)
                cur_my = n_my
                continue
        can = (
            len(nxt) <= glue
            and len(cur) + len(nxt) <= cap
            and abs(cur_my - n_my) <= gap
        )
        if can:
            cur.extend(nxt)
            cur.sort(key=lambda r: float(r["x"]))
            ys = [float(r["y"]) for r in cur]
            cur_my = sum(ys) / len(ys) if ys else cur_my
            continue
        merged.append(cur)
        cur = list(nxt)
        cur_my = n_my
    merged.append(cur)
    return merged


def bridge_beam_sparse_row_clusters(
    rows: list[list[dict[str, Any]]],
    y_gap: float,
    *,
    max_endpoint_items: int = 4,
    max_intermediate_row_items: int = 1,
    max_merged_row_items: int = 512,
    max_bridge_passes: int = 1,
    y_gap_for_mark_endpoints: float | None = None,
) -> list[list[dict[str, Any]]]:
    """
    가로로 멀리 떨어진 **같은 띠**(부호 행 등)가 Y만 비슷한데, 중간에 `cluster_rows` 가
    다른 얇은 행을 끼워 **연속 병합**으로는 못 잇는 경우: 끝 두 행만 한 수평 묶음으로 합친다.

    - 후보 두 행 i,j에 대해 `|mean_y(i)-mean_y(j)| <= y_gap` 이고 끝점 텍스트 수 한도 이하이며,
      **mean_y 가 두 끝의 Y사이(열린 구간)** 에 있고 텍스트 수가 `max_intermediate_row_items` 를 넘는
      다른 행이 있으면 병합하지 않는다(가로로 멀어도 그 사이에 END 등이 끼면 차단).
    - `max_bridge_passes` 기본 1 — 부호↔부호 한 번만 잇고, 그 다음 얇은 행까지 연쇄 병합하지 않음.
    """
    rows = [list(r) for r in rows if r]
    if len(rows) < 2:
        return rows
    gap = float(y_gap) if y_gap > 0 else 10.0
    mark_gap = (
        float(y_gap_for_mark_endpoints)
        if y_gap_for_mark_endpoints is not None and float(y_gap_for_mark_endpoints) > 0
        else gap
    )
    ep = max(2, int(max_endpoint_items))
    mid_max = max(0, int(max_intermediate_row_items))
    cap = max(16, int(max_merged_row_items))
    passes = max(1, int(max_bridge_passes))

    def sort_key(rs: list[list[dict[str, Any]]]) -> None:
        rs.sort(key=lambda r: -_row_mean_y(r))

    sort_key(rows)

    def try_one_merge() -> bool:
        n = len(rows)
        best: tuple[float, int, int] | None = None
        for i in range(n):
            for j in range(i + 1, n):
                ri, rj = rows[i], rows[j]
                # 헤더(부호/형태/상부근 등)는 가교 병합 대상이 아님 — 부호명끼리만 잇는다.
                if _row_has_any_beam_header_token(ri) or _row_has_any_beam_header_token(rj):
                    continue
                if len(ri) > ep or len(rj) > ep:
                    continue
                if len(ri) + len(rj) > cap:
                    continue
                dy = abs(_row_mean_y(ri) - _row_mean_y(rj))
                # 부호명(예: RG2 vs RG2C) 끼리는 도면에서 Y가 더 크게 흔들려도 같은 띠로 보는 경우가 있어 완화
                allow = mark_gap if (_row_has_beam_mark_like(ri) and _row_has_beam_mark_like(rj)) else gap
                if dy > allow:
                    continue
                my_i = _row_mean_y(ri)
                my_j = _row_mean_y(rj)
                y_lo = min(my_i, my_j)
                y_hi = max(my_i, my_j)
                ok_mid = True
                if y_hi - y_lo > 1e-9:
                    for k, rk in enumerate(rows):
                        if k in (i, j):
                            continue
                        my_k = _row_mean_y(rk)
                        if y_lo < my_k < y_hi and len(rk) > mid_max:
                            ok_mid = False
                            break
                if not ok_mid:
                    continue
                cand = (dy, i, j)
                if best is None or cand[0] < best[0]:
                    best = cand
        if best is None:
            return False
        _, bi, bj = best
        ri, rj = rows[bi], rows[bj]
        merged = ri + rj
        merged.sort(key=lambda r: float(r["x"]))
        rows[bi] = merged
        del rows[bj]
        sort_key(rows)
        return True

    for _ in range(passes):
        if not try_one_merge():
            break
    return rows


_RE_D = re.compile(r"D\s*(\d+)", re.I)
_RE_AT = re.compile(r"@\s*(\d+)", re.I)
_RE_X = re.compile(r"(\d+)\s*[xX]\s*(\d+)")
_RE_THICK_PAREN = re.compile(r"(\d+)\s*\(\s*(\d+)\s*\)")
_RE_THICK_SLASH = re.compile(r"(\d+)\s*/\s*(\d+)")
_RE_FLOOR_RANGE = re.compile(
    r"(?P<a>[A-Z]?\d+F|PH|RF|B\d+F)\s*[~～-]\s*(?P<b>[A-Z]?\d+F|PH|RF|B\d+F)",
    re.I,
)
# 지하층 등 숫자만의 층범위 — 부호보다 위에 따로 그린 한 줄(또는 `-4 ~ -3C22` 한 덩어리)
_RE_NUMERIC_FLOOR_STORY_PREFIX = re.compile(
    r"^\s*-?\d+\s*(?:[~～∼〜]|(?:\s+-\s+))\s*-?\d+",
    re.I,
)
_RE_NUMERIC_FLOOR_STORY_LINE_ONLY = re.compile(
    r"^\s*-?\d+\s*(?:[~～∼〜]|(?:\s+-\s+))\s*-?\d+\s*$",
    re.I,
)
_RE_FLOOR_TOKEN = re.compile(r"\b(B?\d+F|PH|RF|\d+층)\b", re.I)
# 기둥 일람 표 머리글·구역 행 추정(글자 중심 1차)
_RE_COLUMN_HEADER = re.compile(
    r"(부호|형태|단면|주근|대근|스트럽|스터럽|간격|비고|도\s*[\d@×x]|\bH\b|\s[x×]\s)",
    re.I,
)


def classify_column_row_role(row: list[dict[str, Any]]) -> str:
    """행 단위: 표 헤더 후보 / 부재(구역) 제목 후보 / 데이터."""
    texts = [str(r.get("text") or "").strip() for r in row]
    joined = " ".join(texts)
    if len(texts) >= 2 and _RE_COLUMN_HEADER.search(joined):
        return "header_like"
    if len(texts) == 1:
        t = texts[0]
        if 1 <= len(t) <= 48:
            if not re.search(r"\d{4,}", t):
                return "section_like"
    return "data"


def _norm_sched_label(s: str) -> str:
    """기둥 일람 좌측 라벨 비교용(공백·전각 공백 제거)."""
    t = (s or "").strip()
    return re.sub(r"[\s\u3000]+", "", t)


def classify_column_schedule_label(first_cell: str) -> str:
    """
    기둥 일람 한 행의 맨 왼쪽 셀 의미(행 종류).
    표는 [라벨 | 열1 | 열2 | …] 형태라 첫 셀로 구분한다.
    """
    raw = (first_cell or "").strip()
    t = _norm_sched_label(raw)
    if not t:
        return "unknown"
    if t in ("부호", "기호"):
        return "부호"
    if t in ("형태", "단면"):
        return "형태"
    if t == "주근":
        return "주근"
    if "대근" in t and "중앙" in t:
        return "대근_중앙"
    if "대근" in t and ("상하" in t or "상단" in t or "하단" in t):
        return "대근_상하단"
    if re.fullmatch(r"[A-Za-z]?\d+동", t) or (t.endswith("동") and len(t) <= 20 and not re.search(r"[~～-]", t)):
        return "동_헤더"
    return "other"


def pivot_column_schedule(
    merged_rows: list[dict[str, Any]],
    *,
    building_tag: str | None,
    wall_mode: str,
) -> list[dict[str, Any]] | None:
    """
    기둥 일람: '부호' 행의 부재명 열에 맞춰 주근·대근·형태 행을 열 단위로 묶는다.
    Y 정렬·행 라벨 인식이 맞을 때만 동작하며, 실패 시 None.
    """
    if not merged_rows:
        return None
    rows_sorted = sorted(
        merged_rows,
        key=lambda r: -float(r.get("row_y_mean") if r.get("row_y_mean") is not None else 0.0),
    )

    marks: list[str] = []
    ncols = 0
    for r in rows_sorted:
        cells = [str(c).strip() for c in (r.get("cells") or [])]
        if len(cells) < 2:
            continue
        kind = r.get("column_row_label") or classify_column_schedule_label(cells[0])
        if kind == "부호":
            marks = cells[1:]
            ncols = len(marks)
            break

    if ncols == 0 or not marks:
        return None

    building_from_drawing: str | None = None
    matrix: dict[str, list[str]] = {}

    for r in rows_sorted:
        cells = [str(c).strip() for c in (r.get("cells") or [])]
        if not cells:
            continue
        kind = r.get("column_row_label") or classify_column_schedule_label(cells[0])
        if kind == "동_헤더":
            building_from_drawing = cells[0].strip() or building_from_drawing
            continue
        if kind == "부호":
            continue
        if kind in ("주근", "형태", "대근_중앙", "대근_상하단") and len(cells) > 1:
            vals = cells[1 : 1 + ncols]
            while len(vals) < ncols:
                vals.append("")
            matrix[kind] = vals[:ncols]

    key_ko = {
        "주근": "주근",
        "형태": "형태",
        "대근_중앙": "대근(중앙)",
        "대근_상하단": "대근(상하단)",
    }

    eff_building = (building_tag or "").strip() or building_from_drawing

    out: list[dict[str, Any]] = []
    for j in range(ncols):
        mark_raw = (marks[j] if j < len(marks) else "") or ""
        story, mk, frng, _trail_mm = _parse_column_name_story_and_mark(mark_raw)
        _norm_nm = _normalize_column_story_text(mark_raw)
        base_mark_raw, _ = _strip_trailing_lone_dim_mm(_norm_nm)
        eff_mark = mk if mk is not None else base_mark_raw
        rec: dict[str, Any] = {
            "category": CATEGORY_COLUMN,
            "wall_mode": wall_mode,
            "mark": eff_mark,
        }
        if story:
            rec["story_label"] = story
        if frng:
            rec["floor_range"] = frng
        if eff_building:
            rec["building"] = eff_building
        flat_cells = [eff_mark]
        for mk_en, mk_ko in key_ko.items():
            col = matrix.get(mk_en)
            val = (col[j] if col and j < len(col) else "") or ""
            rec[mk_ko] = val
            flat_cells.append(val)
        rec["cells"] = flat_cells
        sig = _merge_signals([c for c in flat_cells if c])
        for k, v in sig.items():
            if k == "cells":
                continue
            if k not in rec:
                rec[k] = v
        if not (rec.get("story_label") or "").strip():
            sl = _story_label_from_floor_range(rec.get("floor_range"))
            if sl:
                rec["story_label"] = sl
        rec["column_row_role"] = "column_entry"
        out.append(rec)

    return out if out else None


# 기둥·벽기둥 부호: C1, -2C41B, WC1, -4C~-2C1(층범위+부호) 등 — NAME 끝에 붙은 500·700 등 단독 mm
_RE_COL_MARK_NAME = re.compile(
    r"^\s*-?\d+\s+(?:WC\d+[A-Za-z]?|[Cc]\d+[A-Za-z0-9]*|RC\d+[A-Za-z]?)(?:\s+\d{3,4})?\s*$"
    r"|^\s*-?\d+\s*[Cc]\d+[A-Za-z0-9]*(?:\s+\d{3,4})?\s*$"
    r"|^\s*-?\d+[A-Za-z][A-Za-z0-9]*(?:\s+\d{3,4})?\s*$"
    r"|^\s*-?\d+[A-Za-z0-9]*[~～][^\s]+(?:\s+\d{3,4})?\s*$"
    r"|^\s*WC\d+[A-Za-z]?\s*$"
    r"|^\s*RC\d+[A-Za-z]?\s*$"
    r"|^\s*[Cc]\d+[A-Za-z0-9]*\s*$",
    re.I,
)
_RE_EN_SCHED_HEADER = re.compile(
    r"^\s*(NAME|SECTION|SIZE|MAIN\s*BAR|MAINBAR|\(MID\)\s*HOOP\s*BAR|\(T&B\)\s*HOOP\s*BAR|"
    r"HOOP\s*BAR|HOOP\s*중앙|HOOP\s*상,?\s*하단|TIE\s*BAR|TIEBAR|"
    r"FCK|FC'|FCU|STRENGTH|CONCRETE\s*STRENGTH)\s*$",
    re.I,
)
_RE_KO_SCHED_HEADER = re.compile(
    r"^\s*(부\s*호|부호|형\s*태|형태|단면|주\s*근|주근|"
    r"대근\s*\(?\s*중앙\s*\)?|대근\s*\(?\s*상하단\s*\)?|대근|스트럽|스터럽|"
    r"강도|설계\s*기준\s*강도|기준\s*강도|압축\s*강도|콘크리트\s*강도)\s*$",
    re.I,
)
# MAIN BAR 셀에 붙어 들어온 fck = 35MPa 등
_RE_CONCRETE_STRENGTH_SNIPPET = re.compile(
    r"(?i)(?:\b(?:fck|fcu|fc'|f'c)\s*[:=]\s*[\d.]+\s*(?:MPa|mpa|N/mm²|N/mm2|N/mm\^2|kgf/cm²|kgf/cm2)?"
    r"|\b(?:fck|fcu)\s+[\d.]+\s*(?:MPa|mpa|N/mm²|N/mm2|kgf/cm²|kgf/cm2)?)",
)


def _strip_concrete_strength_snippets(text: str) -> tuple[str, str]:
    """강도 문구를 떼어낸 (나머지, 추출된 강도 문자열)."""
    if not (text or "").strip():
        return "", ""
    s = str(text).strip()
    found = [m.group(0).strip() for m in _RE_CONCRETE_STRENGTH_SNIPPET.finditer(s)]
    if not found:
        return s, ""
    rest = _RE_CONCRETE_STRENGTH_SNIPPET.sub(" ", s)
    rest = re.sub(r"\s+", " ", rest).strip()
    return rest, " ".join(found).strip()


def _lab_is_main_bar_header(lab: str) -> bool:
    lu = (lab or "").upper()
    cl = re.sub(r"[\s_\-]", "", (lab or ""))
    return "MAIN" in lu or "MAINBAR" in cl.upper() or "주근" in cl


def _lab_is_concrete_strength_header(lab: str, key_s: str) -> bool:
    """좌측 템플릿 행이 콘크리트 설계강도(fck) 칸인지 — `(1) 콘크리트`, `설계강도` 등."""
    ks = str(key_s or "")
    if re.search(r"(?i)fck|fcu|fc_spec|concrete_strength", ks):
        return True
    t = str(lab or "")
    if "콘크리트" in t and "철근" not in t:
        return True
    return bool(
        re.search(
            r"(?i)fck|fcu|fc'|f'c|설계\s*기준\s*강도|기준\s*강도|설계\s*강도|설계강도|"
            r"압축\s*강도|콘크리트\s*강도|구조\s*재료|구조재료",
            t,
        )
    )


def _harvest_concrete_strength_from_non_main_cells(rec: dict[str, Any]) -> None:
    """
    MAIN BAR 분리만으로는 못 잡는 경우: `(1) 콘크리트` 전용 행 등에만 `fck = 35MPa`가 있을 때 CONCRETE_STRENGTH 채움.
    """
    if (rec.get("CONCRETE_STRENGTH") or "").strip():
        return
    titles = rec.get("column_field_titles") or {}
    order = rec.get("column_field_key_order") or []
    cells_in = rec.get("cells") or []
    cells: list[str] = [str(c or "") for c in cells_in]
    while len(cells) < len(order):
        cells.append("")
    bits_list: list[str] = []
    for i, k in enumerate(order):
        if i >= len(cells):
            break
        lab = str(titles.get(k) or "")
        ks = str(k or "")
        raw = cells[i].strip()
        if not raw or not _RE_CONCRETE_STRENGTH_SNIPPET.search(raw):
            continue
        if _lab_is_main_bar_header(lab):
            continue
        if _lab_is_section_shape_row(lab):
            continue
        if re.search(r"(?i)name|부\s*호", lab) or "부호" in re.sub(r"[\s_\-]", "", lab):
            continue
        if re.search(r"(?i)-?\s*fy\s*=", lab) or re.search(r"(?i)-?\s*fy\s*=", raw[:32]):
            continue
        if "HOOP" in lab.upper() and "MAIN" not in lab.upper():
            continue
        if not _lab_is_concrete_strength_header(lab, ks) and not re.search(
            r"(?i)concrete|강도|콘크", ks
        ):
            continue
        _clean, bits = _strip_concrete_strength_snippets(raw)
        if bits:
            bits_list.append(bits)
    if bits_list:
        rec["CONCRETE_STRENGTH"] = " ".join(bits_list).strip()


# 단면도 안 디버그·참조용 '#32.C51' 등 — 현재 행 부호(C42)와 다르면 잘못 붙은 것으로 본다.
_RE_DRAWING_MARK_CALLOUT = re.compile(r"#\s*\d+\s*[.,]\s*([A-Za-z]\w*)", re.I)


def _lab_is_section_shape_row(lab: str) -> bool:
    lu = (lab or "").upper()
    cl = re.sub(r"[\s_\-]", "", (lab or ""))
    return "SECTION" in lu or "형태" in cl or "단면" in cl


def _strip_mismatched_draw_mark_callouts(text: str, member_mark: str | None) -> str:
    if not (text or "").strip() or not (member_mark or "").strip():
        return str(text or "").strip()
    em = str(member_mark).strip().upper()
    s = str(text).strip()
    out = s
    for m in list(_RE_DRAWING_MARK_CALLOUT.finditer(s)):
        tag = (m.group(1) or "").strip().upper()
        if not tag:
            continue
        if tag != em:
            out = out.replace(m.group(0), " ")
        else:
            # #32.C42 처럼 부호와 같으면 도면 참조 접두만 제거하고 부호 문자열은 유지
            out = out.replace(m.group(0), member_mark.strip())
    return re.sub(r"\s+", " ", out).strip()


def _strip_mismatched_draw_refs_in_section_cells(rec: dict[str, Any]) -> None:
    """형태/SECTION 칸에 아래 블록 부호(#n.Cxx)가 Y 오매칭으로 섞였을 때 제거."""
    mk = (rec.get("mark") or "").strip()
    if not mk:
        return
    titles = rec.get("column_field_titles") or {}
    order = rec.get("column_field_key_order") or []
    cells = [str(c or "") for c in (rec.get("cells") or [])]
    while len(cells) < len(order):
        cells.append("")
    changed = False
    for i, k in enumerate(order):
        if i >= len(cells):
            break
        lab = str(titles.get(k) or "")
        if not _lab_is_section_shape_row(lab):
            continue
        raw = cells[i].strip()
        if not raw:
            continue
        cleaned = _strip_mismatched_draw_mark_callouts(raw, mk)
        if cleaned != raw:
            cells[i] = cleaned
            rec[str(k)] = cleaned
            changed = True
    if changed:
        rec["cells"] = cells
    sec = str(rec.get("SECTION") or "").strip()
    if sec:
        c2 = _strip_mismatched_draw_mark_callouts(sec, mk)
        if c2 != sec:
            rec["SECTION"] = c2


def _apply_concrete_strength_split_to_record(rec: dict[str, Any]) -> None:
    """MAIN BAR(·주근) 칸에 섞인 fck 문구를 CONCRETE_STRENGTH 로 옮기고 cells·슬러그 키를 맞춘다."""
    titles = rec.get("column_field_titles") or {}
    order = rec.get("column_field_key_order") or []
    cells_in = rec.get("cells") or []
    cells: list[str] = [str(c or "") for c in cells_in]
    while len(cells) < len(order):
        cells.append("")
    strength_chunks: list[str] = []
    main_bar_seen = False
    last_main_clean: str = ""
    for i, k in enumerate(order):
        if i >= len(cells):
            break
        lab = str(titles.get(k) or "")
        if not _lab_is_main_bar_header(lab):
            continue
        raw = cells[i].strip()
        main_bar_seen = True
        cleaned, bits = _strip_concrete_strength_snippets(raw)
        if bits:
            strength_chunks.append(bits)
        cells[i] = cleaned
        rec[str(k)] = cleaned
        last_main_clean = cleaned
    rec["cells"] = cells
    if strength_chunks:
        prev = str(rec.get("CONCRETE_STRENGTH") or "").strip()
        merged = " ".join(strength_chunks)
        rec["CONCRETE_STRENGTH"] = " ".join(x for x in [prev, merged] if x).strip()
    if main_bar_seen:
        rec["MAIN_BAR"] = last_main_clean
        return
    main0 = str(rec.get("MAIN_BAR") or "").strip()
    if main0:
        cleaned, bits = _strip_concrete_strength_snippets(main0)
        if bits:
            prev = str(rec.get("CONCRETE_STRENGTH") or "").strip()
            rec["CONCRETE_STRENGTH"] = " ".join(x for x in [prev, bits] if x).strip()
            rec["MAIN_BAR"] = cleaned


def _collapse_repeated_label_half(text: str) -> str:
    """
    CAD에서 같은 라벨이 두 번 붙은 경우(부 호 부 호, MAIN BAR MAIN BAR) → 한 번만 남김.
    """
    raw = (text or "").strip()
    if not raw:
        return raw
    c = re.sub(r"[\s\u3000]+", "", raw)
    n = len(c)
    if n < 4 or n % 2 != 0:
        return raw
    h = n // 2
    if c[:h] != c[h:]:
        return raw
    left = c[:h]
    if left.upper() == "MAINBAR":
        return "MAIN BAR"
    if left.upper() == "TIEBAR":
        return "TIE BAR"
    return left


def _is_schedule_header_only(text: str) -> bool:
    t = _collapse_repeated_label_half((text or "").strip())
    if not t:
        return False
    if _RE_EN_SCHED_HEADER.match(t):
        return True
    if _RE_KO_SCHED_HEADER.match(t):
        return True
    return False


def _norm_label_key(s: str) -> str:
    t = re.sub(r"[\s\u3000]+", "", (s or "").strip())
    return t.casefold()


def _slug_field_key(label: str, index: int) -> str:
    """JSON/엑셀용 필드 키(충돌 시 인덱스)."""
    raw = (label or "").strip() or f"field_{index}"
    slug = re.sub(r"[^\w\u3131-\u318E\uAC00-\uD7A3]+", "_", raw, flags=re.UNICODE)
    slug = slug.strip("_")[:80] or f"field_{index}"
    return f"{slug}__{index}"


def _looks_like_column_mark_suffix_for_story_line(mark: str) -> bool:
    """`-2 C1` 등 층+부호 한 줄 — 좌측 반복 행제목이 아니다. `2 형 태` 같은 오탐은 배제."""
    m = (mark or "").strip()
    if not m or len(m) > 26:
        return False
    if not re.match(r"^[A-Za-z]", m):
        return False
    mu = m.upper()
    if any(x in mu for x in ("MAIN", "HOOP", "SECTION", "SIZE", "TIE", "NAME")):
        return False
    return True


def _is_probable_pure_data_value(text: str) -> bool:
    """행 라벨 후보에서 제외(치수·철근·층범위+부호·강도 등)."""
    t = (text or "").strip()
    if not t:
        return True
    # 보 부호가 좌측 라벨 열로 잘못 분류된 경우 → 값 열로 보냄(RG12, RG13B, RG11C …)
    ns = re.sub(r"[\s\u3000]+", "", t)
    if re.fullmatch(r"(?i)RG\d{1,3}[A-Z]{0,2}", ns) and len(ns) <= 12:
        return True
    if re.search(r"\d+\s*[xX×]\s*\d+", t):
        return True
    if re.search(r"D\s*\d+\s*@\s*\d+", t, re.I):
        return True
    if re.search(r"\d+\s*-\s*D\d+", t, re.I):
        return True
    if re.search(r"\d+\s*-\s*(?:U?HD|SHD)\d+", t, re.I):
        return True
    if _RE_CONCRETE_STRENGTH_SNIPPET.search(t):
        return True
    st, mk, frng, _td = _parse_column_name_story_and_mark(t)
    if frng is not None:
        return True
    if mk and st and re.fullmatch(r"-?\d+", st.strip()) and _looks_like_column_mark_suffix_for_story_line(mk):
        return True
    if _RE_COL_MARK_NAME.match(t):
        return True
    return False


def _is_likely_left_column_header_token(text: str) -> bool:
    """좌측 '행 제목' 열에만 올 문자 — 부호 값·치수와 구분."""
    return _is_schedule_header_only(text or "")


def _demote_misassigned_label_entities(
    label_items: list[dict[str, Any]],
    data_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    x_gap/비율로 좌측에 넣었으나 실제로는 부재 칸(부호·치수 등)인 TEXT를 우측으로 보낸다.
    같은 Y에 NAME과 -2 C1이 모두 좌측으로 잡히면 템플릿이 '-2 C1 NAME'으로 오염된다.
    """
    keep: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for it in label_items:
        t = str(it.get("text") or "").strip()
        if not t:
            continue
        if _is_probable_pure_data_value(t):
            demoted.append(it)
            continue
        if _is_likely_left_column_header_token(t):
            keep.append(it)
            continue
        # 애매하면: 부호 패턴이 섞인 복합 문자열은 데이터로
        if re.search(r"-?\d+\s+(?:WC\d+|[Cc]\d+|RC\d+)", t, re.I) and re.search(
            r"(NAME|SECTION|SIZE|MAIN|HOOP|부호|단면|주근)", t, re.I
        ):
            demoted.append(it)
            continue
        if len(t) > 36 and (re.search(r"\d+\s*-\s*D\d+", t, re.I) or re.search(r"D\s*\d+\s*@", t, re.I)):
            demoted.append(it)
            continue
        keep.append(it)
    return keep, data_items + demoted


def _is_beam_row_label_korean_anchor_text(text: str) -> bool:
    """좌열 '행 제목' 한글(부호·형태·근…) — X 기준점 잡을 때만 사용."""
    t = (text or "").strip()
    if not t or _is_probable_pure_data_value(t):
        return False
    collapsed = _collapse_repeated_label_half(t)
    if collapsed and _RE_KO_SCHED_HEADER.match(collapsed):
        return True
    return _is_beam_loose_left_row_title_for_x_cluster(t)


def _demote_beam_zone_column_headers_from_labels(
    label_items: list[dict[str, Any]],
    data_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    CENTER·INT.(BOTH)·ALL 등 데이터 열 머리를 좌측 행 라벨에서 제거한다.
    한글 행앵커(부호·형태…) X보다 오른쪽에만 적용 — 합성에서 INT가 좁은 x에 붙은 경우는 유지.
    """
    anchor_xs = [
        float(it["x"])
        for it in label_items
        if _is_beam_row_label_korean_anchor_text(str(it.get("text") or "").strip())
    ]
    anchor = _median_float(anchor_xs) if anchor_xs else None
    slop = 150.0
    keep: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for it in label_items:
        t = str(it.get("text") or "").strip()
        if t and _is_beam_data_zone_column_header_text(t):
            x = float(it["x"])
            if anchor is not None and math.isfinite(anchor) and x <= anchor + slop:
                keep.append(it)
            else:
                demoted.append(it)
        else:
            keep.append(it)
    return keep, data_items + demoted


def _build_y_template_from_bands(
    bands: list[tuple[float, str]],
    *,
    dedupe_same_label: bool,
) -> list[tuple[float, str, str]]:
    """
    (y, 표시 라벨, 고유 키 slug) 리스트. 위→아래(y 내림차순으로 bands가 온다고 가정.
    dedupe_same_label=True면 동일 문자열 라벨은 첫 줄만(전역 설명 열용).
    """
    rows: list[tuple[float, str, str]] = []
    seen_norm: set[str] = set()
    for i, (ym, lab) in enumerate(bands):
        lab_s = (lab or "").strip()
        if not lab_s:
            continue
        if _is_probable_pure_data_value(lab_s):
            continue
        nk = _norm_label_key(lab_s)
        if dedupe_same_label and nk in seen_norm:
            continue
        seen_norm.add(nk)
        key = _slug_field_key(lab_s, len(rows))
        rows.append((float(ym), lab_s, key))
    return rows


def _discover_row_template_from_labels(
    label_items: list[dict[str, Any]],
    band_y_tol: float,
    split_mode: str,
) -> tuple[list[tuple[float, str, str]], str]:
    """
    지정 영역 **좌측 열**만 사용. NAME·SECTION·부호·형태 등이 세로로 **여러 번 반복**되어도
    줄마다 별도 템플릿 행으로 둔다(전역에서 동일 라벨을 합치지 않음).
    """
    if not label_items:
        return [], "none"
    bands = _merge_strip_into_y_bands(label_items, band_y_tol, label_column=True)
    rows = _build_y_template_from_bands(bands, dedupe_same_label=False)
    rows = _trim_incomplete_trailing_template(rows, band_y_tol)
    if rows:
        return rows, split_mode or "labels_repeat"
    return [], "none"


def _trim_incomplete_trailing_template(
    template: list[tuple[float, str, str]],
    band_y_tol: float,
) -> list[tuple[float, str, str]]:
    """
    마지막 헤더 블록이 NAME 한 줄만 있고 잘리면(표 영역·인식 오류) 템플릿 꼬리를 제거해
    빈 부재 행·키 불일치를 줄인다.
    """
    # (T&B) HOOP BAR 다음에만 오는 고립 NAME = 새 블록 시작인데 아래 행이 없음 → 꼬리 제거
    if len(template) >= 2:
        last_lab = _norm_label_key((template[-1][1] or "").strip())
        prev_norm = _norm_label_key((template[-2][1] or "").strip())
        if last_lab == "name" and "t&b" in prev_norm:
            return _trim_incomplete_trailing_template(template[:-1], band_y_tol)
    if len(template) < 8:
        return template
    segs = _segment_template_rows(template, band_y_tol)
    if len(segs) < 2:
        return template
    tail = segs[-1]
    if len(tail) != 1:
        return template
    only = tail[0]
    _y, lab, _k = template[only]
    if _norm_label_key((lab or "").strip()) != "name":
        return template
    return template[:only]


def _is_beam_data_zone_column_header_text(text: str) -> bool:
    """보 세로블록: 데이터 열 머리(INT/CENTER/ALL…) — 좌측 행 라벨 템플릿에 있으면 안 된다."""
    t = (text or "").strip()
    if not t:
        return False
    ns = re.sub(r"[\s\u3000·.]+", "", t)
    if len(ns) > 28:
        return False
    if re.fullmatch(
        r"(?i)center|centre|all|int\.?\(?both\)?|ext\.?\(?both\)?|cen\.?|외부|내부",
        ns,
    ):
        return True
    if re.search(r"(?i)^end\s*\(", t):
        return True
    if re.fullmatch(r"(?i)int\.?", ns):
        return True
    return False


def _is_beam_loose_left_row_title_for_x_cluster(text: str) -> bool:
    """_RE_KO에 없는 짧은 변형(기호·형상 단독 등) — 클러스터 보조."""
    t = (text or "").strip()
    if not t or len(t) > 22 or _is_probable_pure_data_value(t) or _is_beam_data_zone_column_header_text(t):
        return False
    if re.match(
        r"^\s*(?:부\s*호|기호|보호|형\s*태|형상|단면|주\s*근|대\s*근|스\s*트\s*럽|스\s*터\s*럽)\s*$",
        t,
        re.I,
    ):
        return True
    return False


def _is_beam_row_label_token_for_x_cluster(text: str) -> bool:
    """
    좌측 라벨 열 X 클러스터 전용.
    `_RE_EN_SCHED_HEADER`의 SECTION/SIZE 등은 값 영역에 반복되어 hdr_x가 표 전체로 퍼지고
    refine( hdr_cluster )가 막히므로 **한글 일람 제목 + 좁은 영문(name/mark) + extras**만 사용한다.
    """
    t = (text or "").strip()
    if not t or _is_probable_pure_data_value(t) or _is_beam_data_zone_column_header_text(t):
        return False
    collapsed = _collapse_repeated_label_half(t)
    if not collapsed:
        return False
    if _RE_KO_SCHED_HEADER.match(collapsed):
        return True
    ct = collapsed.strip()
    if re.fullmatch(r"(?i)(name|mark)\s*", ct):
        return True
    if len(t) > 28:
        return False
    ns = re.sub(r"[\s\u3000·.]+", "", t)
    if len(ns) > 18:
        return False
    extras = (
        "상부근",
        "하부근",
        "피복철근",
        "표피철근",
        "측면근",
        "형상",
        "단면재",
        "골조",
        "주철근",
        "부철근",
    )
    for w in extras:
        if w in ns and len(ns) <= len(w) + 6:
            return True
    if re.fullmatch(r"(?i)(top|bottom)\s*bar", t):
        return True
    return False


def _is_beam_tight_row_label_token(text: str) -> bool:
    """
    보 일람 좌측 '행 제목' 후보(비율/x_gap보다 오른쪽에 잡힌 주황선을 좁힐 때 사용).
    값(INT/CENTER 등)·부재명(RG…)·철근 치수는 제외(_is_probable_pure_data_value 선행).
    """
    if _is_likely_left_column_header_token(text):
        return True
    return _is_beam_row_label_token_for_x_cluster(text)


def _percentile_sorted(vals: list[float], q: float) -> float:
    """q in [0,1]. len>=1."""
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    if n == 1:
        return float(s[0])
    i = int(round(q * (n - 1)))
    i = max(0, min(n - 1, i))
    return float(s[i])


def _refine_tight_beam_label_column_split(
    items: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    data: list[dict[str, Any]],
    split_mode: str,
    boundary: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, float]:
    """
    tight 보: 좌측 행제목(부호·상부근 등) X의 오른쪽 끝 + 마진으로 경계를 잡아
    비율·x_gap이 단면/INT 열 한가운데로 밀린 경우를 보정한다.
    """
    xs_all = [float(it["x"]) for it in items]
    mn, mx = min(xs_all), max(xs_all)
    span = mx - mn
    if span < 250.0:
        return labels, data, split_mode, boundary
    bx0 = float(boundary)
    label_zone_w = max(bx0 - mn, span * 0.02, 400.0)
    hdr_xs: list[float] = []
    for it in items:
        x = float(it["x"])
        # 잘못 넓게 잡힌 라벨 구역 안의 '진짜' 좌열 행제목만 사용(SECTION 등 원거리 제외)
        if x > bx0:
            continue
        t = str(it.get("text") or "").strip()
        if not t:
            continue
        if _is_beam_row_label_token_for_x_cluster(t) or _is_beam_loose_left_row_title_for_x_cluster(t):
            hdr_xs.append(x)
    if len(hdr_xs) < 1:
        return labels, data, split_mode, boundary
    hdr_lo = _percentile_sorted(hdr_xs, 0.10) if len(hdr_xs) >= 5 else min(hdr_xs)
    hdr_hi = _percentile_sorted(hdr_xs, 0.90) if len(hdr_xs) >= 5 else max(hdr_xs)
    # 좌열 행제목 X가 표 전체로 퍼진 오탐: '비율 라벨 구간' 폭 대비로만 제한
    if hdr_hi - hdr_lo > min(span * 0.45, label_zone_w * 1.05, 12000.0):
        return labels, data, split_mode, boundary
    margin = max(70.0, min(span * 0.024, 560.0))
    new_b = hdr_hi + margin
    if new_b >= bx0 - 0.5:
        return labels, data, split_mode, boundary
    new_labels = [it for it in items if float(it["x"]) <= new_b]
    new_data = [it for it in items if float(it["x"]) > new_b]
    min_data = max(4, len(items) // 24)
    if len(new_labels) < 1 or len(new_data) < min_data:
        return labels, data, split_mode, boundary
    return new_labels, new_data, split_mode + "|hdr_cluster", float(new_b)


def _refine_tight_beam_label_median_short_text_fallback(
    items: list[dict[str, Any]],
    labels: list[dict[str, Any]],
    data: list[dict[str, Any]],
    split_mode: str,
    boundary: float,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, float]:
    """
    행제목 토큰이 한 건도 안 잡힐 때: 좌측에 남은 짧은 문자 X 중앙 + 마진으로 경계를 좁힌다.
    """
    bx0 = float(boundary)
    xs: list[float] = []
    for it in labels:
        t = str(it.get("text") or "").strip()
        if not t or len(t) > 22:
            continue
        if _is_beam_data_zone_column_header_text(t) or _is_probable_pure_data_value(t):
            continue
        xs.append(float(it["x"]))
    if len(xs) < 5:
        return labels, data, split_mode, boundary
    xs_all = [float(it["x"]) for it in items]
    span = max(xs_all) - min(xs_all)
    med = _median_float(xs)
    nb = med + max(85.0, min(span * 0.028, 520.0))
    if nb >= bx0 - 0.5:
        return labels, data, split_mode, boundary
    new_labels = [it for it in items if float(it["x"]) <= nb]
    new_data = [it for it in items if float(it["x"]) > nb]
    min_data = max(4, len(items) // 24)
    if len(new_labels) < 1 or len(new_data) < min_data:
        return labels, data, split_mode, boundary
    return new_labels, new_data, split_mode + "|hdr_med_fallback", float(nb)


def _finalize_tight_beam_column_split_if_needed(
    items: list[dict[str, Any]],
    tight_label_column: bool,
    labels: list[dict[str, Any]],
    data: list[dict[str, Any]],
    split_mode: str,
    boundary: float | None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, float | None]:
    if (
        not tight_label_column
        or boundary is None
        or not items
        or not labels
        or not data
    ):
        return labels, data, split_mode, boundary
    bx = float(boundary)
    if not math.isfinite(bx):
        return labels, data, split_mode, boundary
    nl, nd, sm, nb = _refine_tight_beam_label_column_split(items, labels, data, split_mode, bx)
    if "hdr_cluster" not in sm and sm.startswith("left_ratio"):
        nl2, nd2, sm2, nb2 = _refine_tight_beam_label_median_short_text_fallback(
            items, nl, nd, sm, float(nb)
        )
        if "hdr_med_fallback" in sm2:
            return nl2, nd2, sm2, nb2
    return nl, nd, sm, nb


def _split_x_gap_looks_like_bad_label_partition(
    items: list[dict[str, Any]],
    left_g: list[dict[str, Any]],
    split_x: float,
) -> bool:
    """
    전체 X에서 '최대 간격'으로 잡은 분할이 라벨↔첫 값열이 아니라
    값 열들 사이(단면·부재명 가운데를 가르는 주황선)일 때 True.
    """
    if len(items) < 10 or not left_g:
        return False
    xs = [float(it["x"]) for it in items]
    mn, mx = min(xs), max(xs)
    span = mx - mn
    if span < 400.0:
        return False
    left_xs = [float(it["x"]) for it in left_g]
    left_max = max(left_xs)
    left_min = min(left_xs)
    left_w = left_max - left_min
    # 라벨 열만이면 보통 전체 폭의 ~28% 미만에 머문다.
    if left_w > span * 0.30:
        return True
    if left_max > mn + span * 0.33:
        return True
    rel = (float(split_x) - mn) / span
    if rel > 0.36:
        return True
    if len(left_g) > len(items) * 0.45:
        return True
    return False


def _split_column_label_and_data(
    items: list[dict[str, Any]],
    *,
    left_ratio: float = 0.22,
    tight_label_column: bool = False,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, float | None]:
    """
    좌측 = 반복 행 라벨(헤더), 우측 = 부재별 칸(값).
    큰 X 간격이 있으면 그 경계로 나누고, 없으면 전체 폭의 좌측 비율만 라벨로 본다.
    """
    left_g, right_g, split_x = _split_items_left_right_by_x_gap(items)
    if left_g and right_g and split_x is not None:
        if tight_label_column and _split_x_gap_looks_like_bad_label_partition(
            items, left_g, split_x
        ):
            left_g, right_g, split_x = [], list(items), None
        else:
            return _finalize_tight_beam_column_split_if_needed(
                items, tight_label_column, left_g, right_g, "x_gap", float(split_x)
            )
    if not items:
        return [], [], "none", None
    xs = [float(it["x"]) for it in items]
    mn, mx = min(xs), max(xs)
    span = mx - mn
    if span < 1e-6:
        return [], list(items), "single_column_x", float(mx)
    eff_ratio = min(left_ratio, 0.14) if tight_label_column else left_ratio
    lim = mn + span * eff_ratio
    labels = [it for it in items if float(it["x"]) <= lim]
    data = [it for it in items if float(it["x"]) > lim]
    if not data and labels:
        lim2 = mn + span * 0.08
        labels = [it for it in items if float(it["x"]) <= lim2]
        data = [it for it in items if float(it["x"]) > lim2]
        if data:
            return _finalize_tight_beam_column_split_if_needed(
                items, tight_label_column, labels, data, "left_ratio_tight", lim2
            )
        return [], list(items), "no_right_zone", lim2
    if not labels and data:
        return [], data, "no_left_zone", lim
    return _finalize_tight_beam_column_split_if_needed(
        items, tight_label_column, labels, data, "left_ratio", lim
    )


def _is_vertical_block_start_label(lab: str) -> bool:
    """좌측 헤더 열에서 '한 덩어리'가 다시 시작하는 줄(NAME·부호·기호 등). 2×2 격자에서 블록 경계."""
    t = (lab or "").strip()
    if not t:
        return False
    if re.match(r"^\s*(?:NAME|MARK)\s*$", t, re.I):
        return True
    ns = re.sub(r"[\s\u3000]+", "", t)
    if re.fullmatch(r"(?i)(name|mark)", ns):
        return True
    # 한글 일람: 부호 외에 '기호'만 쓰는 도면이 많음. 보호는 부호 오타로 자주 나옴.
    if ns in ("부호", "기호", "보호"):
        return True
    if re.match(r"^\s*부\s*호\s*$", t, re.I):
        return True
    return False


def _segment_template_rows(
    template: list[tuple[float, str, str]],
    band_y_tol: float,
) -> list[list[int]]:
    """
    좌측 라벨이 NAME…(T&B) 처럼 한 블록이 아래로 반복될 때 경계를 잡는다.
    1) **우선**: NAME·MARK·부호·기호 등 블록 시작 줄이 두 번 이상이면 그 위치만 경계로 쓴다.
       (한 블록 안에서 NAME↔SECTION Y 간격이 크면 '큰 갭' 분할이 NAME만 잘라내는 오탐이 난다.)
    2) 보조: 큰 Y 간격(행 간격보다 훨씬 큼) — 블록 시작 줄이 한 번뿐일 때만 의미 있음
    반환: 각 세그먼트는 템플릿 행 인덱스 목록.
    """
    n = len(template)
    if n <= 1:
        return [list(range(n))]

    starts = [i for i, (_y, lab, _k) in enumerate(template) if _is_vertical_block_start_label(lab)]
    if len(starts) >= 2:
        split: list[list[int]] = []
        for j, st in enumerate(starts):
            end = starts[j + 1] if j + 1 < len(starts) else n
            if st < end:
                split.append(list(range(st, end)))
        if len(split) > 1:
            return split

    ys = [template[i][0] for i in range(n)]
    gaps = [abs(ys[i] - ys[i + 1]) for i in range(n - 1)]
    med = sorted(gaps)[len(gaps) // 2] if gaps else 1.0
    thresh = max(med * 2.2, float(band_y_tol) * 6, 20.0)
    segments: list[list[int]] = []
    cur: list[int] = [0]
    for i in range(n - 1):
        if gaps[i] > thresh:
            segments.append(cur)
            cur = [i + 1]
        else:
            cur.append(i + 1)
    segments.append(cur)
    return [s for s in segments if s]


def _template_segment_y_intervals(
    template: list[tuple[float, str, str]],
    segments: list[list[int]],
    band_y_tol: float,
) -> list[tuple[float, float, list[int]]]:
    """각 세그먼트의 Y 구간(패딩 포함). 데이터 줄이 어느 헤더 블록에 속하는지 판별용."""
    pad = max(float(band_y_tol) * 5.5, 22.0)
    intervals: list[tuple[float, float, list[int]]] = []
    for seg in segments:
        ys = [template[i][0] for i in seg]
        lo, hi = min(ys) - pad, max(ys) + pad
        intervals.append((lo, hi, seg))
    return intervals


def _pick_template_segment_for_y(
    y_val: float,
    template: list[tuple[float, str, str]],
    segments: list[list[int]],
    band_y_tol: float,
) -> list[int]:
    """데이터 Y가 좌측 헤더 블록(세그먼트) 중 어디에 속하는지.

    부호·NAME 블록이 세로로 여러 번 반복될 때, 패딩 구간이 겹치면 잘못된 블록에 붙는 문제가 있어
    윗블록(Y 큰 쪽)부터 정렬한 뒤, 인접 블록 사이 Y 중점으로만 나눈다(겹침 없음).
    비정상(블록 Y가 서로 뒤섞임)이면 기존 패딩·중심 거리 방식으로 폴백한다.
    """
    if not segments:
        return []
    if len(segments) == 1:
        return segments[0]

    def _seg_y_minmax(seg: list[int]) -> tuple[float, float]:
        ys = [float(template[j][0]) for j in seg if 0 <= j < len(template)]
        if not ys:
            return float(y_val), float(y_val)
        return min(ys), max(ys)

    sorted_idx = sorted(
        range(len(segments)),
        key=lambda si: _seg_y_minmax(segments[si])[1],
        reverse=True,
    )
    sorted_segs = [segments[si] for si in sorted_idx]
    mids: list[float] = []
    chain_ok = True
    for i in range(len(sorted_segs) - 1):
        y_min_u, _y_max_u = _seg_y_minmax(sorted_segs[i])
        _y_min_l, y_max_l = _seg_y_minmax(sorted_segs[i + 1])
        if y_min_u < y_max_l - 1e-6:
            chain_ok = False
            break
        mids.append(0.5 * (y_min_u + y_max_l))

    y = float(y_val)
    if chain_ok and mids:
        for i, split in enumerate(mids):
            if y >= split:
                return sorted_segs[i]
        return sorted_segs[-1]

    intervals = _template_segment_y_intervals(template, segments, band_y_tol)

    def _dist_to_interval(yp: float, lo: float, hi: float) -> float:
        if yp < lo:
            return lo - yp
        if yp > hi:
            return yp - hi
        return 0.0

    def _seg_mid_y(seg: list[int]) -> float:
        lo2, hi2 = _seg_y_minmax(seg)
        return 0.5 * (lo2 + hi2)

    scored: list[tuple[float, float, list[int]]] = []
    for lo, hi, seg in intervals:
        d = _dist_to_interval(y, lo, hi)
        scored.append((d, abs(y - _seg_mid_y(seg)), seg))
    scored.sort(key=lambda t: (t[0], t[1]))
    min_d = scored[0][0]
    ties = [t for t in scored if abs(t[0] - min_d) < 1e-9]
    ties.sort(key=lambda t: (t[1], min(t[2])))
    return ties[0][2]


def _compact_label_text(lab: str) -> str:
    return re.sub(r"[\s_\-]", "", (lab or "").strip())


def _first_buho_or_name_template_index(
    template: list[tuple[float, str, str]],
    seg: list[int],
) -> int | None:
    """세그먼트 안에서 부호·기호·NAME 행 인덱스(우측 부재명 줄 매핑용)."""
    for i in seg:
        _y, lab, _k = template[i]
        ct = _compact_label_text(lab)
        if "부호" in ct or "기호" in ct or "보호" in ct:
            return i
        if re.fullmatch(r"(?i)name|mark", (lab or "").strip() or ""):
            return i
    return None


def _normalize_column_story_text(s: str) -> str:
    """부호/층 표기용: 유니코드 마이너스·물결을 ASCII에 맞춘다."""
    t = (s or "").strip()
    t = t.replace("\u2212", "-").replace("\u2013", "-").replace("\u2014", "-")
    t = t.replace("～", "~").replace("〜", "~").replace("∼", "~")
    return t


def _strip_trailing_lone_dim_mm(s: str) -> tuple[str, int | None]:
    """
    NAME 칸 끝에 치수선과 붙어 `… 500`처럼 읽힌 단면 한 변(mm)을 부호에서 분리한다.
    본문에 글자·물결 등이 없으면(숫자만) 분리하지 않는다.
    """
    t = (s or "").strip()
    if not t:
        return t, None
    m = re.match(r"^(.+?)\s+(\d{3,4})\s*$", t)
    if not m:
        return t, None
    body, ns = m.group(1).rstrip(), m.group(2)
    try:
        num = int(ns)
    except ValueError:
        return t, None
    if not (200 <= num <= 4000):
        return t, None
    if not re.search(r"[A-Za-z가-힣~～]", body):
        return t, None
    return body, num


def _story_label_from_floor_range(fr: list[str] | tuple[str, ...] | None) -> str | None:
    """
    화면·보내기 '층' 컬럼(story_label)용.
    floor_range 가 ['-4','-3'] 이거나 ['~','-1'] 일 때 '-4~-3', ~-1 형으로 만든다.
    """
    if not fr or len(fr) < 2:
        return None
    a, b = str(fr[0] or "").strip(), str(fr[1] or "").strip()
    if a == "~" and b:
        return f"~{b}"
    if a and b:
        return f"{a}~{b}"
    return None


def _map_data_bands_to_template(
    bands: list[tuple[float, str]],
    template: list[tuple[float, str, str]],
    band_y_tol: float,
    *,
    beam_vertical_schedule: bool = False,
) -> dict[str, str]:
    """
    우측 세로 칸의 각 텍스트 줄을 템플릿 행에 매핑.
    좌측에 헤더 블록이 세로로 반복되면(2×2 격자 등), Y 구간으로 세그먼트를 고른 뒤 그 안에서만 최근접 매칭한다.

    beam_vertical_schedule=True (보 세로 일람):
    세그먼트 세로 폭(span)이 커도 `span*0.58` 만큼 허용하면 형태·상부근 등 **먼 줄**에
    같은 열의 다른 행 텍스트가 붙는다. 인접 템플릿 행 간격(median gap)으로만 loose를 잡고,
    기둥용 부호 강제 스냅(층+부호)은 사용하지 않는다.
    """
    if not template:
        return {}
    segments = _segment_template_rows(template, band_y_tol)
    all_label_norms = {_norm_label_key(lab) for _y, lab, _k in template}
    out: dict[str, str] = {}
    byt = float(band_y_tol)
    base_loose = max(byt * 14.0, 40.0)

    for y_val, txt in bands:
        t = (txt or "").strip()
        if not t:
            continue
        if _is_schedule_header_only(t):
            continue
        if _norm_label_key(t) in all_label_norms:
            continue
        seg = _pick_template_segment_for_y(y_val, template, segments, band_y_tol)
        if not seg:
            continue
        seg_ys = [template[i][0] for i in seg]
        span = max(seg_ys) - min(seg_ys) if seg_ys else 0.0
        if beam_vertical_schedule:
            seg_yf = sorted(float(template[i][0]) for i in seg)
            gaps = [
                abs(seg_yf[i] - seg_yf[i + 1]) for i in range(len(seg_yf) - 1)
            ]
            med_gap = float(_median_float(gaps)) if gaps else max(byt * 25.0, 120.0)
            loose = max(byt * 9.0, med_gap * 1.38, 92.0)
            loose = min(loose, max(430.0, med_gap * 2.45))
        else:
            # 행 간격·SECTION 등으로 Y가 어긋나도 매칭되도록 여유 확대
            loose = max(base_loose, span * 0.58 + byt * 3.5, 52.0)
        best_i = min(seg, key=lambda i: abs(y_val - template[i][0]))
        dist = abs(y_val - template[best_i][0])
        # WC1·층범위 등 부호(또는 부호 행)보다 Y가 많이 떨어진 줄: loose+extra 안이면
        # 부호 템플릿 행에 붙이되, 그래도 dist>loose 이면 아래에서 continue 하지 않는다.
        relaxed_to_bu = False
        if not beam_vertical_schedule:
            if _RE_COL_MARK_NAME.match(t):
                bu = _first_buho_or_name_template_index(template, seg)
                if bu is not None:
                    dist_bu = abs(y_val - float(template[bu][0]))
                    extra = max(24000.0, span * 2.2, 8000.0)
                    # 부호·형태 중 형태에 더 가깝게 붙는 오매칭보다 부호 행을 우선
                    if dist_bu <= loose + extra:
                        best_i = bu
                        dist = dist_bu
                        relaxed_to_bu = True
            elif dist > loose and _RE_NUMERIC_FLOOR_STORY_PREFIX.match(t):
                bu = _first_buho_or_name_template_index(template, seg)
                if bu is not None:
                    dist_bu = abs(y_val - float(template[bu][0]))
                    extra = max(24000.0, span * 2.2, 8000.0)
                    if dist_bu <= loose + extra:
                        best_i = bu
                        dist = dist_bu
                        relaxed_to_bu = True
        if dist > loose and not relaxed_to_bu:
            continue
        _y, _lab, key = template[best_i]
        if key in out:
            prev = str(out[key] or "").strip()
            t_now = t.strip()
            if _RE_NUMERIC_FLOOR_STORY_LINE_ONLY.match(t_now) and not _RE_NUMERIC_FLOOR_STORY_LINE_ONLY.match(
                prev
            ):
                out[key] = (t_now + " " + prev).strip()
            elif _RE_NUMERIC_FLOOR_STORY_LINE_ONLY.match(prev) and not _RE_NUMERIC_FLOOR_STORY_LINE_ONLY.match(
                t_now
            ):
                out[key] = (prev + " " + t_now).strip()
            else:
                out[key] = (prev + " " + t).strip()
        else:
            out[key] = t
    return out


def _fallback_fill_by_regex(band_texts: list[str]) -> dict[str, str]:
    """템플릿 실패 시 기존 정규식 슬롯."""
    return _fill_vertical_block_fields(band_texts)


def _split_items_left_right_by_x_gap(
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], float | None]:
    """큰 X 간격이 있으면 좌측(행 라벨)·우측(데이터 블록)으로 나눈다."""
    if len(items) < 4:
        return [], items, None
    xs = sorted(it["x"] for it in items)
    best = (-1.0, -1)
    for i in range(len(xs) - 1):
        g = xs[i + 1] - xs[i]
        if g > best[0]:
            best = (g, i)
    split_x = (xs[best[1]] + xs[best[1] + 1]) / 2.0
    if best[0] < max(50.0, (xs[-1] - xs[0]) * 0.04):
        return [], items, None
    left = [it for it in items if it["x"] < split_x]
    right = [it for it in items if it["x"] >= split_x]
    if len(right) < max(2, len(items) // 10):
        return [], items, None
    return left, right, split_x


def _median_float(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    n = len(s)
    m = n // 2
    return float(s[m]) if n % 2 else 0.5 * (s[m - 1] + s[m])


def _auto_column_strip_gap(items: list[dict[str, Any]]) -> float:
    xs = sorted({round(float(it["x"]), 2) for it in items})
    if len(xs) < 2:
        return 120.0
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    gaps.sort()
    med = gaps[len(gaps) // 2]
    return max(med * 3.5, 70.0)


def _auto_column_strip_gap_relaxed(items: list[dict[str, Any]]) -> float:
    """자동 열 분할이 한 덩어리로만 나올 때 보조용(더 촘촘한 임계값)."""
    xs = sorted({round(float(it["x"]), 2) for it in items})
    if len(xs) < 2:
        return 80.0
    gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
    med = _median_float(gaps)
    p75 = sorted(gaps)[len(gaps) * 3 // 4] if gaps else med
    span = xs[-1] - xs[0]
    return max(36.0, min(med * 2.1 + p75 * 0.4, span / 6.0))


_STRIP_CLUSTER_SEED_MAX_TEXT = 96


def _is_column_strip_seed_candidate(it: dict[str, Any]) -> bool:
    """열 시드: 치수만(500)·1~2글자 등은 열 중심 후보에서 제외해 열이 과분할되지 않게 한다."""
    t = str(it.get("text") or "").strip()
    if not t or len(t) <= 2:
        return False
    if len(t) <= 4 and re.fullmatch(r"-?\d{2,4}", t):
        return False
    if len(t) <= _STRIP_CLUSTER_SEED_MAX_TEXT:
        return True
    return False


def _split_seed_items_by_max_x_gap(
    sorted_seeds: list[dict[str, Any]],
    *,
    aggressive: bool = False,
) -> list[list[dict[str, Any]]]:
    """
    짧은 텍스트(셀 값) 위주 시드를 X로 정렬한 뒤, 가장 큰 간격으로 재귀 분할해 부재 열 후보를 만든다.
    SECTION 등 가로로 긴 문자는 시드에서 빼 두어 열이 한 덩어리로 붙는 것을 줄인다.
    """
    n = len(sorted_seeds)
    if n <= 1:
        return [sorted_seeds] if sorted_seeds else []
    xs = [float(r["x"]) for r in sorted_seeds]
    gaps = [(xs[i + 1] - xs[i], i) for i in range(n - 1)]
    gmax, imax = max(gaps, key=lambda t: t[0])
    glist = [g for g, _ in gaps]
    med = _median_float(glist)
    p75 = sorted(glist)[len(glist) * 3 // 4] if glist else gmax
    span = xs[-1] - xs[0]
    if aggressive:
        thresh = max(32.0, med * 1.55, p75 * 1.35, span * 0.009)
    else:
        thresh = max(48.0, med * 2.75, p75 * 1.8, span * 0.014)
    if gmax <= thresh or imax < 0:
        return [sorted_seeds]
    left = sorted_seeds[: imax + 1]
    right = sorted_seeds[imax + 1 :]
    return (
        _split_seed_items_by_max_x_gap(left, aggressive=aggressive)
        + _split_seed_items_by_max_x_gap(right, aggressive=aggressive)
    )


def _partition_data_items_into_member_strips(items: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    우측 값 영역을 부재(세로 칸)별로 나눈다.
    X만으로 단일 임계값을 쓰면 SECTION·긴 칸 때문에 열이 한 줄로 붙는 경우가 많아,
    짧은 텍스트 시드로 열 중심을 잡은 뒤 모든 엔티티를 가장 가까운 열에 넣는다.
    """
    if not items:
        return []
    if len(items) == 1:
        return [items]
    seeds = [it for it in items if _is_column_strip_seed_candidate(it)]
    if len(seeds) < max(4, len(items) // 6):
        seeds = [
            it
            for it in items
            if len(str(it.get("text") or "").strip()) <= _STRIP_CLUSTER_SEED_MAX_TEXT
        ]
    if len(seeds) < max(4, len(items) // 6):
        seeds = list(items)
    s = sorted(seeds, key=lambda r: float(r["x"]))
    seed_groups = _split_seed_items_by_max_x_gap(s, aggressive=False)
    if len(seed_groups) <= 1 and len(s) >= 10:
        seed_groups = _split_seed_items_by_max_x_gap(s, aggressive=True)
    if len(seed_groups) <= 1:
        tight = _auto_column_strip_gap_relaxed(items)
        return _cluster_items_by_x_gap(items, tight)
    centroids: list[float] = []
    for g in seed_groups:
        xsg = [float(r["x"]) for r in g]
        centroids.append(sum(xsg) / len(xsg))
    # 인접 열 중점 사이로만 나누면 경계에서 가장 가까운 열로 몰리는 문제를 줄임
    order = sorted(range(len(centroids)), key=lambda k: centroids[k])
    xc = [centroids[k] for k in order]
    bounds = [0.5 * (xc[i] + xc[i + 1]) for i in range(len(xc) - 1)]
    strips: list[list[dict[str, Any]]] = [[] for _ in centroids]
    for it in items:
        x = float(it["x"])
        si = 0
        for b in bounds:
            if x >= b:
                si += 1
        strips[order[si]].append(it)
    return [st for st in strips if st]


def _merge_narrow_adjacent_strips(
    strips: list[list[dict[str, Any]]],
    *,
    max_center_gap: float = 980.0,
    narrow_strip_max: int = 12,
    neighbor_strip_min: int = 10,
    fragment_pair_gap: float = 880.0,
    fragment_combined_max: int = 96,
    absorb_narrow_gap: float = 1350.0,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """
    단면·주근이 넓게 한 칸인데 TEXT가 좁은 열로 쪼개져(치수·fck·잡글자) 같은 단면이
    여러 스트립으로 나뉘는 경우를 줄인다.

    1) 조각+조각: 둘 다 엔티티 수가 적고 X 중심 간격이 짧으면 먼저 합친다
       (기존 `max(la,lb) >= neighbor_strip_min` 때문에 6+4가 못 붙던 케이스).
    2) 얇은 열+굵은 이웃: 기존 규칙(gap <= max_center_gap).
    3) 남은 얇은 열: 한쪽 이웃과의 간격이 absorb_narrow_gap 이하이면 더 가까운 쪽에 흡수
       (단, 부재 열 간격보다 작은 값만 사용해 다음 칸 전체와는 합치지 않음).
    """
    nonempty = [list(s) for s in strips if s]
    if len(nonempty) < 2:
        return nonempty, []

    def _centers(blocks: list[list[dict[str, Any]]]) -> list[tuple[float, list[dict[str, Any]]]]:
        keyed: list[tuple[float, list[dict[str, Any]]]] = []
        for s in blocks:
            xs = [float(it["x"]) for it in s]
            keyed.append((sum(xs) / len(xs), s))
        keyed.sort(key=lambda t: t[0])
        return keyed

    log: list[dict[str, Any]] = []
    keyed = _centers(nonempty)

    # --- pass A: 조각 스트립끼리 연쇄 병합 ---
    changed = True
    while changed and len(keyed) >= 2:
        changed = False
        out_k: list[tuple[float, list[dict[str, Any]]]] = [keyed[0]]
        for xm, s in keyed[1:]:
            px, prev = out_k[-1]
            gap = xm - px
            la, lb = len(prev), len(s)
            if (
                gap <= fragment_pair_gap
                and la <= narrow_strip_max
                and lb <= narrow_strip_max
                and la + lb <= fragment_combined_max
            ):
                merged = prev + s
                nxc = sum(float(it["x"]) for it in merged) / len(merged)
                out_k[-1] = (nxc, merged)
                log.append(
                    {
                        "merge_kind": "fragment_pair",
                        "merged_from_center_x": round(xm, 4),
                        "into_center_x_before": round(px, 4),
                        "gap": round(gap, 4),
                        "entity_counts": [la, lb],
                    },
                )
                changed = True
            else:
                out_k.append((xm, s))
        keyed = out_k

    # --- pass B: 얇은 열 ↔ 굵은 이웃(기존) ---
    out: list[list[dict[str, Any]]] = [keyed[0][1]]
    for xm, s in keyed[1:]:
        prev = out[-1]
        px = sum(float(it["x"]) for it in prev) / len(prev)
        gap = xm - px
        la, lb = len(prev), len(s)
        if (
            gap <= max_center_gap
            and min(la, lb) <= narrow_strip_max
            and (
                max(la, lb) >= neighbor_strip_min
                or (min(la, lb) <= narrow_strip_max and la + lb >= neighbor_strip_min)
            )
        ):
            out[-1] = prev + s
            log.append(
                {
                    "merge_kind": "narrow_into_neighbor",
                    "merged_from_center_x": round(xm, 4),
                    "into_center_x_before": round(px, 4),
                    "gap": round(gap, 4),
                    "entity_counts": [la, lb],
                },
            )
        else:
            out.append(s)

    # --- pass C: 여전히 얇은 스트립을 가까운 한쪽에 흡수(부재 열 피치보다 작은 gap만) ---
    def _median_neighbor_pitch(blocks: list[list[dict[str, Any]]]) -> float | None:
        if len(blocks) < 3:
            return None
        xs = []
        for s in blocks:
            tx = [float(it["x"]) for it in s]
            xs.append(sum(tx) / len(tx))
        xs.sort()
        gaps = [xs[i + 1] - xs[i] for i in range(len(xs) - 1)]
        if not gaps:
            return None
        gaps.sort()
        return float(gaps[len(gaps) // 2])

    pitch = _median_neighbor_pitch(out)
    absorb_cap = min(absorb_narrow_gap, pitch * 0.42) if pitch and pitch > 400 else absorb_narrow_gap

    i = 0
    while i < len(out):
        s = out[i]
        if len(s) > narrow_strip_max or len(out) < 2:
            i += 1
            continue
        xs = [float(it["x"]) for it in s]
        xc = sum(xs) / len(xs)
        left_g = right_g = None
        if i > 0:
            ps = out[i - 1]
            px = sum(float(it["x"]) for it in ps) / len(ps)
            left_c, left_g = px, xc - px
        if i + 1 < len(out):
            ns = out[i + 1]
            nx = sum(float(it["x"]) for it in ns) / len(ns)
            right_c, right_g = nx, nx - xc
        pick = None
        if left_g is not None and right_g is not None:
            if left_g <= absorb_cap and right_g <= absorb_cap:
                pick = "left" if left_g <= right_g else "right"
            elif left_g <= absorb_cap:
                pick = "left"
            elif right_g <= absorb_cap:
                pick = "right"
        elif left_g is not None and left_g <= absorb_cap:
            pick = "left"
        elif right_g is not None and right_g <= absorb_cap:
            pick = "right"
        if pick == "left":
            tgt = i - 1
            log.append(
                {
                    "merge_kind": "absorb_narrow_to_left",
                    "strip_index": i,
                    "gap": round(float(left_g), 4),
                    "entity_count": len(s),
                    "absorb_cap": round(float(absorb_cap), 4),
                },
            )
            out[tgt] = out[tgt] + s
            del out[i]
            continue
        if pick == "right":
            log.append(
                {
                    "merge_kind": "absorb_narrow_to_right",
                    "strip_index": i,
                    "gap": round(float(right_g), 4),
                    "entity_count": len(s),
                    "absorb_cap": round(float(absorb_cap), 4),
                },
            )
            out[i + 1] = s + out[i + 1]
            del out[i]
            continue
        i += 1

    return out, log


def _cluster_items_by_x_gap(
    items: list[dict[str, Any]],
    gap: float,
) -> list[list[dict[str, Any]]]:
    """X 정렬 후 인접 간격이 gap 초과일 때 새 세로 블록(부재 열)."""
    if not items:
        return []
    s = sorted(items, key=lambda r: float(r["x"]))
    chunks: list[list[dict[str, Any]]] = [[s[0]]]
    for it in s[1:]:
        if float(it["x"]) - float(chunks[-1][-1]["x"]) > gap:
            chunks.append([it])
        else:
            chunks[-1].append(it)
    return chunks


def _merge_strip_into_y_bands(
    strip: list[dict[str, Any]],
    band_y_tol: float,
    *,
    label_column: bool = False,
) -> list[tuple[float, str]]:
    """한 세로 블록 안에서 Y로 줄 묶기(위→아래 순). CAD Y 증가 방향에 맞춰 위쪽이 먼저."""
    if not strip:
        return []
    s = sorted(strip, key=lambda r: (-float(r["y"]), float(r["x"])))
    bands: list[list[dict[str, Any]]] = [[s[0]]]
    for it in s[1:]:
        if abs(float(it["y"]) - float(bands[-1][-1]["y"])) <= band_y_tol:
            bands[-1].append(it)
        else:
            bands.append([it])
    out: list[tuple[float, str]] = []

    def _merge_band_texts(b: list[dict[str, Any]]) -> str:
        if not label_column or len(b) <= 1:
            if len(b) > 1:
                by_x = sorted(b, key=lambda r: float(r["x"]))
                txts = [str(x.get("text") or "").strip() for x in by_x]
            else:
                txts = [str(x.get("text") or "").strip() for x in b]
            merged = " ".join(t for t in txts if t).strip()
            return _collapse_repeated_label_half(merged)
        by_x = sorted(b, key=lambda r: float(r["x"]))
        hdr_only = [x for x in by_x if _is_likely_left_column_header_token(str(x.get("text") or "").strip())]
        if hdr_only:
            merged = " ".join(str(x.get("text") or "").strip() for x in hdr_only).strip()
            return _collapse_repeated_label_half(merged)
        return _collapse_repeated_label_half(str(by_x[0].get("text") or "").strip())

    for b in bands:
        ys = [float(x["y"]) for x in b]
        ym = sum(ys) / len(ys)
        merged = _merge_band_texts(b)
        out.append((ym, merged))
    out.sort(key=lambda t: -t[0])
    return out


def _fill_vertical_block_fields(band_texts: list[str]) -> dict[str, str]:
    """
    세로로 쌓인 문자열(위→아래)에서 NAME/SIZE/MAIN/HOOP 순서를 휴리스틱으로 채운다.
    라벨 전용 줄(NAME 등)은 건너뛴다.
    """
    keys = ("mark", "SECTION", "SIZE", "CONCRETE_STRENGTH", "MAIN_BAR", "(MID) HOOP BAR", "(T&B) HOOP BAR")
    out: dict[str, str] = {k: "" for k in keys}
    hoop_idx = 0
    for raw in band_texts:
        t = raw.strip()
        if not t:
            continue
        if _is_schedule_header_only(t):
            continue
        if _RE_EN_SCHED_HEADER.match(t):
            continue
        if _RE_COL_MARK_NAME.match(t) and not out["mark"]:
            out["mark"] = t
            continue
        if _RE_X.search(t) and re.search(r"\d+\s*[xX×]\s*\d+", t):
            if not out["SIZE"]:
                out["SIZE"] = t
            continue
        if _RE_CONCRETE_STRENGTH_SNIPPET.search(t) and not re.search(
            r"\d+\s*-\s*(?:U?HD|SHD|D)\d+", t, re.I
        ):
            if not out["CONCRETE_STRENGTH"]:
                out["CONCRETE_STRENGTH"] = t
            continue
        if re.search(r"\d+\s*-\s*(?:U?HD|SHD|D)\d+", t, re.I) or re.search(
            r"\d+\s*-\s*D\d+", t, re.I
        ) or re.search(r"\d+\s*D\d+", t, re.I):
            if not out["MAIN_BAR"]:
                out["MAIN_BAR"] = t
            continue
        if re.search(r"D\s*\d+\s*@\s*\d+", t, re.I):
            if hoop_idx == 0:
                out["(MID) HOOP BAR"] = t
                hoop_idx = 1
            else:
                out["(T&B) HOOP BAR"] = t
            continue
        if not out["mark"] and len(t) <= 32:
            out["mark"] = t
            continue
        if not out["SECTION"] and len(t) > 40:
            out["SECTION"] = t
    return out


def _segment_y_bounds_world(
    template: list[tuple[float, str, str]],
    seg_indices: list[int],
    band_y_tol: float,
) -> tuple[float, float]:
    """좌측 템플릿 세그먼트의 Y 범위(+패딩) — 우측 데이터 줄이 어느 층 블록에 속하는지 가늠."""
    if not seg_indices:
        return 0.0, 0.0
    pad = max(float(band_y_tol) * 8.0, 40.0)
    ys = [float(template[i][0]) for i in seg_indices if 0 <= i < len(template)]
    if not ys:
        return 0.0, 0.0
    return min(ys) - pad, max(ys) + pad


def _row_data_anchor_y_from_strip(
    strip: list[dict[str, Any]],
    y_lo: float,
    y_hi: float,
    *,
    strip_item_allow: Callable[[dict[str, Any]], bool] | None = None,
) -> float | None:
    """해당 템플릿 세그먼트 Y구간에 걸리는 우측(값) 엔티티 Y 중앙값 — 단면 조회 세로 중심."""
    ys: list[float] = []
    for it in strip:
        if strip_item_allow is not None and not strip_item_allow(it):
            continue
        try:
            y = float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if y_lo <= y <= y_hi:
            ys.append(y)
    if not ys:
        return None
    return round(_median_float(ys), 4)


def _row_data_anchor_y_from_strip_relaxed(
    strip: list[dict[str, Any]],
    y_lo: float,
    y_hi: float,
    band_y_tol: float,
    *,
    strip_item_allow: Callable[[dict[str, Any]], bool] | None = None,
) -> float | None:
    """좌·우 열 Y가 어긋진 도면: 구간을 넓혀 한 번 더 시도."""
    rda = _row_data_anchor_y_from_strip(strip, y_lo, y_hi, strip_item_allow=strip_item_allow)
    if rda is not None:
        return rda
    span = max(y_hi - y_lo, float(band_y_tol) * 12.0, 120.0)
    # span*0.42+180 은 세그먼트가 길수록 과도하게 넓어져 이웃 층·하단 주석 Y가
    # 중앙값에 섞여 단면 앵커가 아래로 밀리는 경우가 있음 → 비율 기반으로 상한.
    pad_raw = span * 0.42 + 180.0
    pad_cap = span * 0.24 + max(280.0, float(band_y_tol) * 14.0)
    pad = min(pad_raw, pad_cap)
    mid = 0.5 * (y_lo + y_hi)
    return _row_data_anchor_y_from_strip(strip, mid - pad, mid + pad, strip_item_allow=strip_item_allow)


def _split_dyn_by_template_segments(
    dyn: dict[str, str],
    template: list[tuple[float, str, str]],
    band_y_tol: float,
) -> list[tuple[dict[str, str], list[tuple[float, str, str]], list[int]]]:
    """
    한 세로 스트립에 -2층·-1층처럼 좌측 라벨 블록이 세로로 여러 번 붙은 경우,
    템플릿 세그먼트마다 별도 부재 레코드로 나눈다.
    """
    segs = _segment_template_rows(template, band_y_tol)
    out: list[tuple[dict[str, str], list[tuple[float, str, str]], list[int]]] = []
    for seg_indices in segs:
        sub_t = [template[i] for i in seg_indices]
        keys = [template[i][2] for i in seg_indices]
        sub_dyn = {k: str(dyn.get(k) or "").strip() for k in keys}
        if not any(sub_dyn.values()):
            continue
        out.append((sub_dyn, sub_t, list(seg_indices)))
    return out


def _parse_column_name_story_and_mark(
    name_cell: str,
) -> tuple[str | None, str | None, list[str] | None, int | None]:
    """
    부재명(NAME/부호 셀)에서 층(표시)·부호·층범위·끝단독치수(mm)를 분리한다.
    도면에서 `~`는 층 범위(예: -4~-3, ~-1)로 쓰이는 경우가 많고,
    `-3C31` 처럼 층·부호가 붙은 표기는 앞의 `-3`만 층·뒤를 부호로 본다.
    `-4C~-2C1` 처럼 층·부호가 `~`에 붙어 한 덩어리인 표기는 해석하지 않는다(도면을 `-4 ~ -2 C1` 형으로 고치도록 오류 플래그).
    Returns (story_label, mark, floor_range, trailing_dim_mm).
    """
    s0 = _normalize_column_story_text(name_cell or "")
    if not s0:
        return None, None, None, None
    s, trail_dim = _strip_trailing_lone_dim_mm(s0)
    if not s:
        return None, None, None, trail_dim

    # "a ~ b" + 선택 부호 (끝층이 부호에 붙은 `-4 ~ -3C2A` 포함, ` - ` 구간 표기 허용)
    m = re.match(
        r"^\s*(?P<a>-?\d+)\s*(?:[~～]|(?:\s+-\s+))\s*(?P<b>-?\d+)(?:\s+(?P<mark1>.+)|(?P<mark2>[A-Za-z].*))?\s*$",
        s,
        re.I,
    )
    if m:
        a, b = m.group("a"), m.group("b")
        mark = (m.group("mark1") or m.group("mark2") or "").strip() or None
        return f"{a}~{b}", mark, [a, b], trail_dim

    # "~ b" + 부호 (앞쪽 층 생략·물결만 있는 범위 표기)
    m = re.match(
        r"^\s*[~～]\s*(?P<b>-?\d+)(?:\s+(?P<mark1>.+)|(?P<mark2>[A-Za-z].*))?\s*$",
        s,
        re.I,
    )
    if m:
        b = m.group("b")
        mark = (m.group("mark1") or m.group("mark2") or "").strip() or None
        return f"~{b}", mark, ["~", b], trail_dim

    # "-3C31", "2C5" : 앞의 정수·음수만 층, 바로 이어지는 영문 시작 부호(공백 없음)
    m = re.match(r"^\s*(-?\d+)([A-Za-z][A-Za-z0-9]*)\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None, trail_dim

    m = re.match(r"^\s*(-?\d+)\s+(.+?)\s*$", s)
    if m:
        return m.group(1).strip(), m.group(2).strip(), None, trail_dim

    return None, None, None, trail_dim


_RE_MEMBER_NAME_TILDE = re.compile(r"[~～∼〜]")


def _vertical_block_record_has_useful_member_data(rec: dict[str, Any]) -> bool:
    """
    부호·주근·단면 등이 없는 열(페이지 틈·타이틀블록 문자만 템플릿에 걸린 경우)은 행으로 남기지 않는다.
    """
    if (rec.get("mark") or "").strip():
        return True
    if (rec.get("member_label") or "").strip():
        return True
    main = (rec.get("MAIN_BAR") or "").strip()
    if main and re.search(r"(?:\d+\s*[-–]?\s*(?:U?HD|SHD)|\d\s*-\s*D|D\s*\d)", main, re.I):
        return True
    size = (rec.get("SIZE") or "").strip()
    if size and re.search(r"\d{2,4}\s*[xX×]\s*\d{2,4}", size):
        return True
    if rec.get("width_mm") is not None or rec.get("depth_mm") is not None:
        return True
    return False


def _apply_column_member_story_parse_error_flag(rec: dict[str, Any]) -> None:
    """
    NAME(부호 셀)에 층 범위(~)가 보이는데 층·floor_range 를 채우지 못한 경우.
    예: 도면이 `-4 ~ -2 C1` 인데 `-4C~-2C1` 처럼 공백이 없거나, `A~B` 처럼 숫자 층이 아닌 경우.
    """
    ml = (rec.get("member_label") or "").strip()
    if not ml or not _RE_MEMBER_NAME_TILDE.search(ml):
        return
    if (rec.get("story_label") or "").strip():
        return
    if rec.get("floor_range"):
        return
    st, _mk, frng, _td = _parse_column_name_story_and_mark(ml)
    if st is not None or frng is not None:
        return
    rec["story_parse_error"] = (
        "NAME에 층 범위(~)가 있으나 층을 해석하지 못했습니다. "
        "도면이 `-4 ~ -2 C1` 형식인지(층–층–부호 사이 공백) 확인하세요."
    )


def _enrich_vertical_block_record(rec: dict[str, Any]) -> None:
    """
    행 라벨(NAME·SIZE·MAIN BAR…)과 cells를 읽어 부재 1행용 필드(층·부호·B·H·띠장)를 채운다.
    """
    titles = rec.get("column_field_titles") or {}
    order = rec.get("column_field_key_order") or []
    cells = rec.get("cells") or []

    def lab_c(lab: str) -> str:
        return re.sub(r"[\s_\-]", "", (lab or ""))

    def cell_match(pred) -> str:
        for i, k in enumerate(order):
            if i >= len(cells):
                break
            lab = str(titles.get(k) or "")
            key_s = str(k or "")
            if pred(lab, key_s):
                return str(cells[i] or "").strip()
        return ""

    trail_from_name: int | None = None
    name_cell = cell_match(
        lambda lab, ks: bool(
            re.search(r"(?i)name", lab)
            or re.search(r"부\s*호", lab)
            or "부호" in lab_c(lab)
            or "name" in ks.lower(),
        ),
    )
    if name_cell:
        norm_nm = _normalize_column_story_text(name_cell)
        base_nm, trail_from_name = _strip_trailing_lone_dim_mm(norm_nm)
        rec["member_label"] = base_nm
        story, mk, frng, _td_nm = _parse_column_name_story_and_mark(name_cell)
        if story:
            rec["story_label"] = story
        if mk:
            rec["mark"] = mk
        elif not (rec.get("mark") or "").strip():
            rec["mark"] = base_nm
        if frng:
            rec["floor_range"] = frng

    size_cell = cell_match(
        lambda lab, ks: bool(
            re.search(r"(?i)size|치수|단면\s*치수", lab)
            or "size" in ks.lower()
            or "크기" in lab_c(lab)
        ),
    )
    if size_cell:
        rec.setdefault("SIZE", size_cell)
        xm = re.search(r"(\d+)\s*[xX×]\s*(\d+)", size_cell)
        if xm:
            w, h = int(xm.group(1)), int(xm.group(2))
            rec["width_mm"] = w
            rec["depth_mm"] = h
            rec["size_mm"] = [w, h]

    if trail_from_name is not None and not (rec.get("SIZE") or "").strip():
        rec.setdefault("SIZE", str(trail_from_name))

    strength_cell = cell_match(lambda lab, ks: _lab_is_concrete_strength_header(lab, ks))
    if strength_cell:
        rec["CONCRETE_STRENGTH"] = strength_cell

    main_cell = cell_match(
        lambda lab, ks: bool(
            "MAIN" in lab.upper()
            or "MAINBAR" in lab_c(lab).upper()
            or "주근" in lab_c(lab)
            or "main" in ks.lower(),
        ),
    )
    if main_cell:
        rec.setdefault("MAIN_BAR", main_cell)

    mid_cell = cell_match(
        lambda lab, ks: bool(
            "(MID)" in lab.upper()
            or "MID" in lab.upper()
            or "중앙" in lab
            or ("HOOP" in lab.upper() and "중앙" in lab)
        ),
    )
    if mid_cell:
        rec["center_hoop_bar"] = mid_cell

    tb_cell = cell_match(
        lambda lab, ks: bool(
            "T&B" in lab.upper()
            or "상하" in lab
            or "단부" in lab
            or ("HOOP" in lab.upper() and ("상" in lab or "하" in lab or "하단" in lab))
        ),
    )
    if tb_cell:
        rec["end_hoop_bar"] = tb_cell

    tie_cell = cell_match(
        lambda lab, ks: bool(
            re.search(r"(?i)tie\s*bar", lab)
            or ("TIE" in lab.upper() and "BAR" in lab.upper())
            or "tiebar" in lab_c(lab).lower(),
        ),
    )
    if tie_cell:
        rec["TIE_BAR"] = tie_cell

    if not (rec.get("mark") or "").strip():
        for i, _k in enumerate(order):
            if i >= len(cells):
                break
            v = str(cells[i] or "").strip()
            if _RE_COL_MARK_NAME.match(v):
                rec["mark"] = v
                rec.setdefault("member_label", v)
                break

    # 층 컬럼(story_label): NAME 파싱에서 story 가 없어도 floor_range 만 있으면 '-4~-3' 등으로 채움
    if not (rec.get("story_label") or "").strip():
        sl = _story_label_from_floor_range(rec.get("floor_range"))
        if sl:
            rec["story_label"] = sl

    _apply_concrete_strength_split_to_record(rec)
    _harvest_concrete_strength_from_non_main_cells(rec)
    _strip_mismatched_draw_refs_in_section_cells(rec)
    _apply_column_member_story_parse_error_flag(rec)


def _anchor_y_from_template_segment(
    template_segment: list[tuple[float, str, str]] | None,
) -> float | None:
    """SECTION 열·단면도 bbox 앵커용 — 템플릿 행의 평균 Y."""
    if not template_segment:
        return None
    ys = [float(t[0]) for t in template_segment]
    return sum(ys) / len(ys)


def _section_row_anchor_y_from_template(
    template_segment: list[tuple[float, str, str]] | None,
) -> float | None:
    """
    좌측 라벨 중 SECTION 칸의 참고 Y.
    단면도는 보통 이 줄에 맞춰 그려지므로, 평균 row_y_mean보다 세로 정렬에 적합하다.
    """
    if not template_segment:
        return None
    for _y, lab, k in template_segment:
        if header_row_is_section_geometry_anchor(lab, k):
            try:
                return float(_y)
            except (TypeError, ValueError):
                continue
    return None


def _record_from_vertical_dynamic(
    dyn: dict[str, str],
    template: list[tuple[float, str, str]],
    template_source: str,
    cfg: ExtractionConfig,
) -> dict[str, Any]:
    titles = {k: lab for _y, lab, k in template}
    key_order = [k for _y, _lab, k in template]
    # 좌측 행 헤더 순서와 열 개수를 부재마다 동일하게 유지(빈 칸 포함)
    flat_vals = [str(dyn.get(k) or "").strip() for k in key_order]
    rec: dict[str, Any] = {
        "category": CATEGORY_COLUMN,
        "wall_mode": cfg.wall_mode,
        "column_layout": "vertical_blocks",
        "column_row_role": "column_vertical_block",
        "column_template_source": template_source,
        "column_field_titles": titles,
        "column_field_key_order": key_order,
        "cells": flat_vals,
    }
    if cfg.building_tag:
        rec["building"] = cfg.building_tag
    for k, v in dyn.items():
        if v:
            rec[k] = v
    sig = _merge_signals([c for c in flat_vals if c])
    for k, v in sig.items():
        if k == "cells":
            continue
        if k not in rec:
            rec[k] = v
    _enrich_vertical_block_record(rec)
    ay = _anchor_y_from_template_segment(template)
    if ay is not None:
        rec["row_y_mean"] = round(ay, 4)
    sec_anchor = _section_row_anchor_y_from_template(template)
    if sec_anchor is not None:
        rec["row_section_anchor_y"] = round(sec_anchor, 4)
    return rec


_LEGACY_VB_KEYS = (
    "mark",
    "SECTION",
    "SIZE",
    "CONCRETE_STRENGTH",
    "MAIN_BAR",
    "(MID) HOOP BAR",
    "(T&B) HOOP BAR",
)
_LEGACY_VB_TITLES_KO = {
    "mark": "부호/NAME",
    "SECTION": "SECTION",
    "SIZE": "SIZE",
    "CONCRETE_STRENGTH": "강도(fck 등)",
    "MAIN_BAR": "MAIN_BAR",
    "(MID) HOOP BAR": "(MID) HOOP BAR",
    "(T&B) HOOP BAR": "(T&B) HOOP BAR",
}


def _record_from_vertical_legacy(
    fields: dict[str, str],
    cfg: ExtractionConfig,
) -> dict[str, Any]:
    key_order = list(_LEGACY_VB_KEYS)
    flat = [str(fields.get(k) or "").strip() for k in key_order]
    titles = {k: _LEGACY_VB_TITLES_KO.get(k, k) for k in key_order}
    rec: dict[str, Any] = {
        "category": CATEGORY_COLUMN,
        "wall_mode": cfg.wall_mode,
        "column_layout": "vertical_blocks",
        "column_row_role": "column_vertical_block",
        "column_template_source": "regex_fallback",
        "column_field_titles": titles,
        "column_field_key_order": key_order,
        "mark": fields.get("mark") or "",
        "SECTION": fields.get("SECTION") or "",
        "SIZE": fields.get("SIZE") or "",
        "CONCRETE_STRENGTH": fields.get("CONCRETE_STRENGTH") or "",
        "MAIN_BAR": fields.get("MAIN_BAR") or "",
        "(MID) HOOP BAR": fields.get("(MID) HOOP BAR") or "",
        "(T&B) HOOP BAR": fields.get("(T&B) HOOP BAR") or "",
        "cells": flat,
    }
    if cfg.building_tag:
        rec["building"] = cfg.building_tag
    sig = _merge_signals([c for c in flat if c])
    for k, v in sig.items():
        if k == "cells":
            continue
        if k not in rec:
            rec[k] = v
    _enrich_vertical_block_record(rec)
    return rec


def extract_column_vertical_blocks(
    items: list[dict[str, Any]],
    cfg: ExtractionConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    세로 블록형 기둥 일람:
    - **좌측** 열: 페이지(선택 영역) 안에서 반복되는 행 명칭 → 표 헤더(필드 제목) 템플릿.
    - **우측** 영역: 부재마다 한 세로 칸씩 값만 모아 Y로 템플릿 행에 맞춘다.
    좌·우는 X 간격(큰 갭) 또는 좌측 비율로 나눈다. 우측 텍스트는 라벨 열에 쓰이지 않는다.
    """
    meta: dict[str, Any] = {
        "column_vertical_split_mode": "none",
        "column_label_entity_count": 0,
        "column_data_entity_count": 0,
        "column_vertical_split_boundary": None,
    }
    if not items:
        return [], meta

    band_tol = float(cfg.column_band_y_tol or 4.0)
    gap = float(cfg.column_strip_gap) if cfg.column_strip_gap is not None else None

    label_items, data_items, split_mode, boundary = _split_column_label_and_data(items)
    label_items, data_items = _demote_misassigned_label_entities(label_items, data_items)
    meta["column_vertical_split_mode"] = split_mode
    meta["column_label_entity_count"] = len(label_items)
    meta["column_vertical_split_boundary"] = boundary

    if len(data_items) == 0 and len(items) > 0:
        data_items = list(items)
        split_mode = split_mode + "|data_fallback_all"
        meta["column_vertical_split_mode"] = split_mode
        meta["column_vertical_data_fallback"] = True

    meta["column_data_entity_count"] = len(data_items)

    template, tsrc = _discover_row_template_from_labels(label_items, band_tol, split_mode)
    if len(template) < 2 and label_items:
        template, tsrc = _discover_row_template_from_labels(
            label_items, band_tol * 1.25, split_mode + "_retry"
        )

    rows_out: list[dict[str, Any]] = []
    if gap is not None:
        strips = _cluster_items_by_x_gap(data_items, gap)
        meta["column_strip_cluster_mode"] = "manual_gap"
    else:
        strips = _partition_data_items_into_member_strips(data_items)
        meta["column_strip_cluster_mode"] = "auto_seed_partition"
    strips, strip_merge_log = _merge_narrow_adjacent_strips(strips)
    if strip_merge_log:
        meta["column_vertical_strip_merges"] = strip_merge_log
    strip_infos: list[dict[str, Any]] = []
    for si, strip in enumerate(strips):
        if not strip:
            continue
        xs = [float(it["x"]) for it in strip]
        xc = sum(xs) / len(xs)
        strip_infos.append(
            {
                "index": si,
                "x_center": round(xc, 4),
                "entity_count": len(strip),
            },
        )
    meta["column_vertical_strips"] = strip_infos
    if template:
        meta["column_vertical_field_headers"] = [
            {"key": k, "label": lab, "y": round(float(y), 4)} for y, lab, k in template
        ]
        meta["column_vertical_template_segment_count"] = len(
            _segment_template_rows(template, band_tol),
        )
    else:
        meta["column_vertical_field_headers"] = []
        meta["column_vertical_template_segment_count"] = 0

    for si, strip in enumerate(strips):
        if not strip:
            continue
        bands = _merge_strip_into_y_bands(strip, band_tol)
        texts = [b[1] for b in bands if b[1]]

        if len(template) >= 2:
            dyn = _map_data_bands_to_template(bands, template, band_tol)
            if not any(dyn.values()):
                continue
            splits = _split_dyn_by_template_segments(dyn, template, band_tol)
            if splits:
                for seg_i, (sub_dyn, sub_t, seg_indices) in enumerate(splits):
                    rec = _record_from_vertical_dynamic(sub_dyn, sub_t, tsrc, cfg)
                    if not _vertical_block_record_has_useful_member_data(rec):
                        continue
                    rec["column_strip_index"] = si
                    rec["column_segment_index"] = seg_i
                    y_lo, y_hi = _segment_y_bounds_world(template, seg_indices, band_tol)
                    rda = _row_data_anchor_y_from_strip_relaxed(strip, y_lo, y_hi, band_tol)
                    if rda is not None:
                        rec["row_data_anchor_y"] = rda
                    rec["row_data_anchor_y_bounds"] = [round(y_lo, 4), round(y_hi, 4)]
                    rows_out.append(rec)
            else:
                rec = _record_from_vertical_dynamic(dyn, template, tsrc, cfg)
                if not _vertical_block_record_has_useful_member_data(rec):
                    continue
                rec["column_strip_index"] = si
                rec["column_segment_index"] = 0
                ys_strip = [float(it["y"]) for it in strip if it.get("y") is not None]
                if ys_strip:
                    rec["row_data_anchor_y"] = round(_median_float(ys_strip), 4)
                rows_out.append(rec)
            continue

        fields = _fallback_fill_by_regex(texts)
        if any(fields.values()):
            rec = _record_from_vertical_legacy(fields, cfg)
            if not _vertical_block_record_has_useful_member_data(rec):
                continue
            rec["column_strip_index"] = si
            rec["column_segment_index"] = 0
            yms = [float(ym) for ym, txt in bands if (txt or "").strip()]
            if yms:
                rec["row_y_mean"] = round(sum(yms) / len(yms), 4)
            rows_out.append(rec)

    return rows_out, meta


def parse_text_signals(text: str) -> dict[str, Any]:
    """단일 문자열에서 일람마스터 문서의 토큰 신호 추출."""
    s = text.strip()
    out: dict[str, Any] = {"raw": s}
    m = re.search(r"(\d+)\s*-\s*(?:U?HD|SHD|D)\s*(\d+)", s, re.I)
    if m:
        out["main_bar_bars"] = int(m.group(1))
        out["rebar_d"] = int(m.group(2))
    else:
        m = _RE_D.search(s)
        if m:
            out["rebar_d"] = int(m.group(1))
    if "rebar_d" not in out:
        m_sd = re.search(r"(?:SHD|UHD|UH)\s*(\d+)", s, re.I)
        if m_sd:
            out["rebar_d"] = int(m_sd.group(1))
    m = _RE_AT.search(s)
    if m:
        out["spacing"] = int(m.group(1))
    m = _RE_X.search(s)
    if m:
        out["size_mm"] = [int(m.group(1)), int(m.group(2))]
    m = _RE_THICK_PAREN.search(s)
    if m:
        out["thickness_pair"] = [int(m.group(1)), int(m.group(2))]
    m = _RE_THICK_SLASH.search(s)
    if m and "thickness_pair" not in out:
        out["thickness_pair"] = [int(m.group(1)), int(m.group(2))]
    fr = _RE_FLOOR_RANGE.search(s)
    if fr:
        out["floor_range"] = [fr.group("a"), fr.group("b")]
    else:
        _, _, frng, _td = _parse_column_name_story_and_mark(s)
        if frng:
            out["floor_range"] = frng
        else:
            floors = _RE_FLOOR_TOKEN.findall(s)
            if floors:
                out["floor_tokens"] = floors
    return out


def _row_to_cells(row: list[dict[str, Any]]) -> list[str]:
    return [r["text"] for r in row]


def _merge_signals(cells: list[str]) -> dict[str, Any]:
    merged: dict[str, Any] = {}
    for c in cells:
        sig = parse_text_signals(c)
        for k, v in sig.items():
            if k == "raw":
                continue
            merged[k] = v
    merged["cells"] = cells
    return merged


def _slab_xy_variant(cells: list[str]) -> str:
    """슬라브 X/Y 열 순서 휴리스틱."""
    joined = " ".join(cells)
    has_x = bool(_RE_X.search(joined))
    if not has_x:
        return "unknown"
    upper = joined.upper()
    x_pos = upper.find("X")
    if x_pos < 0:
        return "unknown"
    before = joined[: x_pos + 1]
    after = joined[x_pos + 1 :]
    if "Y" in after.upper() or "Y" in before:
        return "mixed_or_labeled"
    return "inline"


def _detect_duplicates(rows_out: list[dict[str, Any]], key_fn) -> list[int]:
    seen: dict[Any, int] = {}
    dup_idx: list[int] = []
    for i, r in enumerate(rows_out):
        k = key_fn(r)
        if k is None:
            continue
        if k in seen:
            dup_idx.append(i)
        else:
            seen[k] = i
    return dup_idx


def _beam_flat_zone_wh_mm(r: dict[str, Any], z: dict[str, Any]) -> tuple[Any, Any]:
    sg = z.get("section_geometry") if isinstance(z.get("section_geometry"), dict) else {}
    w = sg.get("width_mm")
    d = sg.get("depth_mm")
    if w is None:
        w = r.get("width_mm")
    if d is None:
        d = r.get("depth_mm")
    return w, d


def build_beam_flat_member_table_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """flat 단면·존·하단텍스트만으로 부재별 표 1행 후보를 만든다(슬랩/존이 여러 개면 행을 나눔)."""
    out: list[dict[str, Any]] = []
    for ri, r in enumerate(rows):
        zones = r.get("beam_section_geometry_zones")
        if isinstance(zones, list) and len(zones) > 1:
            for zi, z in enumerate(zones):
                if not isinstance(z, dict):
                    continue
                w, d = _beam_flat_zone_wh_mm(r, z)
                slab_idx = z.get("slab_index")
                if slab_idx is None:
                    slab_idx = zi
                mark = str(z.get("member_mark_text") or r.get("mark") or r.get("name") or "").strip()
                zone_t = str(z.get("member_zone_text") or r.get("beam_vertical_zone_display_label") or "").strip()
                btr = z.get("below_text_role_values") if isinstance(z.get("below_text_role_values"), dict) else {}
                out.append(
                    {
                        "source_row_index": ri,
                        "slab_index": int(slab_idx) if isinstance(slab_idx, (int, float)) else slab_idx,
                        "member_mark_text": mark,
                        "member_zone_text": zone_t,
                        "member_mark_dims": z.get("member_mark_dims"),
                        "below_text_role_values": dict(btr),
                        "below_text_chain_values": list(z.get("below_text_chain_values") or [])
                        if isinstance(z.get("below_text_chain_values"), list)
                        else [],
                        "width_mm": w,
                        "depth_mm": d,
                        "strip_x": z.get("x_center"),
                        "section_geometry": z.get("section_geometry"),
                    }
                )
            continue
        if isinstance(zones, list) and len(zones) == 1 and isinstance(zones[0], dict):
            z = zones[0]
            w, d = _beam_flat_zone_wh_mm(r, z)
            mark = str(z.get("member_mark_text") or r.get("mark") or "").strip()
            zone_t = str(z.get("member_zone_text") or r.get("beam_vertical_zone_display_label") or "").strip()
            btr = z.get("below_text_role_values") if isinstance(z.get("below_text_role_values"), dict) else {}
            out.append(
                {
                    "source_row_index": ri,
                    "slab_index": z.get("slab_index", 0),
                    "member_mark_text": mark,
                    "member_zone_text": zone_t,
                    "member_mark_dims": z.get("member_mark_dims"),
                    "below_text_role_values": dict(btr),
                    "below_text_chain_values": list(z.get("below_text_chain_values") or [])
                    if isinstance(z.get("below_text_chain_values"), list)
                    else [],
                    "width_mm": w,
                    "depth_mm": d,
                    "strip_x": z.get("x_center"),
                    "section_geometry": z.get("section_geometry"),
                }
            )
            continue
        w, d = r.get("width_mm"), r.get("depth_mm")
        btr = r.get("below_text_role_values") if isinstance(r.get("below_text_role_values"), dict) else {}
        out.append(
            {
                "source_row_index": ri,
                "slab_index": 0,
                "member_mark_text": str(r.get("mark") or r.get("name") or "").strip(),
                "member_zone_text": str(r.get("member_zone_text") or r.get("beam_vertical_zone_display_label") or "").strip(),
                "member_mark_dims": r.get("member_mark_dims"),
                "below_text_role_values": dict(btr),
                "below_text_chain_values": list(r.get("below_text_chain_values") or [])
                if isinstance(r.get("below_text_chain_values"), list)
                else [],
                "width_mm": w,
                "depth_mm": d,
                "strip_x": r.get("_beam_row_cluster_centroid_x"),
                "section_geometry": r.get("section_geometry"),
            }
        )
    return out


def build_beam_flat_member_zone_match_lines(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """
    서버 flat 단일 경로에서 부재→부위→단면(중심) 연결선을 프론트에 승격해 내려준다.
    points: [(member_x,y), (zone_x,y), (section_center_x,y)]
    """
    out: list[dict[str, Any]] = []
    for ri, r in enumerate(rows or []):
        zones = r.get("beam_section_geometry_zones")
        blocks: list[dict[str, Any]] = (
            [z for z in zones if isinstance(z, dict)] if isinstance(zones, list) and zones else []
        )
        if not blocks:
            sg = r.get("section_geometry") if isinstance(r.get("section_geometry"), dict) else {}
            blocks = [{"slab_index": 0, "section_geometry": sg}]
        for zi, z in enumerate(blocks):
            mm = z.get("member_mark_anchor") if isinstance(z.get("member_mark_anchor"), dict) else None
            mz = z.get("member_zone_anchor") if isinstance(z.get("member_zone_anchor"), dict) else None
            sc = z.get("section_center") if isinstance(z.get("section_center"), dict) else None
            if not (mm and mz and sc):
                continue
            try:
                pts = [
                    [round(float(mm["x"]), 4), round(float(mm["y"]), 4)],
                    [round(float(mz["x"]), 4), round(float(mz["y"]), 4)],
                    [round(float(sc["x"]), 4), round(float(sc["y"]), 4)],
                ]
            except (TypeError, ValueError, KeyError):
                continue
            slab_idx = z.get("slab_index")
            if slab_idx is None:
                slab_idx = zi
            out.append(
                {
                    "row_index": ri,
                    "slab_index": slab_idx,
                    "member_mark_text": str(z.get("member_mark_text") or r.get("mark") or r.get("name") or "").strip(),
                    "member_zone_text": str(z.get("member_zone_text") or "").strip(),
                    "points": pts,
                }
            )
    return out


def extract_schedule(
    db: Session,
    commit_id: int,
    category: str,
    config: dict[str, Any] | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    일람 행 목록과 validation 요약 반환.
    category: wall|beam|column|slab
    """
    if category not in SCHEDULE_CATEGORIES:
        raise ValueError(f"Unknown category: {category}")

    raw = config or {}
    sb = raw.get("selection_bbox")
    selection_bbox: tuple[float, float, float, float] | None = None
    if isinstance(sb, (list, tuple)) and len(sb) >= 4:
        try:
            selection_bbox = _normalize_bbox(
                float(sb[0]),
                float(sb[1]),
                float(sb[2]),
                float(sb[3]),
            )
        except (TypeError, ValueError):
            selection_bbox = None

    selection_bboxes = _parse_selection_bboxes(raw)

    csg = raw.get("column_strip_gap")
    column_strip_gap: float | None
    try:
        column_strip_gap = float(csg) if csg is not None and csg != "" else None
    except (TypeError, ValueError):
        column_strip_gap = None

    beam_band_raw = raw.get("beam_band_y_tol")
    try:
        beam_band_y_tol = float(beam_band_raw) if beam_band_raw is not None and beam_band_raw != "" else None
    except (TypeError, ValueError):
        beam_band_y_tol = None

    brmm_raw = raw.get("beam_row_merge_y_max")
    try:
        beam_row_merge_y_max = float(brmm_raw) if brmm_raw is not None and brmm_raw != "" else None
    except (TypeError, ValueError):
        beam_row_merge_y_max = None

    def _optional_positive_int(v: Any) -> int | None:
        if v is None or v == "":
            return None
        try:
            n = int(v)
            return n if n > 0 else None
        except (TypeError, ValueError):
            return None

    beam_row_merge_max_glue = _optional_positive_int(raw.get("beam_row_merge_max_glue"))
    beam_row_merge_max_merged = _optional_positive_int(raw.get("beam_row_merge_max_merged"))
    beam_row_bridge_max_endpoint = _optional_positive_int(raw.get("beam_row_bridge_max_endpoint"))
    beam_row_bridge_max_mid = _optional_positive_int(raw.get("beam_row_bridge_max_mid"))
    beam_row_bridge_passes = _optional_positive_int(raw.get("beam_row_bridge_passes"))

    inc_empty = category == CATEGORY_BEAM
    if isinstance(raw, dict) and raw.get("include_empty_text_entities") is False:
        inc_empty = False

    cfg = ExtractionConfig(
        rules_version=str(raw.get("rules_version") or "1.0"),
        layer_include=list(raw.get("layer_include") or []),
        layer_exclude=list(raw.get("layer_exclude") or []),
        y_tolerance=float(raw.get("y_tolerance") or 2.5),
        min_row_texts=int(raw.get("min_row_texts") or 1),
        bbox_wkt=raw.get("bbox_wkt"),
        selection_bbox=selection_bbox,
        selection_bboxes=selection_bboxes,
        wall_mode=str(raw.get("wall_mode") or "cad"),
        building_tag=raw.get("building_tag"),
        slab_layout=str(raw.get("slab_layout") or "auto"),
        column_layout=str(raw.get("column_layout") or "horizontal_table").strip().lower(),
        column_strip_gap=column_strip_gap,
        column_band_y_tol=float(raw.get("column_band_y_tol") or 4.0),
        beam_layout=str(raw.get("beam_layout") or "auto").strip().lower(),
        beam_band_y_tol=beam_band_y_tol,
        include_empty_text_entities=inc_empty,
        beam_row_merge_y_max=beam_row_merge_y_max,
        beam_row_merge_max_glue=beam_row_merge_max_glue,
        beam_row_merge_max_merged=beam_row_merge_max_merged,
        beam_row_bridge_max_endpoint=beam_row_bridge_max_endpoint,
        beam_row_bridge_max_mid=beam_row_bridge_max_mid,
        beam_row_bridge_passes=beam_row_bridge_passes,
    )

    items = load_text_entities(db, commit_id, cfg)

    if category == CATEGORY_BEAM:
        from app.services.beam_extraction import beam_duplicate_key, process_beam_extraction

        rows_out, beam_validation = process_beam_extraction(cfg, items)
        validation: dict[str, Any] = {
            "text_entity_count": len(items),
            "row_count": len(rows_out),
            "category": category,
            "selection_bbox": list(selection_bbox) if selection_bbox else None,
            "selection_bboxes": [list(b) for b in selection_bboxes] if selection_bboxes else None,
        }
        # 디버그/배포 확인용 태그(프론트에서 표시). 이 값이 보이지 않으면 다른 서버/옛 코드에 요청 중.
        validation["_illam_server_tag"] = "beam_flat_cad_outline_first_v3"
        validation.update(beam_validation)
        # --- 보 가로묶음(Y행 클러스터) 디버그: 추출 로직과 무관하게 항상 요약 제공 ---
        # (프론트는 run.validation.beam_row_clusters + beam_row_cluster_merge 를 그대로 표시/복사)
        try:
            rows_raw = cluster_rows(items, cfg.y_tolerance)
            gap_used = (
                float(cfg.beam_row_merge_y_max)
                if cfg.beam_row_merge_y_max is not None and float(cfg.beam_row_merge_y_max) > 0
                else max(float(cfg.y_tolerance) * 3.5, 10.0)
            )
            glue_n = int(cfg.beam_row_merge_max_glue) if cfg.beam_row_merge_max_glue is not None else 4
            if glue_n < 1:
                glue_n = 4
            cap_n = int(cfg.beam_row_merge_max_merged) if cfg.beam_row_merge_max_merged is not None else 512
            if cap_n < 8:
                cap_n = 512
            br_ep = (
                int(cfg.beam_row_bridge_max_endpoint)
                if cfg.beam_row_bridge_max_endpoint is not None
                else 4
            )
            if br_ep < 2:
                br_ep = 2
            br_mid = int(cfg.beam_row_bridge_max_mid) if cfg.beam_row_bridge_max_mid is not None else 1
            if br_mid < 0:
                br_mid = 0
            br_pass = (
                int(cfg.beam_row_bridge_passes) if cfg.beam_row_bridge_passes is not None else 1
            )
            if br_pass < 1:
                br_pass = 1

            rows_sparse = merge_beam_sparse_row_clusters(
                rows_raw,
                cfg.y_tolerance,
                y_gap_max=gap_used,
                max_glue_row_items=glue_n,
                max_merged_row_items=cap_n,
            )
            # mark-like(부호명)끼리는 Y가 더 흔들릴 수 있음(예: RG2 vs RG2C)
            mark_gap = (
                float(raw.get("beam_row_merge_y_max_mark"))
                if isinstance(raw, dict) and raw.get("beam_row_merge_y_max_mark") not in (None, "")
                else max(float(gap_used), 80.0)
            )
            rows_dbg = bridge_beam_sparse_row_clusters(
                rows_sparse,
                float(gap_used),
                max_endpoint_items=br_ep,
                max_intermediate_row_items=br_mid,
                max_merged_row_items=cap_n,
                max_bridge_passes=br_pass,
                y_gap_for_mark_endpoints=mark_gap,
            )
            validation["row_cluster_y_tolerance"] = cfg.y_tolerance
            validation["beam_row_clusters"] = beam_row_clusters_for_validation(rows_dbg)
            validation["beam_row_cluster_merge"] = {
                "before_row_count": len(rows_raw),
                "after_sparse_merge_row_count": len(rows_sparse),
                "after_bridge_row_count": len(rows_dbg),
                "y_gap_max_used": round(float(gap_used), 4),
                "y_gap_max_mark_used": round(float(mark_gap), 4) if mark_gap is not None else None,
                "max_glue_row_items": int(glue_n),
                "max_merged_row_items": int(cap_n),
                "bridge_max_endpoint_items": int(br_ep),
                "bridge_max_mid_row_items": int(br_mid),
                "bridge_passes": int(br_pass),
            }
        except Exception as ex:
            validation["beam_row_cluster_merge_error"] = f"{type(ex).__name__}: {ex}"[:300]
        if (selection_bboxes or selection_bbox) and len(items) == 0:
            validation["warning"] = (
                "선택 영역 안에 TEXT/MTEXT/ATTRIB 삽입점이 없습니다. 영역을 넓히거나 도면을 확인하세요."
            )
        bv = beam_validation or {}
        _st = bv.get("beam_vertical_strips")
        _fh = bv.get("beam_vertical_field_headers")
        strips_ok = isinstance(_st, list) and len(_st) > 0
        headers_ok = isinstance(_fh, list) and len(_fh) > 0
        layout_res = str(bv.get("beam_layout_resolved") or "").strip().lower()
        csg_on = raw.get("column_section_geometry", True) is not False
        _do_beam_flat_geo = csg_on and bool(rows_out)
        clip_bbs: list[tuple[float, float, float, float]] = list(selection_bboxes)
        if selection_bbox is not None:
            clip_bbs.append(selection_bbox)
        clip_arg = clip_bbs if clip_bbs else None
        validation["beam_section_geo_debug"] = {
            "layout_res": layout_res,
            "csg_on": csg_on,
            "do_vertical_zones": False,
            "do_flat_geo": _do_beam_flat_geo,
            "strips_ok": strips_ok,
            "headers_ok": headers_ok,
            "note": "beam uses flat section pipeline only",
        }
        validation["beam_flat_section_geometry"] = False
        if _do_beam_flat_geo:
            validation["beam_section_geo_branch"] = "flat"
            try:
                enrich_rows_beam_flat_section_geometry(
                    db,
                    commit_id,
                    rows_out,
                    half_width=_optional_positive_float(raw, "column_section_half_width"),
                    half_height=_optional_positive_float(raw, "column_section_half_height"),
                    include_block_definitions=raw.get("column_section_block_geometry", True) is not False,
                    selection_world_bboxes=clip_arg,
                )
                validation["beam_flat_section_geometry"] = True
            except Exception as ex:
                validation["section_geometry_error"] = f"{type(ex).__name__}: {ex}"[:400]
        else:
            validation["beam_section_geo_branch"] = "none"

        try:
            validation["beam_flat_member_table_rows"] = build_beam_flat_member_table_rows(rows_out)
        except Exception as ex:
            validation["beam_flat_member_table_error"] = f"{type(ex).__name__}: {ex}"[:300]
        try:
            validation["beam_flat_member_zone_match_lines"] = build_beam_flat_member_zone_match_lines(rows_out)
        except Exception as ex:
            validation["beam_flat_member_zone_match_error"] = f"{type(ex).__name__}: {ex}"[:300]

        validation["duplicate_row_indices"] = _detect_duplicates(rows_out, beam_duplicate_key)
        return rows_out, validation

    if category == CATEGORY_COLUMN and cfg.column_layout == "vertical_blocks":
        rows_out, vb_meta = extract_column_vertical_blocks(items, cfg)
        column_pivot_applied = False
        validation: dict[str, Any] = {
            "text_entity_count": len(items),
            "row_count": len(rows_out),
            "category": category,
            "selection_bbox": list(selection_bbox) if selection_bbox else None,
            "selection_bboxes": [list(b) for b in selection_bboxes] if selection_bboxes else None,
            "column_layout": "vertical_blocks",
            "column_schedule_pivot": False,
            "column_vertical_blocks": True,
            "column_vertical_split_mode": vb_meta.get("column_vertical_split_mode"),
            "column_label_entity_count": vb_meta.get("column_label_entity_count"),
            "column_data_entity_count": vb_meta.get("column_data_entity_count"),
            "column_vertical_split_boundary": vb_meta.get("column_vertical_split_boundary"),
        }
        if vb_meta.get("column_vertical_data_fallback"):
            validation["column_vertical_data_fallback"] = True
        if vb_meta.get("column_vertical_strips") is not None:
            validation["column_vertical_strips"] = vb_meta["column_vertical_strips"]
        if vb_meta.get("column_vertical_field_headers") is not None:
            validation["column_vertical_field_headers"] = vb_meta["column_vertical_field_headers"]
        if vb_meta.get("column_vertical_template_segment_count") is not None:
            validation["column_vertical_template_segment_count"] = vb_meta[
                "column_vertical_template_segment_count"
            ]
        if vb_meta.get("column_strip_cluster_mode") is not None:
            validation["column_strip_cluster_mode"] = vb_meta["column_strip_cluster_mode"]
        if (selection_bboxes or selection_bbox) and len(items) == 0:
            validation["warning"] = (
                "선택 영역 안에 TEXT/MTEXT/ATTRIB 삽입점이 없습니다. 영역을 넓히거나 도면을 확인하세요."
            )

        if raw.get("column_section_geometry", True) is not False:
            try:
                clip_bbs: list[tuple[float, float, float, float]] = list(selection_bboxes)
                if selection_bbox is not None:
                    clip_bbs.append(selection_bbox)
                enrich_rows_column_section_geometry(
                    db,
                    commit_id,
                    rows_out,
                    vb_meta.get("column_vertical_field_headers"),
                    vb_meta.get("column_vertical_strips"),
                    half_width=_optional_positive_float(raw, "column_section_half_width"),
                    half_height=_optional_positive_float(raw, "column_section_half_height"),
                    include_block_definitions=raw.get("column_section_block_geometry", True) is not False,
                    selection_world_bboxes=clip_bbs if clip_bbs else None,
                )
            except Exception as ex:
                validation["section_geometry_error"] = f"{type(ex).__name__}: {ex}"[:400]

        def _key_v(r: dict[str, Any]):
            return (
                tuple(r.get("size_mm") or ()),
                r.get("rebar_d"),
                r.get("spacing"),
                " ".join(r.get("cells") or [])[:120],
            )

        validation["duplicate_row_indices"] = _detect_duplicates(rows_out, _key_v)
        story_parse_errors: list[dict[str, Any]] = []
        for i, r in enumerate(rows_out):
            msg = (r.get("story_parse_error") or "").strip()
            if msg:
                story_parse_errors.append(
                    {
                        "row_index": i,
                        "member_label": r.get("member_label"),
                        "mark": r.get("mark"),
                        "message": msg,
                    }
                )
        if story_parse_errors:
            validation["column_story_parse_errors"] = story_parse_errors
        return rows_out, validation

    rows_cluster = cluster_rows(items, cfg.y_tolerance)

    rows_out: list[dict[str, Any]] = []
    for row in rows_cluster:
        if len(row) < cfg.min_row_texts:
            continue
        cells = _row_to_cells(row)
        merged = _merge_signals(cells)
        merged["category"] = category
        merged["wall_mode"] = cfg.wall_mode
        if cfg.building_tag:
            merged["building"] = cfg.building_tag
        if category == CATEGORY_COLUMN:
            merged["column_row_role"] = classify_column_row_role(row)
            ys = [float(r["y"]) for r in row]
            merged["row_y_mean"] = sum(ys) / len(ys)
            merged["column_row_label"] = (
                classify_column_schedule_label(cells[0]) if cells else "unknown"
            )
        if category == CATEGORY_SLAB:
            merged["slab_layout_guess"] = (
                cfg.slab_layout if cfg.slab_layout != "auto" else _slab_xy_variant(cells)
            )
        rows_out.append(merged)

    column_pivot_applied = False
    if category == CATEGORY_COLUMN and rows_out:
        pivoted = pivot_column_schedule(
            rows_out,
            building_tag=cfg.building_tag,
            wall_mode=cfg.wall_mode,
        )
        if pivoted:
            rows_out = pivoted
            column_pivot_applied = True

    validation: dict[str, Any] = {
        "text_entity_count": len(items),
        "row_count": len(rows_out),
        "category": category,
        "selection_bbox": list(selection_bbox) if selection_bbox else None,
        "selection_bboxes": [list(b) for b in selection_bboxes] if selection_bboxes else None,
    }
    if category == CATEGORY_COLUMN:
        validation["column_schedule_pivot"] = column_pivot_applied
        validation["column_layout"] = cfg.column_layout
    if (selection_bboxes or selection_bbox) and len(items) == 0:
        validation["warning"] = (
            "선택 영역 안에 TEXT/MTEXT/ATTRIB 삽입점이 없습니다. 영역을 넓히거나 도면을 확인하세요."
        )

    if category == CATEGORY_COLUMN:

        def _key(r: dict[str, Any]):
            return (
                tuple(r.get("size_mm") or ()),
                r.get("rebar_d"),
                r.get("spacing"),
                " ".join(r.get("cells") or [])[:120],
            )

        validation["duplicate_row_indices"] = _detect_duplicates(rows_out, _key)
    elif category == CATEGORY_WALL:
        validation["duplicate_row_indices"] = _detect_duplicates(
            rows_out,
            lambda r: (" ".join(r.get("cells") or []))[:200],
        )
    elif category == CATEGORY_SLAB:
        validation["duplicate_row_indices"] = _detect_duplicates(
            rows_out,
            lambda r: tuple(r.get("cells") or ()),
        )

    return rows_out, validation


def load_near_geometry(
    db: Session,
    commit_id: int,
    center_xy: tuple[float, float],
    radius: float,
    types: tuple[str, ...] = ("CIRCLE", "ARC", "LWPOLYLINE", "POLYLINE", "LINE"),
) -> list[dict[str, Any]]:
    """
    (2단계) 보/기둥 단면 근접 기하 힌트: 중심 기준 반경 내 단순 도형 요약.
    """
    cx, cy = center_xy
    hints: list[dict[str, Any]] = []
    for ent in (
        db.query(Entity)
        .filter(Entity.commit_id == commit_id, Entity.entity_type.in_(types))
        .all()
    ):
        xy = _entity_xy(ent)
        if xy is None:
            continue
        if math.hypot(xy[0] - cx, xy[1] - cy) > radius:
            continue
        hints.append({"type": ent.entity_type, "layer": ent.layer, "near_x": xy[0], "near_y": xy[1]})
    return hints
