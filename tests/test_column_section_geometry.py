"""column_section_geometry: 스트립 X 전용 폭 등 순수 함수."""
from unittest.mock import MagicMock, patch

from shapely.geometry import LineString

from app.services.column_section_geometry import (
    _bbox_from_centerline_cluster,
    _clip_bbox_loose_y,
    _exclusive_strip_x_half_width,
    _infer_section_dimension_bh_mm,
    _merge_beam_vertical_twin_rebar_clusters,
    _pick_viewport_y_clip_for_section,
    enrich_rows_beam_vertical_section_geometry_zones,
)


def test_exclusive_strip_x_half_width_narrow_slot():
    """인접 스트립이 가까울 때 이웃 열 주근이 섞이지 않게 반폭이 줄어든다."""
    strips = [
        {"x_center": -3828.1358},
        {"x_center": -3062.7516},
        {"x_center": -591.2661},
    ]
    h_mid = _exclusive_strip_x_half_width(strips, 1, 6000.0)
    assert h_mid is not None
    assert 280.0 < h_mid < 520.0
    h_left = _exclusive_strip_x_half_width(strips, 0, 6000.0)
    assert h_left is not None and h_left > 200.0


def test_merge_beam_vertical_twin_rebar_clusters_joins_top_bottom():
    """보 단면: 상·하부 주근 덩어리를 y_ref 가 세로로 가로지르면 한 클러스터로 합친다."""
    top = [(0.0, 1500.0 + (i % 6) * 28.0 + (i // 6) * 6.0) for i in range(24)]
    bot = [(4.0, 1180.0 + (i % 4) * 32.0 + (i // 4) * 8.0) for i in range(16)]
    meta: dict = {}
    out = _merge_beam_vertical_twin_rebar_clusters(
        [top, bot],
        y_ref=1340.0,
        strip_xc=0.0,
        gx=22.0,
        gy=18.0,
        min_cluster=4,
        meta=meta,
    )
    assert len(out) == 1
    assert len(out[0]) == 40
    assert meta.get("beam_twin_rebar_cluster_merge") is True


def test_merge_beam_vertical_twin_rebar_clusters_skips_distant_floors():
    """인접 층 두 단면: y_ref 가 층 사이에 있어도 중심 간격이 크면 twin 합병하지 않는다."""
    low = [(0.0, 1000.0 + (i % 4) * 30.0 + (i // 4) * 8.0) for i in range(12)]
    high = [(5.0, 4000.0 + (i % 4) * 30.0 + (i // 4) * 8.0) for i in range(12)]
    meta: dict = {}
    out = _merge_beam_vertical_twin_rebar_clusters(
        [low, high],
        y_ref=2500.0,
        strip_xc=0.0,
        gx=22.0,
        gy=18.0,
        min_cluster=4,
        meta=meta,
        max_centroid_dy=900.0,
    )
    assert len(out) == 2
    assert meta.get("beam_twin_rebar_cluster_merge") is None


def test_beam_like_bbox_cluster_spans_top_and_bottom_rebar():
    top = [(0.0, 1500.0 + (i % 6) * 28.0 + (i // 6) * 6.0) for i in range(24)]
    bot = [(4.0, 1180.0 + (i % 4) * 32.0 + (i // 4) * 8.0) for i in range(16)]
    pts = top + bot
    bb, meta = _bbox_from_centerline_cluster(
        pts,
        strip_xc=0.0,
        y_ref=1340.0,
        loose_hw=8000.0,
        loose_hh=9000.0,
        min_cluster=4,
        anchor_y_bounds=(1050.0, 1620.0),
        beam_like_section=True,
    )
    assert bb is not None
    assert meta.get("beam_twin_rebar_cluster_merge") is True
    assert bb[3] - bb[1] > 280.0


def test_centerline_cluster_row_y_bounds_excludes_adjacent_floor():
    """row_data_anchor_y_bounds와 맞는 행의 주근만 남겨 위·아래 단면 혼선을 줄인다."""

    def cluster(cx: float, cy: float, n: int = 12) -> list[tuple[float, float]]:
        pts: list[tuple[float, float]] = []
        for i in range(n):
            row, col = divmod(i, 4)
            pts.append((cx - 150.0 + col * 100.0, cy - 80.0 + row * 40.0))
        return pts

    low = cluster(0.0, 1000.0)
    high = cluster(25.0, 5000.0)
    pts = low + high
    bb, meta = _bbox_from_centerline_cluster(
        pts,
        0.0,
        1020.0,
        4000.0,
        9000.0,
        min_cluster=4,
        segment_y_span=350.0,
        strip_x_half_exclusive=3500.0,
        expected_main_bars=12,
        anchor_y_bounds=(880.0, 1220.0),
    )
    assert bb is not None
    assert meta.get("row_y_bounds_filter") is True
    cyb = float(meta["cluster_centroid"][1])
    assert cyb < 2800.0, meta


def test_pick_viewport_y_clip_prefers_box_containing_anchor():
    a = (-20000.0, 500.0, -15000.0, 17000.0)
    b = (-20000.0, -4000.0, 80000.0, 17000.0)
    yc = _pick_viewport_y_clip_for_section(-16000.0, 15000.0, [a, b], pad_y=100.0)
    assert yc is not None
    assert yc[0] >= 400.0 and yc[1] <= 17100.0


def test_clip_bbox_loose_y_reduces_vertical_span():
    loose = (-1000.0, -5000.0, 1000.0, 25000.0)
    out = _clip_bbox_loose_y(loose, 1000.0, 20000.0)
    assert out[1] >= 1000.0 and out[3] <= 20000.0


def test_infer_section_dimension_bh_from_horizontal_vertical_lines_and_text():
    """가로 치수선→B, 세로 치수선→H, 단면 주근 bbox 기준."""
    minx, miny, maxx, maxy = 0.0, 0.0, 500.0, 700.0
    lines = [
        LineString([(0.0, 710.0), (500.0, 710.0)]),
        LineString([(-35.0, 0.0), (-35.0, 700.0)]),
    ]
    texts = [
        {"x": 250.0, "y": 750.0, "val": 500, "text": "500"},
        {"x": -120.0, "y": 350.0, "val": 700, "text": "700"},
    ]
    b_mm, h_mm, meta = _infer_section_dimension_bh_mm(
        minx,
        miny,
        maxx,
        maxy,
        lines,
        texts,
        outline_w_mm=500,
        outline_h_mm=700,
    )
    assert b_mm == 500
    assert h_mm == 700
    assert meta.get("b_from_line") == 500
    assert meta.get("h_from_line") == 700


def test_enrich_rows_beam_vertical_section_geometry_zones_one_geometry_per_strip():
    """부위(스트립)마다 합성 행으로 enrich 하고 beam_section_geometry_zones 에 순서대로 담는다."""

    def fake_enrich(_db, _cid, synth, _fh, _strips, **kwargs):
        for r in synth:
            si = r.get("column_strip_index")
            r["section_geometry"] = {"ok": True, "beam_strip_index": si}

    rows = [
        {
            "beam_vertical_merged_strip_indices": [2, 0],
            "width_mm": 1000,
            "depth_mm": 900,
        },
    ]
    strip_infos = [
        {"x_center": 10.0},
        {"x_center": 50.0},
        {"x_center": 30.0},
    ]
    field_headers = [{"label": "형태", "key": "section"}]

    with patch(
        "app.services.column_section_geometry.enrich_rows_column_section_geometry",
        side_effect=fake_enrich,
    ):
        enrich_rows_beam_vertical_section_geometry_zones(
            MagicMock(),
            1,
            rows,
            field_headers,
            strip_infos,
        )

    zones = rows[0]["beam_section_geometry_zones"]
    # x_center 오름차순: 스트립 0(10), 2(30)
    assert len(zones) == 2
    assert zones[0]["beam_strip_index"] == 0
    assert zones[0]["zone_index"] == 0
    assert zones[0]["section_geometry"]["beam_strip_index"] == 0
    assert zones[1]["beam_strip_index"] == 2
    assert zones[1]["zone_index"] == 1
    assert zones[1]["section_geometry"]["beam_strip_index"] == 2
    assert rows[0]["section_geometry"]["beam_strip_index"] == 0
