"""
가로 flat 보 일람: 열(그리드) 분리·단면 세로창 — `beam_extraction` / `column_section_geometry` 공용.
순환 import 없이 좌표·텍스트 패턴만 다룬다.
"""
from __future__ import annotations

import re
from typing import Any

RE_FLAT_BEAM_MARK = re.compile(r"^\s*[A-Z]{1,3}\s*\d{1,4}[A-Z]?\s*(?:\(.+\))?\s*$", re.I)
# 줄 끝 괄호 설명: "END (INT)", "INT (BOTH)" 등 — `is_flat_zone_anchor_text`에서 제거 후 매칭
_RE_FLAT_ZONE_CORE = re.compile(
    r"^\s*(?:INT\.?\s*\(?BOTH\)?|INT|CEN(?:TER|\.?)?|EXT|END|ALL|CENTER|MIDDLE|"
    r"BOTH|\(BOTH\)|중앙|내부|외부|단부|인테|익스테)\s*$",
    re.I,
)
_RE_FLAT_REBARISH = re.compile(
    r"\d+\s*[-–]\s*(?:U?HD|SHD|D)\s*\d+|@\s*\d+|S(?:HD|TR)\s*\d|스트럽|스터럽|띠장|띠철근",
    re.I,
)


def _strip_trailing_zone_qualifiers(tn: str) -> str:
    """부위 뒤에 붙는 '(INT)', '(BOTH)' 등 괄호 꼬리를 반복 제거한다."""
    s = re.sub(r"\s+", " ", (tn or "").strip())
    for _ in range(8):
        s2 = re.sub(r"\s*\([^)]{0,96}\)\s*$", "", s).strip()
        if s2 == s:
            break
        s = s2
    return s


def beam_flat_row_has_beam_mark(row: dict[str, Any]) -> bool:
    """행에 부호(RG11 등)가 있으면 True — 주근·부위만 있는 표 줄(무부호)은 단면 enrich 대상이 아니다."""
    for k in ("mark", "name", "member_label"):
        t = str(row.get(k) or "").strip()
        if not t:
            continue
        tn = re.sub(r"\s+", " ", t)
        if len(tn) <= 48 and RE_FLAT_BEAM_MARK.match(tn) and re.search(r"\d", tn):
            return True
    ent = row.get("_flat_sorted_entities")
    if isinstance(ent, list):
        for e in ent:
            t = str(e.get("text") or "").strip()
            if not t:
                continue
            tn = re.sub(r"\s+", " ", t)
            if len(tn) <= 48 and RE_FLAT_BEAM_MARK.match(tn) and re.search(r"\d", tn):
                return True
    return False


def is_flat_zone_anchor_text(raw: str) -> bool:
    """부위 열 앵커(INT/CENTER/END/INT(BOTH)/END(INT) …)인지."""
    core = _strip_trailing_zone_qualifiers(raw)
    if not core:
        return False
    if _RE_FLAT_ZONE_CORE.match(core):
        return True
    # "END · INT" 처럼 괄호 없이 두 토막만 있는 경우(드묾)
    return bool(
        re.match(
            r"^\s*(INT|CEN(?:TER)?|EXT|END|ALL|CENTER|MIDDLE|BOTH)\b",
            core,
            re.I,
        )
    )


def _merge_close_zone_x(xs: list[float], *, tol: float) -> list[float]:
    """같은 열에 부위 텍스트가 두 번 찍힌 경우 X를 한 점으로 합친다."""
    if not xs:
        return []
    xs = sorted(xs)
    out: list[float] = [xs[0]]
    for x in xs[1:]:
        if x - out[-1] >= tol:
            out.append(x)
        else:
            out[-1] = 0.5 * (out[-1] + x)
    return out


def beam_flat_schedule_tight_y_bounds(
    slab: list[dict[str, Any]],
    *,
    pad_ratio: float = 0.16,
    pad_min: float = 50.0,
) -> tuple[float, float] | None:
    """
    한 열 슬랩 안에서 부위·주근·부호 텍스트의 Y만 모아 행 전체 bbox보다 좁은 앵커 후보를 만든다.
    (단면 도형·치수만 있고 Y가 벌어진 `row_entity_bbox` 대비)
    """
    ys: list[float] = []
    for e in slab:
        t = str(e.get("text") or "").strip()
        if not t:
            continue
        tn = re.sub(r"\s+", " ", t)
        if is_flat_zone_anchor_text(tn):
            try:
                ys.append(float(e["y"]))
            except (TypeError, ValueError, KeyError):
                pass
            continue
        if _RE_FLAT_REBARISH.search(tn):
            try:
                ys.append(float(e["y"]))
            except (TypeError, ValueError, KeyError):
                pass
            continue
        if len(tn) <= 28 and RE_FLAT_BEAM_MARK.match(tn) and re.search(r"\d", tn):
            try:
                ys.append(float(e["y"]))
            except (TypeError, ValueError, KeyError):
                pass
    if len(ys) < 2:
        return None
    lo, hi = min(ys), max(ys)
    if hi - lo < 28.0:
        return None
    pad = max(pad_min, (hi - lo) * pad_ratio)
    return (lo - pad, hi + pad)


