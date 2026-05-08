"""
보(RC beam) 일람 추출: Y행 클러스터(가로 띠)에서 와이드·Location 표를 우선 읽고,
실패 시에만 세로 블록(좌측 라벨+우측 스트립)으로 폴백한다.
"""
from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from typing import Any

from app.services.column_section_geometry import (
    _field_headers_y_median_gap,
    header_row_is_section_geometry_anchor,
)
from app.services.beam_flat_spatial import RE_FLAT_BEAM_MARK, beam_flat_section_focus_y_bounds

from app.services.schedule_extraction import (
    CATEGORY_BEAM,
    ExtractionConfig,
    beam_row_clusters_for_validation,
    _cluster_items_by_x_gap,
    _demote_beam_zone_column_headers_from_labels,
    _demote_misassigned_label_entities,
    _discover_row_template_from_labels,
    _map_data_bands_to_template,
    _median_float,
    _merge_narrow_adjacent_strips,
    _merge_strip_into_y_bands,
    _merge_signals,
    _partition_data_items_into_member_strips,
    _pick_template_segment_for_y,
    _row_data_anchor_y_from_strip_relaxed,
    _segment_template_rows,
    _segment_y_bounds_world,
    _template_segment_y_intervals,
    _slug_field_key,
    _split_column_label_and_data,
    _split_dyn_by_template_segments,
    cluster_rows,
    bridge_beam_sparse_row_clusters,
    merge_beam_sparse_row_clusters,
    parse_text_signals,
    _row_has_beam_mark_like,
    _row_is_beam_header_like,
    _row_nonempty_texts,
)

# --- 헤더 → 내부 키 (JSON/엑셀 공통) ---
BEAM_WIDE_KEYS_ORDER = [
    "name",
    "type",
    "material",
    "width_mm",
    "depth_mm",
    "int_top_bar",
    "cen_top_bar",
    "ext_top_bar",
    "int_bot_bar",
    "cen_bot_bar",
    "ext_bot_bar",
    "int_stirrup_bar",
    "cen_stirrup_bar",
    "ext_stirrup_bar",
    "side_bar",
]

_BEAM_KO_HEADERS: dict[str, tuple[str, ...]] = {
    "name": ("name", "부호", "기호", "부재명", "보호", "명칭"),
    "type": ("type", "종류"),
    "material": ("material", "재료", "콘크리트", "by story", "층별"),
    "width_mm": ("width", "폭", "가로", "b", "비"),
    "depth_mm": ("depth", "높이", "세로", "h", "에이치", "깊이", "단면고"),
}

_RE_COMPACT = re.compile(r"[\s\u3000_\-./]+", re.UNICODE)

# 상·하부근 2단: 바깥→안쪽. 도면 `11-11-D16` 등을 `11/11-D16`으로 통일 (D근만, SHD/UHD 단일근은 유지).
_RE_BEAM_TWO_TIER_D = re.compile(r"\b(\d{1,2})\s*[-–]\s*(\d{1,2})\s*[-–]\s*(D\d+)", re.I)


def normalize_beam_rebar_two_tier_notation(s: str) -> str:
    """`11-11-D16` → `11/11-D16`. 이미 `/` 구분이면 유지."""
    if not s or not isinstance(s, str):
        return s
    t = s.strip()
    return _RE_BEAM_TWO_TIER_D.sub(r"\1/\2-\3", t)


def _apply_beam_bar_notation_to_record(rec: dict[str, Any]) -> None:
    for fk in (
        "int_top_bar",
        "cen_top_bar",
        "ext_top_bar",
        "int_bot_bar",
        "cen_bot_bar",
        "ext_bot_bar",
        "int_stirrup_bar",
        "cen_stirrup_bar",
        "ext_stirrup_bar",
        "side_bar",
    ):
        v = rec.get(fk)
        if isinstance(v, str) and v.strip():
            rec[fk] = normalize_beam_rebar_two_tier_notation(v)


def _finalize_beam_extraction_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    for r in rows:
        _normalize_beam_mark_paren_dimensions(r)
        _apply_beam_bar_notation_to_record(r)
    return rows


def _compact_header(s: str) -> str:
    t = (s or "").strip().lower()
    t = _RE_COMPACT.sub("", t)
    t = t.replace("－", "").replace("–", "")
    return t


def _beam_wide_header_score(cell: str) -> str | None:
    """단일 셀 → 와이드 표 열 키. 없으면 None."""
    raw = (cell or "").strip()
    if not raw:
        return None
    c = _compact_header(raw)
    if not c:
        return None
    for key, aliases in _BEAM_KO_HEADERS.items():
        for a in aliases:
            if c == _compact_header(a) or c.endswith(_compact_header(a)) or c.startswith(
                _compact_header(a)
            ):
                return key
    if c == "name" or (len(c) <= 24 and re.fullmatch(r"name|부호|기호|부재명", c)):
        return "name"
    # Int/Cen/Ext × Top/Bot/Stirrup (compact c는 보조, 구간은 단어 경계로 오탐 방지)
    rs = re.sub(r"\s+", " ", raw.lower())
    has_int = bool(re.search(r"\bint\b", rs)) or "인테" in raw or "내부" in raw
    has_cen = bool(re.search(r"\b(cen|center|centre)\b", rs)) or "중앙" in raw or "중간" in raw
    has_cen = has_cen or bool(re.search(r"\b(mid|span)\b", rs))
    has_ext = bool(re.search(r"\b(ext|end)\b", rs)) or "외부" in raw or "단부" in raw or "외단" in raw
    has_top = "top" in c or "상부" in c or "상근" in c
    has_bot = "bot" in c or "bottom" in c or "하부" in c or "하근" in c
    has_st = "stirr" in c or "스트럽" in c or "스터럽" in c or "후프" in c or "띠장" in c
    has_side = "side" in c and "bar" in c
    if has_side or ("side" in c and "철근" in raw):
        return "side_bar"
    zone = None
    if has_int and not has_cen and not has_ext:
        zone = "int"
    elif has_cen and not has_int and not has_ext:
        zone = "cen"
    elif has_ext and not has_int and not has_cen:
        zone = "ext"
    elif c in ("int", "i", "in"):
        zone = "int"
    elif c in ("cen", "c", "mid"):
        zone = "cen"
    elif c in ("ext", "e", "end"):
        zone = "ext"
    if zone:
        if has_top:
            return f"{zone}_top_bar"
        if has_bot:
            return f"{zone}_bot_bar"
        if has_st:
            return f"{zone}_stirrup_bar"
    return None


def _beam_narrow_header_score(cell: str) -> str | None:
    """Location + 단일 Top/Bot 열 형식."""
    c = _compact_header(cell)
    if not c:
        return None
    if c in _compact_header("name") or "부호" in (cell or "") or re.match(
        r"^\s*name\s*$", (cell or "").strip(), re.I
    ):
        return "name"
    if "location" in c or "위치" in (cell or "") or "구간" in (cell or "") or "zone" in c:
        return "location"
    if "topbar" in c or ("top" in c and "bar" in c) or "상부근" in (cell or ""):
        return "top_bar"
    if "botbar" in c or ("bot" in c and "bar" in c) or "하부근" in (cell or ""):
        return "bot_bar"
    if "stirr" in c or "스트럽" in (cell or "") or "스터럽" in (cell or ""):
        return "stirrup_bar"
    if "width" in c or "폭" in (cell or ""):
        return "width_mm"
    if "depth" in c or "높이" in (cell or "") or "단면" in (cell or ""):
        return "depth_mm"
    if "material" in c or "재료" in (cell or ""):
        return "material"
    if "type" in c and len(c) <= 8:
        return "type"
    if "side" in c and "bar" in c:
        return "side_bar"
    return None


def _rows_to_cell_strings(rows: list[list[dict[str, Any]]]) -> list[list[str]]:
    out: list[list[str]] = []
    for row in rows:
        row_sorted = sorted(row, key=lambda r: float(r["x"]))
        out.append([str(r.get("text") or "").strip() for r in row_sorted])
    return out


def _merge_cells_vertical(a: str, b: str) -> str:
    x, y = (a or "").strip(), (b or "").strip()
    if x and y:
        return f"{x} {y}".strip()
    return x or y


def _try_map_wide_header_row(cells: list[str]) -> list[str | None]:
    keys: list[str | None] = []
    for cell in cells:
        keys.append(_beam_wide_header_score(cell))
    return keys


def _try_map_narrow_header_row(cells: list[str]) -> list[str | None]:
    return [_beam_narrow_header_score(c) for c in cells]


def _count_non_none(keys: list[str | None]) -> int:
    return sum(1 for k in keys if k is not None)


def _resolve_wide_header_two_lines(
    row0: list[str], row1: list[str]
) -> list[str | None] | None:
    n = max(len(row0), len(row1))
    r0 = list(row0) + [""] * (n - len(row0))
    r1 = list(row1) + [""] * (n - len(row1))
    merged = [_merge_cells_vertical(r0[i], r1[i]) for i in range(n)]
    keys = _try_map_wide_header_row(merged)
    # 열이 쪼개진 경우: 윗줄 Int-, 아랫줄 TopBar 만 있는 열
    keys2: list[str | None] = []
    for i in range(n):
        k0 = _beam_wide_header_score(r0[i])
        k1 = _beam_wide_header_score(r1[i])
        if k0 and k1 and k0 != k1:
            m = _merge_cells_vertical(r0[i], r1[i])
            keys2.append(_beam_wide_header_score(m))
        elif k0:
            keys2.append(k0)
        elif k1:
            keys2.append(k1)
        else:
            keys2.append(keys[i] if i < len(keys) else None)
    return keys2 if _count_non_none(keys2) >= 6 else None


def _find_beam_wide_header(cell_rows: list[list[str]]) -> tuple[int, list[str | None]] | None:
    """(헤더_시작_행_인덱스, 열별_키). 실패 시 None."""
    best_score = -1
    best_pair: tuple[int, list[str | None]] | None = None

    def consider(i: int, keys: list[str | None], min_score: int) -> None:
        nonlocal best_score, best_pair
        score = _count_non_none(keys)
        has_name = "name" in keys
        has_dim = "width_mm" in keys and "depth_mm" in keys
        has_sec = any(
            k in ("int_top_bar", "cen_top_bar", "ext_top_bar") for k in keys if k
        )
        if not has_name or not has_dim or score < min_score:
            return
        pri = score + (3 if has_sec else 0)
        if pri > best_score:
            best_score = pri
            best_pair = (i, keys)

    for i, cells in enumerate(cell_rows):
        keys = _try_map_wide_header_row(cells)
        consider(i, keys, 8)
        if i + 1 < len(cell_rows):
            keys2 = _resolve_wide_header_two_lines(cells, cell_rows[i + 1])
            if keys2:
                consider(i, keys2, 8)
    if best_pair is None:
        for i, cells in enumerate(cell_rows):
            keys = _try_map_wide_header_row(cells)
            consider(i, keys, 6)
    return best_pair


def _find_beam_narrow_header(cell_rows: list[list[str]]) -> tuple[int, list[str | None]] | None:
    for i, cells in enumerate(cell_rows):
        keys = _try_map_narrow_header_row(cells)
        if "location" in keys and ("top_bar" in keys or "name" in keys):
            if _count_non_none(keys) >= 4:
                return i, keys
    return None


_RE_INT = re.compile(r"^\s*(\d{2,4})\s*$")


def _parse_int_mm(s: str) -> int | None:
    m = _RE_INT.match((s or "").strip())
    if not m:
        return None
    v = int(m.group(1))
    return v if 100 <= v <= 5000 else None


def _beam_row_to_record_wide(
    col_keys: list[str | None],
    cells: list[str],
    *,
    wall_mode: str,
    building: str | None,
    row_y_mean: float | None,
    cluster_row_index: int | None = None,
) -> dict[str, Any] | None:
    n = min(len(col_keys), len(cells))
    rec: dict[str, Any] = {
        "category": CATEGORY_BEAM,
        "wall_mode": wall_mode,
        "beam_layout": "horizontal_wide",
        "beam_row_role": "beam_entry",
    }
    if building:
        rec["building"] = building
    flat: list[str] = []
    for i in range(n):
        k = col_keys[i]
        v = (cells[i] if i < len(cells) else "").strip()
        flat.append(v)
        if not k:
            continue
        if k == "width_mm":
            pn = _parse_int_mm(v)
            rec["width_mm"] = pn if pn is not None else v
        elif k == "depth_mm":
            pn = _parse_int_mm(v)
            rec["depth_mm"] = pn if pn is not None else v
        else:
            rec[k] = v
    if isinstance(rec.get("width_mm"), str):
        pn = _parse_int_mm(str(rec["width_mm"]))
        if pn is not None:
            rec["width_mm"] = pn
    if isinstance(rec.get("depth_mm"), str):
        pn = _parse_int_mm(str(rec["depth_mm"]))
        if pn is not None:
            rec["depth_mm"] = pn
    w = rec.get("width_mm")
    d = rec.get("depth_mm")
    if isinstance(w, int) and isinstance(d, int):
        rec["size_mm"] = [w, d]
        rec["SIZE"] = f"{w}x{d}"
    nm = str(rec.get("name") or "").strip()
    if not nm or _compact_header(nm) in ("name", "부호", "type", "material"):
        return None
    if re.match(r"^\d+$", nm) and len(nm) <= 4:
        return None
    rec["mark"] = nm
    rec["member_label"] = nm
    sig = _merge_signals([c for c in flat if c])
    for k, v in sig.items():
        if k == "cells":
            continue
        if k not in rec:
            rec[k] = v
    rec["cells"] = flat
    if row_y_mean is not None:
        rec["row_y_mean"] = round(float(row_y_mean), 4)
    if cluster_row_index is not None:
        rec["beam_cluster_row_index"] = int(cluster_row_index)
    return rec


