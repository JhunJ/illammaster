"""
프론트( index.html )의 보 가로 Y묶음 매칭 규칙을 서버에서 재현:

- selection bbox 안의 CIRCLE(주근) 중심 + 직교선분을 수집해 "단면(section) 후보 중심"을 만든다.
- beam_row_clusters(행 클러스터)의 텍스트에서 부재명(mark)과 부위(zone)를 찾고,
  부재→부위→단면(picked)을 만든다.

목표는 프론트의 `computeBeamHorizontalZoneMatches`와 동일한 매칭 동작(특히 부위→단면 1:1 할당)을
서버에서 재현해 validation으로 내려주는 것.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.services.column_section_geometry import _collect_circles_lines_radii, _load_combined_shapes_in_bbox


def _is_finite(x: Any) -> bool:
    try:
        return math.isfinite(float(x))
    except Exception:
        return False


def _norm_bbox4(bb: list[float] | tuple[float, ...]) -> tuple[float, float, float, float] | None:
    if not isinstance(bb, (list, tuple)) or len(bb) < 4:
        return None
    try:
        x0, y0, x1, y1 = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
    except (TypeError, ValueError):
        return None
    lo_x, hi_x = (x0, x1) if x0 <= x1 else (x1, x0)
    lo_y, hi_y = (y0, y1) if y0 <= y1 else (y1, y0)
    if hi_x <= lo_x or hi_y <= lo_y:
        return None
    return (lo_x, lo_y, hi_x, hi_y)


def _bbox_contains(bb: tuple[float, float, float, float], x: float, y: float, pad: float = 0.0) -> bool:
    return (bb[0] - pad) <= x <= (bb[2] + pad) and (bb[1] - pad) <= y <= (bb[3] + pad)


def _median(vals: list[float]) -> float | None:
    a = [float(x) for x in vals if _is_finite(x)]
    if not a:
        return None
    a.sort()
    mid = len(a) // 2
    return a[mid] if len(a) % 2 else 0.5 * (a[mid - 1] + a[mid])


def _preview_eps_from_circle_centers(points: list[tuple[float, float]]) -> float:
    # JS: beamFlatPreviewCircleClusterEps(nearest distances) *1.9 clamp [72,180]
    if len(points) < 2:
        return 120.0
    nearest: list[float] = []
    for i, (xi, yi) in enumerate(points):
        best = float("inf")
        for j, (xj, yj) in enumerate(points):
            if i == j:
                continue
            d = math.hypot(xi - xj, yi - yj)
            if math.isfinite(d) and d < best:
                best = d
        if math.isfinite(best) and best < float("inf"):
            nearest.append(best)
    med = _median(nearest) or 70.0
    return max(72.0, min(180.0, float(med) * 1.9))


def _cluster_points_by_eps(points: list[tuple[float, float]], eps: float) -> list[list[tuple[float, float]]]:
    n = len(points)
    if n == 0:
        return []
    parent = list(range(n))
    e2 = float(eps) * float(eps)

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i: int, j: int) -> None:
        ri, rj = find(i), find(j)
        if ri != rj:
            parent[ri] = rj

    for i in range(n):
        xi, yi = points[i]
        for j in range(i + 1, n):
            xj, yj = points[j]
            dx, dy = xi - xj, yi - yj
            if dx * dx + dy * dy <= e2:
                union(i, j)

    buckets: dict[int, list[int]] = {}
    for i in range(n):
        buckets.setdefault(find(i), []).append(i)
    return [[points[i] for i in idxs] for idxs in buckets.values()]


def _split_clusters_by_y_band(clusters: list[list[tuple[float, float]]]) -> list[list[tuple[float, float]]]:
    # JS는 원 반경 median으로 yTol 계산. 서버는 반경 없이, 안전하게 고정 범위 사용.
    y_tol = 28.0
    out: list[list[tuple[float, float]]] = []
    for cl in clusters:
        pts = sorted(cl, key=lambda p: -p[1])
        cur: list[tuple[float, float]] = []
        y_ref: float | None = None
        for (x, y) in pts:
            if not cur:
                cur = [(x, y)]
                y_ref = y
                continue
            assert y_ref is not None
            if abs(y - y_ref) <= y_tol:
                cur.append((x, y))
                y_ref = sum(p[1] for p in cur) / len(cur)
            else:
                out.append(cur)
                cur = [(x, y)]
                y_ref = y
        if cur:
            out.append(cur)
    return out


def _bbox_from_points(pts: list[tuple[float, float]]) -> tuple[float, float, float, float] | None:
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))


@dataclass
class SectionCandidate:
    key: str
    x: float
    y: float
    bbox: tuple[float, float, float, float]


def collect_beam_flat_section_candidates_from_selection(
    db: Session,
    commit_id: int,
    selection_world_bboxes: list[tuple[float, float, float, float]] | None,
    *,
    include_block_definitions: bool = True,
) -> list[SectionCandidate]:
    bbs = selection_world_bboxes or []
    shapes_all: list[tuple[str, Any, str | None]] = []
    for bb in bbs:
        nb = _norm_bbox4(bb)
        if not nb:
            continue
        shapes, _src = _load_combined_shapes_in_bbox(
            db, commit_id, nb, include_block_definitions=include_block_definitions
        )
        shapes_all.extend(shapes)
    if not shapes_all:
        return []
    centers, _radii, _lines = _collect_circles_lines_radii(shapes_all)
    if len(centers) < 2:
        return []
    eps = _preview_eps_from_circle_centers(centers)
    raw_clusters = _cluster_points_by_eps(centers, eps)
    y_split = _split_clusters_by_y_band(raw_clusters)
    out: list[SectionCandidate] = []
    for i, cl in enumerate(y_split):
        bb = _bbox_from_points(cl)
        if not bb:
            continue
        cx = sum(p[0] for p in cl) / len(cl)
        cy = sum(p[1] for p in cl) / len(cl)
        out.append(SectionCandidate(key=f"flat_{i}", x=cx, y=cy, bbox=bb))
    # 큰 단면 먼저(면적 큰)
    out.sort(key=lambda s: (s.bbox[2] - s.bbox[0]) * (s.bbox[3] - s.bbox[1]), reverse=True)
    return out


_RE_MARK = re.compile(r"^\s*[A-Z]{1,3}\s*\d{1,4}[A-Z]?\s*(?:\(.+\))?\s*$", re.I)


def _is_mark_text(txt: str) -> bool:
    t = (txt or "").strip()
    return bool(t and _RE_MARK.match(t) and re.search(r"\d", t))


def _zone_key(txt: str) -> str:
    raw = (txt or "").strip()
    if not raw:
        return ""
    s = re.sub(r"\s+", "", raw).upper()
    if "INT" in s:
        return "INT"
    if s.startswith("CEN") or "CENTER" in s:
        return "CENTER"
    if "ALL" in s:
        return "ALL"
    if "EXT" in s or "EXTERIOR" in s:
        return "EXT"
    if "END" in s:
        return "END"
    return ""


def compute_beam_horizontal_picked_server(
    beam_row_clusters: list[dict[str, Any]],
    label_pool: list[dict[str, Any]],
    sections: list[SectionCandidate],
    *,
    row_y_tol: float = 10.0,
) -> dict[str, Any] | None:
    """
    JS computeBeamHorizontalZoneMatches의 핵심(부재↔부위, 부위↔단면 1:1)을 서버에서 재현.
    label_pool: load_text_entities 결과( id,x,y,text,... )
    """
    if not beam_row_clusters or not label_pool or not sections:
        return None
    lab_by_eid: dict[str, dict[str, Any]] = {}
    for it in label_pool:
        if not isinstance(it, dict):
            continue
        eid = it.get("id")
        if eid is None:
            continue
        lab_by_eid[str(eid)] = it

    marks: list[dict[str, Any]] = []
    zones: list[dict[str, Any]] = []
    for c in beam_row_clusters:
        ids = c.get("entity_ids") or []
        texts = c.get("texts") or []
        n = max(len(ids) if isinstance(ids, list) else 0, len(texts) if isinstance(texts, list) else 0)
        for i in range(n):
            eid = str(ids[i]) if isinstance(ids, list) and i < len(ids) and ids[i] is not None else ""
            txt = str(texts[i]) if isinstance(texts, list) and i < len(texts) and texts[i] is not None else ""
            txt = re.sub(r"\s+", " ", txt).strip()
            if not eid or not txt:
                continue
            lb = lab_by_eid.get(eid)
            if not lb:
                continue
            x, y = lb.get("x"), lb.get("y")
            if not (_is_finite(x) and _is_finite(y)):
                continue
            if _is_mark_text(txt):
                marks.append({"eid": eid, "x": float(x), "y": float(y), "text": txt})
            zk = _zone_key(txt)
            if zk:
                zones.append({"eid": eid, "x": float(x), "y": float(y), "text": txt, "z": zk})

    if not marks or not zones:
        return None

    mark_y_mean = sum(m["y"] for m in marks) / len(marks)
    zone_y_mean = sum(z["y"] for z in zones) / len(zones)
    prefer_mark_above = mark_y_mean > zone_y_mean

    def score_mz(m: dict[str, Any], z: dict[str, Any]) -> float:
        dx = abs(float(m["x"]) - float(z["x"]))
        dy = abs(float(m["y"]) - float(z["y"]))
        return dy + dx * 0.25

    def pick_best_mark_for_zone(z: dict[str, Any], locked: set[str] | None) -> dict[str, Any] | None:
        def scan(prefer_above: bool) -> dict[str, Any] | None:
            best: dict[str, Any] | None = None
            best_s = float("inf")
            for m in marks:
                if locked and str(m["eid"]) in locked:
                    continue
                if prefer_above and not (m["y"] > z["y"] + 0.25):
                    continue
                if (not prefer_above) and not (m["y"] < z["y"] - 0.25):
                    continue
                s = score_mz(m, z)
                if s < best_s:
                    best_s = s
                    best = m
            if not best:
                return None
            return {"from": best, "to": z, "zone": z["z"], "score": best_s}

        return scan(prefer_mark_above) or scan(not prefer_mark_above)

    locked_marks: set[str] = set()
    member_zone_lines: list[dict[str, Any]] = []
    # ALL 먼저
    for z in zones:
        if z["z"] != "ALL":
            continue
        L = pick_best_mark_for_zone(z, None)
        if not L:
            continue
        member_zone_lines.append(L)
        locked_marks.add(str(L["from"]["eid"]))
    # 나머지
    for z in zones:
        if z["z"] == "ALL":
            continue
        L = pick_best_mark_for_zone(z, locked_marks) or pick_best_mark_for_zone(z, None)
        if L:
            member_zone_lines.append(L)

    # zone -> section 후보(아래쪽) 1:1 greedy
    def score_zs(z: dict[str, Any], s: SectionCandidate) -> float:
        dx = abs(float(z["x"]) - float(s.x))
        dy = abs(float(z["y"]) - float(s.y))
        return dy + dx * 0.25

    cand: list[tuple[float, dict[str, Any], SectionCandidate]] = []
    for z in zones:
        for s in sections:
            # JS: section.y < zone.y (아래)
            if float(s.y) >= float(z["y"]):
                continue
            cand.append((score_zs(z, s), z, s))
    cand.sort(key=lambda t: t[0])
    used_zone: set[str] = set()
    used_sec: set[str] = set()
    zone_section_lines: list[dict[str, Any]] = []
    for sc, z, s in cand:
        zid = str(z["eid"])
        sid = str(s.key)
        if zid in used_zone or sid in used_sec:
            continue
        used_zone.add(zid)
        used_sec.add(sid)
        zone_section_lines.append(
            {"from": z, "to": {"key": s.key, "x": s.x, "y": s.y}, "zone": z["z"], "score": sc}
        )

    zone_to_section: dict[str, dict[str, Any]] = {str(L["from"]["eid"]): L["to"] for L in zone_section_lines}
    picked = [
        {
            "member": L["from"],
            "zone": L["to"],
            "section": zone_to_section.get(str(L["to"]["eid"])),
        }
        for L in member_zone_lines
    ]

    return {
        "memberZoneLines": member_zone_lines,
        "zoneSectionLines": zone_section_lines,
        "picked": picked,
    }