def beam_flat_section_focus_y_bounds(sorted_row: list[dict[str, Any]]) -> tuple[float, float] | None:
    """
    부위 라벨 블록과 주근·띠근 텍스트 블록이 Y로 떨어져 있으면, 그 사이를 단면(원) 클러스터용 세로창으로 쓴다.
    한 줄에만 있으면(부위·철근 Y가 비슷) None → 기존 행 전체 bbox 유지.
    """
    zone_ys: list[float] = []
    bar_ys: list[float] = []
    for it in sorted_row:
        t = str(it.get("text") or "").strip()
        if not t:
            continue
        try:
            y = float(it["y"])
        except (TypeError, KeyError, ValueError):
            continue
        tn = re.sub(r"\s+", " ", t)
        if len(tn) <= 28 and RE_FLAT_BEAM_MARK.match(tn) and re.search(r"\d", tn):
            continue
        if is_flat_zone_anchor_text(tn):
            zone_ys.append(y)
        if _RE_FLAT_REBARISH.search(tn):
            bar_ys.append(y)
    if len(zone_ys) < 1 or len(bar_ys) < 1:
        return None
    z_min, z_max = min(zone_ys), max(zone_ys)
    b_min, b_max = min(bar_ys), max(bar_ys)
    gap_zone_bar = min(abs(z_max - b_min), abs(b_max - z_min))
    if gap_zone_bar < 100.0:
        return None
    if z_max < b_min - 40.0:
        y_lo = z_max - 45.0
        y_hi = b_min + 45.0
    elif b_max < z_min - 40.0:
        y_lo = b_max - 45.0
        y_hi = z_min + 45.0
    else:
        return None
    if y_hi <= y_lo:
        return None
    if y_hi - y_lo < 240.0:
        mid = 0.5 * (y_lo + y_hi)
        half = 220.0
        y_lo, y_hi = mid - half, mid + half
    return (y_lo, y_hi)


def beam_flat_x_slabs(sorted_row: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """
    부위명(INT / CENTER / END / ALL / …) 텍스트의 X를 열 앵커로 보고,
    인접 부위 X의 **중점**을 경계로 **열(슬랩)**을 나눈다. (고정 mm 간격 없음)

    부위 텍스트가 2개 미만이면(열 구분 불가) 행 전체를 한 슬랩으로 돌려준다.
    """
    if not sorted_row:
        return []
    sr = sorted(sorted_row, key=lambda r: float(r.get("x") or 0))
    if len(sr) < 2:
        return [sr]
    xs_z: list[float] = []
    for e in sr:
        t = str(e.get("text") or "").strip()
        if not t:
            continue
        tn = re.sub(r"\s+", " ", t)
        if not is_flat_zone_anchor_text(tn):
            continue
        try:
            xs_z.append(float(e["x"]))
        except (TypeError, ValueError, KeyError):
            continue
    # 같은 열에 라벨이 근접 중복될 때(캐드 분할 등) 한 앵커로 병합
    xs_z = _merge_close_zone_x(xs_z, tol=120.0)
    if len(xs_z) < 2:
        return [sr]
    try:
        min_x = float(sr[0]["x"])
        max_x = float(sr[-1]["x"])
    except (TypeError, ValueError, KeyError):
        return [sr]
    n = len(xs_z)
    boundaries: list[float] = [min_x - 1.0]
    for i in range(1, n):
        boundaries.append(0.5 * (xs_z[i - 1] + xs_z[i]))
    boundaries.append(max_x + 1.0)
    slabs: list[list[dict[str, Any]]] = []
    for i in range(n):
        lo, hi = boundaries[i], boundaries[i + 1]
        chunk: list[dict[str, Any]] = []
        for e in sr:
            try:
                xe = float(e["x"])
            except (TypeError, ValueError, KeyError):
                continue
            if i < n - 1:
                if lo <= xe < hi:
                    chunk.append(e)
            else:
                if lo <= xe <= hi:
                    chunk.append(e)
        if chunk:
            slabs.append(chunk)
    return slabs if slabs else [sr]