def _beam_horizontal_wide_match_lines(
    rows_cluster: list[list[dict[str, Any]]],
    col_keys: list[str | None],
    records: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """
    가로 와이드( Y행 클러스터 + 열 헤더 ) 성공 시 뷰어 폴리라인.
    한 행 안에서 X순 열 인덱스 = col_keys 인덱스와 같다고 보고 부호→부위(또는 첫 주근 열)→단면(폭·고) TEXT 좌표를 잇는다.
    """
    out: list[dict[str, Any]] = []
    zone_priority = (
        "location",
        "int_top_bar",
        "cen_top_bar",
        "ext_top_bar",
        "int_bot_bar",
        "cen_bot_bar",
        "ext_bot_bar",
        "int_stirrup_bar",
        "cen_stirrup_bar",
        "ext_stirrup_bar",
    )
    for si, rec in enumerate(records):
        ri = rec.get("beam_cluster_row_index")
        if not isinstance(ri, int) or ri < 0 or ri >= len(rows_cluster):
            continue
        mk = str(rec.get("mark") or rec.get("name") or "").strip()
        if not mk:
            continue
        row_sorted = sorted(rows_cluster[ri], key=lambda r: float(r["x"]))
        n = min(len(col_keys), len(row_sorted))
        if n < 1:
            continue
        j_name = next((j for j in range(n) if col_keys[j] == "name"), -1)
        if j_name < 0:
            continue
        jw = next((j for j in range(n) if col_keys[j] == "width_mm"), -1)
        jd = next((j for j in range(n) if col_keys[j] == "depth_mm"), -1)
        cells = [str(row_sorted[k].get("text") or "").strip() for k in range(len(row_sorted))]

        j_zone = -1
        for pk in zone_priority:
            j_cand = next((j for j in range(n) if col_keys[j] == pk), -1)
            if j_cand < 0:
                continue
            if j_cand < len(cells) and cells[j_cand].strip():
                j_zone = j_cand
                break
        if j_zone < 0:
            if jd >= 0 and j_name >= 0:
                j_zone = max(0, min((j_name + jd) // 2, n - 1))
            else:
                j_zone = j_name

        def _xy(j: int) -> tuple[float, float] | None:
            if j < 0 or j >= len(row_sorted):
                return None
            it = row_sorted[j]
            try:
                return float(it["x"]), float(it["y"])
            except (TypeError, KeyError, ValueError):
                return None

        p0 = _xy(j_name)
        p1 = _xy(j_zone)
        p2: tuple[float, float] | None = None
        if jw >= 0 and jd >= 0:
            try:
                x2 = 0.5 * (float(row_sorted[jw]["x"]) + float(row_sorted[jd]["x"]))
                y2 = 0.5 * (float(row_sorted[jw]["y"]) + float(row_sorted[jd]["y"]))
                p2 = (x2, y2)
            except (TypeError, KeyError, ValueError):
                p2 = None
        elif jd >= 0:
            p2 = _xy(jd)
        elif jw >= 0:
            p2 = _xy(jw)
        if not p0 or not p1 or not p2:
            continue
        zt = cells[j_zone][:48] if 0 <= j_zone < len(cells) else ""
        pts = [
            [round(p0[0], 4), round(p0[1], 4)],
            [round(p1[0], 4), round(p1[1], 4)],
            [round(p2[0], 4), round(p2[1], 4)],
        ]
        out.append(
            {
                "strip_index": si,
                "mark": mk,
                "mark_inferred": mk,
                "zone_text": (zt or "—").strip(),
                "points": pts,
            }
        )
    return out


def _beam_row_entities_y_bounds(row: list[dict[str, Any]]) -> tuple[float, float] | None:
    ys: list[float] = []
    for it in row or []:
        try:
            ys.append(float(it["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not ys:
        return None
    return (min(ys), max(ys))


def _beam_horizontal_or_tree_synth_strips_and_headers(
    rows_out: list[dict[str, Any]],
    match_lines: list[dict[str, Any]] | None,
    rows_cluster: list[list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    가로 와이드·Location 트리는 세로 블록용 strip/헤더가 없어 단면 enrich·뷰어 poly 내비가 빈다.
    출력 행 인덱스 = 스트립 인덱스(와이드 match_line.strip_index 와 동일)로 합성한다.
    """
    if not rows_out:
        return [], []
    xc_by_si: dict[int, float] = {}
    for line in match_lines or []:
        try:
            si = int(line.get("strip_index"))
        except (TypeError, ValueError):
            continue
        pts = line.get("points")
        if not isinstance(pts, list) or not pts:
            continue
        xs: list[float] = []
        for p in pts[:4]:
            if isinstance(p, (list, tuple)) and len(p) >= 1:
                try:
                    xs.append(float(p[0]))
                except (TypeError, ValueError):
                    pass
        if xs:
            xc_by_si[si] = sum(xs) / len(xs)
    strip_infos: list[dict[str, Any]] = []
    for si, rec in enumerate(rows_out):
        xc = float(xc_by_si.get(si) or 0.0)
        if xc == 0.0:
            cxy = None
            cri = rec.get("beam_cluster_row_index")
            if isinstance(cri, int) and 0 <= cri < len(rows_cluster):
                cxy = _beam_centroid_entities(rows_cluster[cri])
            if cxy is None:
                idxs = rec.get("_beam_loc_flush_row_indices")
                if isinstance(idxs, list):
                    ents: list[dict[str, Any]] = []
                    for ri in idxs:
                        if isinstance(ri, int) and 0 <= ri < len(rows_cluster):
                            ents.extend(rows_cluster[ri] or [])
                    cxy = _beam_centroid_entities(ents)
            if cxy:
                xc = float(cxy[0])
        strip_infos.append({"index": si, "x_center": round(xc, 4), "entity_count": 0})
        rec.setdefault("beam_vertical_merged_strip_indices", [si])
        yb = None
        cri2 = rec.get("beam_cluster_row_index")
        if isinstance(cri2, int) and 0 <= cri2 < len(rows_cluster):
            yb = _beam_row_entities_y_bounds(rows_cluster[cri2])
        if yb is None:
            idxs2 = rec.get("_beam_loc_flush_row_indices")
            if isinstance(idxs2, list):
                ys_acc: list[float] = []
                for ri in idxs2:
                    if not isinstance(ri, int) or ri < 0 or ri >= len(rows_cluster):
                        continue
                    yp = _beam_row_entities_y_bounds(rows_cluster[ri])
                    if yp:
                        ys_acc.extend([yp[0], yp[1]])
                if len(ys_acc) >= 2:
                    yb = (min(ys_acc), max(ys_acc))
        if yb is not None:
            rec.setdefault("row_data_anchor_y_bounds", [round(float(yb[0]), 4), round(float(yb[1]), 4)])
    ys_mean: list[float] = []
    for r in rows_out:
        ry = r.get("row_y_mean")
        if ry is not None:
            try:
                ys_mean.append(float(ry))
            except (TypeError, ValueError):
                pass
    y_tmpl = (max(ys_mean) + 280.0) if ys_mean else 0.0
    field_headers = [
        {"key": "section_shape", "label": "형 태", "y": round(float(y_tmpl), 4)},
    ]
    return field_headers, strip_infos


_LOC_ZONE = {
    "int": "int",
    "interior": "int",
    "인테리어": "int",
    "인테": "int",
    "내부": "int",
    "in": "int",
    "i": "int",
    "cen": "cen",
    "center": "cen",
    "centre": "cen",
    "중앙": "cen",
    "중간": "cen",
    "mid": "cen",
    "span": "cen",
    "c": "cen",
    "ext": "ext",
    "exterior": "ext",
    "외부": "ext",
    "end": "ext",
    "단부": "ext",
    "e": "ext",
    "외단": "ext",
    "both": "_both",
    "양단": "_both",
    "양쪽": "_both",
    "all": "_all",
    "전체": "_all",
    "통근": "_all",
}


def _normalize_location_zone(loc: str) -> str | None:
    t = _compact_header(loc)
    if not t:
        return None
    for alias, z in _LOC_ZONE.items():
        if t == _compact_header(alias) or t.startswith(_compact_header(alias)):
            return z
    return None


def _finalize_grouped_beam(
    base: dict[str, Any],
    *,
    wall_mode: str,
    building: str | None,
    row_y_mean: float | None,
) -> dict[str, Any]:
    rec: dict[str, Any] = {
        "category": CATEGORY_BEAM,
        "wall_mode": wall_mode,
        "beam_layout": "horizontal_location_tree",
        "beam_row_role": "beam_entry",
    }
    if building:
        rec["building"] = building
    for k, v in base.items():
        if v is not None and v != "":
            rec[k] = v
    for key in ("width_mm", "depth_mm"):
        v = rec.get(key)
        if isinstance(v, str):
            p = _parse_int_mm(v)
            if p is not None:
                rec[key] = p
    w, d = rec.get("width_mm"), rec.get("depth_mm")
    if isinstance(w, int) and isinstance(d, int):
        rec["size_mm"] = [w, d]
        rec["SIZE"] = f"{w}x{d}"
    nm = str(rec.get("name") or rec.get("mark") or "").strip()
    rec["mark"] = nm
    rec["member_label"] = nm
    flat = [str(rec.get(k, "") or "") for k in BEAM_WIDE_KEYS_ORDER]
    rec["cells"] = flat
    sig = _merge_signals([c for c in flat if c])
    for k, v in sig.items():
        if k == "cells":
            continue
        if k not in rec:
            rec[k] = v
    if row_y_mean is not None:
        rec["row_y_mean"] = round(float(row_y_mean), 4)
    return rec


def _extract_beam_location_grouped(
    cell_rows: list[list[str]],
    header_idx: int,
    col_keys: list[str | None],
    *,
    wall_mode: str,
    building: str | None,
    row_y_means: list[float],
) -> list[dict[str, Any]]:
    """Location 열이 있는 좁은 표 → 부재명별로 Int/Cen/Ext 칸 채움."""
    loc_col = col_keys.index("location") if "location" in col_keys else -1
    if loc_col < 0:
        return []
    out: list[dict[str, Any]] = []
    cur_name: str | None = None
    cur: dict[str, Any] = {}
    cur_y: list[float] = []
    cur_ri: list[int] = []

    def flush():
        nonlocal cur, cur_name, cur_y, cur_ri
        if cur_name and any(
            cur.get(k) for k in BEAM_WIDE_KEYS_ORDER if k not in ("name", "type", "material")
        ):
            ym = sum(cur_y) / len(cur_y) if cur_y else None
            rec = _finalize_grouped_beam(cur, wall_mode=wall_mode, building=building, row_y_mean=ym)
            if cur_ri:
                rec["_beam_loc_flush_row_indices"] = list(cur_ri)
            out.append(rec)
        cur = {}
        cur_name = None
        cur_y = []
        cur_ri = []

    for i in range(header_idx + 1, len(cell_rows)):
        cells = cell_rows[i]
        if not any(c.strip() for c in cells):
            continue
        keys_use = col_keys + [None] * max(0, len(cells) - len(col_keys))
        row_map: dict[str, str] = {}
        for j, c in enumerate(cells):
            ku = keys_use[j] if j < len(keys_use) else None
            if ku:
                row_map[ku] = c.strip()
        name_cell = row_map.get("name", "")
        loc_cell = row_map.get("location", cells[loc_col] if loc_col < len(cells) else "")
        if name_cell and _beam_narrow_header_score(name_cell) is None:
            if cur_name and name_cell != cur_name:
                flush()
            cur_name = name_cell
            cur["name"] = cur_name
            cur["mark"] = cur_name
        zone = _normalize_location_zone(loc_cell)
        if not zone:
            if name_cell and not cur.get("name"):
                cur["name"] = name_cell
                cur_name = name_cell
            continue
        for dim_key in ("type", "material", "width_mm", "depth_mm"):
            if dim_key in row_map and row_map[dim_key]:
                raw_v = row_map[dim_key]
                if dim_key in ("width_mm", "depth_mm"):
                    pn = _parse_int_mm(raw_v)
                    cur[dim_key] = pn if pn is not None else raw_v
                else:
                    cur[dim_key] = raw_v
        tb = row_map.get("top_bar", "")
        bb = row_map.get("bot_bar", "")
        sb = row_map.get("stirrup_bar", "")
        sdb = row_map.get("side_bar", "")
        if sdb:
            cur["side_bar"] = sdb

        def assign(z: str, both: bool = False):
            if tb:
                cur[f"{z}_top_bar"] = tb
            if bb:
                cur[f"{z}_bot_bar"] = bb
            if sb:
                cur[f"{z}_stirrup_bar"] = sb

        if zone == "_both":
            assign("int")
            assign("ext")
        elif zone == "_all":
            assign("int")
            assign("cen")
            assign("ext")
        elif zone in ("int", "cen", "ext"):
            assign(zone)
        cur_y.append(row_y_means[i] if i < len(row_y_means) else row_y_means[-1])
        cur_ri.append(i)
    flush()
    return out


def extract_beam_horizontal_from_clusters(
    rows_cluster: list[list[dict[str, Any]]],
    *,
    wall_mode: str,
    building: str | None,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """
    Y-클러스터 행에서 보 일람(가로 와이드 또는 Location 트리) 인식.
    성공 시 (rows, meta), 실패 시 None.
    """
    if not rows_cluster:
        return None
    row_y_means: list[float] = []
    for row in rows_cluster:
        ys = [float(r["y"]) for r in row]
        row_y_means.append(sum(ys) / len(ys) if ys else 0.0)
    cell_rows = _rows_to_cell_strings(rows_cluster)

    wide = _find_beam_wide_header(cell_rows)
    if wide:
        hdr_i, col_keys = wide
        rows_out: list[dict[str, Any]] = []
        for i in range(hdr_i + 1, len(cell_rows)):
            cells = cell_rows[i]
            if not any(x.strip() for x in cells):
                continue
            rec = _beam_row_to_record_wide(
                col_keys,
                cells,
                wall_mode=wall_mode,
                building=building,
                row_y_mean=row_y_means[i] if i < len(row_y_means) else None,
                cluster_row_index=i,
            )
            if rec:
                rows_out.append(rec)
        if rows_out:
            meta = {
                "beam_wide_table": True,
                "beam_header_row_index": hdr_i,
                "beam_column_keys": col_keys,
            }
            ml = _beam_horizontal_wide_match_lines(rows_cluster, col_keys, rows_out)
            if ml:
                meta["beam_vertical_member_zone_match_lines"] = ml
            fh_syn, strips_syn = _beam_horizontal_or_tree_synth_strips_and_headers(
                rows_out, ml, rows_cluster
            )
            if fh_syn and strips_syn:
                meta["beam_vertical_field_headers"] = fh_syn
                meta["beam_vertical_strips"] = strips_syn
            return rows_out, meta

    narrow = _find_beam_narrow_header(cell_rows)
    if narrow:
        hi, nk = narrow
        grouped = _extract_beam_location_grouped(
            cell_rows,
            hi,
            nk,
            wall_mode=wall_mode,
            building=building,
            row_y_means=row_y_means,
        )
        if grouped:
            meta_lt = {
                "beam_location_tree": True,
                "beam_header_row_index": hi,
                "beam_column_keys": nk,
            }
            fh_g, strips_g = _beam_horizontal_or_tree_synth_strips_and_headers(
                grouped, None, rows_cluster
            )
            if fh_g and strips_g:
                meta_lt["beam_vertical_field_headers"] = fh_g
                meta_lt["beam_vertical_strips"] = strips_g
            return grouped, meta_lt
    return None


def _beam_lab_compact(lab: str) -> str:
    return re.sub(r"[\s_\-]", "", (lab or "").strip())


_RE_BEAM_MARK_PAREN_SIZE = re.compile(
    r"^(?P<body>.+?)\s*\(\s*(?P<a>\d{3,4})\s*[xX×]\s*(?P<b>\d{3,4})\s*\)\s*$",
)

# `BX1`·`RG11` 등: 영문 접두 + 숫자(스케줄용 _RE_BEAM_MARK_LIKE는 글자·숫자 사이 공백을 요구해 BX1 누락됨).
_RE_BEAM_MEMBER_ID_LOOSE = re.compile(
    r"^\s*[A-Z]+\d[A-Z0-9]*\s*(?:\(.+\))?\s*$",
    re.I,
)


def _split_beam_mark_and_dim_parens(s: str) -> tuple[str, int | None, int | None]:
    """`RG13C (1000x900)` → (RG13C, 1000, 900). 괄호 없으면 전체 문자열, None, None."""
    raw = (s or "").strip()
    if not raw:
        return "", None, None
    m = _RE_BEAM_MARK_PAREN_SIZE.match(raw)
    if not m:
        return raw, None, None
    body = (m.group("body") or "").strip()
    try:
        w, h = int(m.group("a")), int(m.group("b"))
    except ValueError:
        return raw, None, None
    if not (200 <= w <= 4000 and 200 <= h <= 4000):
        return raw, None, None
    return body or raw, w, h


def _normalize_beam_mark_paren_dimensions(rec: dict[str, Any]) -> None:
    """
    부호/부재명에 `RG11 (1000x900)` 형태가 붙은 경우 본문·치수를 분리한다.
    member_label·mark·name 중 처음으로 괄호 치수가 파싱되는 문자열을 기준으로 한다.
    """
    candidates: list[str] = []
    for k in ("member_label", "mark", "name"):
        v = rec.get(k)
        if isinstance(v, str) and v.strip():
            candidates.append(v.strip())
    if not candidates:
        return
    base: str | None = None
    wmm: int | None = None
    hmm: int | None = None
    for s in candidates:
        b, w, h = _split_beam_mark_and_dim_parens(s)
        if w is not None and h is not None:
            base = (b or "").strip()
            wmm, hmm = w, h
            break
    if not base or wmm is None or hmm is None:
        return
    rec["mark"] = base
    rec["name"] = base
    rec["member_label"] = base
    if not isinstance(rec.get("width_mm"), int):
        rec["width_mm"] = wmm
    if not isinstance(rec.get("depth_mm"), int):
        rec["depth_mm"] = hmm
    rec["size_mm"] = [wmm, hmm]
    rec["SIZE"] = f"{wmm}x{hmm}"


def _norm_text_for_standalone_mm_label(text: str) -> str:
    """전각 숫자·쉼표·공백·선행 mm 등 제거 후 숫자만 비교."""
    t = unicodedata.normalize("NFKC", (text or "").strip())
    t = t.replace(",", "").replace("，", "")
    t = re.sub(r"[\s\u00a0\u3000]+", "", t)
    t = re.sub(r"(?i)mm$", "", t)
    return t


def _is_standalone_section_mm_label(text: str) -> bool:
    """좌측에 `900`·`１０００`·`900mm` 처럼 단면 치수만 한 줄로 잡힌 경우 → 데이터로 보냄/템플릿에서 제거."""
    t = _norm_text_for_standalone_mm_label(text)
    if not re.fullmatch(r"\d{3,4}", t):
        return False
    try:
        v = int(t)
    except ValueError:
        return False
    return 200 <= v <= 4000


def _demote_beam_standalone_mm_labels(
    label_items: list[dict[str, Any]],
    data_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    keep: list[dict[str, Any]] = []
    demoted: list[dict[str, Any]] = []
    for it in label_items:
        t = str(it.get("text") or "").strip()
        if _is_standalone_section_mm_label(t):
            demoted.append(it)
        else:
            keep.append(it)
    return keep, data_items + demoted


def _is_beam_vertical_sheet_title_row(lab: str) -> bool:
    """좌측 라벨 중 표 제목(보 일람표-1 …)은 템플릿 행으로 쓰지 않는다."""
    raw = (lab or "").strip().replace("\u3000", " ")
    if not raw:
        return False
    u = raw.upper()
    if "일람표" in raw and ("보" in raw or "BEAM" in u):
        return True
    if re.search(r"보\s*일람", raw) or re.search(r"beam\s*schedule", u, re.I):
        return True
    return False


def _prune_beam_vertical_template(
    template: list[tuple[float, str, str]],
) -> list[tuple[float, str, str]]:
    """표 제목·순수 mm 숫자 행 제거 후 키 재부여."""
    kept: list[tuple[float, str, str]] = []
    for y, lab, _k in template:
        if _is_beam_vertical_sheet_title_row(lab):
            continue
        if _is_standalone_section_mm_label(lab):
            continue
        kept.append((float(y), lab, ""))
    out: list[tuple[float, str, str]] = []
    for i, (y, lab, _) in enumerate(kept):
        out.append((y, lab, _slug_field_key(lab, i)))
    return out


def _beam_vertical_zone_from_label(lab: str) -> str | None:
    """
    세로 보 일람의 구간 라벨 → int|cen|ext|both|all.
    END( INT. ) / END( EXT. ) 처럼 괄호 안 한정이 있으면 그쪽을 우선한다(무조건 END→ext 금지).
    Grasshopper 패널(에코델타 보 일람 V68) 동의어: BOTH·양단부·CEN·중앙부 등.
    """
    raw = unicodedata.normalize("NFKC", (lab or "").strip())
    if not raw:
        return None
    u = re.sub(r"\s+", " ", raw.upper())
    if re.fullmatch(r"ALL\.?", u) or u == "ALL" or "전체" in raw:
        return "all"
    if re.fullmatch(r"BOTH\.?", u) or u == "BOTH":
        return "both"
    if "양단부" in raw or "양단면" in raw:
        return "both"
    # END(INT) / END( EXT ) — 단일 'END'보다 먼저 판별
    if re.search(r"\bEND\s*[\[(]\s*INT", u):
        return "int"
    if re.search(r"\bEND\s*[\[(]\s*EXT", u):
        return "ext"
    if re.search(r"\bEND\s*[\[(]\s*BOTH", u):
        return "both"
    if re.search(r"\bEND\b", u) or "단부" in raw or re.fullmatch(r"END\.?", u):
        return "ext"
    if re.search(r"\bEXT\b|\bEXTERIOR\b", u) or "외부" in raw:
        return "ext"
    if re.search(r"\bINT\b", u) or "인테" in raw or "내부" in raw:
        if "BOTH" in u or "양" in raw or "양단" in raw:
            return "both"
        return "int"
    if "중앙부" in raw:
        return "cen"
    if re.search(r"\b(CEN|CENTER|CENTRE)\b", u) or "중앙" in raw or "중간" in raw:
        return "cen"
    if re.fullmatch(r"CEN\.?", u):
        return "cen"
    if re.search(r"\bMID\b|\bSPAN\b", u):
        return "cen"
    return None


def _beam_vertical_zone_header_indices(template: list[tuple[float, str, str]]) -> list[int]:
    """좌측 템플릿에서 INT/END/ALL 등 구간(부위) 전용 행 인덱스."""
    out: list[int] = []
    for i, (_y, lab, _k) in enumerate(template):
        if _beam_vertical_zone_from_label(lab):
            out.append(i)
    return out


def _beam_vertical_zone_display_label(template: list[tuple[float, str, str]]) -> str | None:
    """구간 전용 행의 원문 라벨(INT.(BOTH), ALL …) — 오버레이·이슈 문구용."""
    for _y, lab, _k in template:
        if _beam_vertical_zone_from_label(lab):
            s = str(lab or "").strip()
            return s or None
    return None


def _beam_vertical_zone_display_label_from_bands(
    bands: list[tuple[float, str]],
    z_t: list[tuple[float, str, str]],
) -> str | None:
    """
    가로로 나란한 단면(INT / CENTER 등)에서 부위명이 데이터 열(스트립) 위에만 있을 때,
    좌측 템플릿의 '첫 구간' 라벨보다 이 값을 우선한다.
    """
    if not bands or not z_t:
        return None
    sec_y = _beam_vertical_row_section_anchor_y(z_t)
    if sec_y is None:
        ys_tpl = [float(t[0]) for t in z_t]
        sec_y = float(sum(ys_tpl) / len(ys_tpl)) if ys_tpl else None
    def _zone_phrases_from_band_text(raw: str) -> list[str]:
        """한 Y 밴드에 'INT.(BOTH) CENTER'처럼 붙은 경우 각 토큰을 후보로 분해한다."""
        t0 = (raw or "").strip()
        if not t0:
            return []
        parts = re.split(r"\s+", t0)
        tokens = [p for p in parts if p and _beam_vertical_zone_from_label(p) is not None]
        if len(tokens) >= 2:
            return tokens
        if len(tokens) == 1:
            return tokens
        if _beam_vertical_zone_from_label(t0) is not None:
            return [t0]
        return []

    candidates: list[tuple[float, str]] = []
    for y_val, raw in bands:
        try:
            yf = float(y_val)
        except (TypeError, ValueError):
            continue
        for t in _zone_phrases_from_band_text(str(raw or "")):
            if _beam_vertical_zone_from_label(t) is None:
                continue
            candidates.append((yf, t))
    if not candidates:
        return None
    if sec_y is not None:
        y0 = float(sec_y)
        best_t: str | None = None
        best_d = 1e18
        for yv, tx in candidates:
            d = abs(yv - y0)
            # 동일 Y·동일 거리면 뒤에 오는 토큰(가로로 오른쪽에 붙인 텍스트)을 우선
            if d < best_d - 1e-6 or (abs(d - best_d) <= 1e-6):
                best_d = d
                best_t = tx
        return (best_t or "").strip() or None
    candidates.sort(key=lambda p: (-p[0], p[1]))
    return candidates[0][1].strip() or None


def _beam_vertical_template_name_row_y(template: list[tuple[float, str, str]]) -> float | None:
    for y, lab, _k in template:
        if _beam_vertical_row_kind_from_title(lab) == "name":
            try:
                return float(y)
            except (TypeError, ValueError):
                continue
    return None


def _beam_vertical_template_zone_row_y_estimate(template: list[tuple[float, str, str]]) -> float | None:
    ys: list[float] = []
    for y, lab, _k in template:
        if _beam_vertical_zone_from_label(lab):
            try:
                ys.append(float(y))
            except (TypeError, ValueError):
                continue
    if not ys:
        return None
    return float(_median_float(ys))


def _beam_vertical_text_looks_like_member_title(text: str) -> bool:
    """부호/부재명 후보( RG13, RG13 (1000x900), RG13B-INT 등 ) — 근·치수·구간 라벨 제외."""
    t = (text or "").strip()
    if not t or len(t) > 96:
        return False
    compact = re.sub(r"[\s\u3000.]+", "", t)
    looks_like_beam_mark_code = bool(
        re.search(r"(?i)(?:[A-Za-z가-힣]\d|\d[A-Za-z가-힣]{2})", compact)
    )
    if _beam_vertical_zone_from_label(t) and not looks_like_beam_mark_code:
        return False
    if _is_standalone_section_mm_label(t):
        return False
    if re.fullmatch(r"-?\d+(?:\.\d+)?", t):
        return False
    if re.match(r"^\d+\s*[-/]", t) or re.search(r"\d+\s*-\s*(?:U?HD|SHD|D)\d", t, re.I):
        return False
    if re.fullmatch(r"(?i)(RC|SOM|BY\s*STORY)", t.replace("\u3000", " ").strip()):
        return False
    base, wmm, hmm = _split_beam_mark_and_dim_parens(t)
    body = (base or t).strip()
    if len(body) < 2:
        return False
    if not re.search(r"(?i)[A-Za-z가-힣]", body):
        return False
    if re.fullmatch(r"(?i)(width|depth|type|name|mark|material)", body):
        return False
    return True


def _beam_vertical_prepare_bands_for_template_map(
    bands: list[tuple[float, str]],
    template: list[tuple[float, str, str]],
) -> list[tuple[float, str]]:
    """
    템플릿 매핑 직전: **밴드 텍스트는 잘라내지 않는다**(단면·부위·부호 우측 셀 누락 방지).
    단면(형태) Y에 가까운 순으로만 정렬해 `_map_data_bands_to_template` 스냅 순서를 안정화한다.
    """
    if not bands:
        return []
    sec_y = _beam_vertical_row_section_anchor_y(template)
    if sec_y is None:
        return list(bands)
    sy = float(sec_y)
    out = list(bands)
    out.sort(key=lambda p: (abs(float(p[0]) - sy), -float(p[0])))
    return out


def _beam_vertical_next_other_member_row_y_in_strip(
    strip: list[dict[str, Any]],
    xc_strip: float,
    y_cutoff: float,
    current_mark_disp: str,
    *,
    x_gate: float = 4500.0,
    gap_below_cutoff: float = 55.0,
) -> float | None:
    """
    같은 스트립에서 `y_cutoff`(형태/단면 기준 Y)보다 **아래**(world Y가 더 작은 쪽)에 나타나는
    **다른 부재** 부호 텍스트 Y 중, `y_cutoff`에 가장 가까운(가장 큰 Y) 한 줄.
    그 줄 아래는 다음 부재 구역이므로, 그 **위**만 현재 부재로 읽는다.
    """
    want = _beam_member_title_norm_for_infer(str(current_mark_disp or "").strip())
    best: float | None = None
    x0 = float(xc_strip)
    yc = float(y_cutoff)
    for it in strip or []:
        t = str(it.get("text") or "").strip()
        if not t or not _beam_vertical_text_looks_like_member_title(t):
            continue
        try:
            xf = float(it["x"])
            yf = float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if abs(xf - x0) > float(x_gate):
            continue
        if yf >= yc - float(gap_below_cutoff):
            continue
        ot = _beam_member_title_norm_for_infer(t)
        if want and ot == want:
            continue
        if want and ot.startswith(want) and len(ot) <= len(want) + 8:
            continue
        if best is None or yf > best:
            best = yf
    return best


def _beam_vertical_mark_hint_for_zone_dyn(
    z_dyn: dict[str, str],
    z_t: list[tuple[float, str, str]],
    om_strip: str | None,
) -> str:
    om0 = str(om_strip or "").strip()
    if om0:
        return om0
    for _y, lab, k in z_t:
        if _beam_vertical_row_kind_from_title(lab) == "name":
            v = str(z_dyn.get(k) or "").strip()
            if v:
                return v
    return ""


def _beam_vertical_cluster_mark_tuples_by_x(
    tuples_xyt: list[tuple[float, float, str]],
    x_gap: float,
) -> list[tuple[float, float, str]]:
    """(x,y,text) → (x 중심, 그룹 Y 중앙값, 대표 문자열). X만으로 인접 묶음."""
    if not tuples_xyt:
        return []
    s = sorted(tuples_xyt, key=lambda p: p[0])
    groups: list[list[tuple[float, float, str]]] = [[s[0]]]
    for p in s[1:]:
        if p[0] - groups[-1][-1][0] <= x_gap:
            groups[-1].append(p)
        else:
            groups.append([p])
    out: list[tuple[float, float, str]] = []
    for g in groups:
        xc = sum(p[0] for p in g) / len(g)
        ys = [float(p[1]) for p in g]
        y_med = float(_median_float(ys)) if ys else float("nan")
        texts = [p[2].strip() for p in g if p[2].strip()]
        if not texts:
            continue
        canonical = max(texts, key=lambda u: (len(u), u))
        out.append((xc, y_med, canonical))
    return sorted(out, key=lambda t: t[0])


def _beam_vertical_merge_mark_points_euclidean(
    pts: list[tuple[float, float, str]],
    tol: float,
) -> list[tuple[float, float, str]]:
    """
    GH `dist_threshold` 근사: 같은 부재명 조각이 여러 TEXT로 쪼개진 경우만 완화 병합.
    tol은 도면 단위(픽셀/월드) 유클리드 거리 상한.
    """
    if not pts or tol <= 0:
        return pts
    n = len(pts)
    parent = list(range(n))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    t2 = float(tol)
    for i in range(n):
        x1, y1, _ = pts[i]
        for j in range(i + 1, n):
            x2, y2, _ = pts[j]
            if ((x1 - x2) ** 2 + (y1 - y2) ** 2) ** 0.5 <= t2:
                union(i, j)
    roots: dict[int, list[int]] = defaultdict(list)
    for i in range(n):
        roots[find(i)].append(i)
    out: list[tuple[float, float, str]] = []
    for idxs in roots.values():
        xs = [float(pts[i][0]) for i in idxs]
        ys = [float(pts[i][1]) for i in idxs]
        texts = [pts[i][2].strip() for i in idxs if pts[i][2].strip()]
        if not texts:
            continue
        canon = max(texts, key=lambda u: (len(u), u))
        out.append((sum(xs) / len(xs), sum(ys) / len(ys), canon))
    return out


def _beam_vertical_cluster_mark_tuples_by_y_then_x(
    tuples_xyt: list[tuple[float, float, str]],
    y_tol: float,
    x_gap: float,
) -> list[tuple[float, float, str]]:
    """
    Grasshopper `groupNearbyTextsByY`와 동일 순서: Y 정렬 → |Y−앵커|≤y_tol 묶음 → 각 묶음에서 X 정렬 →
    인접 X간격≤x_gap 묶음 → (x중심, y중앙, 대표문자열).
    """
    if not tuples_xyt:
        return []
    y_tol = float(y_tol)
    x_gap = float(x_gap)
    s = sorted(tuples_xyt, key=lambda p: (p[1], p[0]))
    y_groups: list[list[tuple[float, float, str]]] = []
    for p in s:
        placed = False
        for g in y_groups:
            if abs(float(g[0][1]) - float(p[1])) <= y_tol:
                g.append(p)
                placed = True
                break
        if not placed:
            y_groups.append([p])
    out: list[tuple[float, float, str]] = []
    for g in y_groups:
        g.sort(key=lambda p: p[0])
        x_runs: list[list[tuple[float, float, str]]] = [[g[0]]]
        for p in g[1:]:
            if float(p[0]) - float(x_runs[-1][-1][0]) <= x_gap:
                x_runs[-1].append(p)
            else:
                x_runs.append([p])
        for run in x_runs:
            xc = sum(float(q[0]) for q in run) / len(run)
            ys2 = [float(q[1]) for q in run]
            y_med = float(_median_float(ys2)) if ys2 else float("nan")
            texts = [q[2].strip() for q in run if q[2].strip()]
            if not texts:
                continue
            canon = max(texts, key=lambda u: (len(u), u))
            out.append((xc, y_med, canon))
    return sorted(out, key=lambda t: t[0])


def _beam_vertical_template_name_row_y_nearest(
    template: list[tuple[float, str, str]],
    y_ref: float,
) -> float | None:
    """여러 부호 행 중 스트립 세로 위치에 가장 가까운 Y."""
    cand: list[float] = []
    for y, lab, _k in template:
        if _beam_vertical_row_kind_from_title(lab) == "name":
            try:
                cand.append(float(y))
            except (TypeError, ValueError):
                continue
    if not cand:
        return None
    return min(cand, key=lambda yn: abs(yn - y_ref))


def _beam_vertical_template_zone_row_y_nearest(
    template: list[tuple[float, str, str]],
    y_ref: float,
) -> float | None:
    """템플릿 구간 행(INT/END/ALL…) 중 y_ref에 가장 가까운 Y."""
    cand: list[float] = []
    for y, lab, _k in template:
        if _beam_vertical_zone_from_label(lab):
            try:
                cand.append(float(y))
            except (TypeError, ValueError):
                continue
    if not cand:
        return None
    return min(cand, key=lambda yz: abs(yz - y_ref))


def _beam_member_title_norm_for_infer(text: str) -> str:
    """부재명 통바·중복 텍스트 비교용(공백·전각 제거, 대소문자 무시)."""
    t = re.sub(r"[\s\u3000]+", "", (text or "").strip()).casefold()
    return t[:96]


def _strip_infos_by_index(strip_infos: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    """strip_infos는 빈 스트립을 건너뛰어 append되므로, 반드시 `index` 필드로 조회한다."""
    out: dict[int, dict[str, Any]] = {}
    for s in strip_infos or []:
        try:
            ii = int(s.get("index", -1))
        except (TypeError, ValueError):
            continue
        if ii >= 0:
            out[ii] = s
    return out


def _beam_vertical_apply_inferred_mark_to_rec(rec: dict[str, Any], om_raw: str) -> None:
    """스트립별 추론 부재명을 표준 필드에 반영(병합·표시·후속 검증이 동일 키를 쓰도록)."""
    om = str(om_raw or "").strip()
    if not om:
        return
    rec["beam_vertical_inferred_mark"] = om
    base, wmm, hmm = _split_beam_mark_and_dim_parens(om)
    disp = om.strip()
    base2 = (base or disp).strip()
    rec["member_label"] = disp
    rec["mark"] = base2
    rec["name"] = base2
    # 일람에서 이미 Width/Depth 숫자가 잡혀 있으면 덮어쓰지 않음 — 단면 search·SECTION 키가 흔들리는 원인 방지
    w0, d0 = rec.get("width_mm"), rec.get("depth_mm")
    has_pair = isinstance(w0, int) and isinstance(d0, int) and w0 > 0 and d0 > 0
    if wmm is not None and hmm is not None and not has_pair:
        rec["width_mm"] = wmm
        rec["depth_mm"] = hmm
        rec["size_mm"] = [wmm, hmm]
        rec["SIZE"] = f"{wmm}x{hmm}"


def _beam_vertical_infer_strip_member_fallback_nearest(
    data_items: list[dict[str, Any]],
    order_si: list[int],
    info_by_si: dict[int, dict[str, Any]],
    y_name: float,
    x_lo: float,
    x_hi: float,
    name_band_loose: float,
) -> tuple[dict[int, str], dict[str, Any]]:
    """
    클러스터 규칙이 빈 primary를 줄 때: 각 스트립 X중심에 가장 가까운 부재명 TEXT를 택한다(GH 거리 매칭 단순화).
    """
    out: dict[int, str] = {}
    y0 = float(y_name)
    band = max(float(name_band_loose) * 1.55, 96.0)
    for si in order_si:
        inf = info_by_si.get(si) or {}
        try:
            xc = float(inf.get("x_center") or 0.0)
        except (TypeError, ValueError):
            xc = 0.0
        best: tuple[float, float, str] | None = None
        for it in data_items:
            t = str(it.get("text") or "").strip()
            if not _beam_vertical_text_looks_like_member_title(t):
                continue
            try:
                x = float(it["x"])
                y = float(it["y"])
            except (TypeError, KeyError, ValueError):
                continue
            if not (x_lo <= x <= x_hi):
                continue
            if abs(y - y0) > band:
                continue
            dx = abs(x - xc)
            dy = abs(y - y0)
            cand = (dx, dy, t)
            if best is None or (dx, dy) < (best[0], best[1]):
                best = cand
        if best is not None:
            out[si] = best[2].strip()
    return out, {"fallback_nearest_n": len(out)}


def _beam_vertical_infer_strip_member_map(
    data_items: list[dict[str, Any]],
    template: list[tuple[float, str, str]],
    strips: list[list[dict[str, Any]]],
    strip_infos: list[dict[str, Any]],
    band_tol: float,
) -> tuple[dict[int, str], dict[str, Any]]:
    """
    부재명이 부위(구간) 행 바로 위에 모이는지(1·3개 후보) vs 부호 행에만(2개 등) 나뉘는지 세어
    좌→우 스트립과 X로 매칭한다. (도면 Y는 위로 갈수록 값이 커지는 경우를 가정)

    GH와의 대응: 유클리드 `merge_euclid_tol`(점 거리)로 조각 TEXT를 합친 뒤,
    `y_tol_cluster`·`x_gap_cluster`로 `groupNearbyTextsByY`식 Y→X 클러스터를 만든다.
    """
    dbg: dict[str, Any] = {"ok": False}
    if not data_items or not template or not strip_infos:
        return {}, dbg
    info_by_si = _strip_infos_by_index(strip_infos)
    if not info_by_si:
        return {}, dbg
    xcs = [float(s.get("x_center") or 0.0) for s in info_by_si.values()]
    if not xcs:
        return {}, dbg
    x_span = max(xcs) - min(xcs)
    x_pad = max(560.0, x_span * 0.24, 340.0)
    x_lo, x_hi = min(xcs) - x_pad, max(xcs) + x_pad

    ys_strip: list[float] = []
    for st in strips:
        for it in st or []:
            try:
                ys_strip.append(float(it["y"]))
            except (TypeError, KeyError, ValueError):
                continue
    if not ys_strip:
        return {}, dbg
    y_ref = float(_median_float(ys_strip))

    y_name = _beam_vertical_template_name_row_y_nearest(template, y_ref)
    if y_name is None:
        y_name = _beam_vertical_template_name_row_y(template)
    if y_name is None:
        return {}, dbg
    y_tpl_zone = _beam_vertical_template_zone_row_y_nearest(template, y_ref)
    if y_tpl_zone is None:
        zyf: list[float] = []
        for it in data_items:
            t = str(it.get("text") or "").strip()
            if not _beam_vertical_zone_from_label(t):
                continue
            try:
                xf = float(it["x"])
                yf = float(it["y"])
            except (TypeError, KeyError, ValueError):
                continue
            if x_lo <= xf <= x_hi:
                zyf.append(yf)
        if zyf:
            y_tpl_zone = float(_median_float(zyf))
        else:
            y_tpl_zone = float(y_ref)

    zy_data: list[float] = []
    for it in data_items:
        t = str(it.get("text") or "").strip()
        if not _beam_vertical_zone_from_label(t):
            continue
        try:
            xf = float(it["x"])
            yf = float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if x_lo <= xf <= x_hi:
            zy_data.append(yf)
    zys = list(zy_data)
    zys.append(float(y_tpl_zone))
    y_zone_eff = float(_median_float(zys))

    vertical_span = max(80.0, abs(float(y_name) - float(y_zone_eff)))
    loose = max(88.0, float(band_tol) * 14.0)
    # GH 슬라이더 '부위-부재 범위인식 기준 거리/크기'에 해당: band_tol·스트립 폭·부위~부호 세로폭으로 스케일
    x_gap = max(128.0, float(band_tol) * 20.0, min(420.0, 0.09 * x_span))
    y_tol_pts = max(24.0, float(band_tol) * 4.2, min(78.0, 0.055 * vertical_span))
    euclid_tol = max(20.0, float(band_tol) * 3.25, min(60.0, 0.048 * vertical_span))

    mark_pts: list[tuple[float, float, str]] = []
    for it in data_items:
        t = str(it.get("text") or "").strip()
        if not _beam_vertical_text_looks_like_member_title(t):
            continue
        try:
            x = float(it["x"])
            y = float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if not (x_lo <= x <= x_hi):
            continue
        mark_pts.append((x, y, t))

    mark_pts = _beam_vertical_merge_mark_points_euclidean(mark_pts, euclid_tol)

    y_lo = min(y_zone_eff, y_name)
    y_hi = max(y_zone_eff, y_name)
    # 마진이 크면 부재명 통바(부호↔부위 사이)가 between 밴드에서 빠져 n_row만 남는 경우가 많음(GH 위치관계)
    margin = max(4.0, float(band_tol) * 1.05)
    inner_lo = y_lo + margin
    inner_hi = y_hi - margin
    between_pts: list[tuple[float, float, str]] = []
    if inner_hi > inner_lo + 12.0:
        for x, y, t in mark_pts:
            if inner_lo < y < inner_hi:
                between_pts.append((x, y, t))

    def _in_name_row_band_only(_x: float, y: float, _t: str) -> bool:
        if abs(y - float(y_name)) > loose:
            return False
        # 부호~부위 사이(between)에만 있는 텍스트는 row 쪽 개수에 넣지 않음 — between과 이중 집계되어
        # n_row==n_st가 깨지거나 한 클러스터로 뭉치는 경우가 있다.
        if inner_hi > inner_lo + 12.0 and inner_lo < y < inner_hi:
            return False
        return True

    row_pts = [(x, y, t) for x, y, t in mark_pts if _in_name_row_band_only(x, y, t)]
    between_cs = _beam_vertical_cluster_mark_tuples_by_y_then_x(between_pts, y_tol_pts, x_gap)
    row_cs = _beam_vertical_cluster_mark_tuples_by_y_then_x(row_pts, y_tol_pts, x_gap)
    n_between, n_row = len(between_cs), len(row_cs)

    order_si = sorted(
        range(len(strips)),
        key=lambda si: float((info_by_si.get(si) or {}).get("x_center") or 0.0),
    )
    order_si = [si for si in order_si if strips[si]]
    if not order_si:
        return {}, dbg
    xs_st = [float((info_by_si.get(si) or {}).get("x_center") or 0.0) for si in order_si]
    n_st = len(order_si)

    # 스트립 수(2·3열)와 부재명 클러스터 수가 같을 때: 같은 쪽을 우선(GH 부재-부위 위치 매칭)
    # — between만 1개인데 부호 행에 3개가 있으면 예전 로직은 between만 써서 전부 한 부재로 붙는 오류가 난다.
    # — 부호행·부위~부호 사이 클러스터 수가 동시에 스트립 수와 같으면, GH처럼 Y(기대 부착선)에 더 가까운 쪽을 택한다.
    primary: list[tuple[float, float, str]]
    source: str
    if n_st >= 2 and n_between == n_st and n_row == n_st and n_between >= 2:
        y_mid_band = 0.5 * (float(y_name) + float(y_zone_eff))
        row_y_med = float(_median_float([m[1] for m in row_cs])) if row_cs else float("nan")
        bet_y_med = float(_median_float([m[1] for m in between_cs])) if between_cs else float("nan")
        d_row = abs(row_y_med - float(y_name)) if row_cs else 1e18
        d_bet = abs(bet_y_med - y_mid_band) if between_cs else 1e18
        tie_slack = max(22.0, float(band_tol) * 3.2)
        if d_row + tie_slack < d_bet:
            primary, source = row_cs, "name_row_strip_count_match|count_tie_y"
        elif d_bet + tie_slack < d_row:
            primary, source = between_cs, "between_strip_count_match|count_tie_y"
        else:
            primary, source = row_cs, "name_row_strip_count_match|count_tie_default_row"
    elif n_st >= 2 and n_between == n_st and n_between >= 2:
        primary, source = between_cs, "between_strip_count_match"
    elif n_st >= 2 and n_row == n_st and n_row >= 2:
        primary, source = row_cs, "name_row_strip_count_match"
    elif n_between in (1, 3) and n_between > 0 and n_row <= 1:
        primary, source = between_cs, "between_zone_and_name"
    elif n_row == 2 and n_between == 0:
        primary, source = row_cs, "name_row_two_marks"
    elif n_row in (1, 3) and n_row > 0:
        primary, source = row_cs, "name_row"
    elif n_between == 2 and n_between > 0:
        primary, source = between_cs, "between_two_marks"
    else:
        primary, source = (between_cs if between_cs else row_cs), "fallback_between_or_row"

    if not primary:
        fb, fb_ex = _beam_vertical_infer_strip_member_fallback_nearest(
            data_items, order_si, info_by_si, float(y_name), x_lo, x_hi, loose
        )
        if fb:
            return fb, {
                "ok": True,
                "source": "fallback_nearest_x",
                "n_between": n_between,
                "n_row": n_row,
                "y_name": y_name,
                "y_zone_eff": round(y_zone_eff, 4),
                "y_ref": round(y_ref, 4),
                "x_lo": round(x_lo, 4),
                "x_hi": round(x_hi, 4),
                "n_strips": n_st,
                "n_mark_clusters": 0,
                "merge_euclid_tol": round(euclid_tol, 4),
                "y_tol_cluster": round(y_tol_pts, 4),
                "x_gap_cluster": round(x_gap, 4),
                **fb_ex,
            }
        return {}, dbg

    n_mk = len(primary)

    # 상단 한 줄 부재명이 여러 열(INT/CENTER)에 걸쳐 동일 문자열로만 찍힌 경우 → 모든 스트립에 동일 부재명(GH '부재-부위 위치 매칭')
    norms = {_beam_member_title_norm_for_infer(m[2]) for m in primary}
    if len(norms) == 1 and n_st >= 2:
        one = primary[0][2]
        dbg_out = {
            "ok": True,
            "source": f"{source}|same_title_all_strips",
            "n_between": n_between,
            "n_row": n_row,
            "y_name": y_name,
            "y_zone_eff": round(y_zone_eff, 4),
            "y_ref": round(y_ref, 4),
            "x_lo": round(x_lo, 4),
            "x_hi": round(x_hi, 4),
            "n_strips": n_st,
            "n_mark_clusters": n_mk,
            "merge_euclid_tol": round(euclid_tol, 4),
            "y_tol_cluster": round(y_tol_pts, 4),
            "x_gap_cluster": round(x_gap, 4),
        }
        return {si: one for si in order_si}, dbg_out

    prim_x = sorted(primary, key=lambda m: m[0])
    out: dict[int, str] = {}
    if n_mk == 1:
        m0 = prim_x[0][2]
        for si in order_si:
            out[si] = m0
    elif n_mk == n_st:
        for si, mc in zip(order_si, prim_x):
            out[si] = mc[2]
    elif n_mk == 2 and n_st == 3:
        m0, m1 = prim_x[0], prim_x[1]
        for j, si in enumerate(order_si):
            xc = xs_st[j]
            if j == 0:
                out[si] = m0[2]
            elif j == n_st - 1:
                out[si] = m1[2]
            else:
                out[si] = m0[2] if abs(xc - m0[0]) <= abs(xc - m1[0]) else m1[2]
    elif n_mk > n_st:
        pool = list(prim_x)
        for j, si in enumerate(order_si):
            xc = xs_st[j]
            best = min(pool, key=lambda m: abs(m[0] - xc))
            out[si] = best[2]
            pool.remove(best)
    else:
        for j, si in enumerate(order_si):
            xc = xs_st[j]
            best = min(primary, key=lambda m: abs(m[0] - xc))
            out[si] = best[2]

    dbg = {
        "ok": True,
        "source": source,
        "n_between": n_between,
        "n_row": n_row,
        "y_name": y_name,
        "y_zone_eff": round(y_zone_eff, 4),
        "y_ref": round(y_ref, 4),
        "x_lo": round(x_lo, 4),
        "x_hi": round(x_hi, 4),
        "n_strips": n_st,
        "n_mark_clusters": n_mk,
        "merge_euclid_tol": round(euclid_tol, 4),
        "y_tol_cluster": round(y_tol_pts, 4),
        "x_gap_cluster": round(x_gap, 4),
    }
    return out, dbg


def _split_beam_vertical_dyn_by_zone_spans(
    sub_dyn: dict[str, str],
    sub_t: list[tuple[float, str, str]],
) -> list[tuple[dict[str, str], list[tuple[float, str, str]]]]:
    """
    한 부호 블록 안에 구간 행(INT/END/ALL…)이 여러 번 나오면 부위별로 하위 템플릿을 나눈다.
    두 번째 이후 구간에는 부호 행이 템플릿에 없을 수 있어, 첫 행(부호) 값을 carry 키로 앞에 붙인다.
    """
    if len(sub_t) < 2:
        return [(sub_dyn, sub_t)]
    zix = _beam_vertical_zone_header_indices(sub_t)
    if len(zix) < 2:
        return [(sub_dyn, sub_t)]

    bu_y, bu_lab, bu_k = sub_t[0]
    ns0 = bu_lab.replace(" ", "").replace("\u3000", "")
    if "부호" not in ns0 and "기호" not in ns0 and not re.fullmatch(r"(?i)name", (bu_lab or "").strip()):
        return [(sub_dyn, sub_t)]

    out: list[tuple[dict[str, str], list[tuple[float, str, str]]]] = []
    for j in range(len(zix)):
        z1 = zix[j + 1] if j + 1 < len(zix) else len(sub_t)
        if j == 0:
            piece_t = list(sub_t[0:z1])
        else:
            z0 = zix[j]
            carry_k = _slug_field_key(f"{bu_lab}_name_carry_{j}", 8000 + j)
            piece_t = [(bu_y, bu_lab, carry_k)] + list(sub_t[z0:z1])
        keys = [k for _y, _l, k in piece_t]
        piece_dyn: dict[str, str] = {}
        for k in keys:
            if j > 0 and k == piece_t[0][2]:
                piece_dyn[k] = str(sub_dyn.get(bu_k, "") or "").strip()
            else:
                piece_dyn[k] = str(sub_dyn.get(k, "") or "").strip()
        if not any(piece_dyn.values()):
            continue
        out.append((piece_dyn, piece_t))
    return out if out else [(sub_dyn, sub_t)]


def _beam_vertical_row_kind_from_title(lab: str) -> str | None:
    """한글·영문 좌측 라벨 → 행 종류."""
    raw = (lab or "").strip()
    if not raw:
        return None
    c = _beam_lab_compact(raw).lower()
    ns = raw.replace(" ", "").replace("\u3000", "")
    if "부호" in ns or "기호" in ns or re.fullmatch(r"(?i)name", raw.strip()):
        return "name"
    if _beam_vertical_zone_from_label(raw):
        return None
    if "하부" in ns or re.search(r"하\s*부\s*근", raw):
        return "bot_bar"
    if "상부" in ns or re.search(r"상\s*부\s*근", raw):
        return "top_bar"
    if "스트럽" in ns or "스터럽" in raw or "띠장" in raw:
        return "stirrup_bar"
    if "표피" in ns:
        return "side_bar"
    if re.search(r"^depth$|높이|단면고", c, re.I) or ("depth" in c and "bar" not in c) or "단면고" in ns:
        return "depth_mm"
    if re.search(r"^width$|폭", c, re.I) or ("width" in c and "bar" not in c):
        return "width_mm"
    if "형태" in ns or ("단면" in ns and "단면고" not in ns):
        return "section"
    if "type" in c and len(c) <= 14:
        return "type"
    if "material" in c or "재료" in ns:
        return "material"
    return None


def _beam_apply_bar_to_zones(
    rec: dict[str, Any],
    zone: str | None,
    bar_kind: str,
    val: str,
) -> None:
    """bar_kind: top_bar | bot_bar | stirrup_bar → int_top_bar …"""
    v = (val or "").strip()
    if not v:
        return
    suffix = bar_kind  # int_top_bar uses _top_bar
    key_body = f"_{suffix}"
    if zone == "both":
        for z in ("int", "ext"):
            rec[f"{z}{key_body}"] = v
    elif zone == "all":
        for z in ("int", "cen", "ext"):
            rec[f"{z}{key_body}"] = v
    elif zone in ("int", "cen", "ext"):
        rec[f"{zone}{key_body}"] = v
    else:
        rec[f"cen{key_body}"] = v


def _beam_template_row_to_field_key(lab: str) -> str | None:
    """좌측 세로 라벨 → beam 레코드 필드 (영문·와이드 표 호환)."""
    raw = (lab or "").strip()
    if not raw:
        return None
    if _beam_vertical_zone_from_label(raw):
        return None
    vk = _beam_vertical_row_kind_from_title(raw)
    if vk == "name":
        return "name"
    if vk == "section":
        return "SECTION"
    if vk == "top_bar":
        return "cen_top_bar"
    if vk == "bot_bar":
        return "cen_bot_bar"
    if vk == "stirrup_bar":
        return "cen_stirrup_bar"
    if vk == "side_bar":
        return "side_bar"
    c = _beam_lab_compact(raw).lower()
    if "type" in c and len(c) <= 12:
        return "type"
    if "material" in c or "재료" in raw or "bystory" in c:
        return "material"
    if re.search(r"^width$|폭|비", c, re.I) or ("width" in c and "bar" not in c):
        return "width_mm"
    if re.search(r"^depth$|높이|단면고|에이치", c, re.I) or ("depth" in c and "bar" not in c):
        return "depth_mm"
    wk = _beam_wide_header_score(raw)
    if wk:
        return wk
    nk = _beam_narrow_header_score(raw)
    if nk and nk != "name":
        return nk
    return None


def _enrich_beam_vertical_record(rec: dict[str, Any]) -> None:
    titles = rec.get("beam_field_titles") or {}
    order = rec.get("beam_field_key_order") or []
    cells = [str(c or "") for c in (rec.get("cells") or [])]
    while len(cells) < len(order):
        cells.append("")

    zone: str | None = None
    for i, k in enumerate(order):
        if i >= len(cells):
            break
        tit = str(titles.get(k) or "").strip()
        val = str(cells[i] or "").strip()
        if _is_standalone_section_mm_label(tit):
            continue
        loc = _beam_vertical_zone_from_label(tit)
        if loc:
            zone = loc
            continue
        rk = _beam_vertical_row_kind_from_title(tit)
        if rk == "name" and val:
            base, wmm, hmm = _split_beam_mark_and_dim_parens(val)
            rec["member_label"] = val.strip()
            rec["mark"] = (base or val).strip()
            rec["name"] = (base or val).strip()
            if wmm is not None and hmm is not None:
                rec["width_mm"] = wmm
                rec["depth_mm"] = hmm
                rec["size_mm"] = [wmm, hmm]
                rec["SIZE"] = f"{wmm}x{hmm}"
        elif rk == "section" and val:
            rec["SECTION"] = val
        elif rk == "top_bar":
            _beam_apply_bar_to_zones(rec, zone, "top_bar", val)
        elif rk == "bot_bar":
            _beam_apply_bar_to_zones(rec, zone, "bot_bar", val)
        elif rk == "stirrup_bar":
            _beam_apply_bar_to_zones(rec, zone, "stirrup_bar", val)
        elif rk == "side_bar" and val:
            rec["side_bar"] = val
        elif rk == "width_mm" and val:
            pn = _parse_int_mm(val)
            rec["width_mm"] = pn if pn is not None else val
        elif rk == "depth_mm" and val:
            pn = _parse_int_mm(val)
            rec["depth_mm"] = pn if pn is not None else val

    def cell_for_field(fk: str) -> str:
        for j, kk in enumerate(order):
            if j >= len(cells):
                break
            t = str(titles.get(kk) or "")
            if _beam_template_row_to_field_key(t) == fk or _beam_template_row_to_field_key(str(kk or "")) == fk:
                return str(cells[j] or "").strip()
        return ""

    w = rec.get("width_mm")
    d = rec.get("depth_mm")
    if not (isinstance(w, int) and isinstance(d, int)):
        w_s, d_s = cell_for_field("width_mm"), cell_for_field("depth_mm")
        if w_s and not isinstance(rec.get("width_mm"), int):
            pn = _parse_int_mm(w_s)
            if pn is not None:
                rec["width_mm"] = pn
        if d_s and not isinstance(rec.get("depth_mm"), int):
            pn = _parse_int_mm(d_s)
            if pn is not None:
                rec["depth_mm"] = pn
    w2, d2 = rec.get("width_mm"), rec.get("depth_mm")
    if isinstance(w2, int) and isinstance(d2, int):
        rec["size_mm"] = [w2, d2]
        rec["SIZE"] = f"{w2}x{d2}"

    for fk in BEAM_WIDE_KEYS_ORDER:
        if fk in ("name", "width_mm", "depth_mm", "SECTION"):
            continue
        v = cell_for_field(fk)
        if v and not (rec.get(fk) or "").strip():
            rec[fk] = v

    sig = _merge_signals([c for c in cells if c])
    _beam_skip_sig = set(BEAM_WIDE_KEYS_ORDER) | {
        "SECTION",
        "mark",
        "name",
        "member_label",
        "size_mm",
        "cells",
        "raw",
    }
    for kk, vv in sig.items():
        if kk in ("cells", "raw") or kk in _beam_skip_sig:
            continue
        if not str(rec.get(kk) or "").strip():
            rec[kk] = vv


def _record_from_beam_vertical_dynamic(
    dyn: dict[str, str],
    template: list[tuple[float, str, str]],
    template_source: str,
    cfg: ExtractionConfig,
    *,
    strip_bands: list[tuple[float, str]] | None = None,
) -> dict[str, Any]:
    titles = {k: lab for _y, lab, k in template}
    key_order = [k for _y, _lab, k in template]
    flat_vals = [str(dyn.get(k) or "").strip() for k in key_order]
    rec: dict[str, Any] = {
        "category": CATEGORY_BEAM,
        "wall_mode": cfg.wall_mode,
        "beam_layout": "vertical_blocks",
        "beam_row_role": "beam_vertical_block",
        "beam_template_source": template_source,
        "beam_field_titles": titles,
        "beam_field_key_order": key_order,
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
    _enrich_beam_vertical_record(rec)
    zdl = _beam_vertical_zone_display_label(template)
    if zdl:
        rec["beam_vertical_zone_display_label"] = zdl
    if strip_bands:
        z_from_strip = _beam_vertical_zone_display_label_from_bands(strip_bands, template)
        if z_from_strip:
            rec["beam_vertical_zone_display_label"] = z_from_strip
    for rk in list(rec.keys()):
        if isinstance(rk, str) and "_name_carry_" in rk:
            rec.pop(rk, None)
    ys = [float(t[0]) for t in template]
    if ys:
        rec["row_y_mean"] = round(sum(ys) / len(ys), 4)
    # 단면 enrich: column_section_geometry 가 row_section_anchor_y 를 최우선으로 쓴다.
    # 없으면 row_data_anchor_y(스트립 텍스트 Y중앙)로 떨어져 상부근·치수 쪽으로 클러스터가 밀린다.
    sec_ay = _beam_vertical_row_section_anchor_y(template)
    if sec_ay is not None:
        rec["row_section_anchor_y"] = round(float(sec_ay), 4)
    return rec


def _beam_vertical_record_useful(rec: dict[str, Any]) -> bool:
    if (rec.get("mark") or rec.get("name") or "").strip():
        return True
    for fk in (
        "int_top_bar",
        "cen_top_bar",
        "ext_top_bar",
        "int_bot_bar",
        "cen_bot_bar",
        "ext_bot_bar",
        "int_stirrup_bar",
        "cen_stirrup_bar",
        "ext_stirrup_bar",
        "side_bar",
    ):
        if (rec.get(fk) or "").strip():
            return True
    return False


def _beam_vertical_strip_x_center(strip: list[dict[str, Any]]) -> float:
    xs = [float(it["x"]) for it in strip if it.get("x") is not None]
    return sum(xs) / len(xs) if xs else 0.0


def _beam_vertical_merge_identity(rec: dict[str, Any]) -> str:
    """가로로 인접한 서로 다른 부재를 (seg_i,zs_i) 병합에서 구분하기 위한 정규화 부호."""
    for k in ("mark", "name", "member_label"):
        v = str(rec.get(k) or "").strip()
        if v:
            base, _w, _h = _split_beam_mark_and_dim_parens(v)
            lab = (base or v).strip()
            return re.sub(r"[\s\u3000]+", "", lab).upper()
    return ""


def _beam_vertical_split_pending_by_member_mark(
    lst: list[tuple[float, dict[str, Any]]],
) -> list[list[tuple[float, dict[str, Any]]]]:
    """
    같은 (seg_i, zs_i) 안에서도 X로 나란한 부재(RG11 옆 RG12 등)는 부호가 바뀌면 별도 행으로 나눈다.
    부호가 비어 있는 열(INT/CENTER 등)은 왼쪽에서 온 부호를 채운 뒤, 부호 정규값이 바뀌는 지점에서 끊는다.
    """
    if not lst:
        return []
    srt = sorted(lst, key=lambda t: float(t[0]))
    carry_disp = ""
    carry_norm = ""
    filled: list[tuple[float, dict[str, Any]]] = []
    for xc, rec in srt:
        r = dict(rec)
        mid0 = _beam_vertical_merge_identity(r)
        if mid0:
            src = str(r.get("member_label") or r.get("mark") or r.get("name") or "").strip()
            if src:
                carry_disp = src
            carry_norm = mid0
        elif carry_disp:
            r["member_label"] = carry_disp
            base, _w, _h = _split_beam_mark_and_dim_parens(carry_disp)
            r["mark"] = (base or carry_disp).strip()
            r["name"] = r["mark"]
        filled.append((xc, r))

    runs: list[list[tuple[float, dict[str, Any]]]] = []
    cur: list[tuple[float, dict[str, Any]]] = []
    prev_norm: str | None = None
    for xc, r in filled:
        mid = _beam_vertical_merge_identity(r)
        if not mid:
            mid = prev_norm if prev_norm is not None else f"__orphan_{round(float(xc))}"
        if prev_norm is not None and mid != prev_norm and cur:
            runs.append(cur)
            cur = []
        prev_norm = mid
        cur.append((xc, r))
    if cur:
        runs.append(cur)
    return runs


def _beam_vertical_row_section_anchor_y(
    tpl_rows: list[tuple[float, str, str]],
) -> float | None:
    """좌측 '형태' 등 단면 행 Y — 단면 원·치수 클러스터 세로 중심에 맞춘다."""
    ys: list[float] = []
    for y, lab, k in tpl_rows:
        if header_row_is_section_geometry_anchor(lab, k):
            try:
                ys.append(float(y))
            except (TypeError, ValueError):
                continue
    if not ys:
        return None
    return round(sum(ys) / len(ys), 4)


def _beam_vertical_strip_y_median(strip: list[dict[str, Any]]) -> float | None:
    ys: list[float] = []
    for it in strip or []:
        try:
            ys.append(float(it["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not ys:
        return None
    return float(_median_float(ys))


def _beam_vertical_template_section_y_for_strip(
    template: list[tuple[float, str, str]],
    strip: list[dict[str, Any]],
    *,
    xc_focus: float | None = None,
    x_gate: float = 4500.0,
    neighbor_strips: list[list[dict[str, Any]]] | None = None,
) -> float | None:
    """스트립 우측 표 문자의 위쪽(Y 큰 값)을 기준으로, 그보다 아래(작은 Y)에 있는 '형태' Y 중 가장 위(max)를 고른다.

    스트립에 층이 세로로 여러 번 있으면 전체 Y중앙값이 아래쪽으로 가며
    `_beam_vertical_template_y_nearest_strip` 가 잘못된 층 형태(예: 37205)를 고르는 경우가 많다.

    INT 등 맨 왼쪽 열만 보면 부호·부재명이 다른 열보다 적어 max(Y)가 형태 행보다 낮게 잡히는 경우가 있다.
    이때는 **좌우 인접 스트립** 문자까지 포함해 같은 표 행의 상단 Y를 잡는다.
    """
    cand: list[float] = []
    for y, lab, k in template or []:
        if not header_row_is_section_geometry_anchor(lab, k):
            continue
        try:
            cand.append(float(y))
        except (TypeError, ValueError):
            continue
    if not cand:
        return None
    cand.sort()
    gate = max(2200.0, float(x_gate))
    pools: list[list[dict[str, Any]]] = [strip]
    if neighbor_strips:
        pools.extend(s for s in neighbor_strips if s)
    # 인접 열은 X가 달라도 같은 행의 부호/철근이 있으므로 xc 기준 게이트를 넓힌다.
    mult = 1.72 if len(pools) > 1 else 1.0
    gate_collect = max(gate * mult, 2800.0)
    ys_focus: list[float] = []
    for pool in pools:
        for it in pool or []:
            try:
                xf = float(it["x"])
                yf = float(it["y"])
            except (TypeError, KeyError, ValueError):
                continue
            if xc_focus is not None and abs(xf - float(xc_focus)) > gate_collect:
                continue
            ys_focus.append(yf)
    if ys_focus:
        y_top = float(max(ys_focus))
        below = [c for c in cand if c < y_top - 60.0]
        if below:
            return round(float(max(below)), 4)
    y_mid = _beam_vertical_strip_y_median(strip)
    if y_mid is None:
        return round(float(sum(cand) / len(cand)), 4)
    return round(float(min(cand, key=lambda yy: abs(yy - y_mid))), 4)


def _beam_vertical_template_y_nearest_strip(
    template: list[tuple[float, str, str]],
    strip: list[dict[str, Any]],
    *,
    row_ok,
) -> float | None:
    """
    좌측 템플릿이 세로로 여러 번 반복될 때, 전역 평균 Y 대신 **이 스트립 문자의 세로 중심에 가장 가까운** 템플릿 행 Y.
    (매칭선 y_strip_floor·부위점 필터가 전역 형태 Y에 맞춰져 CENTER/ALL 등을 전부 걸러 내는 것 방지)
    """
    cand: list[float] = []
    for y, lab, k in template or []:
        if not row_ok(lab, k):
            continue
        try:
            cand.append(float(y))
        except (TypeError, ValueError):
            continue
    if not cand:
        return None
    y_mid = _beam_vertical_strip_y_median(strip)
    if y_mid is None:
        return round(float(sum(cand) / len(cand)), 4)
    return round(float(min(cand, key=lambda yy: abs(yy - y_mid))), 4)


def _beam_vertical_template_median_row_step(
    tpl_rows: list[tuple[float, str, str]],
) -> float:
    """좌측 템플릿 줄 사이 |ΔY| 중앙값 — 단면 세로 패딩 상한 추정에 사용."""
    ys: list[float] = []
    for y, _lab, _k in tpl_rows:
        try:
            ys.append(float(y))
        except (TypeError, ValueError):
            continue
    if len(ys) < 2:
        return max(80.0, float(len(tpl_rows)) * 10.0)
    ys_sorted = sorted(ys, reverse=True)
    gaps = [abs(ys_sorted[i] - ys_sorted[i + 1]) for i in range(len(ys_sorted) - 1)]
    return max(40.0, float(_median_float(gaps)))


def _beam_vertical_cap_row_y_span_to_one_schedule_block(
    y_lo: float,
    y_hi: float,
    tpl_rows: list[tuple[float, str, str]],
    band_tol: float,
) -> tuple[float, float]:
    """세로 창이 템플릿 한 칸(부호~표피)보다 크면 인접 층 줄이 섞인 것 — 형태 행 중심으로 한 줄 높이로 수축."""
    if y_hi <= y_lo + 40.0:
        return y_lo, y_hi
    med_step = _beam_vertical_template_median_row_step(tpl_rows)
    cap = med_step * 4.68 + max(185.0, float(band_tol) * 7.5)
    cap = max(880.0, min(cap, 1820.0))
    span = y_hi - y_lo
    if span <= cap:
        return y_lo, y_hi
    sec_y = _beam_vertical_row_section_anchor_y(tpl_rows)
    if sec_y is not None:
        cy = float(sec_y)
        half = cap * 0.5
        return cy - half, cy + half
    ym = 0.5 * (y_lo + y_hi)
    half = cap * 0.5
    return ym - half, ym + half


def _beam_vertical_section_geom_y_bounds(
    tpl_rows: list[tuple[float, str, str]],
    band_tol: float,
) -> tuple[float, float] | None:
    """
    단면 클러스터용 세로 창: **형태(SECTION) 행**을 중심으로, 같은 조각 안에서
    바로 위(부호·구간)·바로 아래(상부근…) 줄까지만 넓힌다.
    상·하·스트럽 전체를 한 번에 넣으면 행간이 커서 윗줄 부재 단면과 Y로 겹친다.
    """
    if not tpl_rows:
        return None
    med_step = _beam_vertical_template_median_row_step(tpl_rows)

    sec_ys: list[float] = []
    for y, lab, k in tpl_rows:
        if header_row_is_section_geometry_anchor(lab, k):
            try:
                sec_ys.append(float(y))
            except (TypeError, ValueError):
                continue
    if sec_ys:
        # 도면 Y가 위로 갈수록 값이 커지는 경우(일람 추출 샘플) 기준
        y_top = max(sec_ys)
        y_bot = min(sec_ys)
        above = [float(y) for y, _, _ in tpl_rows if float(y) > y_top + 1e-3]
        below = [float(y) for y, _, _ in tpl_rows if float(y) < y_bot - 1e-3]

        pad_u = max(50.0, min(med_step * 1.05, 360.0))
        # 아래로 과도하게 넓히면 상부근·다음 부재 행까지 search 창이 세로로 늘어나 단면 박스가 부자연스럽다.
        pad_d = max(52.0, min(med_step * 1.22, 460.0))

        if above:
            y_hi = max(above) + pad_u
            y_hi = min(y_hi, y_top + med_step * 3.5 + max(70.0, float(band_tol) * 9.0))
        else:
            y_hi = y_top + max(110.0, med_step * 2.2)

        if below:
            closest_below = max(below)
            y_lo = closest_below - pad_d
            y_lo = max(y_lo, y_bot - med_step * 2.55 - max(45.0, float(band_tol) * 6.0))
        else:
            y_lo = y_bot - max(140.0, med_step * 2.45)

        if y_hi <= y_lo + 90.0:
            y_hi = y_lo + max(380.0, med_step * 3.6)
        y_lo, y_hi = _beam_vertical_cap_row_y_span_to_one_schedule_block(y_lo, y_hi, tpl_rows, band_tol)
        return y_lo, y_hi

    ys_bar: list[float] = []
    for y, lab, k in tpl_rows:
        raw = (lab or "").strip()
        ns = raw.replace(" ", "").replace("\u3000", "")
        if "상부" in ns or re.search(r"상\s*부\s*근", raw):
            ys_bar.append(float(y))
        elif "하부" in ns or re.search(r"하\s*부\s*근", raw):
            ys_bar.append(float(y))
        elif "스트럽" in ns or "스터럽" in raw or "띠장" in raw:
            ys_bar.append(float(y))
    if len(ys_bar) >= 2:
        pad = max(float(band_tol) * 3.5, 55.0)
        y_lo, y_hi = min(ys_bar) - pad, max(ys_bar) + pad
        return _beam_vertical_cap_row_y_span_to_one_schedule_block(y_lo, y_hi, tpl_rows, band_tol)
    if len(ys_bar) == 1:
        pad = max(float(band_tol) * 6.0, 70.0)
        v = ys_bar[0]
        y_lo, y_hi = v - pad, v + pad
        return _beam_vertical_cap_row_y_span_to_one_schedule_block(y_lo, y_hi, tpl_rows, band_tol)
    return None


def _beam_vertical_filter_diagram_mm_bands(
    bands: list[tuple[float, str]],
    template: list[tuple[float, str, str]],
) -> list[tuple[float, str]]:
    """단면(형태) 도형 안의 900·1000 등 단독 mm 텍스트가 인접 행으로 잡히지 않게 제거."""
    if not bands or not template:
        return bands
    tpl_ys = [(float(y), str(lab or "")) for y, lab, _k in template]
    out: list[tuple[float, str]] = []
    for y_val, raw in bands:
        t = (raw or "").strip()
        if not re.fullmatch(r"\d{3,4}", t):
            out.append((y_val, raw))
            continue
        try:
            v = int(t)
        except ValueError:
            out.append((y_val, raw))
            continue
        if not (200 <= v <= 4000):
            out.append((y_val, raw))
            continue
        yf = float(y_val)
        _, nearest_lab = min(tpl_ys, key=lambda p: abs(p[0] - yf))
        ns = (nearest_lab or "").replace(" ", "").replace("\u3000", "")
        if "형태" in ns or ("단면" in ns and "단면고" not in ns):
            continue
        if "SECTION" in nearest_lab.upper().replace(" ", ""):
            continue
        out.append((y_val, raw))
    return out


def _beam_vertical_resplit_strips_by_distinct_zone_columns(
    strips: list[list[dict[str, Any]]],
    template: list[tuple[float, str, str]],
    band_tol: float,
) -> tuple[list[list[dict[str, Any]]], list[dict[str, Any]]]:
    """
    좁은 열 병합 뒤 한 스트립에 END(INT)·CENTER·END(EXT) 등 서로 다른 부위 헤더가
    X로 떨어져 있으면, 가장 큰 X 공극(세로 구분선 부근)으로 열을 다시 나눈다.
    한 번에 한 분할만 적용하고, 바뀌는 동안 반복해 3열 이상도 처리한다.
    """
    if not template or not strips:
        return strips, []
    min_gap = max(120.0, float(band_tol) * 15.0)
    min_side = max(3, int(len(template) // 3))
    log: list[dict[str, Any]] = []
    out = strips
    for _pass in range(8):
        changed = False
        nxt: list[list[dict[str, Any]]] = []
        for st in out:
            if not st or len(st) < min_side * 2:
                nxt.append(st)
                continue
            xs = sorted(float(it["x"]) for it in st)
            gaps = [(xs[i + 1] - xs[i], i) for i in range(len(xs) - 1)]
            if not gaps:
                nxt.append(st)
                continue
            gmax, imax = max(gaps, key=lambda t: t[0])
            if gmax < min_gap:
                nxt.append(st)
                continue
            split_x = 0.5 * (xs[imax] + xs[imax + 1])
            left = [it for it in st if float(it["x"]) < split_x]
            right = [it for it in st if float(it["x"]) >= split_x]
            if len(left) < min_side or len(right) < min_side:
                nxt.append(st)
                continue
            bl = _merge_strip_into_y_bands(left, band_tol)
            br = _merge_strip_into_y_bands(right, band_tol)
            zl = _beam_vertical_zone_display_label_from_bands(bl, template)
            zr = _beam_vertical_zone_display_label_from_bands(br, template)
            if (
                zl
                and zr
                and str(zl).strip().upper() != str(zr).strip().upper()
            ):
                nxt.append(left)
                nxt.append(right)
                changed = True
                log.append(
                    {
                        "kind": "zone_column_resplit",
                        "split_x": round(split_x, 4),
                        "zone_left": str(zl).strip(),
                        "zone_right": str(zr).strip(),
                        "gmax": round(gmax, 4),
                    }
                )
            else:
                nxt.append(st)
        out = nxt
        if not changed:
            break
    return out, log


def _beam_vertical_max_row_y_bounds_span_world(
    field_headers: list[dict[str, Any]] | None,
) -> float:
    """형태 행 간격 기반 — 병합·클럼프 시 한 단면 세로창 상한."""
    if field_headers:
        return max(780.0, _field_headers_y_median_gap(field_headers) * 4.2)
    return max(780.0, 315.0 * 4.2)


def _beam_vertical_bind_row_bounds_to_seg_section_y(
    z_t: list[tuple[float, str, str]],
    y_lo: float,
    y_hi: float,
    band_tol: float,
) -> tuple[float, float]:
    """이 세그먼트(z_t)의 형태 행 Y에 맞춰 세로창을 한 줄 폭으로 고정(인접 층 줄과 분리)."""
    sec_line = _beam_vertical_row_section_anchor_y(z_t)
    if sec_line is None:
        return y_lo, y_hi
    med = _beam_vertical_template_median_row_step(z_t)
    ms = max(720.0, min(med * 4.38 + float(band_tol) * 5.5, 1380.0))
    return _beam_vertical_clamp_y_bounds_to_anchor(y_lo, y_hi, float(sec_line), ms)


def _beam_vertical_clamp_y_bounds_to_anchor(
    y_lo: float,
    y_hi: float,
    anchor_y: float,
    max_span: float,
) -> tuple[float, float]:
    if y_hi <= y_lo + 40.0:
        return y_lo, y_hi
    span = y_hi - y_lo
    if span <= max_span:
        return y_lo, y_hi
    half = max_span * 0.5
    cy = float(anchor_y)
    return cy - half, cy + half


def _merge_beam_vertical_records_across_strips(
    recs: list[dict[str, Any]],
    field_headers: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """같은 세그먼트·구간의 X 인접 스트립(예: INT 열 + CENTER 열)을 왼쪽→오른쪽으로 합친다."""
    if not recs:
        return {}
    if len(recs) == 1:
        one = dict(recs[0])
        si = one.get("beam_strip_index")
        if si is not None and one.get("beam_vertical_merged_strip_indices") is None:
            one["beam_vertical_merged_strip_indices"] = [int(si)]
        lab0 = str(one.get("beam_vertical_zone_display_label") or "").strip()
        if lab0 and si is not None:
            try:
                one["beam_vertical_zone_label_by_strip"] = {str(int(si)): lab0}
            except (TypeError, ValueError):
                pass
        pick0 = (
            str(one.get("beam_vertical_inferred_mark") or "").strip()
            or str(one.get("member_label") or "").strip()
            or str(one.get("mark") or "").strip()
            or str(one.get("name") or "").strip()
        )
        if pick0 and si is not None:
            try:
                one["beam_vertical_mark_by_strip"] = {str(int(si)): pick0}
            except (TypeError, ValueError):
                pass
        inf0 = str(one.get("beam_vertical_inferred_mark") or "").strip()
        if inf0 and si is not None:
            try:
                one["beam_vertical_inferred_mark_by_strip"] = {str(int(si)): inf0}
            except (TypeError, ValueError):
                pass
        max_span = _beam_vertical_max_row_y_bounds_span_world(field_headers)
        b0 = one.get("row_data_anchor_y_bounds")
        rsa0 = one.get("row_section_anchor_y")
        if isinstance(b0, list) and len(b0) >= 2 and rsa0 is not None:
            try:
                lo, hi = float(b0[0]), float(b0[1])
                lo2, hi2 = _beam_vertical_clamp_y_bounds_to_anchor(lo, hi, float(rsa0), max_span)
                one["row_data_anchor_y_bounds"] = [round(lo2, 4), round(hi2, 4)]
            except (TypeError, ValueError):
                pass
        return one

    base = dict(recs[0])
    strip_ix: list[int] = []
    for r in recs:
        si = r.get("beam_strip_index")
        if si is not None:
            strip_ix.append(int(si))
    base["beam_vertical_merged_strip_indices"] = sorted(set(strip_ix))
    base["beam_strip_index"] = min(strip_ix) if strip_ix else base.get("beam_strip_index")

    mark_keys = ("mark", "name", "member_label")
    for r in recs[1:]:
        for mk in mark_keys:
            if not str(base.get(mk) or "").strip() and str(r.get(mk) or "").strip():
                base[mk] = r[mk]
        for fk in BEAM_WIDE_KEYS_ORDER:
            if fk in mark_keys:
                continue
            va = str(base.get(fk) or "").strip()
            vb = str(r.get(fk) or "").strip()
            if vb and not va:
                base[fk] = r[fk]
        for fk in ("SECTION", "type", "material", "SIZE", "size_mm"):
            if not str(base.get(fk) or "").strip() and str(r.get(fk) or "").strip():
                base[fk] = r[fk]
        wa, wb = base.get("width_mm"), r.get("width_mm")
        if wa is None or (isinstance(wa, str) and not str(wa).strip()):
            if wb is not None and (not isinstance(wb, str) or str(wb).strip()):
                base["width_mm"] = wb
        da, db = base.get("depth_mm"), r.get("depth_mm")
        if da is None or (isinstance(da, str) and not str(da).strip()):
            if db is not None and (not isinstance(db, str) or str(db).strip()):
                base["depth_mm"] = db

    los: list[float] = []
    his: list[float] = []
    strip_y_bounds: dict[int, list[float]] = {}
    strip_sec_anchor: dict[int, float] = {}
    for r in recs:
        si_r = r.get("beam_strip_index")
        b = r.get("row_data_anchor_y_bounds")
        if isinstance(b, list) and len(b) >= 2:
            try:
                lo = float(b[0])
                hi = float(b[1])
                los.append(lo)
                his.append(hi)
                if si_r is not None:
                    try:
                        si_k = int(si_r)
                        strip_y_bounds[si_k] = [round(lo, 4), round(hi, 4)]
                    except (TypeError, ValueError):
                        pass
            except (TypeError, ValueError):
                pass
        vsec = r.get("row_section_anchor_y")
        if si_r is not None and vsec is not None:
            try:
                strip_sec_anchor[int(si_r)] = round(float(vsec), 4)
            except (TypeError, ValueError):
                pass
    max_span_m = _beam_vertical_max_row_y_bounds_span_world(field_headers)
    for sk in list(strip_y_bounds.keys()):
        pair = strip_y_bounds.get(sk)
        if not isinstance(pair, list) or len(pair) < 2:
            continue
        try:
            lo_m, hi_m = float(pair[0]), float(pair[1])
        except (TypeError, ValueError):
            continue
        ay_m = strip_sec_anchor.get(int(sk))
        if ay_m is not None and hi_m > lo_m + 40.0:
            lo_c, hi_c = _beam_vertical_clamp_y_bounds_to_anchor(lo_m, hi_m, float(ay_m), max_span_m)
            strip_y_bounds[int(sk)] = [round(lo_c, 4), round(hi_c, 4)]
    los = []
    his = []
    for r in recs:
        si_r2 = r.get("beam_strip_index")
        if si_r2 is None:
            continue
        try:
            sk2 = int(si_r2)
        except (TypeError, ValueError):
            continue
        pair2 = strip_y_bounds.get(sk2)
        if isinstance(pair2, list) and len(pair2) >= 2:
            try:
                los.append(float(pair2[0]))
                his.append(float(pair2[1]))
            except (TypeError, ValueError):
                pass
    if strip_y_bounds:
        base["beam_vertical_strip_row_y_bounds"] = {
            str(k): list(v) for k, v in sorted(strip_y_bounds.items())
        }
    if strip_sec_anchor:
        base["beam_vertical_strip_section_anchor_y"] = {
            str(k): v for k, v in sorted(strip_sec_anchor.items())
        }

    if los and his:
        lo_med = float(_median_float(los))
        hi_med = float(_median_float(his))
        # 같은 부호·세그먼트로 X만 다른 열: 한 열이 어긋나 합집합이 4중 블록 전체 세로로 늘어나는 경우 방지
        if lo_med + 120.0 < hi_med:
            base["row_data_anchor_y_bounds"] = [round(lo_med, 4), round(hi_med, 4)]
        else:
            lo_u, hi_u = min(los), max(his)
            lo_i, hi_i = max(los), min(his)
            span_u = hi_u - lo_u
            span_i = hi_i - lo_i if lo_i + 1e-3 < hi_i else 0.0
            if lo_i + 1e-3 < hi_i and span_i >= min(220.0, 0.42 * max(span_u, 1.0)):
                base["row_data_anchor_y_bounds"] = [round(lo_i, 4), round(hi_i, 4)]
            else:
                base["row_data_anchor_y_bounds"] = [round(lo_u, 4), round(hi_u, 4)]

    rsa_vals: list[float] = []
    for r in recs:
        v = r.get("row_section_anchor_y")
        if v is not None:
            try:
                rsa_vals.append(float(v))
            except (TypeError, ValueError):
                pass
    if rsa_vals:
        base["row_section_anchor_y"] = round(_median_float(rsa_vals), 4)

    # 스트립별 anc가 비어 클램프가 빠졌거나, 중앙값 anc 확정 후에도 행 전체 Y창이 과대한 경우
    rsa_fin = base.get("row_section_anchor_y")
    max_sp_fin = _beam_vertical_max_row_y_bounds_span_world(field_headers)
    brb = base.get("row_data_anchor_y_bounds")
    if isinstance(brb, list) and len(brb) >= 2 and rsa_fin is not None:
        try:
            lx, hx = float(brb[0]), float(brb[1])
            ly, hy = _beam_vertical_clamp_y_bounds_to_anchor(lx, hx, float(rsa_fin), max_sp_fin)
            base["row_data_anchor_y_bounds"] = [round(ly, 4), round(hy, 4)]
        except (TypeError, ValueError):
            pass
    syb_u = base.get("beam_vertical_strip_row_y_bounds")
    if isinstance(syb_u, dict) and rsa_fin is not None:
        anc_map = strip_sec_anchor if strip_sec_anchor else {}
        for ks, pv in list(syb_u.items()):
            if not isinstance(pv, (list, tuple)) or len(pv) < 2:
                continue
            try:
                ki = int(ks)
            except (TypeError, ValueError):
                continue
            try:
                lo_s, hi_s = float(pv[0]), float(pv[1])
            except (TypeError, ValueError):
                continue
            ay_s = anc_map.get(ki)
            if ay_s is None:
                ay_s = float(rsa_fin)
            if hi_s > lo_s + 40.0:
                lo_s, hi_s = _beam_vertical_clamp_y_bounds_to_anchor(lo_s, hi_s, float(ay_s), max_sp_fin)
            syb_u[str(ki)] = [round(lo_s, 4), round(hi_s, 4)]
        base["beam_vertical_strip_row_y_bounds"] = syb_u

    rda_vals: list[float] = []
    for r in recs:
        v = r.get("row_data_anchor_y")
        if v is not None:
            try:
                rda_vals.append(float(v))
            except (TypeError, ValueError):
                pass
    if rda_vals:
        base["row_data_anchor_y"] = round(_median_float(rda_vals), 4)

    by_si_zone: dict[int, str] = {}
    for r in recs:
        si = r.get("beam_strip_index")
        if si is None:
            continue
        try:
            si_i = int(si)
        except (TypeError, ValueError):
            continue
        lab = str(r.get("beam_vertical_zone_display_label") or "").strip()
        if lab:
            by_si_zone[si_i] = lab
    if by_si_zone:
        base["beam_vertical_zone_label_by_strip"] = {str(k): v for k, v in sorted(by_si_zone.items())}
        lo_si = min(by_si_zone.keys())
        base["beam_vertical_zone_display_label"] = by_si_zone[lo_si]

    by_si_mark: dict[int, str] = {}
    for r in recs:
        si = r.get("beam_strip_index")
        if si is None:
            continue
        try:
            si_i = int(si)
        except (TypeError, ValueError):
            continue
        pick = (
            str(r.get("beam_vertical_inferred_mark") or "").strip()
            or str(r.get("member_label") or "").strip()
            or str(r.get("mark") or "").strip()
            or str(r.get("name") or "").strip()
        )
        if pick:
            by_si_mark[si_i] = pick
    if by_si_mark:
        base["beam_vertical_mark_by_strip"] = {str(k): v for k, v in sorted(by_si_mark.items())}

    by_si_infer: dict[int, str] = {}
    for r in recs:
        si = r.get("beam_strip_index")
        if si is None:
            continue
        try:
            si_i = int(si)
        except (TypeError, ValueError):
            continue
        inf = str(r.get("beam_vertical_inferred_mark") or "").strip()
        if inf:
            by_si_infer[si_i] = inf
    if by_si_infer:
        base["beam_vertical_inferred_mark_by_strip"] = {
            str(k): v for k, v in sorted(by_si_infer.items())
        }
    return base


def _beam_vertical_strip_pick_nearest_member_mark(
    strip: list[dict[str, Any]],
    xc_strip: float,
    y_name_ref: float,
    y_slop: float,
) -> str:
    """스트립 안에서 부재명 후보 TEXT 중 (xc, 부호행 Y)에 가장 가까운 문자열."""
    best_t = ""
    best_d = 1e18
    y_gate = max(float(y_slop) * 1.65, 130.0)
    for it in strip or []:
        t = str(it.get("text") or "").strip()
        if not _beam_vertical_text_looks_like_member_title(t):
            continue
        try:
            x = float(it["x"])
            y = float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if abs(y - float(y_name_ref)) > y_gate:
            continue
        d = (x - float(xc_strip)) ** 2 + (y - float(y_name_ref)) ** 2
        if d < best_d:
            best_d = d
            best_t = t
    return best_t.strip()


def _beam_vertical_strip_trim_left_of_schedule_split(
    strips: list[list[dict[str, Any]]],
    boundary: float | None,
    split_mode: str,
    band_tol: float,
) -> list[list[dict[str, Any]]]:
    """
    left_ratio 등으로 경계가 잡혀도 값 열 TEXT가 x≤경계에 남는 경우가 있어
    주황 구분선 좌측 문자를 스트립에서 제외한다(부재명·치수 오염 방지).
    """
    if boundary is None or "data_fallback_all" in split_mode:
        return strips
    cut = float(boundary) + max(14.0, float(band_tol) * 2.5)
    out: list[list[dict[str, Any]]] = []
    for st in strips:
        if not st:
            out.append(st)
            continue
        filt = [it for it in st if float(it.get("x") or 0.0) > cut]
        out.append(filt if filt else st)
    return out


def _beam_vertical_member_zone_match_polyline(
    strip_index: int,
    strip: list[dict[str, Any]],
    band_tol: float,
    xc_strip: float,
    section_anchor_y: float,
    y_zone_ref: float,
    y_name_ref: float,
    name_loose: float,
    mark_inferred: str,
    template: list[tuple[float, str, str]] | None = None,
) -> dict[str, Any] | None:
    """
    뷰어용 폴리라인: **궤적 좌표**는 스트립 X중심 × (부호행·부위행·단면행의 기준 Y)만 쓴다.
    TEXT 실제 삽입 Y는 베이스라인 등으로 한 칸 아래 줄로 끌려가므로 선에 쓰지 않는다.
    `mark`·`zone_text`만 스트립 안 문자열 후보에서 고른다.
    세 점 연결 순서는 항상 **부호 TEXT → 부위 TEXT → 단면(열 X중심×형태 Y)**.
    `template`가 있으면 같은 블록 안에서만 부호·부위 후보를 고른다.
    """
    mk = str(mark_inferred or "").strip()
    if not mk:
        return None

    def _collect_xy(pool: list[dict[str, Any]], pred) -> list[tuple[float, float]]:
        acc: list[tuple[float, float]] = []
        for it in pool or []:
            t = str(it.get("text") or "").strip()
            if not t or not pred(t):
                continue
            try:
                acc.append((float(it["x"]), float(it["y"])))
            except (TypeError, KeyError, ValueError):
                continue
        return acc

    y_sec_f = float(section_anchor_y)
    # 부위·부호는 형태(단면) 행보다 위쪽(larger Y). 그보다 아래는 주근 행 등 — 매칭선이 내려가는 원인
    y_base_floor = y_sec_f - max(70.0, float(band_tol) * 11.0)
    y_seg_lo: float | None = None
    y_seg_hi: float | None = None
    tpl = template if template else []
    if tpl:
        segs = _segment_template_rows(tpl, band_tol)
        if segs:
            seg_ix = _pick_template_segment_for_y(y_sec_f, tpl, segs, band_tol)
            if seg_ix:
                inter = _template_segment_y_intervals(tpl, [seg_ix], band_tol)
                if inter:
                    y_seg_lo, y_seg_hi, _ = inter[0]

    y_low = max(y_base_floor, float(y_seg_lo)) if y_seg_lo is not None else y_base_floor

    def _y_accept(y: float) -> bool:
        if y < y_low:
            return False
        if y_seg_hi is not None and y > float(y_seg_hi) + 0.5:
            return False
        return True

    def _strip_above_section(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
        return [(x, y) for x, y in pts if _y_accept(float(y))]

    def _pick_near(
        pts: list[tuple[float, float]],
        x0: float,
        y_pref: float,
        y_slop: float,
        *,
        relax_y: bool,
    ) -> tuple[float, float] | None:
        if not pts:
            return None
        scored: list[tuple[float, float, float, float]] = []
        for x, y in pts:
            if abs(y - y_pref) <= y_slop:
                scored.append((abs(x - x0), abs(y - y_pref), x, y))
        if not scored and relax_y:
            scored = [(abs(x - x0), abs(y - y_pref), x, y) for x, y in pts]
        if not scored:
            return None
        scored.sort(key=lambda q: (q[0], q[1]))
        return scored[0][2], scored[0][3]

    z_slop = max(95.0, float(band_tol) * 16.0)
    zone_pts = _strip_above_section(
        _collect_xy(strip, lambda tx: _beam_vertical_zone_from_label(tx) is not None)
    )
    zp = _pick_near(zone_pts, float(xc_strip), float(y_zone_ref), z_slop, relax_y=True)
    if zp is None:
        zp = (float(xc_strip), float(y_zone_ref))
    zone_txt = ""
    best_d = 1e18
    for it in strip or []:
        t = str(it.get("text") or "").strip()
        if not t or _beam_vertical_zone_from_label(t) is None:
            continue
        try:
            x, y = float(it["x"]), float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if not _y_accept(y):
            continue
        d = (x - zp[0]) ** 2 + (y - zp[1]) ** 2
        if d < best_d:
            best_d = d
            zone_txt = t

    mi = mk
    m_norm = _beam_member_title_norm_for_infer(mi) if mi else ""

    title_pts: list[tuple[float, float, str]] = []
    for it in strip or []:
        t = str(it.get("text") or "").strip()
        if not _beam_vertical_text_looks_like_member_title(t):
            continue
        try:
            x, y = float(it["x"]), float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if not _y_accept(y):
            continue
        title_pts.append((x, y, t))

    def _dist2(px: float, py: float) -> float:
        dx = px - float(xc_strip)
        dy = py - float(y_name_ref)
        return dx * dx + dy * dy

    mp: tuple[float, float] | None = None
    # 폴리라인이 붙은 부재명 TEXT(시각·JSON `mark`·부재별 표가 동일 출처가 되게)
    poly_title = ""
    if title_pts:
        best_all = min(title_pts, key=lambda p: _dist2(p[0], p[1]))
        d_best = _dist2(best_all[0], best_all[1])
        if m_norm:
            same = [p for p in title_pts if _beam_member_title_norm_for_infer(p[2]) == m_norm]
            if same:
                best_same = min(same, key=lambda p: _dist2(p[0], p[1]))
                d_same = _dist2(best_same[0], best_same[1])
                slack = max(220.0**2, float(name_loose) ** 2 * 1.55)
                if d_same <= d_best + slack:
                    mp = (best_same[0], best_same[1])
                    poly_title = str(best_same[2]).strip()
                else:
                    mp = (best_all[0], best_all[1])
                    poly_title = str(best_all[2]).strip()
            else:
                mp = (best_all[0], best_all[1])
                poly_title = str(best_all[2]).strip()
        else:
            mp = (best_all[0], best_all[1])
            poly_title = str(best_all[2]).strip()
    if mp is None:
        def _mark_pred(tx: str) -> bool:
            if not _beam_vertical_text_looks_like_member_title(tx):
                return False
            return _beam_member_title_norm_for_infer(tx) == m_norm

        mark_pts = _strip_above_section(_collect_xy(strip, _mark_pred))
        mp = _pick_near(mark_pts, float(xc_strip), float(y_name_ref), name_loose, relax_y=False)
        if mp is None:
            mp = _pick_near(mark_pts, float(xc_strip), float(y_name_ref), name_loose * 1.45, relax_y=True)
    if mp is None:
        mp = (float(xc_strip), float(y_name_ref))

    mark_shown = poly_title if poly_title else mk
    if not poly_title and mp is not None and title_pts:
        _bt = ""
        _bd = 1e18
        for _x, _y, _t in title_pts:
            _d = (_x - mp[0]) ** 2 + (_y - mp[1]) ** 2
            if _d < _bd:
                _bd = _d
                _bt = _t
        _gate = max(600.0**2, float(name_loose) ** 2 * 4.0)
        if _bt and str(_bt).strip() and _bd <= _gate:
            mark_shown = str(_bt).strip()

    ys = float(section_anchor_y)
    # 부재·부위는 스트립에서 고른 **실제 TEXT 좌표**, 단면은 열 X중심×형태 행 Y.
    # 뷰어·내비는 항상 **부호 → 부위 → 단면** 순으로 한 갈래만 잇는다(템플릿 Y로 재정렬하면
    # 좌표계에 따라 꺾임·허공 선이 생길 수 있음).
    x_draw = float(xc_strip)
    pts = [
        [round(float(mp[0]), 4), round(float(mp[1]), 4)],
        [round(float(zp[0]), 4), round(float(zp[1]), 4)],
        [round(x_draw, 4), round(ys, 4)],
    ]
    return {
        "strip_index": int(strip_index),
        "mark": mark_shown,
        "mark_inferred": mk,
        "zone_text": zone_txt.strip(),
        "points": pts,
    }


def extract_beam_vertical_blocks(
    items: list[dict[str, Any]],
    cfg: ExtractionConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """기둥 세로 블록과 동일 기하: 좌측 라벨 + 우측 부재 열 → 보 필드로 매핑."""
    meta: dict[str, Any] = {
        "beam_vertical_split_mode": "none",
        "beam_label_entity_count": 0,
        "beam_data_entity_count": 0,
        "beam_vertical_split_boundary": None,
    }
    if not items:
        return [], meta

    band_tol = (
        float(cfg.beam_band_y_tol)
        if cfg.beam_band_y_tol is not None
        else float(cfg.column_band_y_tol or 4.0)
    )
    gap = float(cfg.column_strip_gap) if cfg.column_strip_gap is not None else None

    label_items, data_items, split_mode, boundary = _split_column_label_and_data(
        items,
        tight_label_column=True,
    )
    label_items, data_items = _demote_misassigned_label_entities(label_items, data_items)
    label_items, data_items = _demote_beam_zone_column_headers_from_labels(label_items, data_items)
    label_items, data_items = _demote_beam_standalone_mm_labels(label_items, data_items)
    meta["beam_vertical_split_mode"] = split_mode
    meta["beam_label_entity_count"] = len(label_items)
    meta["beam_vertical_split_boundary"] = boundary
    meta["beam_vertical_split_boundary_draw"] = None
    if boundary is not None:
        try:
            bx = float(boundary)
            if bx == bx:  # finite (not NaN)
                xs_d = [float(it["x"]) for it in data_items]
                span_d = (max(xs_d) - min(xs_d)) if xs_d else 0.0
                # 뷰어 주황선: 라벨 열 오른쪽 실제 격선에 맞추기 위해 소폭 우측 오프셋(GH/도면 맞춤)
                dx = max(28.0, float(band_tol) * 5.5, span_d * 0.0018)
                meta["beam_vertical_split_boundary_draw"] = bx + dx
        except (TypeError, ValueError):
            pass

    if len(data_items) == 0 and len(items) > 0:
        data_items = list(items)
        split_mode = split_mode + "|data_fallback_all"
        meta["beam_vertical_split_mode"] = split_mode
        meta["beam_vertical_data_fallback"] = True

    meta["beam_data_entity_count"] = len(data_items)

    template, tsrc = _discover_row_template_from_labels(label_items, band_tol, split_mode)
    if len(template) < 2 and label_items:
        template, tsrc = _discover_row_template_from_labels(
            label_items, band_tol * 1.25, split_mode + "_retry"
        )
    if template:
        template = _prune_beam_vertical_template(template)

    rows_out: list[dict[str, Any]] = []
    pending_merge: list[tuple[float, int, int, dict[str, Any]]] = []
    if gap is not None:
        strips = _cluster_items_by_x_gap(data_items, gap)
        meta["beam_strip_cluster_mode"] = "manual_gap"
    else:
        strips = _partition_data_items_into_member_strips(data_items)
        meta["beam_strip_cluster_mode"] = "auto_seed_partition"
    strips, strip_merge_log = _merge_narrow_adjacent_strips(strips)
    if strip_merge_log:
        meta["beam_vertical_strip_merges"] = strip_merge_log
    if template and strips:
        strips, resplit_log = _beam_vertical_resplit_strips_by_distinct_zone_columns(
            strips, template, band_tol
        )
        if resplit_log:
            meta["beam_vertical_zone_column_resplits"] = resplit_log
    strips = _beam_vertical_strip_trim_left_of_schedule_split(
        strips, boundary, split_mode, band_tol
    )
    strip_infos: list[dict[str, Any]] = []
    for si, strip in enumerate(strips):
        if not strip:
            continue
        xs = [float(it["x"]) for it in strip]
        xc = sum(xs) / len(xs)
        strip_infos.append({"index": si, "x_center": round(xc, 4), "entity_count": len(strip)})
    meta["beam_vertical_strips"] = strip_infos
    if template:
        meta["beam_vertical_field_headers"] = [
            {"key": k, "label": lab, "y": round(float(y), 4)} for y, lab, k in template
        ]
        meta["beam_vertical_template_segment_count"] = len(_segment_template_rows(template, band_tol))
    else:
        meta["beam_vertical_field_headers"] = []
        meta["beam_vertical_template_segment_count"] = 0

    infer_m: dict[int, str] = {}
    infer_dbg: dict[str, Any] = {}
    strip_member_override: dict[int, str] = {}
    if template and strip_infos and strips:
        infer_m, infer_dbg = _beam_vertical_infer_strip_member_map(
            data_items, template, strips, strip_infos, band_tol
        )
        strip_member_override = infer_m
        if infer_dbg.get("ok") or infer_m:
            meta["beam_vertical_member_layout_infer"] = infer_dbg

    for si, strip in enumerate(strips):
        if not strip:
            continue
        # 세로로 쌓인 셀을 한 덩어리로 묶지 않도록 기둥용 band_tol 보다 약하게(보 일람 전용)
        band_z = min(float(band_tol), max(2.05, float(band_tol) * 0.62))
        bands = _merge_strip_into_y_bands(strip, band_z)
        if len(template) >= 2:
            bands = _beam_vertical_filter_diagram_mm_bands(bands, template)
            bands_map = _beam_vertical_prepare_bands_for_template_map(bands, template)
            dyn = _map_data_bands_to_template(
                bands_map, template, band_tol, beam_vertical_schedule=True
            )
            if not any(dyn.values()):
                continue
            splits = _split_dyn_by_template_segments(dyn, template, band_tol)
            if splits:
                xc = _beam_vertical_strip_x_center(strip)
                for seg_i, (sub_dyn, sub_t, seg_indices) in enumerate(splits):
                    zone_parts = _split_beam_vertical_dyn_by_zone_spans(sub_dyn, sub_t)
                    for zs_i, (z_dyn, z_t) in enumerate(zone_parts):
                        om_pre = strip_member_override.get(si)
                        mk_hint = _beam_vertical_mark_hint_for_zone_dyn(
                            z_dyn, z_t, str(om_pre).strip() if om_pre else ""
                        )
                        y_sec_h = _beam_vertical_row_section_anchor_y(z_t)
                        ys_piece = [float(t[0]) for t in z_t]
                        if y_sec_h is not None:
                            y_cut = float(y_sec_h)
                        elif ys_piece:
                            y_cut = float(min(ys_piece))
                        else:
                            y_med_st = _beam_vertical_strip_y_median(strip)
                            y_cut = float(y_med_st) if y_med_st is not None else 0.0
                        x_gate_nm = max(4500.0, float(band_tol) * 18.0)
                        y_next_m = _beam_vertical_next_other_member_row_y_in_strip(
                            strip, xc, y_cut, mk_hint, x_gate=x_gate_nm
                        )
                        strip_read: list[dict[str, Any]] = list(strip)
                        if y_next_m is not None:
                            pad_nm = max(10.0, float(band_tol) * 1.65)
                            floor_y = float(y_next_m) + pad_nm
                            strip_read = [
                                it for it in strip if float(it.get("y") or 0.0) >= floor_y
                            ]
                            if len(strip_read) < max(2, min(5, max(1, len(strip) // 5))):
                                strip_read = list(strip)
                        bands_local = _merge_strip_into_y_bands(strip_read, band_z)
                        bands_local = _beam_vertical_filter_diagram_mm_bands(bands_local, z_t)
                        bands_map_l = _beam_vertical_prepare_bands_for_template_map(bands_local, z_t)
                        z_dyn2 = _map_data_bands_to_template(
                            bands_map_l, z_t, band_tol, beam_vertical_schedule=True
                        )
                        z_use = (
                            z_dyn2
                            if any(str(v or "").strip() for v in z_dyn2.values())
                            else z_dyn
                        )
                        rec = _record_from_beam_vertical_dynamic(
                            z_use,
                            z_t,
                            tsrc,
                            cfg,
                            strip_bands=bands_local,
                        )
                        if not _beam_vertical_record_useful(rec):
                            continue
                        rec["beam_strip_index"] = si
                        rec["beam_segment_index"] = seg_i
                        rec["beam_vertical_zone_span_index"] = zs_i
                        om = strip_member_override.get(si)
                        if om and str(om).strip():
                            _beam_vertical_apply_inferred_mark_to_rec(rec, str(om).strip())
                        tb = _beam_vertical_section_geom_y_bounds(z_t, band_tol)
                        if tb is not None:
                            y_lo, y_hi = tb
                        elif ys_piece:
                            pad = max(float(band_tol) * 8.0, 40.0)
                            y_lo, y_hi = min(ys_piece) - pad, max(ys_piece) + pad
                            y_lo, y_hi = _beam_vertical_cap_row_y_span_to_one_schedule_block(
                                y_lo, y_hi, z_t, band_tol
                            )
                        else:
                            y_lo, y_hi = _segment_y_bounds_world(template, seg_indices, band_tol)
                            y_lo, y_hi = _beam_vertical_cap_row_y_span_to_one_schedule_block(
                                y_lo, y_hi, z_t if z_t else template, band_tol
                            )
                        if y_next_m is not None:
                            pad_nm2 = max(10.0, float(band_tol) * 1.65)
                            fy = float(y_next_m) + pad_nm2
                            y_lo = max(float(y_lo), fy)
                            if y_hi <= y_lo + 8.0:
                                y_hi = y_lo + max(120.0, float(band_tol) * 14.0)
                        rda = _row_data_anchor_y_from_strip_relaxed(strip_read, y_lo, y_hi, band_tol)
                        if rda is not None:
                            rec["row_data_anchor_y"] = rda
                        y_lo, y_hi = _beam_vertical_bind_row_bounds_to_seg_section_y(
                            z_t, y_lo, y_hi, band_tol
                        )
                        rec["row_data_anchor_y_bounds"] = [round(y_lo, 4), round(y_hi, 4)]
                        pending_merge.append((xc, seg_i, zs_i, rec))
            else:
                rec = _record_from_beam_vertical_dynamic(
                    dyn,
                    template,
                    tsrc,
                    cfg,
                    strip_bands=bands,
                )
                if not _beam_vertical_record_useful(rec):
                    continue
                rec["beam_strip_index"] = si
                rec["beam_segment_index"] = 0
                om = strip_member_override.get(si)
                if om and str(om).strip():
                    _beam_vertical_apply_inferred_mark_to_rec(rec, str(om).strip())
                ys_strip = [float(it["y"]) for it in strip if it.get("y") is not None]
                tb = _beam_vertical_section_geom_y_bounds(template, band_tol)
                if tb is not None:
                    y_lo_f, y_hi_f = float(tb[0]), float(tb[1])
                    y_lo_f, y_hi_f = _beam_vertical_bind_row_bounds_to_seg_section_y(
                        template, y_lo_f, y_hi_f, band_tol
                    )
                    rec["row_data_anchor_y_bounds"] = [round(y_lo_f, 4), round(y_hi_f, 4)]
                    rda = _row_data_anchor_y_from_strip_relaxed(strip, y_lo_f, y_hi_f, band_tol)
                    if rda is not None:
                        rec["row_data_anchor_y"] = rda
                elif ys_strip:
                    rec["row_data_anchor_y"] = round(_median_float(ys_strip), 4)
                rows_out.append(rec)
            continue
        # 템플릿 없으면 한 스트립 = 한 행(값만 나열)
        texts = [b[1] for b in bands if b[1]]
        merged = _merge_signals(texts)
        merged["category"] = CATEGORY_BEAM
        merged["wall_mode"] = cfg.wall_mode
        merged["beam_layout"] = "vertical_blocks_fallback"
        merged["beam_row_role"] = "beam_flat_fallback"
        if cfg.building_tag:
            merged["building"] = cfg.building_tag
        ys_strip = [float(it["y"]) for it in strip if it.get("y") is not None]
        if ys_strip:
            merged["row_y_mean"] = round(sum(ys_strip) / len(ys_strip), 4)
        if _beam_vertical_record_useful(merged):
            rows_out.append(merged)

    # 뷰어: 단면(형태) 앵커 Y×스트립 X → 부위 라벨 TEXT → 추론 부재명 TEXT (폴리라인 points)
    match_lines: list[dict[str, Any]] = []
    idb2 = meta.get("beam_vertical_member_layout_infer") or infer_dbg
    idb2 = idb2 if isinstance(idb2, dict) else {}
    yz_raw = idb2.get("y_zone_eff")
    yn_raw = idb2.get("y_name")
    # infer가 비어 있어도 템플릿만으로 Y 앵커를 채워 첫 경로에서 폴리라인을 만들 수 있게 한다.
    if template:
        if yn_raw is None:
            ynt = _beam_vertical_template_name_row_y(template)
            if ynt is not None:
                yn_raw = float(ynt)
        if yz_raw is None:
            yze = _beam_vertical_template_zone_row_y_estimate(template)
            if yze is not None:
                yz_raw = float(yze)
            elif yn_raw is not None:
                yz_raw = float(yn_raw)
    try:
        if strip_infos and yz_raw is not None and yn_raw is not None:
            y_zone_g = float(yz_raw)
            y_name_g = float(yn_raw)
            loose_m = max(88.0, float(band_tol) * 14.0)
            sec_y_global = _beam_vertical_row_section_anchor_y(template) if template else None
            ov = strip_member_override or {}
            for s in strip_infos:
                try:
                    ii = int(s.get("index", -1))
                except (TypeError, ValueError):
                    continue
                if ii < 0:
                    continue
                try:
                    xc = float(s.get("x_center") or 0.0)
                except (TypeError, ValueError):
                    continue
                st = strips[ii] if 0 <= ii < len(strips) else []
                mk = str(ov.get(ii) or "").strip()
                if not mk:
                    mk = _beam_vertical_strip_pick_nearest_member_mark(st, xc, y_name_g, loose_m)
                if not mk:
                    continue
                sec_gate = max(4500.0, loose_m * 1.35)
                neigh: list[list[dict[str, Any]]] = []
                if ii - 1 >= 0:
                    neigh.append(strips[ii - 1])
                if ii + 1 < len(strips):
                    neigh.append(strips[ii + 1])
                sec_loc = _beam_vertical_template_section_y_for_strip(
                    template, st, xc_focus=xc, x_gate=sec_gate, neighbor_strips=neigh or None
                )
                sec_yf = float(sec_loc) if sec_loc is not None else (
                    float(sec_y_global) if sec_y_global is not None else 0.5 * (y_zone_g + y_name_g)
                )
                y_zone_loc = _beam_vertical_template_y_nearest_strip(
                    template,
                    st,
                    row_ok=lambda lab, k: _beam_vertical_zone_from_label(lab) is not None,
                )
                y_zone_f = float(y_zone_loc) if y_zone_loc is not None else y_zone_g
                y_name_loc = _beam_vertical_template_y_nearest_strip(
                    template,
                    st,
                    row_ok=lambda lab, k: _beam_vertical_row_kind_from_title(lab) == "name",
                )
                y_name_f = float(y_name_loc) if y_name_loc is not None else y_name_g
                poly = _beam_vertical_member_zone_match_polyline(
                    ii,
                    st,
                    band_tol,
                    xc,
                    sec_yf,
                    y_zone_f,
                    y_name_f,
                    loose_m,
                    mk,
                    template=template,
                )
                if poly:
                    match_lines.append(poly)
    except (TypeError, ValueError):
        pass
    if not match_lines and template and strip_infos and strips:
        try:
            ys_all: list[float] = []
            for st0 in strips:
                for it0 in st0 or []:
                    try:
                        ys_all.append(float(it0["y"]))
                    except (TypeError, KeyError, ValueError):
                        continue
            if ys_all:
                y_ref_fb = float(_median_float(ys_all))
                y_name_fb = _beam_vertical_template_name_row_y_nearest(template, y_ref_fb)
                if y_name_fb is None:
                    y_name_fb = _beam_vertical_template_name_row_y(template)
                if y_name_fb is None:
                    y_name_fb = y_ref_fb
                y_zone_fb = _beam_vertical_template_zone_row_y_nearest(template, y_ref_fb)
                if y_zone_fb is None:
                    est = _beam_vertical_template_zone_row_y_estimate(template)
                    y_zone_fb = float(est) if est is not None else y_ref_fb
                sec_y_glob_fb = _beam_vertical_row_section_anchor_y(template)
                loose_fb = max(88.0, float(band_tol) * 14.0)
                ov_fb = strip_member_override or {}
                for s in strip_infos:
                    try:
                        ii = int(s.get("index", -1))
                    except (TypeError, ValueError):
                        continue
                    if ii < 0 or ii >= len(strips):
                        continue
                    st = strips[ii]
                    try:
                        xc = float(s.get("x_center") or 0.0)
                    except (TypeError, ValueError):
                        continue
                    mk = str(ov_fb.get(ii) or "").strip()
                    if not mk:
                        mk = _beam_vertical_strip_pick_nearest_member_mark(st, xc, float(y_name_fb), loose_fb)
                    if not mk:
                        continue
                    sec_gate_fb = max(4500.0, loose_fb * 1.35)
                    neigh_fb: list[list[dict[str, Any]]] = []
                    if ii - 1 >= 0:
                        neigh_fb.append(strips[ii - 1])
                    if ii + 1 < len(strips):
                        neigh_fb.append(strips[ii + 1])
                    sec_loc = _beam_vertical_template_section_y_for_strip(
                        template,
                        st,
                        xc_focus=xc,
                        x_gate=sec_gate_fb,
                        neighbor_strips=neigh_fb or None,
                    )
                    sec_yf = float(sec_loc) if sec_loc is not None else (
                        float(sec_y_glob_fb)
                        if sec_y_glob_fb is not None
                        else 0.5 * (float(y_zone_fb) + float(y_name_fb))
                    )
                    y_zone_loc = _beam_vertical_template_y_nearest_strip(
                        template,
                        st,
                        row_ok=lambda lab, k: _beam_vertical_zone_from_label(lab) is not None,
                    )
                    y_zone_f = float(y_zone_loc) if y_zone_loc is not None else float(y_zone_fb)
                    y_name_loc = _beam_vertical_template_y_nearest_strip(
                        template,
                        st,
                        row_ok=lambda lab, k: _beam_vertical_row_kind_from_title(lab) == "name",
                    )
                    y_name_f = float(y_name_loc) if y_name_loc is not None else float(y_name_fb)
                    poly = _beam_vertical_member_zone_match_polyline(
                        ii,
                        st,
                        band_tol,
                        xc,
                        sec_yf,
                        y_zone_f,
                        y_name_f,
                        loose_fb,
                        mk,
                        template=template,
                    )
                    if poly:
                        match_lines.append(poly)
        except (TypeError, ValueError):
            pass
    meta["beam_vertical_member_zone_match_lines"] = match_lines

    if pending_merge:
        grp: dict[tuple[int, int], list[tuple[float, dict[str, Any]]]] = defaultdict(list)
        for xc, seg_i, zs_i, rec in pending_merge:
            grp[(seg_i, zs_i)].append((xc, rec))
        for seg_i, zs_i in sorted(grp.keys(), key=lambda k: (k[0], k[1])):
            for run in _beam_vertical_split_pending_by_member_mark(grp[(seg_i, zs_i)]):
                merged = _merge_beam_vertical_records_across_strips(
                    [r for _xc, r in run],
                    meta.get("beam_vertical_field_headers"),
                )
                if _beam_vertical_record_useful(merged):
                    rows_out.append(merged)

    return rows_out, meta


def _row_starts_beam_member_bundle(row: list[dict[str, Any]]) -> bool:
    if _row_has_beam_mark_like(row):
        return True
    for it in row or []:
        t = str(it.get("text") or "").strip()
        if not t:
            continue
        if _RE_BEAM_MEMBER_ID_LOOSE.match(t) and re.search(r"\d", t):
            return True
    return False


def _beam_cluster_row_mean_y(row: list[dict[str, Any]]) -> float:
    ys = [float(r["y"]) for r in row]
    return sum(ys) / len(ys) if ys else 0.0


def _beam_centroid_entities(ents: list[dict[str, Any]]) -> tuple[float, float] | None:
    if not ents:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for it in ents:
        try:
            xs.append(float(it["x"]))
            ys.append(float(it["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not xs:
        return None
    return sum(xs) / len(xs), sum(ys) / len(ys)


def _beam_row_cluster_centroid_xy_mode_x(
    ents: list[dict[str, Any]],
    *,
    bin_width: float,
    column_center_x: float | None = None,
) -> tuple[float, float] | None:
    """
    같은 열(부호)에 붙은 TEXT 삽입점 X를 모아, X 히스토그램에서 건수가 가장 많은 구간의
    중심을 X로 쓴다. 병합 크기(예: 400x600) 한 점이 평균을 가운데로 잡아당기는 경우를 완화한다.
    동률이면 column_center_x(열 마크 중심)에 가까운 구간을 택한다.
    """
    if not ents:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for it in ents:
        try:
            xs.append(float(it["x"]))
            ys.append(float(it["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not xs:
        return None
    if len(xs) == 1:
        return float(xs[0]), float(ys[0])
    x_min = min(xs)
    x_max = max(xs)
    span = x_max - x_min
    bw = max(28.0, min(float(bin_width), max(40.0, span / 8.0 + 1e-6)))
    buckets: dict[int, list[tuple[float, float]]] = {}
    for xv, yv in zip(xs, ys):
        bi = int((xv - x_min) // bw)
        buckets.setdefault(bi, []).append((xv, yv))

    def bucket_score(items: list[tuple[float, float]]) -> tuple[int, float]:
        n = len(items)
        mx = sum(p[0] for p in items) / n
        tie = abs(mx - float(column_center_x)) if column_center_x is not None else 0.0
        return (n, -tie)

    best_items = max(buckets.values(), key=bucket_score)
    mx = sum(p[0] for p in best_items) / len(best_items)
    my = sum(p[1] for p in best_items) / len(best_items)
    # 상·하부근 등이 한쪽에만 몰리고 병합 크기가 반대편에 있을 때: 최빈 구간이 소수면 중앙값 X로 보정
    if len(best_items) < max(3, int(0.22 * len(xs))):
        xs_sorted = sorted(xs)
        med_x = float(xs_sorted[len(xs_sorted) // 2])
        near_y = [yv for xv, yv in zip(xs, ys) if abs(xv - med_x) <= bw * 1.75]
        my2 = sum(near_y) / len(near_y) if near_y else sum(ys) / len(ys)
        return med_x, my2
    return mx, my


def _looks_like_value_text(s: str) -> bool:
    """
    좌측 라벨(헤더)로 오인되기 쉬운 값 텍스트를 구분해, 같은 R(가로띠)에서도 값이 라벨로 뭉치는 것을 방지한다.
    """
    t = (s or "").strip()
    if not t:
        return False
    u = re.sub(r"\s+", "", t).upper()
    if re.search(r"\d+\s*[-/]\s*\d+\s*-\s*(?:U?HD|SHD|D)\d+", u):
        return True
    if re.search(r"\b(?:U?HD|SHD|D)\d+\b", u) and re.search(r"\d", u):
        return True
    if "@" in u and re.search(r"\d", u):
        return True
    if re.search(r"\d+\s*[xX×]\s*\d+", t):
        return True
    if re.fullmatch(r"\d{2,5}", u):
        return True
    return False


def _beam_cluster_mark_column_centers(block_rows: list[list[dict[str, Any]]]) -> list[float] | None:
    """부호 행 후보 중 RE_FLAT_BEAM_MARK 열이 2개 이상인 행의 X중심 목록."""
    rows_sorted = sorted(block_rows, key=lambda br: -_beam_cluster_row_mean_y(br))
    best: list[float] = []
    for row in rows_sorted:
        marks: list[float] = []
        for e in row:
            t = re.sub(r"\s+", " ", str(e.get("text") or "").strip())
            if not t or len(t) > 48:
                continue
            if not (RE_FLAT_BEAM_MARK.match(t) and re.search(r"\d", t)):
                continue
            try:
                marks.append(float(e["x"]))
            except (TypeError, KeyError, ValueError):
                continue
        if len(marks) >= 2 and len(marks) > len(best):
            best = marks
    if len(best) < 2:
        return None
    if len(best) > 40:
        return None
    return sorted(best)


def _beam_row_partition_label_and_col_entities(
    row: list[dict[str, Any]],
    centers: list[float],
) -> tuple[str, list[list[dict[str, Any]]]]:
    """한 Y행: 좌측 라벨 + 부호열별 엔티티 리스트(단면 Y를 부위별로 나눌 때 재사용)."""
    if not row:
        return "", [[] for _ in range(len(centers))]
    c_sorted = sorted(float(x) for x in centers)
    c_count = len(c_sorted)
    if c_count < 1:
        parts = [str(r.get("text") or "").strip() for r in row if str(r.get("text") or "").strip()]
        joined = " ".join(parts)
        sorted_r = sorted(row, key=lambda r: float(r["x"]))
        return "", [list(sorted_r)] if sorted_r else [[]]

    if c_count >= 2:
        dx = max(1.0, c_sorted[1] - c_sorted[0])
        x_label_hi = c_sorted[0] - max(120.0, min(420.0, dx * 0.45))
    else:
        x_label_hi = c_sorted[0] - 320.0

    sorted_r = sorted(row, key=lambda r: float(r["x"]))
    label_parts: list[str] = []
    by_col: list[list[dict[str, Any]]] = [[] for _ in range(c_count)]
    for e in sorted_r:
        t = str(e.get("text") or "").strip()
        if not t:
            continue
        try:
            x = float(e["x"])
        except (TypeError, KeyError, ValueError):
            continue
        if x <= x_label_hi and not _looks_like_value_text(t):
            label_parts.append(t)
            continue
        j = min(range(c_count), key=lambda k: abs(x - c_sorted[k]))
        by_col[j].append(e)
    lab = " ".join(label_parts).strip()
    return lab, by_col


def _beam_row_label_and_cells_by_mark_centers(
    row: list[dict[str, Any]],
    centers: list[float],
) -> tuple[str, list[str]]:
    """한 Y행에서 좌측 라벨 + 부호열 N개 값을 분리."""
    if not row:
        return "", [""] * len(centers)
    c_count = len(centers)
    if c_count < 1:
        parts = [str(r.get("text") or "").strip() for r in row if str(r.get("text") or "").strip()]
        joined = " ".join(parts)
        return "", [joined]

    lab, by_col = _beam_row_partition_label_and_col_entities(row, centers)
    cells: list[str] = []
    for j in range(len(by_col)):
        ents = sorted(by_col[j], key=lambda r: float(r["x"]))
        txts = [str(e.get("text") or "").strip() for e in ents if str(e.get("text") or "").strip()]
        cells.append(" ".join(txts).strip())
    while len(cells) < c_count:
        cells.append("")
    return lab, cells


_BEAM_ROW_CLUSTER_ZONE_SPAN_KEYS = frozenset({"int", "cen", "ext", "both", "all"})


def _beam_row_cluster_zone_band_row(
    block_rows: list[list[dict[str, Any]]],
    centers: list[float],
) -> list[dict[str, Any]] | None:
    """부호 열 위 ALL/END/CEN… 띠 후보 행(열 셀별 부위 문자열과 동일 소스)."""
    if not block_rows or not centers:
        return None
    rows_sorted = sorted(block_rows or [], key=lambda br: -_beam_cluster_row_mean_y(br))
    best_row: list[dict[str, Any]] | None = None
    best_score = 0.0
    for row in rows_sorted:
        tx = _row_nonempty_texts(row)
        if len(tx) < 3:
            continue
        hits = 0
        for t in tx:
            u = re.sub(r"\s+", " ", str(t).strip()).upper()
            if u in ("ALL", "END", "CEN", "CENTER", "INT", "EXT", "BOTH"):
                hits += 1
                continue
            z = _beam_vertical_zone_from_label(str(t))
            if z in _BEAM_ROW_CLUSTER_ZONE_SPAN_KEYS:
                hits += 1
        score = hits / max(len(tx), 1)
        if score > best_score and hits >= max(3, int(0.45 * len(tx))):
            best_score = score
            best_row = row
    return best_row


def _beam_row_cluster_parse_horizontal_zone_slots(zcell: str) -> list[tuple[str, str]]:
    """
    한 부호 열 셀에 'END CEN', 'INT/CEN/EXT'처럼 여러 부위가 있을 때 (표시문, zone_key) 목록.
    ALL은 다른 부위와 같이 있으면 스킵(단면 앵커는 END/CEN…만 분리).
    """
    raw = (zcell or "").strip()
    if not raw:
        return []
    chunks = [p.strip() for p in re.split(r"[/,\s|]+", raw) if p.strip()]
    raw_slots: list[tuple[str, str]] = []
    seen_zk: set[str] = set()
    for ch in chunks:
        zk = _beam_vertical_zone_from_label(ch)
        if zk not in _BEAM_ROW_CLUSTER_ZONE_SPAN_KEYS:
            continue
        if zk == "all":
            raw_slots.append((ch, zk))
            continue
        if zk in seen_zk:
            continue
        seen_zk.add(zk)
        raw_slots.append((ch, zk))
    non_all = [(a, b) for a, b in raw_slots if b != "all"]
    return non_all if non_all else raw_slots


def _beam_row_cluster_col_x_inner_span(c_sorted: list[float], j: int) -> tuple[float, float]:
    """부호 열 j 안에서 부위별 X를 나눌 때 쓰는 가로 구간(이웃 열 중점 사이)."""
    c_count = len(c_sorted)
    if c_count <= 0:
        return (-400.0, 400.0)
    if c_count == 1:
        c0 = float(c_sorted[0])
        return (c0 - 520.0, c0 + 520.0)
    gap_l = abs(float(c_sorted[j]) - float(c_sorted[j - 1])) if j > 0 else abs(float(c_sorted[1]) - float(c_sorted[0]))
    gap_r = (
        abs(float(c_sorted[j + 1]) - float(c_sorted[j])) if j < c_count - 1 else abs(float(c_sorted[-1]) - float(c_sorted[-2]))
    )
    x_lo = 0.5 * (float(c_sorted[j - 1]) + float(c_sorted[j])) if j > 0 else float(c_sorted[0]) - 0.5 * gap_l
    x_hi = 0.5 * (float(c_sorted[j]) + float(c_sorted[j + 1])) if j < c_count - 1 else float(c_sorted[j]) + 0.5 * gap_r
    if x_hi <= x_lo:
        x_lo, x_hi = float(c_sorted[j]) - 260.0, float(c_sorted[j]) + 260.0
    return (x_lo, x_hi)


def _beam_row_cluster_zone_slot_centroids_x(
    band_row: list[dict[str, Any]],
    centers: list[float],
    col_j: int,
    slots: list[tuple[str, str]],
) -> list[float | None]:
    """부위 띠 행에서 열 col_j 텍스트를 zone_key로 묶어 삽입점 X 중심(슬롯별)."""
    if not band_row or not slots:
        return []
    _lab, by_col = _beam_row_partition_label_and_col_entities(band_row, centers)
    if col_j < 0 or col_j >= len(by_col):
        return [None] * len(slots)
    by_zk: dict[str, list[dict[str, Any]]] = {}
    for e in by_col[col_j]:
        t = str(e.get("text") or "").strip()
        z = _beam_vertical_zone_from_label(t)
        if z:
            by_zk.setdefault(z, []).append(e)
    out: list[float | None] = []
    for _raw_tok, zk in slots:
        pool = by_zk.get(zk) or []
        cc = _beam_centroid_entities(pool) if pool else None
        out.append(float(cc[0]) if cc else None)
    return out


def _beam_row_cluster_fill_slot_xs(
    xs: list[float | None],
    x_lo: float,
    x_hi: float,
) -> list[float]:
    """None 슬롯은 동일 간격으로 x_lo~x_hi 사이에 배치."""
    n = len(xs)
    if n <= 0:
        return []
    known_idx = [i for i, v in enumerate(xs) if v is not None]
    if len(known_idx) == n:
        return [float(xs[i]) for i in range(n)]  # type: ignore[list-item]
    filled = [float(x) if x is not None else None for x in xs]
    unk = [i for i in range(n) if filled[i] is None]
    if not unk:
        return [float(x) for x in filled]  # type: ignore[arg-type]
    span = max(x_hi - x_lo, 120.0)
    if not known_idx:
        for k, ii in enumerate(unk):
            filled[ii] = x_lo + (k + 0.5) * span / max(len(unk), 1)
        return [float(x) for x in filled]  # type: ignore[arg-type]
    # 일부만 알려진 경우: 구간을 나눠 균등 배치(단순)
    for k, ii in enumerate(unk):
        filled[ii] = x_lo + (k + 1.0) * span / (len(unk) + 1)
    return [float(x) for x in filled]  # type: ignore[arg-type]


def _beam_row_cluster_zone_spans_rows(
    block_rows: list[list[dict[str, Any]]],
    centers: list[float],
) -> list[tuple[str | None, str | None, list[list[dict[str, Any]]]]] | None:
    """부위(INT/CENTER/END…) 라벨 행마다 세로 구간을 나눈다. high Y → low Y."""
    rows_sorted = sorted(block_rows or [], key=lambda br: -_beam_cluster_row_mean_y(br))
    segments: list[tuple[str | None, str | None, list[list[dict[str, Any]]]]] = []
    cur_key: str | None = None
    cur_disp: str | None = None
    buf: list[list[dict[str, Any]]] = []
    any_zone = False

    def flush() -> None:
        nonlocal buf, cur_key, cur_disp
        if not buf:
            return
        segments.append((cur_key, cur_disp, buf))
        buf = []

    for row in rows_sorted:
        lab, cells = _beam_row_label_and_cells_by_mark_centers(row, centers)
        blob = " ".join([lab] + [str(c or "").strip() for c in cells if str(c or "").strip()]).strip()
        z = _beam_vertical_zone_from_label(lab)
        if z not in _BEAM_ROW_CLUSTER_ZONE_SPAN_KEYS:
            z = _beam_vertical_zone_from_label(blob)
        if z not in _BEAM_ROW_CLUSTER_ZONE_SPAN_KEYS:
            # 부호열 분리 전(가로띠 한 줄)에서만 INT 등이 잡히는 도면
            lab2, val2, _l2, _r2 = _beam_row_split_label_value_row(row)
            z = _beam_vertical_zone_from_label(f"{lab2} {val2}".strip())
        if z in _BEAM_ROW_CLUSTER_ZONE_SPAN_KEYS:
            any_zone = True
            flush()
            cur_key = z
            cur_disp = (lab or blob or "").strip()[:80] or str(z).upper()
            buf = [row]
        else:
            buf.append(row)
    flush()
    if not any_zone:
        return None
    return segments


def _beam_cluster_col_y_bounds_in_rows(
    centers: list[float],
    seg_rows: list[list[dict[str, Any]]],
    col_j: int,
) -> tuple[float, float] | None:
    """한 부위 구간 안에서 특정 부호 열(col_j) 텍스트의 Y min/max."""
    ys: list[float] = []
    for row in seg_rows or []:
        _lab, by_col = _beam_row_partition_label_and_col_entities(row, centers)
        if col_j < 0 or col_j >= len(by_col):
            continue
        for e in by_col[col_j]:
            try:
                ys.append(float(e["y"]))
            except (TypeError, KeyError, ValueError):
                continue
    if not ys:
        return None
    return (min(ys), max(ys))


def _bbox_union_from_entities(ents: list[dict[str, Any]]) -> list[float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for it in ents or []:
        try:
            xs.append(float(it["x"]))
            ys.append(float(it["y"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not xs or not ys:
        return None
    return [round(min(xs), 4), round(min(ys), 4), round(max(xs), 4), round(max(ys), 4)]


def _beam_row_cluster_entities_in_y_span(
    ents: list[dict[str, Any]],
    y_lo: float,
    y_hi: float,
    *,
    pad: float = 80.0,
) -> list[dict[str, Any]]:
    """부위 세로 구간(y_lo~y_hi)에 걸리는 TEXT만 모아, 존별 X·bbox를 열 전체와 분리할 때 사용."""
    lo = float(y_lo) - float(pad)
    hi = float(y_hi) + float(pad)
    out: list[dict[str, Any]] = []
    for e in ents or []:
        try:
            y = float(e["y"])
        except (TypeError, KeyError, ValueError):
            continue
        if lo <= y <= hi:
            out.append(e)
    return out


def _beam_row_cluster_x_histogram_peak_centers(
    ents: list[dict[str, Any]],
    *,
    bin_width: float,
    column_center_x: float,
    num_peaks: int,
) -> list[float]:
    """
    열 전체 TEXT의 X 히스토그램에서 건수 상위 num_peaks개 빈의 중심 X(최빈 → 차순).
    부위 개수만큼 서로 다른 삽입점 군집이 있을 때 각각의 대표 X 후보가 된다.
    """
    if num_peaks < 1 or not ents:
        return []
    xs: list[float] = []
    for it in ents:
        try:
            xs.append(float(it["x"]))
        except (TypeError, KeyError, ValueError):
            continue
    if not xs:
        return []
    if len(xs) == 1:
        return [xs[0]]
    x_min = min(xs)
    x_max = max(xs)
    span = x_max - x_min
    bw = max(28.0, min(float(bin_width), max(40.0, span / 8.0 + 1e-6)))
    buckets: dict[int, list[float]] = {}
    for xv in xs:
        bi = int((xv - x_min) // bw)
        buckets.setdefault(bi, []).append(xv)

    def bin_sort_key(kv: tuple[int, list[float]]) -> tuple[int, float]:
        bi, xvs = kv
        mx = sum(xvs) / len(xvs)
        return (-len(xvs), abs(mx - float(column_center_x)))

    ranked = sorted(buckets.items(), key=bin_sort_key)
    centers: list[float] = []
    for _bi, xvs in ranked:
        if len(centers) >= num_peaks:
            break
        centers.append(sum(xvs) / len(xvs))
    centers.sort()
    return centers


def _beam_row_cluster_resolve_zone_centroids_modal(
    c_sorted: list[float],
    col_j: int,
    column_ents: list[dict[str, Any]],
    recs: list[dict[str, Any]],
    *,
    bin_width: float,
) -> None:
    """
    세로 존별 1차 힌트는 `_beam_row_cluster_centroid_xy_mode_x`(존 Y필터 후)로 이미 rec에 들어 있다.
    같은 열에 존이 여러 개이면, 열 전체 X 히스토그램에서 상위 K개 최빈 빈 중심을 뽑고,
    힌트 X와 빈 중심을 각각 X 오름차순으로 정렬해 i번째끼리 대응(1차원 최적 배치).
    최빈 빈이 하나로만 나오면 열 내 가로 허용구간 균등 분할로 폴백.
    """
    n = len(recs)
    if n < 2:
        return
    hints: list[float] = []
    for r in recs:
        v = r.get("_beam_row_cluster_centroid_x")
        if v is None:
            return
        hints.append(float(v))
    cx = float(c_sorted[col_j])
    x_lo, x_hi = _beam_row_cluster_col_x_inner_span(c_sorted, col_j)
    span = max(float(x_hi) - float(x_lo), 120.0)
    min_spread = max(28.0, 0.025 * span)

    peaks = _beam_row_cluster_x_histogram_peak_centers(
        column_ents,
        bin_width=bin_width,
        column_center_x=cx,
        num_peaks=n,
    )
    # 상위 K개 빈이 실제로 가로로 갈라져야 의미 있음 — 한 덩어리면 폴백
    if (
        len(peaks) < n
        or (max(peaks) - min(peaks)) < min_spread
    ):
        filled = _beam_row_cluster_fill_slot_xs([None] * n, x_lo, x_hi)
        for r, xf in zip(recs, filled):
            r["_beam_row_cluster_centroid_x"] = round(float(xf), 4)
        return

    peaks_sorted = sorted(peaks[:n])
    idx_order = sorted(range(n), key=lambda i: hints[i])
    for rank, ii in enumerate(idx_order):
        recs[ii]["_beam_row_cluster_centroid_x"] = round(float(peaks_sorted[rank]), 4)


def _beam_row_split_label_value_row(
    row: list[dict[str, Any]],
) -> tuple[str, str, list[dict[str, Any]], list[dict[str, Any]]]:
    """한 Y행을 큰 X 간격 기준으로 좌(라벨)·우(값)로 나눈다."""
    if not row:
        return "", "", [], []
    sorted_r = sorted(row, key=lambda r: float(r["x"]))
    if len(sorted_r) == 1:
        t = str(sorted_r[0].get("text") or "").strip()
        return "", t, list(sorted_r), list(sorted_r)
    xs = [float(r["x"]) for r in sorted_r]
    best_ig = 0
    best_g = -1.0
    span = max(xs[-1] - xs[0], 1.0)
    min_gap = max(36.0, span * 0.045)
    for i in range(len(xs) - 1):
        g = xs[i + 1] - xs[i]
        if g > best_g:
            best_g = g
            best_ig = i
    if best_g < min_gap:
        parts = [str(r.get("text") or "").strip() for r in sorted_r if str(r.get("text") or "").strip()]
        joined = " ".join(parts)
        return "", joined, list(sorted_r), list(sorted_r)
    left = sorted_r[: best_ig + 1]
    right = sorted_r[best_ig + 1 :]
    lab = " ".join(str(r.get("text") or "").strip() for r in left if str(r.get("text") or "").strip())
    val = " ".join(str(r.get("text") or "").strip() for r in right if str(r.get("text") or "").strip())
    return lab, val, left, right


def _beam_row_cluster_segment_to_record(
    block_rows: list[list[dict[str, Any]]],
    cfg: ExtractionConfig,
) -> dict[str, Any] | None:
    if not block_rows:
        return None
    titles: dict[str, str] = {}
    order: list[str] = []
    cells: list[str] = []
    for i, row in enumerate(sorted(block_rows, key=lambda br: -_beam_cluster_row_mean_y(br))):
        lab, val, _left, _right = _beam_row_split_label_value_row(row)
        k = _slug_field_key(lab or f"row_{i}", i)
        order.append(k)
        titles[k] = lab or ""
        cells.append(val)
    rec: dict[str, Any] = {
        "category": CATEGORY_BEAM,
        "wall_mode": cfg.wall_mode,
        "beam_layout": "row_cluster_bundle",
        "beam_row_role": "beam_row_cluster_bundle",
        "beam_field_titles": titles,
        "beam_field_key_order": order,
        "cells": cells,
    }
    if cfg.building_tag:
        rec["building"] = cfg.building_tag
    _enrich_beam_vertical_record(rec)
    _normalize_beam_mark_paren_dimensions(rec)
    ys_all: list[float] = []
    for row in block_rows:
        for it in row:
            try:
                ys_all.append(float(it["y"]))
            except (TypeError, KeyError, ValueError):
                continue
    if ys_all:
        rec["row_y_mean"] = round(sum(ys_all) / len(ys_all), 4)
    if not _beam_vertical_record_useful(rec):
        return None
    return rec


def _beam_row_bundle_match_line_for_segment(
    block_rows: list[list[dict[str, Any]]],
    rec: dict[str, Any],
    strip_index: int,
) -> dict[str, Any] | None:
    _normalize_beam_mark_paren_dimensions(rec)
    mk = str(rec.get("mark") or "").strip()
    if not mk or not block_rows:
        return None
    pt_n: tuple[float, float] | None = None
    pt_z: tuple[float, float] | None = None
    pt_s: tuple[float, float] | None = None
    zone_txt = ""
    w_pts: list[tuple[float, float]] = []
    d_pts: list[tuple[float, float]] = []

    for row in sorted(block_rows, key=lambda br: -_beam_cluster_row_mean_y(br)):
        lab, val, _L, right = _beam_row_split_label_value_row(row)
        lab_s = (lab or "").strip()
        val_s = (val or "").strip()
        fk = _beam_template_row_to_field_key(lab_s)
        if pt_n is None and mk in val_s:
            c = _beam_centroid_entities(right)
            if c:
                pt_n = c
        if not zone_txt and _beam_vertical_zone_from_label(lab_s):
            zone_txt = val_s or lab_s
            c = _beam_centroid_entities(right)
            if c:
                pt_z = c
        rk = _beam_vertical_row_kind_from_title(lab_s)
        if rk == "section" or ("형" in lab_s and "태" in lab_s):
            c = _beam_centroid_entities(right)
            if c:
                pt_s = c
        if fk == "width_mm":
            c = _beam_centroid_entities(right)
            if c:
                w_pts.append(c)
        if fk == "depth_mm":
            c = _beam_centroid_entities(right)
            if c:
                d_pts.append(c)

    if pt_s is None and w_pts and d_pts:
        pt_s = ((w_pts[0][0] + d_pts[0][0]) * 0.5, (w_pts[0][1] + d_pts[0][1]) * 0.5)
    if pt_n is None:
        for row in block_rows:
            lab, val, _L, right = _beam_row_split_label_value_row(row)
            if mk in (val or ""):
                pt_n = _beam_centroid_entities(right)
                break
    if pt_z is None and pt_n is not None:
        pt_z = pt_n
        zone_txt = zone_txt or "—"
    if pt_s is None and pt_n is not None and pt_z is not None:
        pt_s = (pt_n[0] * 0.55 + pt_z[0] * 0.45, min(pt_n[1], pt_z[1]) - 120.0)
    if not (pt_n and pt_z and pt_s):
        return None
    pts = [
        [round(pt_n[0], 4), round(pt_n[1], 4)],
        [round(pt_z[0], 4), round(pt_z[1], 4)],
        [round(pt_s[0], 4), round(pt_s[1], 4)],
    ]
    return {
        "strip_index": int(strip_index),
        "mark": mk,
        "mark_inferred": mk,
        "zone_text": (zone_txt or "—").strip()[:80],
        "points": pts,
    }


def _beam_row_cluster_block_y_bounds(
    block_rows: list[list[dict[str, Any]]],
) -> tuple[float, float] | None:
    ys: list[float] = []
    for row in block_rows or []:
        for it in row or []:
            try:
                ys.append(float(it["y"]))
            except (TypeError, KeyError, ValueError):
                continue
    if not ys:
        return None
    return (min(ys), max(ys))


def _beam_row_cluster_per_column_zone_labels(
    block_rows: list[list[dict[str, Any]]],
    centers: list[float],
) -> list[str | None] | None:
    """R# 띠처럼 한 줄이 거의 전부 ALL/END/CEN… 일 때 열별 부위 문자열(클러스터와 동일 소스)."""
    if not block_rows or not centers:
        return None
    best_row = _beam_row_cluster_zone_band_row(block_rows, centers)
    if best_row is None:
        return None
    _lab, cells = _beam_row_label_and_cells_by_mark_centers(best_row, centers)
    if len(cells) < len(centers):
        return None
    out: list[str | None] = []
    for j in range(len(centers)):
        c = str(cells[j] or "").strip()
        out.append(c if c else None)
    return out


def _beam_row_cluster_synth_strips_and_headers(
    rows_out: list[dict[str, Any]],
    match_lines: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """
    Y행 묶음 번들은 세로 블록용 strip/헤더가 비어 있어 단면 enrich가 스킵된다.
    가로 다열(부호 열마다 X가 다름)일 때 **세그먼트당 한 스트립**이면 단면 X가 한곳으로만 잡혀
    모든 부호가 같은 단면을 공유한다 → (세그먼트, 열)마다 스트립을 평탄화한다.
    """
    if not rows_out:
        return [], []
    ys_mean: list[float] = []
    for r in rows_out:
        ry = r.get("row_y_mean")
        if ry is not None:
            try:
                ys_mean.append(float(ry))
            except (TypeError, ValueError):
                pass
    y_tmpl = (max(ys_mean) + 280.0) if ys_mean else 0.0
    field_headers = [
        {"key": "section_shape", "label": "형 태", "y": round(float(y_tmpl), 4)},
    ]

    bundle = [r for r in rows_out if str(r.get("beam_row_role") or "") == "beam_row_cluster_bundle"]
    if bundle:

        def _seg_col_slot_x(rr: dict[str, Any]) -> tuple[int, int, int, float]:
            try:
                s0 = int(rr.get("beam_row_cluster_segment_index", rr.get("beam_bundle_segment_index", 0)))
            except (TypeError, ValueError):
                s0 = 0
            try:
                c0 = int(rr.get("beam_row_cluster_column_index", 0))
            except (TypeError, ValueError):
                c0 = 0
            try:
                zslot = int(rr.get("beam_row_cluster_zone_slot", 0))
            except (TypeError, ValueError):
                zslot = 0
            xc0 = 0.0
            v = rr.get("_beam_row_cluster_centroid_x")
            if v is not None:
                try:
                    xc0 = float(v)
                except (TypeError, ValueError):
                    pass
            if xc0 == 0.0:
                bb = rr.get("row_entity_bbox")
                if isinstance(bb, (list, tuple)) and len(bb) >= 4:
                    try:
                        xc0 = 0.5 * (float(bb[0]) + float(bb[2]))
                    except (TypeError, ValueError):
                        pass
            return (s0, c0, zslot, xc0)

        seen: dict[tuple[int, int, int], int] = {}
        strip_infos: list[dict[str, Any]] = []
        for r in sorted(bundle, key=_seg_col_slot_x):
            s0, c0, zslot, xc0 = _seg_col_slot_x(r)
            key = (s0, c0, zslot)
            if key not in seen:
                seen[key] = len(strip_infos)
                strip_infos.append(
                    {
                        "index": len(strip_infos),
                        "x_center": round(float(xc0), 4),
                        "entity_count": 0,
                    }
                )
            fi = seen[key]
            r["beam_bundle_segment_index"] = int(fi)
            r["beam_vertical_merged_strip_indices"] = [int(fi)]
        return field_headers, strip_infos

    xc_by_si: dict[int, float] = {}
    for line in match_lines or []:
        try:
            si = int(line.get("strip_index"))
        except (TypeError, ValueError):
            continue
        pts = line.get("points")
        if not isinstance(pts, list) or not pts:
            continue
        xs: list[float] = []
        for p in pts[:4]:
            if isinstance(p, (list, tuple)) and len(p) >= 1:
                try:
                    xs.append(float(p[0]))
                except (TypeError, ValueError):
                    pass
        if xs:
            xc_by_si[si] = sum(xs) / len(xs)
    max_si = -1
    for r in rows_out:
        try:
            si = int(r.get("beam_bundle_segment_index", 0))
        except (TypeError, ValueError):
            si = 0
        max_si = max(max_si, si)
    strip_infos = []
    for si in range(max_si + 1):
        xc = float(xc_by_si.get(si) or 0.0)
        if xc == 0.0:
            for rr in rows_out:
                try:
                    rsi = int(rr.get("beam_bundle_segment_index", -1))
                except (TypeError, ValueError):
                    continue
                if rsi != si:
                    continue
                cx = rr.get("_beam_row_cluster_centroid_x")
                if cx is not None:
                    try:
                        xc = float(cx)
                    except (TypeError, ValueError):
                        pass
                break
        strip_infos.append({"index": si, "x_center": xc, "entity_count": 0})
    return field_headers, strip_infos


def extract_beam_row_cluster_bundle(
    rows_cluster: list[list[dict[str, Any]]],
    cfg: ExtractionConfig,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    와이드/Location 헤더 없이, **Y행 묶음만**으로 부호(부재명)→부위→단면→철근을 읽는다.
    부호가 나오는 행을 기준으로 아래로 이어지는 행을 한 부재 블록으로 묶는다(세로 블록 X분할 없음).
    """
    meta: dict[str, Any] = {
        "beam_row_cluster_bundle": True,
        "beam_vertical_blocks": False,
        "beam_vertical_strips": [],
        "beam_vertical_field_headers": [],
    }
    if not rows_cluster:
        return [], meta

    sorted_rows = sorted(rows_cluster, key=lambda r: -_beam_cluster_row_mean_y(r))
    segments: list[list[list[dict[str, Any]]]] = []
    buf: list[list[dict[str, Any]]] | None = None
    for row in sorted_rows:
        if _row_is_beam_header_like(row):
            continue
        if _row_starts_beam_member_bundle(row):
            if buf:
                segments.append(buf)
            buf = [row]
        else:
            if buf is not None:
                buf.append(row)
    if buf:
        segments.append(buf)

    rows_out: list[dict[str, Any]] = []
    match_lines: list[dict[str, Any]] = []
    for si, block in enumerate(segments):
        # 같은 R(가로띠) 안에서도 부호(TEXT) 열 중심을 앵커로 삼아 부재별로 분리한다.
        centers = _beam_cluster_mark_column_centers(block)
        if not centers:
            rec = _beam_row_cluster_segment_to_record(block, cfg)
            if not rec:
                continue
            rec["beam_bundle_segment_index"] = si
            rec["beam_row_cluster_segment_index"] = int(si)
            rec["beam_row_cluster_column_index"] = 0
            rec["beam_vertical_merged_strip_indices"] = [int(si)]
            yb = _beam_row_cluster_block_y_bounds(block)
            if yb is not None:
                rec["row_data_anchor_y_bounds"] = [round(float(yb[0]), 4), round(float(yb[1]), 4)]
            flat_ents = [it for row in block for it in (row or [])]
            # flat 단일 단면 파이프라인용: row_cluster_bundle(단일 열)도 텍스트 원본을 제공한다.
            # (서버 flat enrich에서 x-slab 분할/부재·부위/하단텍스트 매칭에 사용)
            try:
                rec["_flat_sorted_entities"] = [
                    {
                        "x": round(float(it["x"]), 4),
                        "y": round(float(it["y"]), 4),
                        "text": str(it.get("text") or ""),
                        "id": it.get("id"),
                    }
                    for it in sorted(flat_ents, key=lambda e: float(e.get("x") or 0))
                    if it is not None and it.get("x") is not None and it.get("y") is not None
                ]
            except Exception:
                rec.pop("_flat_sorted_entities", None)
            xs_flat: list[float] = []
            for it in flat_ents:
                try:
                    xs_flat.append(float(it["x"]))
                except (TypeError, KeyError, ValueError):
                    continue
            span_x = (max(xs_flat) - min(xs_flat)) if len(xs_flat) >= 2 else 0.0
            bin_w0 = max(52.0, min(130.0, 0.34 * span_x + 14.0)) if span_x > 1.0 else 88.0
            cc = _beam_row_cluster_centroid_xy_mode_x(flat_ents, bin_width=bin_w0, column_center_x=None)
            if cc:
                rec["_beam_row_cluster_centroid_x"] = round(float(cc[0]), 4)
                rec["_beam_row_cluster_centroid_y"] = round(float(cc[1]), 4)
            rows_out.append(rec)
            poly = _beam_row_bundle_match_line_for_segment(block, rec, si)
            if poly:
                match_lines.append(poly)
            continue

        rows_sorted = sorted(block, key=lambda br: -_beam_cluster_row_mean_y(br))
        titles: dict[str, str] = {}
        order: list[str] = []
        matrix: list[list[str]] = []
        for i, row in enumerate(rows_sorted):
            lab, col_cells = _beam_row_label_and_cells_by_mark_centers(row, centers)
            k = _slug_field_key(lab or f"row_{i}", i)
            order.append(k)
            titles[k] = lab or ""
            matrix.append(list(col_cells) if col_cells else [""] * len(centers))

        # 컬럼별 엔티티를 모아 bbox/centroid에 사용
        c_sorted = sorted(float(x) for x in centers)
        c_count = len(c_sorted)
        ents_by_col: list[list[dict[str, Any]]] = [[] for _ in range(c_count)]
        # flat 단면(x-slab) 분할·부재/부위 텍스트 매칭에는 END/CEN/INT 같은 “값이 아닌 텍스트”도 필요하다.
        # 기존 ents_by_col 은 라벨/값 분리 필터로 인해 이런 토큰이 빠질 수 있어, 원본용 all 리스트를 따로 유지한다.
        ents_by_col_all: list[list[dict[str, Any]]] = [[] for _ in range(c_count)]
        if c_count >= 2:
            dx = max(1.0, c_sorted[1] - c_sorted[0])
            x_label_hi = c_sorted[0] - max(120.0, min(420.0, dx * 0.45))
        else:
            x_label_hi = c_sorted[0] - 320.0
        for row in rows_sorted:
            for e in row or []:
                t = str(e.get("text") or "").strip()
                if not t:
                    continue
                try:
                    x = float(e["x"])
                except (TypeError, KeyError, ValueError):
                    continue
                if x <= x_label_hi and not _looks_like_value_text(t):
                    continue
                j = min(range(c_count), key=lambda k: abs(x - c_sorted[k]))
                ents_by_col[j].append(e)
        # all: 라벨/값 필터 없이 “열 근처” 텍스트를 최대한 보존 (flat x-slab·부위 앵커용)
        for row in rows_sorted:
            for e in row or []:
                t = str(e.get("text") or "").strip()
                if not t:
                    continue
                try:
                    x = float(e["x"])
                except (TypeError, KeyError, ValueError):
                    continue
                if x <= x_label_hi:
                    continue
                j = min(range(c_count), key=lambda k: abs(x - c_sorted[k]))
                ents_by_col_all[j].append(e)

        gap_all: list[float] = []
        for jj in range(c_count - 1):
            gap_all.append(abs(c_sorted[jj + 1] - c_sorted[jj]))
        fallback_bin = max(50.0, min(120.0, min(gap_all) * 0.58)) if gap_all else 88.0

        yb = _beam_row_cluster_block_y_bounds(block)
        zone_spans = _beam_row_cluster_zone_spans_rows(block, centers)
        zone_labels_per_col = _beam_row_cluster_per_column_zone_labels(block, centers)
        poly_once_per_col: set[tuple[int, int, int]] = set()
        band_row = _beam_row_cluster_zone_band_row(block, centers)

        for j in range(c_count):
            col_zone_recs: list[dict[str, Any]] = []
            cells_col = [
                matrix[fi][j] if fi < len(matrix) and j < len(matrix[fi]) else ""
                for fi in range(len(order))
            ]
            zcell = ""
            if zone_labels_per_col and j < len(zone_labels_per_col) and zone_labels_per_col[j]:
                zcell = str(zone_labels_per_col[j]).strip()

            hz_slots = _beam_row_cluster_parse_horizontal_zone_slots(zcell)
            multi_horizontal = len(hz_slots) >= 2

            zone_y_specs: list[tuple[str | None, str | None, tuple[float, float] | None]]
            if zone_spans:
                seen_sig: set[tuple[float, float, str]] = set()
                zone_y_specs = []
                for zk, zdisp, zrows in zone_spans:
                    yb_z = _beam_cluster_col_y_bounds_in_rows(centers, zrows, j)
                    if yb_z is None or yb_z[1] <= yb_z[0] + 0.5:
                        yb_use = yb
                    else:
                        yb_use = yb_z
                    if yb_use is None:
                        continue
                    sig = (
                        round(float(yb_use[0]), 2),
                        round(float(yb_use[1]), 2),
                        str(zk or ""),
                    )
                    if sig in seen_sig:
                        continue
                    seen_sig.add(sig)
                    zone_y_specs.append((zk, zdisp, yb_use))
                if not zone_y_specs:
                    zone_y_specs = [(None, None, yb)]
            else:
                zone_y_specs = [(None, None, yb)]

            yb_col = _beam_cluster_col_y_bounds_in_rows(centers, rows_sorted, j)
            if yb_col is None or yb_col[1] <= yb_col[0] + 0.5:
                yb_col = yb

            x_span_lo, x_span_hi = _beam_row_cluster_col_x_inner_span(c_sorted, j)
            hz_xs: list[float] | None = None
            if multi_horizontal and band_row is not None:
                raw_xs = _beam_row_cluster_zone_slot_centroids_x(band_row, centers, j, hz_slots)
                pad = [None] * max(0, len(hz_slots) - len(raw_xs))
                hz_xs = _beam_row_cluster_fill_slot_xs(list(raw_xs) + pad, x_span_lo, x_span_hi)
            elif multi_horizontal:
                hz_xs = _beam_row_cluster_fill_slot_xs([None] * len(hz_slots), x_span_lo, x_span_hi)

            if multi_horizontal:
                zone_loop = [(None, (zcell or "")[:80] if zcell else None, yb_col)]
            else:
                zone_loop = list(zone_y_specs)

            gaps_j_pre: list[float] = []
            if j > 0:
                gaps_j_pre.append(abs(c_sorted[j] - c_sorted[j - 1]))
            if j < c_count - 1:
                gaps_j_pre.append(abs(c_sorted[j + 1] - c_sorted[j]))
            bin_w_col = max(42.0, min(145.0, (min(gaps_j_pre) if gaps_j_pre else fallback_bin) * 0.58))

            for zyi, (zk_sp, zdisp_sp, yb_use) in enumerate(zone_loop):
                if multi_horizontal:
                    slot_entries: list[tuple[int, str, str, float]] = []
                    assert hz_xs is not None
                    for hi, ((raw_tok, zk_h), xh) in enumerate(zip(hz_slots, hz_xs)):
                        slot_entries.append((hi, raw_tok, zk_h, float(xh)))
                else:
                    slot_entries = [(zyi, "", zk_sp, None)]

                for slot_idx, raw_tok, zk_slot_pass, xh_opt in slot_entries:
                    zk_final = zk_slot_pass if zk_slot_pass else zk_sp
                    if multi_horizontal:
                        zdisp_final = (raw_tok or "").strip()[:80] or (zdisp_sp or "") or ""
                        zk_eff = (str(zk_final).strip() if zk_final is not None else "") or None
                    else:
                        zdisp_final = (zdisp_sp or "") or ""
                        zk_eff = zk_sp if zk_sp else (str(zk_final).strip() if zk_final is not None else None)
                        if not zdisp_final and zcell and not zk_eff:
                            zdisp_final = zcell[:80]

                    rec: dict[str, Any] = {
                        "category": CATEGORY_BEAM,
                        "wall_mode": cfg.wall_mode,
                        "beam_layout": "row_cluster_bundle",
                        "beam_row_role": "beam_row_cluster_bundle",
                        "beam_field_titles": titles,
                        "beam_field_key_order": order,
                        "cells": list(cells_col),
                        "beam_bundle_segment_index": si,
                        "beam_row_cluster_segment_index": int(si),
                        "beam_row_cluster_column_index": int(j),
                        "beam_row_cluster_zone_slot": int(slot_idx),
                        "beam_vertical_merged_strip_indices": [int(si)],
                    }
                    if cfg.building_tag:
                        rec["building"] = cfg.building_tag
                    mk = _first_flat_row_mark(rec["cells"])
                    if mk:
                        rec["mark"] = mk
                        rec["name"] = mk
                        rec["member_label"] = mk

                    _enrich_beam_vertical_record(rec)
                    _normalize_beam_mark_paren_dimensions(rec)
                    if yb_use is not None:
                        rec["row_data_anchor_y_bounds"] = [
                            round(float(yb_use[0]), 4),
                            round(float(yb_use[1]), 4),
                        ]
                        mid_y = 0.5 * (float(yb_use[0]) + float(yb_use[1]))
                        rec["row_y_mean"] = round(mid_y, 4)
                        rec["row_section_anchor_y"] = round(mid_y, 4)
                    if zk_eff:
                        rec["beam_vertical_zone_key"] = zk_eff
                    disp_for_rec = zdisp_final
                    if multi_horizontal and disp_for_rec:
                        rec["beam_vertical_zone_display_label"] = disp_for_rec
                    elif zk_eff and zdisp_sp and not multi_horizontal:
                        rec["beam_vertical_zone_display_label"] = (zdisp_sp or "")[:80]
                    if not multi_horizontal and (not rec.get("beam_vertical_zone_display_label")) and zcell:
                        rec["beam_vertical_zone_display_label"] = zcell[:80]
                    if not multi_horizontal and zcell and (not rec.get("beam_vertical_zone_key")):
                        zk2 = _beam_vertical_zone_from_label(zcell)
                        if zk2:
                            rec["beam_vertical_zone_key"] = zk2

                    ents_geo = ents_by_col[j]
                    if yb_use is not None and not multi_horizontal:
                        y0u, y1u = float(yb_use[0]), float(yb_use[1])
                        if y1u > y0u + 1.0:
                            sub_geo = _beam_row_cluster_entities_in_y_span(ents_by_col[j], y0u, y1u)
                            if sub_geo:
                                ents_geo = sub_geo
                    # flat 단일 단면 파이프라인용: (세그먼트, 열, 존)별 텍스트 원본 제공
                    ents_src = ents_by_col_all[j]
                    if yb_use is not None and not multi_horizontal:
                        y0u, y1u = float(yb_use[0]), float(yb_use[1])
                        if y1u > y0u + 1.0:
                            sub_all = _beam_row_cluster_entities_in_y_span(ents_by_col_all[j], y0u, y1u)
                            if sub_all:
                                ents_src = sub_all
                    try:
                        rec["_flat_sorted_entities"] = [
                            {
                                "x": round(float(it["x"]), 4),
                                "y": round(float(it["y"]), 4),
                                "text": str(it.get("text") or ""),
                                "id": it.get("id"),
                            }
                            for it in sorted(ents_src, key=lambda e: float(e.get("x") or 0))
                            if it is not None and it.get("x") is not None and it.get("y") is not None
                        ]
                    except Exception:
                        rec.pop("_flat_sorted_entities", None)
                    bb = _bbox_union_from_entities(ents_geo)
                    if bb is not None:
                        rec["row_entity_bbox"] = bb
                    cc = _beam_row_cluster_centroid_xy_mode_x(
                        ents_geo,
                        bin_width=bin_w_col,
                        column_center_x=float(c_sorted[j]),
                    )
                    if cc:
                        if multi_horizontal and xh_opt is not None:
                            rec["_beam_row_cluster_centroid_x"] = round(float(xh_opt), 4)
                        else:
                            rec["_beam_row_cluster_centroid_x"] = round(float(cc[0]), 4)
                        rec["_beam_row_cluster_centroid_y"] = round(float(cc[1]), 4)

                    if _beam_vertical_record_useful(rec):
                        if multi_horizontal:
                            rows_out.append(rec)
                            pk = (int(si), int(j), int(slot_idx))
                            if pk not in poly_once_per_col:
                                poly = _beam_row_bundle_match_line_for_segment(block, rec, si)
                                if poly:
                                    match_lines.append(poly)
                                    poly_once_per_col.add(pk)
                        else:
                            col_zone_recs.append(rec)

            if not multi_horizontal and len(col_zone_recs) >= 2:
                _beam_row_cluster_resolve_zone_centroids_modal(
                    c_sorted,
                    j,
                    ents_by_col[j],
                    col_zone_recs,
                    bin_width=bin_w_col,
                )
            for rec in col_zone_recs:
                rows_out.append(rec)
                try:
                    slot_flush = int(rec.get("beam_row_cluster_zone_slot", 0))
                except (TypeError, ValueError):
                    slot_flush = 0
                pk = (int(si), int(j), int(slot_flush))
                if pk not in poly_once_per_col:
                    poly = _beam_row_bundle_match_line_for_segment(block, rec, si)
                    if poly:
                        match_lines.append(poly)
                        poly_once_per_col.add(pk)

    meta["beam_vertical_member_zone_match_lines"] = match_lines
    fh_syn, strips_syn = _beam_row_cluster_synth_strips_and_headers(rows_out, match_lines)
    if fh_syn and strips_syn:
        meta["beam_vertical_field_headers"] = fh_syn
        meta["beam_vertical_strips"] = strips_syn
    return rows_out, meta


# 테스트·하위 호환: `beam_flat_spatial` 구현 별칭
_beam_flat_section_focus_y_bounds = beam_flat_section_focus_y_bounds


def _first_flat_row_mark(cells: list[str]) -> str:
    for c in cells:
        t = (c or "").strip()
        if not t:
            continue
        if RE_FLAT_BEAM_MARK.match(t) and re.search(r"\d", t):
            return t
    return ""


def extract_beam_flat_fallback(
    rows_cluster: list[list[dict[str, Any]]],
    cfg: ExtractionConfig,
) -> list[dict[str, Any]]:
    rows_out: list[dict[str, Any]] = []
    for row in rows_cluster:
        if len(row) < cfg.min_row_texts:
            continue
        sorted_row = sorted(row, key=lambda r: float(r["x"]))
        cells = [str(r.get("text") or "").strip() for r in sorted_row]
        merged = _merge_signals(cells)
        merged["category"] = CATEGORY_BEAM
        merged["wall_mode"] = cfg.wall_mode
        merged["beam_layout"] = "flat"
        merged["beam_row_role"] = "beam_flat"
        if cfg.building_tag:
            merged["building"] = cfg.building_tag
        ys = [float(r["y"]) for r in sorted_row]
        xs = [float(r["x"]) for r in sorted_row]
        merged["row_y_mean"] = round(sum(ys) / len(ys), 4)
        merged["row_entity_bbox"] = [
            round(min(xs), 4),
            round(min(ys), 4),
            round(max(xs), 4),
            round(max(ys), 4),
        ]
        merged["entity_ids"] = [r.get("id") for r in sorted_row]
        merged["_flat_sorted_entities"] = [
            {
                "x": round(float(r["x"]), 4),
                "y": round(float(r["y"]), 4),
                "text": str(r.get("text") or ""),
                "id": r.get("id"),
            }
            for r in sorted_row
        ]
        merged["row_data_anchor_y_bounds"] = [round(min(ys), 4), round(max(ys), 4)]
        merged["row_data_anchor_y"] = merged["row_y_mean"]
        fy = beam_flat_section_focus_y_bounds(sorted_row)
        if fy is not None:
            merged["beam_flat_section_focus_y_bounds"] = [round(fy[0], 4), round(fy[1], 4)]
        mf = _first_flat_row_mark(cells)
        if mf:
            merged["mark"] = mf
            merged["name"] = mf
            merged["member_label"] = mf
            _normalize_beam_mark_paren_dimensions(merged)
        rows_out.append(merged)
    return rows_out


def process_beam_extraction(
    cfg: ExtractionConfig,
    items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """
    보 전용 추출: **가로 Y묶음(rows_cluster)** 만 사용한다.
    (1) 와이드/Location 헤더 표 → (2) 부호 행 기준으로 이어지는 행 묶음(row_cluster_bundle) → (3) 행당 flat.
    세로 블록(좌 라벨/우 스트립) 경로는 호출하지 않는다.
    """
    layout = (cfg.beam_layout or "auto").strip().lower()
    rows_raw = cluster_rows(items, cfg.y_tolerance)
    n_row_raw = len(rows_raw)
    glue_n = int(cfg.beam_row_merge_max_glue) if cfg.beam_row_merge_max_glue is not None else 4
    if glue_n < 1:
        glue_n = 4
    cap_n = int(cfg.beam_row_merge_max_merged) if cfg.beam_row_merge_max_merged is not None else 512
    if cap_n < 8:
        cap_n = 512
    rows_cluster = merge_beam_sparse_row_clusters(
        rows_raw,
        cfg.y_tolerance,
        y_gap_max=cfg.beam_row_merge_y_max,
        max_glue_row_items=glue_n,
        max_merged_row_items=cap_n,
    )
    n_after_sparse = len(rows_cluster)

    gap_used = cfg.beam_row_merge_y_max
    if gap_used is None or gap_used <= 0:
        gap_used = max(float(cfg.y_tolerance) * 3.5, 10.0)

    br_ep = int(cfg.beam_row_bridge_max_endpoint) if cfg.beam_row_bridge_max_endpoint is not None else 4
    br_ep = max(2, br_ep)
    br_mid = int(cfg.beam_row_bridge_max_mid) if cfg.beam_row_bridge_max_mid is not None else 1
    br_mid = max(0, br_mid)
    br_pass = int(cfg.beam_row_bridge_passes) if cfg.beam_row_bridge_passes is not None else 1
    br_pass = max(1, br_pass)

    rows_cluster = bridge_beam_sparse_row_clusters(
        rows_cluster,
        float(gap_used),
        max_endpoint_items=br_ep,
        max_intermediate_row_items=br_mid,
        max_merged_row_items=cap_n,
        max_bridge_passes=br_pass,
    )
    n_row_merged = len(rows_cluster)
    wall_mode = cfg.wall_mode
    building = cfg.building_tag

    validation: dict[str, Any] = {
        "category": CATEGORY_BEAM,
        "beam_layout_resolved": layout,
        "beam_row_cluster_merge": {
            "before_row_count": n_row_raw,
            "after_sparse_merge_row_count": n_after_sparse,
            "after_bridge_row_count": n_row_merged,
            "y_gap_max_used": round(float(gap_used), 4),
            "max_glue_row_items": glue_n,
            "max_merged_row_items": cap_n,
            "bridge_max_endpoint_items": br_ep,
            "bridge_max_mid_row_items": br_mid,
            "bridge_passes": br_pass,
        },
        "row_cluster_y_tolerance": cfg.y_tolerance,
        "beam_row_clusters": beam_row_clusters_for_validation(rows_cluster),
    }

    if layout == "flat":
        rows_f = extract_beam_flat_fallback(rows_cluster, cfg)
        validation["beam_layout_resolved"] = "flat"
        return _finalize_beam_extraction_rows(rows_f), validation

    def _try_horizontal_bundle() -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
        hw = extract_beam_horizontal_from_clusters(
            rows_cluster, wall_mode=wall_mode, building=building
        )
        if not hw:
            return None
        rows_h, meta = hw
        return (rows_h, meta) if rows_h else None

    if layout in ("auto", "vertical_blocks", "horizontal_wide"):
        hb = _try_horizontal_bundle()
        if hb:
            rows_h, meta = hb
            validation["beam_wide_or_tree"] = True
            validation["beam_vertical_blocks"] = False
            validation["beam_row_cluster_bundle"] = False
            validation.update(meta)
            validation["beam_layout_resolved"] = (
                "horizontal_wide" if meta.get("beam_wide_table") else "horizontal_location_tree"
            )
            return _finalize_beam_extraction_rows(rows_h), validation

        rows_b, meta_b = extract_beam_row_cluster_bundle(rows_cluster, cfg)
        if rows_b:
            validation["beam_vertical_blocks"] = False
            validation["beam_row_cluster_bundle"] = True
            validation.update(meta_b)
            validation["beam_layout_resolved"] = "row_cluster_bundle"
            return _finalize_beam_extraction_rows(rows_b), validation

        rows_f = extract_beam_flat_fallback(rows_cluster, cfg)
        validation["beam_layout_resolved"] = "flat"
        validation["beam_vertical_blocks"] = False
        validation["beam_row_cluster_bundle"] = False
        return _finalize_beam_extraction_rows(rows_f), validation

    rows_f = extract_beam_flat_fallback(rows_cluster, cfg)
    validation["beam_layout_resolved"] = "flat"
    validation["beam_vertical_blocks"] = False
    validation["beam_row_cluster_bundle"] = False
    return _finalize_beam_extraction_rows(rows_f), validation


def beam_duplicate_key(r: dict[str, Any]) -> tuple[Any, ...]:
    mark = (r.get("mark") or r.get("name") or "").strip()
    w = r.get("width_mm")
    d = r.get("depth_mm")
    sig = "|".join(
        str(r.get(k) or "")[:48]
        for k in (
            "int_top_bar",
            "cen_top_bar",
            "ext_top_bar",
            "int_bot_bar",
            "cen_bot_bar",
            "ext_bot_bar",
        )
    )
    zk = str(r.get("beam_vertical_zone_key") or "").strip()
    return (mark, w, d, sig, zk)
