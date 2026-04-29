"""
가로 flat 보 단면: **텍스트 행 클러스터와 분리**된 경로.

1) 느슨 창에서 **원(주근) 중심만** 모아 스트립 X로 자른다.
2) eps 클러스터로 **원 덩어리**를 고른다(텍스트 y_ref 없이 기하만).
3) 그 덩어리 bbox를 넓혀 **주변 LINE/LWPOLY(띠·외곽)** 를 포함한 도형 집합을 `analyze_section_geometry`에 넘긴다.

`column_section_geometry` 의 공용 텍스트 앵커·`_bbox_from_centerline_cluster` 는
flat 에서는 보조 폴백으로만 쓴다.
"""
from __future__ import annotations

from typing import Any

from shapely.geometry import Point

from app.services.column_section_geometry import (
    _collect_outline_rectangle_candidates,
    _collect_circles_lines_radii,
    _filter_shapes_by_bbox,
    _median_gap_unique,
    _median_unsorted,
)


def beam_flat_try_circle_first_section(
    loose_shapes: list[tuple[str, Any, str | None]],
    strip_xc: float,
    loose_hw: float,
    loose_hh: float,
    row_entity_bbox: list[float] | tuple[float, ...] | None,
    expected_main_bars: int | None,
    expected_depth_mm: int | None,
    strip_x_half_exclusive: float | None,
) -> (
    tuple[
        list[tuple[float, float]],
        dict[str, Any],
        tuple[float, float, float, float] | None,
        list[tuple[str, Any, str | None]],
        float,
        float,
    ]
    | None
):
    def _cluster_points_aniso(
        pts: list[tuple[float, float]],
        eps_x: float,
        eps_y: float,
    ) -> list[list[tuple[float, float]]]:
        """x/y 허용폭을 다르게 둔 느슨 클러스터(BFS)."""
        n = len(pts)
        if n < 1:
            return []
        seen = [False] * n
        out: list[list[tuple[float, float]]] = []
        ex = max(float(eps_x), 1.0)
        ey = max(float(eps_y), 1.0)
        for i in range(n):
            if seen[i]:
                continue
            seen[i] = True
            q = [i]
            comp: list[tuple[float, float]] = []
            while q:
                k = q.pop()
                xk, yk = pts[k]
                comp.append((xk, yk))
                for j in range(n):
                    if seen[j]:
                        continue
                    xj, yj = pts[j]
                    dx = abs(xj - xk)
                    dy = abs(yj - yk)
                    if dx > ex or dy > ey:
                        continue
                    # 타원형 거리 게이트로 대각선 과연결을 줄인다.
                    if (dx / ex) * (dx / ex) + (dy / ey) * (dy / ey) <= 1.0:
                        seen[j] = True
                        q.append(j)
            out.append(comp)
        return out

    def _filter_shapes_by_circle_center_inside_bbox(
        shapes: list[tuple[str, Any, str | None]],
        bbox: tuple[float, float, float, float],
        *,
        tol: float = 8.0,
        allowed_centers: list[tuple[float, float]] | None = None,
    ) -> list[tuple[str, Any, str | None]]:
        """
        CAD 단면 외곽이 확정된 경우, 원/점/원형 폴리곤은 중심점이 외곽 내부인 것만 유지한다.
        일반 LINE/LWPOLY 외곽·띠근은 bbox 교차 기준으로 유지한다.
        """
        x0, y0, x1, y1 = bbox
        out: list[tuple[str, Any, str | None]] = []
        allow = list(allowed_centers or [])
        tol2 = max(tol, 1.0) * max(tol, 1.0)

        def _allowed(cx: float, cy: float) -> bool:
            if not allow:
                return True
            for ax, ay in allow:
                dx, dy = float(cx) - float(ax), float(cy) - float(ay)
                if dx * dx + dy * dy <= tol2:
                    return True
            return False

        for sh in shapes:
            _et, _geom, ly = sh
            centers_one, _r, _ln = _collect_circles_lines_radii([sh])
            if centers_one:
                inside_centers = [
                    (cx, cy)
                    for cx, cy in centers_one
                    if (x0 - tol) <= cx <= (x1 + tol)
                    and (y0 - tol) <= cy <= (y1 + tol)
                    and _allowed(cx, cy)
                ]
                if not inside_centers:
                    continue
                all_inside = len(inside_centers) == len(centers_one)
                if all_inside and len(centers_one) == 1:
                    out.append(sh)
                    continue
                # 복합 도형/블록처럼 한 shape 안에 여러 원 중심이 섞이면,
                # 내부 중심만 POINT로 분리해 외부 원이 다시 분석에 섞이지 않게 한다.
                for cx, cy in inside_centers:
                    out.append(("POINT", Point(float(cx), float(cy)), ly))
                continue
            # 선분·외곽선은 기존 bbox 교차 기준으로 유지
            if _filter_shapes_by_bbox([sh], bbox):
                out.append(sh)
        return out

    def _collect_flat_rebar_centers(
        shapes: list[tuple[str, Any, str | None]],
    ) -> list[tuple[float, float]]:
        """
        flat 보 단면 원 후보.
        POINT는 TEXT 삽입 원점·디버그 점과 섞일 수 있어, CAD 단면 클러스터 생성에서는 제외한다.
        """
        centers: list[tuple[float, float]] = []
        for et, shp, ly in shapes:
            if str(et).upper() == "POINT":
                continue
            cc, _rr, _ll = _collect_circles_lines_radii([(et, shp, ly)])
            centers.extend(cc)
        return centers

    def _flat_center_source_debug(
        shapes: list[tuple[str, Any, str | None]],
        bbox: tuple[float, float, float, float],
    ) -> tuple[list[dict[str, Any]], dict[str, int]]:
        x0, y0, x1, y1 = bbox
        rows: list[dict[str, Any]] = []
        counts: dict[str, int] = {}
        for et, shp, ly in shapes:
            cc_all, _rr, _ll = _collect_circles_lines_radii([(et, shp, ly)])
            for cx, cy in cc_all:
                if not ((x0 - 8.0) <= cx <= (x1 + 8.0) and (y0 - 8.0) <= cy <= (y1 + 8.0)):
                    continue
                key = f"{str(et).upper()}|{str(ly or '')}"
                counts[key] = counts.get(key, 0) + 1
                if len(rows) < 64:
                    rows.append(
                        {
                            "type": str(et).upper(),
                            "layer": str(ly or ""),
                            "x": round(float(cx), 4),
                            "y": round(float(cy), 4),
                        }
                    )
        return rows, counts

    """
    성공 시: (centers_raw 전체, cluster_meta, tight_cluster_bbox, shapes_for_analysis, y_anchor, xc_cluster)
    실패 시 None → 호출측에서 기존 텍스트 연동 클러스터로 폴백.
    """
    centers_raw = _collect_flat_rebar_centers(loose_shapes)
    if len(centers_raw) < 4:
        return None

    # 최우선 경로: 텍스트 행/strip_xc를 보지 않고 CAD 닫힌 단면 외곽을 먼저 찾는다.
    # 외곽이 있으면 그 내부 원만 단면 원 클러스터로 확정하고 아래 텍스트 기반 보조 경로는 타지 않는다.
    outline_candidates_first = _collect_outline_rectangle_candidates(loose_shapes)
    if outline_candidates_first:
        cad_scored: list[
            tuple[
                tuple[float, float, float, float],
                tuple[float, float, float, float],
                str,
                list[tuple[float, float]],
                dict[str, Any],
            ]
        ] = []
        cad_debug: list[dict[str, Any]] = []
        centers_all_debug, _, _ = _collect_circles_lines_radii(loose_shapes)
        for bb, src in outline_candidates_first:
            ox0, oy0, ox1, oy1 = (float(bb[i]) for i in range(4))
            ow, oh = ox1 - ox0, oy1 - oy0
            inside = [
                p
                for p in centers_raw
                if (ox0 - 8.0) <= p[0] <= (ox1 + 8.0)
                and (oy0 - 8.0) <= p[1] <= (oy1 + 8.0)
            ]
            inside_all = [
                p
                for p in centers_all_debug
                if (ox0 - 8.0) <= p[0] <= (ox1 + 8.0)
                and (oy0 - 8.0) <= p[1] <= (oy1 + 8.0)
            ]
            dbg: dict[str, Any] = {
                "src": src,
                "bbox": [round(ox0, 4), round(oy0, 4), round(ox1, 4), round(oy1, 4)],
                "w": round(ow, 4),
                "h": round(oh, 4),
                "inside_circle_count": len(inside),
                "inside_circle_count_all_types": len(inside_all),
                "ignored_point_like_count": max(0, len(inside_all) - len(inside)),
            }
            if ow < 80.0 or oh < 80.0:
                dbg["reject"] = "outline_too_small"
                cad_debug.append(dbg)
                continue
            if len(inside) < 4:
                dbg["reject"] = "too_few_inside_circles"
                cad_debug.append(dbg)
                continue
            depth_delta = 0.0
            if isinstance(expected_depth_mm, int) and expected_depth_mm >= 220:
                depth_delta = abs(oh - float(expected_depth_mm))
                # 치수 텍스트는 후보 순위 보조만 한다. 위치 기준은 CAD 외곽/내부 원이다.
                if oh < float(expected_depth_mm) * 0.20 or oh > float(expected_depth_mm) * 2.60:
                    dbg["reject"] = "height_far_from_depth_hint"
                    dbg["depth_hint"] = int(expected_depth_mm)
                    cad_debug.append(dbg)
                    continue
            iy0 = min(p[1] for p in inside)
            iy1 = max(p[1] for p in inside)
            fill_y = (iy1 - iy0) / max(oh, 1.0)
            # 정렬 우선순위: 내부 원 많은 외곽, depth 힌트에 가까운 외곽, 큰 닫힌 외곽
            rank = (-float(len(inside)), depth_delta, -float(ow * oh), -float(fill_y))
            dbg["accepted"] = True
            dbg["rank"] = [round(x, 4) for x in rank]
            dbg["inside_y_span"] = round(iy1 - iy0, 4)
            dbg["inside_y_fill_ratio"] = round(fill_y, 4)
            cad_debug.append(dbg)
            cad_scored.append((rank, (ox0, oy0, ox1, oy1), src, inside, dbg))

        if cad_scored:
            _rank, outline_bb, outline_src, best, _dbg = min(cad_scored, key=lambda t: t[0])
            center_debug_rows, center_debug_counts = _flat_center_source_debug(
                loose_shapes,
                outline_bb,
            )
            shapes_for_analysis = _filter_shapes_by_circle_center_inside_bbox(
                loose_shapes,
                outline_bb,
                allowed_centers=best,
            )
            cc_chk, _, _ = _collect_circles_lines_radii(shapes_for_analysis)
            xc_cluster = float(_median_unsorted([p[0] for p in best]))
            y_anchor = float(_median_unsorted([p[1] for p in best]))
            meta: dict[str, Any] = {
                "mode": "beam_flat_cad_outline_first",
                "cluster_pick": "cad_outline_internal_circles",
                "cluster_size": len(best),
                "cluster_bbox": [round(outline_bb[i], 4) for i in range(4)],
                "cad_outline_circle_filter": True,
                "cad_outline_source": outline_src,
                "cad_outline_bbox": [round(outline_bb[i], 4) for i in range(4)],
                "cad_outline_inside_circle_count": len(best),
                "cad_outline_raw_candidate_count": len(outline_candidates_first),
                "cad_outline_candidate_count": len(cad_scored),
                "cad_outline_debug": cad_debug[:12],
                "cad_outline_center_sources": center_debug_rows,
                "cad_outline_center_source_counts": center_debug_counts,
                "cluster_from_cad_outline_internal_circles": True,
                "analysis_bbox": [round(outline_bb[i], 4) for i in range(4)],
                "analysis_bbox_pad": 0.0,
                "analysis_circle_center_filter_applied": True,
                "analysis_circle_count_after_center_filter": len(cc_chk),
                "analysis_circle_allowed_center_count": len(best),
                "centers_raw_total": len(centers_raw),
                "centers_raw_total_all_types": len(centers_all_debug),
                "centers_before_y_coarse": "-",
                "text_row_ignored_for_cluster": True,
                "point_entities_ignored_for_flat_rebar": True,
                "expected_main_bars": expected_main_bars,
                "expected_depth_mm": expected_depth_mm,
            }
            return (
                centers_raw,
                meta,
                outline_bb,
                shapes_for_analysis,
                y_anchor,
                xc_cluster,
            )

    xband = loose_hw * 0.93
    if strip_x_half_exclusive is not None and strip_x_half_exclusive > 50.0:
        # flat 보는 이웃 열 유입이 잦아 strip 반폭 정보를 우선 반영한다.
        xband = min(xband, max(float(strip_x_half_exclusive) * 2.1, 360.0))
    work = [p for p in centers_raw if abs(p[0] - strip_xc) <= xband]
    meta: dict[str, Any] = {
        "mode": "beam_flat_circle_first",
        "x_band": round(xband, 4),
        "strip_xc": round(float(strip_xc), 4),
        "loose_hw": round(float(loose_hw), 4),
        "loose_hh": round(float(loose_hh), 4),
        "expected_main_bars": expected_main_bars,
        "expected_depth_mm": expected_depth_mm,
        "centers_raw_total": len(centers_raw),
        "centers_before_y_coarse": len(work),
    }

    if isinstance(row_entity_bbox, (list, tuple)) and len(row_entity_bbox) >= 4:
        try:
            lo_y = float(row_entity_bbox[1])
            hi_y = float(row_entity_bbox[3])
            if hi_y > lo_y:
                span_y = hi_y - lo_y
                if span_y <= 500.0:
                    margin = max(45.0, min(92.0, span_y * 0.12 + 32.0))
                elif span_y <= 1200.0:
                    margin = max(80.0, min(180.0, span_y * 0.16 + 70.0))
                else:
                    margin = max(380.0, min(2800.0, span_y * 0.28 + 320.0))
                meta["row_bbox_for_circle"] = [
                    round(float(row_entity_bbox[i]), 4) for i in range(4)
                ]
                work_y = [p for p in work if (lo_y - margin) <= p[1] <= (hi_y + margin)]
                if len(work_y) >= 4:
                    work = work_y
                    meta["row_bbox_y_coarse_filter"] = True
                    meta["row_bbox_y_margin"] = round(margin, 4)
                    meta["centers_after_y_coarse"] = len(work)
        except (TypeError, ValueError):
            pass

    if len(work) < 4:
        work = [p for p in centers_raw if abs(p[0] - strip_xc) <= xband]
        meta["row_bbox_y_coarse_filter"] = False

    if len(work) < 4:
        meta["reason"] = "too_few_centers_after_x_y_filters"
        meta["centers_after_initial_filters"] = len(work)
        return None

    cad_outline_meta: dict[str, Any] | None = None
    outline_candidates = _collect_outline_rectangle_candidates(loose_shapes)
    meta["cad_outline_raw_candidate_count"] = len(outline_candidates)
    if outline_candidates:
        y_ref = None
        if isinstance(row_entity_bbox, (list, tuple)) and len(row_entity_bbox) >= 4:
            try:
                y_ref = 0.5 * (float(row_entity_bbox[1]) + float(row_entity_bbox[3]))
            except (TypeError, ValueError):
                y_ref = None
        scored_outline: list[tuple[float, tuple[float, float, float, float], str, list[tuple[float, float]]]] = []
        outline_debug: list[dict[str, Any]] = []
        for bb, src in outline_candidates:
            ox0, oy0, ox1, oy1 = (float(bb[i]) for i in range(4))
            ow, oh = ox1 - ox0, oy1 - oy0
            od: dict[str, Any] = {
                "src": src,
                "bbox": [round(ox0, 4), round(oy0, 4), round(ox1, 4), round(oy1, 4)],
                "w": round(ow, 4),
                "h": round(oh, 4),
            }
            if ow < 80.0 or oh < 80.0:
                od["reject"] = "outline_too_small"
                outline_debug.append(od)
                continue
            if expected_depth_mm is not None and expected_depth_mm >= 220:
                d = float(expected_depth_mm)
                if oh < d * 0.30 or oh > d * 2.25:
                    od["reject"] = "height_outside_depth_gate"
                    od["depth_gate"] = [round(d * 0.30, 4), round(d * 2.25, 4)]
                    outline_debug.append(od)
                    continue
            inside = [
                p
                for p in work
                if (ox0 - 8.0) <= p[0] <= (ox1 + 8.0) and (oy0 - 8.0) <= p[1] <= (oy1 + 8.0)
            ]
            od["inside_circle_count"] = len(inside)
            if len(inside) < 4:
                od["reject"] = "too_few_inside_circles"
                outline_debug.append(od)
                continue
            ocx, ocy = 0.5 * (ox0 + ox1), 0.5 * (oy0 + oy1)
            score = abs(ocx - float(strip_xc)) * 0.72
            if y_ref is not None:
                score += abs(ocy - y_ref) * 0.62
            # 원이 외곽 전체 높이에 분포할수록 단면 후보로 선호한다.
            iy0 = min(p[1] for p in inside)
            iy1 = max(p[1] for p in inside)
            fill_y = (iy1 - iy0) / max(oh, 1.0)
            score -= min(fill_y, 1.0) * 120.0
            od["score"] = round(score, 4)
            od["inside_y_span"] = round(iy1 - iy0, 4)
            od["inside_y_fill_ratio"] = round(fill_y, 4)
            od["accepted"] = True
            outline_debug.append(od)
            scored_outline.append((score, (ox0, oy0, ox1, oy1), src, inside))
        meta["cad_outline_debug"] = outline_debug[:12]
        if scored_outline:
            _sc, outline_bb, outline_src, inside_pts = min(scored_outline, key=lambda t: t[0])
            work = inside_pts
            cad_outline_meta = {
                "cad_outline_circle_filter": True,
                "cad_outline_source": outline_src,
                "cad_outline_bbox": [round(outline_bb[i], 4) for i in range(4)],
                "cad_outline_inside_circle_count": len(inside_pts),
                "cad_outline_candidate_count": len(scored_outline),
            }
            meta.update(cad_outline_meta)
        else:
            meta["cad_outline_circle_filter"] = False
            meta["cad_outline_reject_reason"] = "no_outline_with_enough_internal_circles"

    xs = [p[0] for p in work]
    ys = [p[1] for p in work]
    ux = sorted({round(x, 4) for x in xs})
    uy = sorted({round(y, 4) for y in ys})
    gx = _median_gap_unique(ux)
    gy = _median_gap_unique(uy)
    if gx <= 1e-6:
        gx = max(loose_hw * 0.028, 12.0)
    if gy <= 1e-6:
        gy = max(loose_hh * 0.028, 12.0)
    eps_link = max(gx, gy) * 1.14
    eps_link = max(52.0, min(eps_link, max(gx, gy) * 2.4 + 120.0))
    meta["median_gap_x"] = round(gx, 4)
    meta["median_gap_y"] = round(gy, 4)
    meta["cluster_eps"] = round(eps_link, 4)

    eps_x = max(gx * 1.18, 48.0)
    eps_y = max(min(gy * 0.95, eps_link * 0.94), 42.0)
    if len(uy) <= 3:
        eps_y = max(eps_y, gy * 1.05 + 4.0)
    min_need = 4
    if cad_outline_meta:
        # CAD 닫힌 외곽을 찾은 경우, 외곽 내부 원 전체가 단면 원 클러스터다.
        # 여기서 eps 클러스터를 다시 돌리면 내부 가로띠 일부가 후보로 보이며 혼선이 생긴다.
        clusters = [list(work)]
        candidates = [list(work)]
        meta["cluster_raw_count"] = 1
        meta["cluster_candidate_count_before_fallback"] = 1
        meta["cluster_from_cad_outline_internal_circles"] = True
    else:
        clusters = _cluster_points_aniso(work, eps_x, eps_y)
        candidates = [c for c in clusters if len(c) >= min_need]
        meta["cluster_raw_count"] = len(clusters)
        meta["cluster_candidate_count_before_fallback"] = len(candidates)
        if not candidates:
            # 타원 게이트로 끊기면 기존 원형 eps로 1회 폴백.
            from app.services.column_section_geometry import _cluster_points_by_eps

            clusters = _cluster_points_by_eps(work, eps_link)
            candidates = [c for c in clusters if len(c) >= min_need]
            meta["cluster_fallback_round_eps"] = True
            meta["cluster_raw_count_after_fallback"] = len(clusters)
            meta["cluster_candidate_count_after_fallback"] = len(candidates)
        if not candidates:
            meta["reason"] = "no_cluster_candidates_after_eps"
            return None
    meta["cluster_eps_x"] = round(eps_x, 4)
    meta["cluster_eps_y"] = round(eps_y, 4)

    # flat 오탐 차단: 가로 띠형(세로 스팬이 매우 작은 원줄) 후보는 우선 제거.
    if not cad_outline_meta and isinstance(expected_depth_mm, int) and expected_depth_mm >= 220:
        d = float(expected_depth_mm)
        min_span_y = max(58.0, d * 0.18)
        robust: list[list[tuple[float, float]]] = []
        reject_debug: list[dict[str, Any]] = []
        for cl in candidates:
            by0 = min(p[1] for p in cl)
            by1 = max(p[1] for p in cl)
            bx0 = min(p[0] for p in cl)
            bx1 = max(p[0] for p in cl)
            span_y = by1 - by0
            span_x = bx1 - bx0
            y_levels = len({round(p[1], 1) for p in cl})
            rd = {
                "n": len(cl),
                "bbox": [round(bx0, 4), round(by0, 4), round(bx1, 4), round(by1, 4)],
                "span_x": round(span_x, 4),
                "span_y": round(span_y, 4),
                "y_levels": y_levels,
            }
            if span_y >= min_span_y:
                rd["decision"] = "keep_span_y_ok"
                reject_debug.append(rd)
                robust.append(cl)
                continue
            # 단일 y줄은 단면 후보에서 제외
            if y_levels <= 1:
                rd["decision"] = "reject_single_y_level"
                reject_debug.append(rd)
                continue
            # y tier가 매우 적고 폭만 긴 경우는 수평 띠로 간주
            if y_levels <= 2 and span_x > d * 0.95:
                rd["decision"] = "reject_horizontal_band"
                reject_debug.append(rd)
                continue
            # span_y가 다소 작아도 폭이 과도하지 않으면(실제 단면 2단 주근 등) 유지
            rd["decision"] = "keep_compact_multitier"
            reject_debug.append(rd)
            robust.append(cl)
        meta["beam_flat_candidate_reject_debug"] = reject_debug[:20]
        if robust:
            candidates = robust
            meta["beam_flat_reject_horizontal_band"] = True
            meta["beam_flat_min_span_y"] = round(min_span_y, 4)
            meta["cluster_candidate_count_after_horizontal_reject"] = len(candidates)
        else:
            # depth 기반으로 봤을 때 전 후보가 수평 띠형이면 circle-first 자체를 중단한다.
            # (텍스트 가로행 오탐을 단면으로 확정하지 않기 위함)
            meta["reason"] = "all_candidates_rejected_as_horizontal_band"
            return None

    y_med_work = float(_median_unsorted(ys))

    def _tier_count(vals: list[float], tol: float) -> int:
        if not vals:
            return 0
        seq = sorted(float(v) for v in vals)
        c = 1
        last = seq[0]
        for v in seq[1:]:
            if abs(v - last) > tol:
                c += 1
                last = v
        return c

    def _tier_bin_counts(vals: list[float], tol: float) -> list[int]:
        if not vals:
            return []
        seq = sorted(float(v) for v in vals)
        bins: list[list[float]] = [[seq[0]]]
        for v in seq[1:]:
            if abs(v - bins[-1][-1]) <= tol:
                bins[-1].append(v)
            else:
                bins.append([v])
        return [len(b) for b in bins]

    def _score_cl(cl: list[tuple[float, float]]) -> tuple[float, int, float]:
        n = len(cl)
        cx = sum(p[0] for p in cl) / n
        cy = sum(p[1] for p in cl) / n
        dx = abs(cx - strip_xc)
        dy = abs(cy - y_med_work)
        tier_pen = 0.0
        if strip_x_half_exclusive is not None and strip_x_half_exclusive > 50.0:
            h = strip_x_half_exclusive
            for mul in (1.06, 1.26, 1.48, 1.68, 1.86):
                if dx <= h * mul:
                    tier_pen = mul * 0.001
                    break
            else:
                tier_pen = dx * 0.002
        bar_pen = 0.0
        if expected_main_bars is not None and expected_main_bars >= 8:
            bar_pen = abs(n - expected_main_bars) * 1.45
            if n > expected_main_bars * 1.25:
                # flat 오탐은 보통 "주근 + 주변 띠 원"이 합쳐져 개수가 과도하게 커진다.
                bar_pen += (n - expected_main_bars * 1.25) * 2.35
        bx0 = min(p[0] for p in cl)
        bx1 = max(p[0] for p in cl)
        by0 = min(p[1] for p in cl)
        by1 = max(p[1] for p in cl)
        span_x = max(bx1 - bx0, 1.0)
        span_y = max(by1 - by0, 1.0)
        aspect = max(span_x, span_y) / max(min(span_x, span_y), 1.0)
        dim_pen = 0.0
        if isinstance(expected_depth_mm, int) and expected_depth_mm >= 220:
            d = float(expected_depth_mm)
            # 보 단면 원군의 세로 스팬은 depth 대비 어느 정도 비율을 가져야 한다.
            # (가로 텍스트선 근처 오탐은 span_y가 비정상적으로 작다)
            if span_y < d * 0.26:
                dim_pen += (d * 0.26 - span_y) * 0.055
            elif span_y > d * 2.35:
                dim_pen += (span_y - d * 2.35) * 0.0065
            # 폭이 지나치게 넓고 높이가 매우 작은 띠형도 감점
            if span_x > d * 1.75 and span_y < d * 0.33:
                dim_pen += max(2.5, (span_x / max(span_y, 1.0)) * 0.45)

        y_tol = max(14.0, gy * 0.7)
        y_tiers = _tier_count([p[1] for p in cl], y_tol)
        x_tiers = _tier_count([p[0] for p in cl], max(14.0, gx * 0.7))
        tier_bonus = min(y_tiers, 4) * 1.9 + min(x_tiers, 4) * 0.9
        if y_tiers <= 1 and span_y < max(70.0, gy * 1.25):
            # 가로 띠형(단일 y 줄) 덩어리를 강하게 감점한다.
            tier_bonus -= 4.8
        elif y_tiers >= 2:
            y_bins = sorted(_tier_bin_counts([p[1] for p in cl], y_tol), reverse=True)
            if len(y_bins) >= 2 and y_bins[1] > 0:
                imbalance = float(y_bins[0]) / float(y_bins[1])
                if imbalance > 1.35:
                    # 8+4처럼 한 줄이 과하게 큰 묶음은 띠+단면 혼합일 확률이 높다.
                    tier_bonus -= (imbalance - 1.35) * 2.6

        geo_pad = max(max(gx, gy) * 0.9 + 54.0, 80.0)
        local_bb = (bx0 - geo_pad, by0 - geo_pad, bx1 + geo_pad, by1 + geo_pad)
        local_shapes = _filter_shapes_by_bbox(loose_shapes, local_bb)
        c_loc, _, lines_loc = _collect_circles_lines_radii(local_shapes)
        geom_bonus = min(len(lines_loc), 16) * 0.24 + min(len(local_shapes), 40) * 0.08
        # local 도형 중 원 비율이 지나치게 높으면(원 한 줄만 있는 경우) 보너스를 축소한다.
        if local_shapes:
            circ_ratio = min(1.0, len(c_loc) / max(len(local_shapes), 1))
            geom_bonus *= max(0.35, 1.0 - 0.45 * circ_ratio)

        shape_pen = max(0.0, aspect - 3.8) * 0.85
        score = (
            n * 0.38
            + tier_bonus
            + geom_bonus
            - shape_pen
            - bar_pen
            - dim_pen
            - dy * 0.0011
            - dx * 0.0014
            - tier_pen * 14.0
        )
        return (score, n, -dx)

    best = list(work) if cad_outline_meta else max(candidates, key=_score_cl)
    if cad_outline_meta:
        # CAD 외곽 내부 원만으로 이미 제한했으므로, 클러스터 표시는 원군보다 외곽을 우선한다.
        try:
            obb = cad_outline_meta["cad_outline_bbox"]
            if isinstance(obb, list) and len(obb) == 4:
                best = [p for p in work if float(obb[0]) - 8.0 <= p[0] <= float(obb[2]) + 8.0 and float(obb[1]) - 8.0 <= p[1] <= float(obb[3]) + 8.0]
        except (TypeError, ValueError, KeyError):
            pass
    meta["cluster_size"] = len(best)
    meta["cluster_pick"] = "geometry_only"

    _bx0 = min(p[0] for p in best)
    _bx1 = max(p[0] for p in best)
    _by0 = min(p[1] for p in best)
    _by1 = max(p[1] for p in best)
    span = max(_bx1 - _bx0, _by1 - _by0, 1.0)
    pad = max(gx, gy) * 0.68 + 42.0
    pad = min(pad, max(loose_hw, loose_hh) * 0.42)
    tight_bb = (_bx0 - pad, _by0 - pad, _bx1 + pad, _by1 + pad)
    if cad_outline_meta:
        try:
            obb2 = cad_outline_meta["cad_outline_bbox"]
            if isinstance(obb2, list) and len(obb2) == 4:
                tight_bb = tuple(float(obb2[i]) for i in range(4))  # type: ignore[assignment]
                meta["cluster_pick"] = "cad_outline_internal_circles"
        except (TypeError, ValueError, KeyError):
            pass
    meta["cluster_bbox"] = [round(tight_bb[i], 4) for i in range(4)]

    xc_cluster = float(_median_unsorted([p[0] for p in best]))
    y_anchor = float(_median_unsorted([p[1] for p in best]))

    # 단면 외곽·띠근 선분: CAD 외곽이 있으면 절대 확장하지 않고 그 내부 도형만 분석한다.
    if cad_outline_meta:
        pad_lines = 0.0
        analysis_bb = tight_bb
    else:
        span_b = max(_bx1 - _bx0, _by1 - _by0, 80.0)
        pad_lines = max(span_b * 0.28, 110.0, min(900.0, loose_hw * 0.22))
        ex0, ey0, ex1, ey1 = (
            tight_bb[0] - pad_lines,
            tight_bb[1] - pad_lines,
            tight_bb[2] + pad_lines,
            tight_bb[3] + pad_lines,
        )
        analysis_bb = (ex0, ey0, ex1, ey1)
    meta["analysis_bbox_pad"] = round(pad_lines, 4)
    meta["analysis_bbox"] = [round(analysis_bb[i], 4) for i in range(4)]

    if cad_outline_meta:
        shapes_for_analysis = _filter_shapes_by_circle_center_inside_bbox(
            loose_shapes,
            analysis_bb,
            allowed_centers=best,
        )
        cc_center_filtered, _, _ = _collect_circles_lines_radii(shapes_for_analysis)
        meta["analysis_circle_center_filter_applied"] = True
        meta["analysis_circle_count_after_center_filter"] = len(cc_center_filtered)
        meta["analysis_circle_allowed_center_count"] = len(best)
    else:
        shapes_for_analysis = _filter_shapes_by_bbox(loose_shapes, analysis_bb)
    if not cad_outline_meta and isinstance(expected_depth_mm, int) and expected_depth_mm >= 220:
        # 최종 분석 도형도 depth 기준으로 재클립해 텍스트 가로띠 원군 유입을 줄인다.
        d = float(expected_depth_mm)
        y_pad_strict = max(120.0, min(420.0, d * 0.34))
        x_pad_strict = max(120.0, min(460.0, d * 0.30))
        strict_bb = (
            _bx0 - x_pad_strict,
            _by0 - y_pad_strict,
            _bx1 + x_pad_strict,
            _by1 + y_pad_strict,
        )
        strict_shapes = _filter_shapes_by_bbox(loose_shapes, strict_bb)
        cc_strict, _, _ = _collect_circles_lines_radii(strict_shapes)
        if len(cc_strict) >= 4:
            shapes_for_analysis = strict_shapes
            meta["analysis_bbox_strict"] = [round(strict_bb[i], 4) for i in range(4)]
            meta["analysis_bbox_strict_applied"] = True
    cc_chk, _, _ = _collect_circles_lines_radii(shapes_for_analysis)
    if not cad_outline_meta and len(cc_chk) < 4:
        shapes_for_analysis = _filter_shapes_by_bbox(loose_shapes, tight_bb)
    meta["shapes_for_analysis_circle_count"] = len(cc_chk)

    return (
        centers_raw,
        meta,
        tight_bb,
        shapes_for_analysis,
        y_anchor,
        xc_cluster,
    )
