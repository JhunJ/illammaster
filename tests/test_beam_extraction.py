"""보 일람 추출 단위 테스트 (DB 없음)."""

from app.services.beam_flat_spatial import (
    beam_flat_row_has_beam_mark,
    beam_flat_x_slabs,
    is_flat_zone_anchor_text,
)
from app.services.beam_extraction import (
    _beam_centroid_entities,
    _beam_cluster_col_y_bounds_in_rows,
    _beam_cluster_mark_column_centers,
    extract_beam_row_cluster_bundle,
    _beam_row_cluster_zone_spans_rows,
    _beam_flat_section_focus_y_bounds,
    _beam_vertical_template_section_y_for_strip,
    _beam_vertical_cluster_mark_tuples_by_y_then_x,
    _beam_vertical_infer_strip_member_map,
    _beam_vertical_merge_mark_points_euclidean,
    _beam_vertical_zone_from_label,
    _beam_vertical_resplit_strips_by_distinct_zone_columns,
    _beam_vertical_filter_diagram_mm_bands,
    _beam_vertical_row_section_anchor_y,
    _beam_vertical_section_geom_y_bounds,
    _beam_vertical_merge_identity,
    _beam_vertical_prepare_bands_for_template_map,
    _beam_vertical_split_pending_by_member_mark,
    _is_standalone_section_mm_label,
    _prune_beam_vertical_template,
    normalize_beam_rebar_two_tier_notation,
    _enrich_beam_vertical_record,
    _merge_beam_vertical_records_across_strips,
    _record_from_beam_vertical_dynamic,
    _split_beam_mark_and_dim_parens,
    _split_beam_vertical_dyn_by_zone_spans,
    _beam_row_cluster_centroid_xy_mode_x,
    _beam_row_cluster_parse_horizontal_zone_slots,
    _beam_row_cluster_zone_slot_centroids_x,
    _beam_row_cluster_x_histogram_peak_centers,
    beam_duplicate_key,
    extract_beam_horizontal_from_clusters,
    extract_beam_vertical_blocks,
    process_beam_extraction,
)
from app.services.schedule_extraction import ExtractionConfig, _map_data_bands_to_template


def _row(items: list[tuple[float, float, str]]) -> list[dict]:
    return [{"x": x, "y": y, "text": t, "layer": "0", "id": 0, "entity_type": "TEXT"} for x, y, t in items]


def test_beam_row_cluster_parse_horizontal_zone_slots_end_cen():
    slots = _beam_row_cluster_parse_horizontal_zone_slots("END CEN")
    assert len(slots) == 2
    assert slots[0][1] == "ext" and slots[1][1] == "cen"


def test_beam_row_cluster_zone_slot_centroids_x_per_token():
    centers = [1000.0, 2200.0]
    band = _row(
        [
            (920, 3000, "END"),
            (1250, 3000, "CEN"),
            (1000, 3000, "G1"),
            (2200, 3000, "G2"),
        ]
    )
    slots = [("END", "ext"), ("CEN", "cen")]
    xs = _beam_row_cluster_zone_slot_centroids_x(band, centers, 0, slots)
    assert xs[0] is not None and abs(float(xs[0]) - 920.0) < 1.0
    assert xs[1] is not None and abs(float(xs[1]) - 1250.0) < 1.0


def test_extract_beam_row_cluster_bundle_splits_end_cen_into_two_strips():
    """부위 띠 셀에 END·CEN이 같이 있으면 부호 열마다 단면 스트립( X )을 둘로 나눈다."""
    y0, y1, y2, y3 = 3500.0, 3400.0, 3300.0, 3200.0
    r_mark = _row([(100, y0, "x"), (1000, y0, "G1"), (2400, y0, "G2")])
    r_band = _row(
        [
            (100, y1, "x"),
            (920, y1, "END"),
            (1180, y1, "CEN"),
            (1000, y1, "400x600"),
            (2380, y1, "CENTER"),
            (2400, y1, "500"),
        ]
    )
    r_sec = _row([(100, y2, "SECTION"), (1000, y2, "300x500"), (2400, y2, "300x500")])
    r_bar = _row([(100, y3, "Top"), (1000, y3, "2-D16"), (2400, y3, "2-D16")])
    rows, meta = extract_beam_row_cluster_bundle([r_mark, r_band, r_sec, r_bar], ExtractionConfig())
    g1 = [r for r in rows if r.get("mark") == "G1"]
    assert len(g1) >= 2
    zkeys = {str(r.get("beam_vertical_zone_key") or "") for r in g1}
    assert "ext" in zkeys and "cen" in zkeys
    xcs = sorted(float(r["_beam_row_cluster_centroid_x"]) for r in g1 if r.get("_beam_row_cluster_centroid_x") is not None)
    assert xcs[0] < xcs[-1]
    strips = meta.get("beam_vertical_strips") or []
    assert len(strips) >= 2
    xc_strip = [float(s.get("x_center") or 0) for s in strips[:2]]
    assert xc_strip[0] < xc_strip[1]


def test_extract_beam_row_cluster_bundle_vertical_zones_distinct_centroid_x_same_mark_column():
    """
    부위 띠가 열마다 'END'·'CENTER' 한 토큰(가로 다부위 아님)이어도, 세로 존(yb)마다
    해당 구간 텍스트만으로 X를 잡아 같은 부호 열의 END(ext)·CEN이 서로 다른 앵커 X를 갖는다.
    """
    r_mark = _row([(50, 4000, "부호"), (1000, 4000, "G1"), (2400, 4000, "G2B")])
    r_end = _row([(50, 3400, "END"), (1000, 3400, "400"), (2350, 3400, "400")])
    r_sec = _row([(50, 3300, "형태"), (1000, 3300, "400x600"), (2480, 3300, "400x600")])
    r_cen = _row([(50, 2800, "CENTER"), (1000, 2800, "400"), (2420, 2800, "400")])
    r_tail = _row([(50, 2500, "상부근"), (1000, 2500, "2-D16"), (2520, 2500, "2-D16")])
    rows, _meta = extract_beam_row_cluster_bundle(
        [r_mark, r_end, r_sec, r_cen, r_tail], ExtractionConfig()
    )
    g2b = [
        r
        for r in rows
        if r.get("mark") == "G2B"
        and r.get("beam_vertical_zone_key") in ("ext", "cen")
        and r.get("_beam_row_cluster_centroid_x") is not None
    ]
    assert len(g2b) == 2
    by_z = {str(r["beam_vertical_zone_key"]): float(r["_beam_row_cluster_centroid_x"]) for r in g2b}
    assert abs(by_z["ext"] - by_z["cen"]) > 25.0


def test_extract_beam_row_cluster_bundle_spreads_collapsed_zone_centroids_same_x_column():
    """존별 데이터 열 텍스트가 모두 동일 X(1000)에만 있으면 Y필터 후에도 centroid X가 같아 열 구간으로 분산된다."""
    r_mark = _row([(50, 4000, "부호"), (1000, 4000, "G2B"), (2400, 4000, "G3")])
    r_end = _row([(50, 3400, "END"), (1000, 3400, "400"), (2350, 3400, "400")])
    r_sec = _row([(50, 3300, "형태"), (1000, 3300, "400x600"), (2480, 3300, "400x600")])
    # G2B 열 값은 모두 x=1000 (세로 한 줄) — centroid 붕괴 유도
    r_cen = _row([(50, 2800, "CENTER"), (1000, 2800, "400"), (1000, 2750, "400")])
    r_tail = _row([(50, 2500, "상부근"), (1000, 2500, "2-D16"), (2520, 2500, "2-D16")])
    rows, _meta = extract_beam_row_cluster_bundle(
        [r_mark, r_end, r_sec, r_cen, r_tail], ExtractionConfig()
    )
    g2b = [
        float(r["_beam_row_cluster_centroid_x"])
        for r in rows
        if r.get("mark") == "G2B"
        and r.get("beam_vertical_zone_key") in ("ext", "cen")
        and r.get("_beam_row_cluster_centroid_x") is not None
    ]
    assert len(g2b) == 2
    assert abs(g2b[0] - g2b[1]) > 20.0


def test_beam_row_cluster_x_histogram_peak_centers_top_frequency_bins():
    """열 전체에 X가 두 군집이면 건수 상위 빈 중심이 둘로 나온다."""
    ents = [{"x": 1000.0, "y": float(i)} for i in range(12)] + [{"x": 2480.0, "y": float(i)} for i in range(10)]
    peaks = _beam_row_cluster_x_histogram_peak_centers(
        ents, bin_width=420.0, column_center_x=1700.0, num_peaks=2
    )
    assert len(peaks) == 2
    assert abs(peaks[0] - 1000.0) < 80.0 and abs(peaks[1] - 2480.0) < 80.0


def test_beam_row_cluster_centroid_xy_mode_x_prefers_dense_x_cluster():
    """열 내 다수 TEXT가 한쪽 X에 몰리고 병합 크기 한 점이 멀리 있으면, 최빈 X 구간을 택한다."""
    ents = [{"x": 2100.0, "y": float(i * 30)} for i in range(10)]
    ents.append({"x": 7800.0, "y": 12.0})
    mode_xy = _beam_row_cluster_centroid_xy_mode_x(ents, bin_width=420.0, column_center_x=2150.0)
    mean_xy = _beam_centroid_entities(ents)
    assert mode_xy and mean_xy
    assert abs(mode_xy[0] - 2100.0) < 25.0
    assert mean_xy[0] > 2600.0


def test_beam_flat_section_focus_y_bounds_between_zone_and_bar():
    """부위(INT)와 주근 줄이 Y로 벌어지면 단면용 세로창 후보를 그 사이로 잡는다."""
    row = _row(
        [
            (0, 1000, "RG11"),
            (200, 1100, "INT"),
            (400, 1120, "900"),
            (200, 2600, "28-UHD19"),
        ]
    )
    bb = _beam_flat_section_focus_y_bounds(row)
    assert bb is not None
    lo, hi = bb
    assert lo < hi
    assert hi - lo >= 200.0


def test_beam_flat_section_focus_y_bounds_same_line_skipped():
    """부위·철근이 같은 Y띠면 후보를 만들지 않는다(행 전체 bbox 유지)."""
    row = _row(
        [
            (0, 1000, "RG11"),
            (200, 1000, "INT"),
            (400, 1000, "28-UHD19"),
        ]
    )
    assert _beam_flat_section_focus_y_bounds(row) is None


def test_beam_flat_x_slabs_splits_by_zone_anchors():
    """부위명(INT·CENTER 등) X를 기준으로 인접 중점에서 열(슬랩)으로 나눈다."""
    row = _row(
        [
            (1000, 5000, "INT"),
            (2000, 5000, "28-UHD19"),
            (6000, 5000, "CENTER"),
            (7000, 5000, "4-UHD19"),
        ]
    )
    slabs = beam_flat_x_slabs(row)
    assert len(slabs) == 2
    assert len(slabs[0]) == 2
    assert len(slabs[1]) == 2


def test_beam_flat_row_has_beam_mark():
    """무부호(주근 줄만)는 부호가 없으면 단면 파이프라인에서 제외한다."""
    row_mark = _row([(1000, 5000, "RG11 (1000x900)")])
    row_mark[0]["mark"] = "RG11 (1000x900)"
    assert beam_flat_row_has_beam_mark({"_flat_sorted_entities": row_mark, "mark": "RG11 (1000x900)"})
    row_plain = _row([(2000, 5000, "28-UHD19"), (3000, 5000, "INT")])
    assert not beam_flat_row_has_beam_mark({"_flat_sorted_entities": row_plain, "mark": ""})


def test_flat_zone_anchor_accepts_parenthetical_qualifiers():
    """도면에 흔한 'END (INT)', 'INT (BOTH)' 형태도 부위 앵커로 인식한다."""
    assert is_flat_zone_anchor_text("END (INT)")
    assert is_flat_zone_anchor_text("INT (BOTH)")
    assert is_flat_zone_anchor_text("  CENTER  ")
    row = _row(
        [
            (1000, 5000, "INT (BOTH)"),
            (2000, 5000, "28-UHD19"),
            (6000, 5000, "END (INT)"),
            (7000, 5000, "4-UHD19"),
        ]
    )
    assert len(beam_flat_x_slabs(row)) == 2


def test_beam_flat_x_slabs_single_without_two_zones():
    """부위 앵커가 2개 미만이면 한 슬랩."""
    row = _row([(1000, 5000, "INT"), (2000, 5000, "28-UHD19")])
    assert len(beam_flat_x_slabs(row)) == 1


def test_beam_vertical_prepare_bands_sorts_by_section_y_keeps_all_text():
    """스트립→템플릿 매핑 전: 텍스트는 유지하고 단면(형태) Y에 가까운 순으로만 정렬한다."""
    band_tol = 6.0
    template: list[tuple[float, str, str]] = []
    y = 1000.0
    for i in range(17):
        lab = "부 호" if i == 0 else ("형 태" if i == 3 else ("상 부 근" if i == 8 else f"r{i}"))
        template.append((y, lab, f"k{i}"))
        y -= 50.0
    bands = [(980.0, "RG11"), (975.0, "END(INT)"), (720.0, "26-UHD19")]
    pre = _beam_vertical_prepare_bands_for_template_map(bands, template)
    ts = {t for _y, t in pre}
    assert ts == {"RG11", "END(INT)", "26-UHD19"}
    assert pre[0][0] == 975.0
    dyn = _map_data_bands_to_template(pre, template, band_tol, beam_vertical_schedule=True)
    assert any("26-UHD19" in str(v) for v in dyn.values())


def test_prune_beam_vertical_template_strips_mm_only_rows():
    """좌측에 900·전각 1000·900mm 같은 치수만 있는 행은 템플릿에서 제거(단면 Y창 왜곡 방지)."""
    assert _is_standalone_section_mm_label("900")
    assert _is_standalone_section_mm_label("900mm")
    assert _is_standalone_section_mm_label("１０００")
    assert _is_standalone_section_mm_label("1,000")
    assert not _is_standalone_section_mm_label("형       태")
    tpl = [
        (4700.0, "부       호", "k0"),
        (4650.0, "INT.(BOTH)", "k1"),
        (4550.0, "１０００", "k2"),
        (4520.0, "900mm", "k3"),
        (4500.0, "형       태", "k4"),
    ]
    out = _prune_beam_vertical_template(tpl)
    for _y, lab, _k in out:
        assert not _is_standalone_section_mm_label(lab), lab
    assert any("형" in (lab or "") for _y, lab, _k in out)


def test_beam_wide_table_sample():
    y0 = 1000.0
    y1 = 990.0
    header = _row(
        [
            (0, y0, "Name"),
            (80, y0, "Type"),
            (160, y0, "Material"),
            (240, y0, "Width"),
            (320, y0, "Depth"),
            (400, y0, "Int- TopBar"),
            (500, y0, "Cen- TopBar"),
            (600, y0, "Ext- TopBar"),
            (700, y0, "Int- BotBar"),
            (800, y0, "Cen- BotBar"),
            (900, y0, "Ext- BotBar"),
            (1000, y0, "Int- StirrupBar"),
            (1120, y0, "Cen- StirrupBar"),
            (1240, y0, "Ext- StirrupBar"),
            (1360, y0, "Side Bar"),
        ]
    )
    data = _row(
        [
            (0, y1, "G21"),
            (80, y1, "RC"),
            (160, y1, "By Story"),
            (240, y1, "500"),
            (320, y1, "600"),
            (400, y1, "7/7-D16"),
            (500, y1, "3-D16"),
            (600, y1, "7/7-D16"),
            (700, y1, "5-D16"),
            (800, y1, "7/6-D16"),
            (900, y1, "5-D16"),
            (1000, y1, "2-D10@100"),
            (1120, y1, "2-D10@150"),
            (1240, y1, "2-D10@100"),
            (1360, y1, "0-D10"),
        ]
    )
    hw = extract_beam_horizontal_from_clusters([header, data], wall_mode="cad", building=None)
    assert hw is not None
    rows, meta = hw
    assert meta.get("beam_wide_table")
    assert len(rows) == 1
    r = rows[0]
    assert r["mark"] == "G21"
    assert r.get("beam_cluster_row_index") == 1
    assert r["width_mm"] == 500
    assert r["depth_mm"] == 600
    assert "7/7-D16" in r["int_top_bar"]
    assert r["cen_top_bar"] == "3-D16"
    assert "2-D10@100" in r["ext_stirrup_bar"]
    ml = meta.get("beam_vertical_member_zone_match_lines") or []
    assert len(ml) == 1
    assert ml[0].get("mark") == "G21"
    assert len(ml[0].get("points") or []) == 3


def test_process_beam_auto_prefers_horizontal_cluster_bundle():
    """process_beam: 가로 Y묶음 표 인식이 행 묶음 번들보다 우선."""
    y0 = 1000.0
    y1 = 820.0
    header = _row(
        [
            (0, y0, "Name"),
            (80, y0, "Type"),
            (160, y0, "Material"),
            (240, y0, "Width"),
            (320, y0, "Depth"),
            (400, y0, "Int- TopBar"),
            (500, y0, "Cen- TopBar"),
            (600, y0, "Ext- TopBar"),
            (700, y0, "Int- BotBar"),
            (800, y0, "Cen- BotBar"),
            (900, y0, "Ext- BotBar"),
            (1000, y0, "Int- StirrupBar"),
            (1120, y0, "Cen- StirrupBar"),
            (1240, y0, "Ext- StirrupBar"),
            (1360, y0, "Side Bar"),
        ]
    )
    data = _row(
        [
            (0, y1, "G21"),
            (80, y1, "RC"),
            (160, y1, "By Story"),
            (240, y1, "500"),
            (320, y1, "600"),
            (400, y1, "7/7-D16"),
            (500, y1, "3-D16"),
            (600, y1, "7/7-D16"),
            (700, y1, "5-D16"),
            (800, y1, "7/6-D16"),
            (900, y1, "5-D16"),
            (1000, y1, "2-D10@100"),
            (1120, y1, "2-D10@150"),
            (1240, y1, "2-D10@100"),
            (1360, y1, "0-D10"),
        ]
    )
    items = header + data
    cfg = ExtractionConfig(
        beam_layout="auto",
        y_tolerance=50.0,
        beam_row_merge_y_max=4.0,
        beam_row_bridge_passes=1,
    )
    rows, v = process_beam_extraction(cfg, items)
    assert v.get("beam_layout_resolved") == "horizontal_wide"
    assert v.get("beam_wide_or_tree")
    assert not v.get("beam_vertical_blocks")
    assert not v.get("beam_row_cluster_bundle")
    assert len(rows) == 1 and rows[0].get("mark") == "G21"
    wst = v.get("beam_vertical_strips") or []
    assert len(wst) >= 1 and float(wst[0].get("x_center") or 0) > 0
    assert v.get("beam_vertical_field_headers")


def test_process_beam_row_cluster_bundle_no_vertical_strip():
    """헤더 없는 세로형 텍스트도 가로 Y행만으로 row_cluster_bundle 처리(세로 스트립 미사용)."""
    # Y 간격을 merge/bridge 한도보다 크게 두어 한 묶음으로 합쳐지지 않게 함
    y_name, y_w, y_top = 3000.0, 2910.0, 2820.0
    labels = _row(
        [
            (10, y_name, "Name"),
            (10, y_w, "Width"),
            (10, y_top, "Int. TopBar"),
        ]
    )
    data = _row(
        [
            (200, y_name, "BX1"),
            (200, y_w, "400"),
            (200, y_top, "5-D22"),
        ]
    )
    items = labels + data
    cfg = ExtractionConfig(
        beam_layout="auto",
        column_band_y_tol=6.0,
        y_tolerance=5.0,
        beam_row_merge_y_max=12.0,
        beam_row_bridge_passes=1,
    )
    rows, v = process_beam_extraction(cfg, items)
    assert v.get("beam_layout_resolved") == "row_cluster_bundle"
    assert v.get("beam_row_cluster_bundle")
    assert not v.get("beam_vertical_blocks")
    assert rows and (rows[0].get("mark") == "BX1" or rows[0].get("name") == "BX1")
    # 단면 enrich용 합성 스트립·헤더(이전에는 빈 배열이라 section_geometry 스킵됨)
    strips = v.get("beam_vertical_strips") or []
    headers = v.get("beam_vertical_field_headers") or []
    assert len(strips) >= 1 and float(strips[0].get("x_center") or 0) > 0
    assert headers
    assert rows[0].get("beam_vertical_merged_strip_indices") == [0]


def test_beam_location_tree_grouped():
    yh = 2000.0
    y1 = 1990.0
    y2 = 1980.0
    y3 = 1970.0
    header = _row(
        [
            (0, yh, "Name"),
            (100, yh, "Location"),
            (200, yh, "TopBar"),
            (300, yh, "BotBar"),
            (400, yh, "StirrupBar"),
        ]
    )
    r1 = _row(
        [
            (0, y1, "RG11"),
            (100, y1, "INT"),
            (200, y1, "4-D16"),
            (300, y1, "4-D16"),
            (400, y1, "2-D10@200"),
        ]
    )
    r2 = _row(
        [
            (0, y2, ""),
            (100, y2, "CENTER"),
            (200, y2, "2-D16"),
            (300, y2, "4-D16"),
            (400, y2, "2-D10@250"),
        ]
    )
    r3 = _row(
        [
            (0, y3, "RG12"),
            (100, y3, "ALL"),
            (200, y3, "6-D16"),
            (300, y3, "6-D16"),
            (400, y3, "2-D10@200"),
        ]
    )
    hw = extract_beam_horizontal_from_clusters([header, r1, r2, r3], wall_mode="cad", building="A동")
    assert hw is not None
    rows, meta = hw
    assert meta.get("beam_location_tree")
    assert len(rows) == 2
    by = {x["mark"]: x for x in rows}
    assert "RG11" in by
    assert by["RG11"]["int_top_bar"] == "4-D16"
    assert by["RG11"]["cen_top_bar"] == "2-D16"
    assert by["RG11"]["building"] == "A동"
    assert by["RG12"]["int_top_bar"] == "6-D16" and by["RG12"]["cen_top_bar"] == "6-D16"
    strips = meta.get("beam_vertical_strips") or []
    assert len(strips) == 2 and all(float(s.get("x_center") or 0) > 0 for s in strips)
    assert meta.get("beam_vertical_field_headers")


def test_process_beam_auto_flat_when_no_header():
    items = _row([(0, 100, "noise only")])
    cfg = ExtractionConfig(beam_layout="auto")
    rows, v = process_beam_extraction(cfg, items)
    assert v.get("beam_layout_resolved") == "flat"
    assert len(rows) == 1


def test_beam_vertical_blocks_smoke():
    """좌·우 X 갭으로 라벨/값 분리되는 최소 케이스."""
    y_name, y_w, y_top = 3000.0, 2990.0, 2980.0
    labels = _row(
        [
            (10, y_name, "Name"),
            (10, y_w, "Width"),
            (10, y_top, "Int. TopBar"),
        ]
    )
    data = _row(
        [
            (200, y_name, "BX1"),
            (200, y_w, "400"),
            (200, y_top, "5-D22"),
        ]
    )
    items = labels + data
    cfg = ExtractionConfig(beam_layout="vertical_blocks", column_band_y_tol=6.0)
    rows, meta = extract_beam_vertical_blocks(items, cfg)
    assert meta.get("beam_vertical_split_mode", "").startswith("x_gap") or meta.get(
        "beam_data_entity_count", 0
    ) >= 1
    if rows:
        assert rows[0].get("category") == "beam"
        assert rows[0].get("mark") == "BX1" or rows[0].get("name") == "BX1"


def test_beam_duplicate_key_stable():
    r = {"mark": "G1", "width_mm": 400, "depth_mm": 600, "int_top_bar": "2-D16"}
    assert beam_duplicate_key(r) == beam_duplicate_key(dict(r))


def test_beam_duplicate_key_differs_by_vertical_zone():
    a = {"mark": "G1", "width_mm": 400, "depth_mm": 600, "int_top_bar": "2-D16", "beam_vertical_zone_key": "int"}
    b = {**a, "beam_vertical_zone_key": "cen"}
    assert beam_duplicate_key(a) != beam_duplicate_key(b)


def test_row_cluster_zone_spans_detect_zone_in_data_cells_not_only_left_label():
    """부위 토큰이 좌측 라벨이 아니라 부호 열 셀에만 있어도 구간을 나눈다."""
    # 부호 열 X는 1800·2800 고정, INT는 첫 열(1800 근처) 텍스트로만 온다(좌측 라벨은 '표시'뿐).
    r_int = _row([(400, 3000, "표시"), (1700, 3000, "INT"), (1800, 3000, "RG1"), (2800, 3000, "RG2")])
    r_sec = _row([(400, 2850, "SECTION"), (1800, 2850, "300x500"), (2800, 2850, "300x500")])
    r_cen = _row([(400, 2550, "표시"), (1700, 2550, "CENTER"), (1800, 2550, "RG1"), (2800, 2550, "RG2")])
    block = [r_int, r_sec, r_cen]
    centers = _beam_cluster_mark_column_centers(block)
    assert centers is not None and len(centers) == 2
    spans = _beam_row_cluster_zone_spans_rows(block, centers)
    assert spans is not None
    assert any(seg[0] == "int" for seg in spans)
    assert any(seg[0] == "cen" for seg in spans)


def test_row_cluster_zone_spans_split_y_bounds_per_mark_column():
    """부위 라벨(INT/CENTER)마다 세그먼트를 나누면 열별 Y범위가 달라질 수 있다."""
    r_int = _row([(400, 3000, "INT"), (1000, 3000, "RG1"), (2000, 3000, "RG2")])
    r_sec = _row([(400, 2850, "SECTION"), (1000, 2850, "300x500"), (2000, 2850, "300x500")])
    r_intro = _row([(400, 2700, "기타"), (1000, 2700, "RG1"), (2000, 2700, "RG2")])
    r_cen = _row([(400, 2550, "CENTER"), (1000, 2550, "RG1"), (2000, 2550, "RG2")])
    r_tail = _row([(400, 2400, "HOOP"), (1000, 2400, "2-D10"), (2000, 2400, "2-D10")])
    block = [r_int, r_sec, r_intro, r_cen, r_tail]
    centers = _beam_cluster_mark_column_centers(block)
    assert centers is not None and len(centers) == 2
    spans = _beam_row_cluster_zone_spans_rows(block, centers)
    assert spans is not None
    int_rows = next(seg[2] for seg in spans if seg[0] == "int")
    cen_rows = next(seg[2] for seg in spans if seg[0] == "cen")
    y_int = _beam_cluster_col_y_bounds_in_rows(centers, int_rows, 0)
    y_cen = _beam_cluster_col_y_bounds_in_rows(centers, cen_rows, 0)
    assert y_int is not None and y_cen is not None
    assert y_int[0] <= 2700.0 <= y_int[1]
    assert y_cen[0] <= 2400.0 <= y_cen[1]
    assert y_int[1] > y_cen[1]


def test_normalize_beam_rebar_two_tier_d_notation():
    assert normalize_beam_rebar_two_tier_notation("11-11-D16") == "11/11-D16"
    assert normalize_beam_rebar_two_tier_notation("11/9-D16") == "11/9-D16"
    assert normalize_beam_rebar_two_tier_notation("26-UHD19") == "26-UHD19"
    assert normalize_beam_rebar_two_tier_notation("3-D10@100") == "3-D10@100"


def test_split_beam_mark_paren_size():
    b, w, h = _split_beam_mark_and_dim_parens("RG13C (1000x900)")
    assert b == "RG13C"
    assert w == 1000 and h == 900
    b2, w2, h2 = _split_beam_mark_and_dim_parens("RG13C")
    assert b2 == "RG13C" and w2 is None and h2 is None


def test_beam_vertical_enrich_korean_zones_and_paren_mark():
    order = ["k0", "k1", "k2", "k3", "k4", "k5"]
    titles = {
        "k0": "부       호",
        "k1": "INT.(BOTH)",
        "k2": "상  부  근",
        "k3": "하  부  근",
        "k4": "스  트  럽",
        "k5": "표 피 철 근",
    }
    cells = ["RG11 (1000x900)", "", "7-D16", "6-D16", "2-D10@200", "0-D10"]
    rec = {
        "beam_field_titles": titles,
        "beam_field_key_order": order,
        "cells": cells,
        "category": "beam",
    }
    _enrich_beam_vertical_record(rec)
    assert rec["mark"] == "RG11"
    assert rec["width_mm"] == 1000 and rec["depth_mm"] == 900
    assert rec["int_top_bar"] == "7-D16" and rec["ext_top_bar"] == "7-D16"
    assert rec["int_bot_bar"] == "6-D16" and rec["ext_bot_bar"] == "6-D16"
    assert rec["cen_stirrup_bar"] == "2-D10@200"
    assert rec["side_bar"] == "0-D10"


def test_beam_vertical_section_geom_y_bounds_narrower_than_full_block():
    """부호~표피 전체가 아니라 형태 주변(위 구간·첫 상부근 아래)으로 단면 창을 잡는다."""
    tpl = [
        (46900.0, "부       호", "k0"),
        (46500.0, "INT.(BOTH)", "k1"),
        (45200.0, "형       태", "k2"),
        (44000.0, "상  부  근", "k3"),
        (43750.0, "하  부  근", "k4"),
        (43480.0, "스  트  럽", "k5"),
        (43200.0, "표 피 철 근", "k6"),
    ]
    lo, hi = _beam_vertical_section_geom_y_bounds(tpl, 6.0)
    assert lo > 43100.0
    assert hi < 46950.0
    assert hi - lo < 46900.0 - 43200.0


def test_beam_vertical_section_geom_y_bounds_adjacent_members_do_not_overlap_much():
    """윗줄 부재 끝과 아랫줄 형태 사이에 단면 창이 크게 겹치지 않게 한다."""
    upper = [
        (38901.0, "부       호", "a0"),
        (38508.0, "ALL", "a1"),
        (37205.0, "형       태", "a2"),
        (36026.0, "상  부  근", "a3"),
        (35756.0, "하  부  근", "a4"),
    ]
    lower = [
        (34901.0, "부       호", "b0"),
        (34508.0, "END( INT. )", "b1"),
        (33205.0, "형       태", "b2"),
        (32026.0, "상  부  근", "b3"),
    ]
    u_lo, u_hi = _beam_vertical_section_geom_y_bounds(upper, 6.0)
    l_lo, l_hi = _beam_vertical_section_geom_y_bounds(lower, 6.0)
    assert u_hi is not None and l_hi is not None
    assert l_hi < u_lo + 400.0


def test_beam_vertical_row_section_anchor_y_from_shape_row():
    tpl = [
        (100.0, "부 호", "a"),
        (95.0, "형 태", "b"),
        (90.0, "상 부 근", "c"),
    ]
    y = _beam_vertical_row_section_anchor_y(tpl)
    assert y == 95.0


def test_beam_vertical_split_pending_separates_adjacent_members_same_segment():
    """같은 템플릿 세그먼트·부위라도 부호가 바뀌면 가로 인접 부재로 분리."""
    runs = _beam_vertical_split_pending_by_member_mark(
        [
            (100.0, {"mark": "RG11", "member_label": "RG11 (1000x900)"}),
            (150.0, {"mark": "", "member_label": ""}),
            (4000.0, {"mark": "RG12", "member_label": "RG12 (1000x900)"}),
        ]
    )
    assert len(runs) == 2
    assert len(runs[0]) == 2
    assert len(runs[1]) == 1
    assert _beam_vertical_merge_identity(runs[0][1][1]) == "RG11"


def test_beam_vertical_zone_from_data_bands_overrides_template_first_zone():
    """가로 쌍열: 우측 열에만 CENTER가 있으면 좌측 템플릿 첫 구간(INT)으로 고정되지 않는다."""
    tpl = [
        (3050.0, "부       호", "k0"),
        (3040.0, "INT.(BOTH)", "k1"),
        (3030.0, "형       태", "k2"),
        (3020.0, "상  부  근", "k3"),
    ]
    dyn = {"k0": "RG11 (1000x900)", "k1": "", "k2": "", "k3": "4-UHD19"}
    bands = [(3042.0, "CENTER"), (3021.0, "4-UHD19")]
    cfg = ExtractionConfig(beam_layout="vertical_blocks")
    r = _record_from_beam_vertical_dynamic(dyn, tpl, "unit", cfg, strip_bands=bands)
    assert r.get("beam_vertical_zone_display_label") == "CENTER"


def test_beam_vertical_two_strips_distinct_zone_labels_end_to_end():
    """RG11 가로 두 스트립: 각 열 위 부위 텍스트가 beam_vertical_zone_label_by_strip에 반영된다."""
    yb, yi, ys, ytop = 4050.0, 4040.0, 4030.0, 4020.0
    # x_gap 분할 시 최대 간격이 (라벨열↔첫 데이터열)이 되도록: 데이터 열 간격(140) < 라벨~첫열(190)
    x_lab, x_left, x_right = 10.0, 200.0, 340.0
    labels = _row(
        [
            (x_lab, yb, "Name"),
            (x_lab, yi, "INT.(BOTH)"),
            (x_lab, ys, "형       태"),
            (x_lab, ytop, "상  부  근"),
        ]
    )
    left = _row(
        [
            (x_left, yb, "RG11 (1000x900)"),
            (x_left, yi, "INT.(BOTH)"),
            (x_left, ys, "RC"),
            (x_left, ytop, "26-UHD19"),
        ]
    )
    # 조각 스트립끼리 병합(fragment_pair) 방지: 한 열 엔티티 수 > narrow_strip_max(12)
    for j in range(9):
        left.append(
            {
                "x": x_left,
                "y": 3800.0 - float(j),
                "text": ".",
                "layer": "0",
                "id": 100 + j,
                "entity_type": "TEXT",
            }
        )
    right = _row(
        [
            (x_right, yb, "RG11 (1000x900)"),
            (x_right, yi, "CENTER"),
            (x_right, ys, "RC"),
            (x_right, ytop, "4-UHD19"),
        ]
    )
    for j in range(9):
        right.append(
            {
                "x": x_right,
                "y": 3780.0 - float(j),
                "text": ".",
                "layer": "0",
                "id": 200 + j,
                "entity_type": "TEXT",
            }
        )
    items = labels + left + right
    cfg = ExtractionConfig(
        beam_layout="vertical_blocks",
        column_band_y_tol=6.0,
        column_strip_gap=120.0,
    )
    rows, meta = extract_beam_vertical_blocks(items, cfg)
    assert len(rows) >= 1
    m = rows[0]
    assert m.get("mark") == "RG11" or "RG11" in str(m.get("member_label") or "")
    rsa = m.get("row_section_anchor_y")
    assert rsa is not None and abs(float(rsa) - 4030.0) < 0.6
    zmap = m.get("beam_vertical_zone_label_by_strip") or {}
    assert zmap.get("0") == "INT.(BOTH)"
    assert zmap.get("1") == "CENTER"
    ml = meta.get("beam_vertical_member_zone_match_lines")
    assert isinstance(ml, list) and len(ml) >= 2
    for ent in ml:
        pts = ent.get("points")
        assert isinstance(pts, list) and len(pts) == 3
        assert all(
            isinstance(p, (list, tuple)) and len(p) == 2
            and all(isinstance(c, (int, float)) for c in p)
            for p in pts
        )


def test_beam_vertical_resplit_merged_strip_when_zone_headers_differ():
    """한 스트립으로 잘못 붙은 INT·CENTER 열을 X 공극 + 부위 텍스트로 다시 나눈다."""
    tpl = [
        (4100.0, "부       호", "k0"),
        (4090.0, "INT.(BOTH)", "k1"),
        (4080.0, "형       태", "k2"),
        (4070.0, "상  부  근", "k3"),
    ]
    band_tol = 6.0
    left = _row(
        [
            (200.0, 4100.0, "RG13"),
            (200.0, 4090.0, "INT.(BOTH)"),
            (200.0, 4080.0, "RC"),
            (200.0, 4070.0, "28-UHD19"),
        ]
    )
    right = _row(
        [
            (520.0, 4100.0, "RG13"),
            (520.0, 4090.0, "CENTER"),
            (520.0, 4080.0, "RC"),
            (520.0, 4070.0, "4-UHD19"),
        ]
    )
    merged = left + right
    out, lg = _beam_vertical_resplit_strips_by_distinct_zone_columns([merged], tpl, band_tol)
    assert len(out) == 2
    assert lg and lg[0].get("kind") == "zone_column_resplit"
    assert lg[0].get("zone_left") == "INT.(BOTH)"
    assert lg[0].get("zone_right") == "CENTER"


def test_beam_vertical_merge_euclidean_and_y_then_x_cluster():
    """GH 거리 임계: 근접 TEXT는 유클리드 병합 후 Y→X 순 클러스터."""
    frag = [(100.0, 4200.0, "RG"), (103.0, 4198.0, "13-A")]
    merged = _beam_vertical_merge_mark_points_euclidean(frag, 25.0)
    assert len(merged) == 1
    assert "13-A" in merged[0][2]
    row = [(200.0, 4200.0, "A"), (900.0, 4195.0, "B")]
    cs = _beam_vertical_cluster_mark_tuples_by_y_then_x(row, y_tol=40.0, x_gap=120.0)
    assert len(cs) == 2


def test_beam_vertical_zone_synonyms_per_ghx_panels():
    """Grasshopper 패널(INT/BOTH·CENTER 동의어)과 맞춘 부위 토큰."""
    assert _beam_vertical_zone_from_label("BOTH") == "both"
    assert _beam_vertical_zone_from_label("양단부") == "both"
    assert _beam_vertical_zone_from_label("CEN") == "cen"
    assert _beam_vertical_zone_from_label("중앙부") == "cen"


def test_beam_vertical_zone_end_int_vs_end_ext_parentheses():
    """END( INT. ) / END( EXT. ) — 괄호 한정이 있으면 단순 END(ext)로 오인하지 않는다."""
    assert _beam_vertical_zone_from_label("END( INT. )") == "int"
    assert _beam_vertical_zone_from_label("END(INT)") == "int"
    assert _beam_vertical_zone_from_label("END( EXT. )") == "ext"
    assert _beam_vertical_zone_from_label("END(EXT)") == "ext"


def test_beam_vertical_infer_three_name_row_marks_prefer_over_single_between():
    """3열 + 부호행 부재명 3개가 있으면 between에 1개만 있어도 부호행 3개로 1:1:1."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "END( INT. )", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    marks = _row(
        [
            (200.0, 4200.0, "RG13A"),
            (1100.0, 4200.0, "RG13B"),
            (2100.0, 4200.0, "RG13C"),
            (1900.0, 4150.0, "RG88 (400x400)"),
        ]
    )
    s0 = _row([(200.0, 4100.0, "INT"), (200.0, 4080.0, "x")])
    s1 = _row([(1100.0, 4100.0, "CENTER"), (1100.0, 4080.0, "x")])
    s2 = _row([(2100.0, 4100.0, "EXT"), (2100.0, 4080.0, "x")])
    strips = [s0, s1, s2]
    data = marks + s0 + s1 + s2
    infos = [
        {"index": 0, "x_center": 200.0, "entity_count": 2},
        {"index": 1, "x_center": 1100.0, "entity_count": 2},
        {"index": 2, "x_center": 2100.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("ok")
    assert dbg.get("source") == "name_row_strip_count_match"
    assert m[0] == "RG13A" and m[1] == "RG13B" and m[2] == "RG13C"


def test_beam_vertical_infer_same_title_broadcasts_to_all_strips():
    """부위~부호 사이에 동일 부재명이 2곳(열 위 중복) → INT/CENTER 스트립 모두 동일."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "INT.(BOTH)", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    marks = _row(
        [
            (280.0, 4150.0, "RG11 (1000x900)"),
            (480.0, 4150.0, "RG11 (1000x900)"),
        ]
    )
    s0 = _row([(200.0, 4100.0, "INT"), (200.0, 4080.0, "x")])
    s1 = _row([(400.0, 4100.0, "CENTER"), (400.0, 4080.0, "x")])
    strips = [s0, s1]
    data = marks + s0 + s1
    infos = [
        {"index": 0, "x_center": 200.0, "entity_count": 2},
        {"index": 1, "x_center": 400.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("ok")
    assert "same_title_all_strips" in str(dbg.get("source") or "")
    assert m[0] == m[1] == "RG11 (1000x900)"


def test_beam_vertical_infer_member_map_one_between_three_strips():
    """부위~부호 사이에 부재명 1개 → 세 스트립 동일 부재."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "INT.(BOTH)", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    marks = _row([(300.0, 4150.0, "RG13 (1000x900)")])
    z1 = _row([(200.0, 4100.0, "INT"), (200.0, 4080.0, "x")])
    z2 = _row([(400.0, 4100.0, "CENTER"), (400.0, 4080.0, "x")])
    z3 = _row([(600.0, 4100.0, "EXT"), (600.0, 4080.0, "x")])
    strips = [z1, z2, z3]
    data = marks + z1 + z2 + z3
    infos = [
        {"index": 0, "x_center": 200.0, "entity_count": 2},
        {"index": 1, "x_center": 400.0, "entity_count": 2},
        {"index": 2, "x_center": 600.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("ok")
    assert m.get(0) == "RG13 (1000x900)" and m.get(1) == m.get(0) == m.get(2)


def test_beam_vertical_infer_sparse_strip_infos_uses_index_field():
    """빈 스트립이 앞에 있으면 strip_infos[si]가 아니라 index로 x_center를 찾아야 한다."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "ALL", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    marks = _row(
        [
            (200.0, 4200.0, "RG-LEFT"),
            (5000.0, 4200.0, "RG-RIGHT"),
        ]
    )
    s0: list = []
    s1 = _row([(200.0, 4100.0, "X"), (200.0, 4080.0, "1")])
    s2 = _row([(5000.0, 4100.0, "Y"), (5000.0, 4080.0, "2")])
    strips = [s0, s1, s2]
    data = marks + s1 + s2
    infos = [
        {"index": 1, "x_center": 200.0, "entity_count": 2},
        {"index": 2, "x_center": 5000.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("ok")
    assert m.get(1) == "RG-LEFT"
    assert m.get(2) == "RG-RIGHT"


def test_beam_vertical_infer_count_tie_y_prefers_between_when_centered():
    """부호행·중간 밴드 클러스터 수가 모두 스트립 수와 같을 때 Y로 구분(GHX 위치 매칭)."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "INT.(BOTH)", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    # 부호행보다 아래쪽(도면 Y+)에 부재명이 있으면 d_row가 커져 between 쪽이 선택된다.
    y_row = 4230.0
    y_bet = 4150.0
    marks = _row(
        [
            (200.0, y_bet, "RG-BET-A"),
            (1100.0, y_bet, "RG-BET-B"),
            (2100.0, y_bet, "RG-BET-C"),
            (200.0, y_row, "RG-ROW-A"),
            (1100.0, y_row, "RG-ROW-B"),
            (2100.0, y_row, "RG-ROW-C"),
        ]
    )
    s0 = _row([(200.0, 4100.0, "INT"), (200.0, 4080.0, "x")])
    s1 = _row([(1100.0, 4100.0, "CENTER"), (1100.0, 4080.0, "x")])
    s2 = _row([(2100.0, 4100.0, "EXT"), (2100.0, 4080.0, "x")])
    strips = [s0, s1, s2]
    data = marks + s0 + s1 + s2
    infos = [
        {"index": 0, "x_center": 200.0, "entity_count": 2},
        {"index": 1, "x_center": 1100.0, "entity_count": 2},
        {"index": 2, "x_center": 2100.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("ok")
    assert "count_tie_y" in str(dbg.get("source") or "")
    assert m[0] == "RG-BET-A" and m[1] == "RG-BET-B" and m[2] == "RG-BET-C"


def test_beam_vertical_infer_member_map_three_between_three_strips():
    """부위~부호 사이에 부재명 3개 → 스트립 X 순으로 1:1."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "CENTER", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    marks = _row(
        [
            (200.0, 4150.0, "RG13B-INT"),
            (400.0, 4150.0, "RG13-CEN"),
            (600.0, 4150.0, "RG13-EXT"),
        ]
    )
    s0 = _row([(200.0, 4100.0, "END(INT)"), (200.0, 4070.0, "1")])
    s1 = _row([(400.0, 4100.0, "CENTER"), (400.0, 4070.0, "2")])
    s2 = _row([(600.0, 4100.0, "END(EXT)"), (600.0, 4070.0, "3")])
    strips = [s0, s1, s2]
    data = marks + s0 + s1 + s2
    infos = [
        {"index": 0, "x_center": 200.0, "entity_count": 2},
        {"index": 1, "x_center": 400.0, "entity_count": 2},
        {"index": 2, "x_center": 600.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("source") == "between_strip_count_match"
    assert m[0] == "RG13B-INT" and m[1] == "RG13-CEN" and m[2] == "RG13-EXT"


def test_beam_vertical_infer_member_map_two_on_name_row_three_strips():
    """부호 행에 부재명 2개만(부재 사이 패턴) → 가운데 스트립은 더 가까운 쪽."""
    tpl = [
        (4200.0, "부       호", "k0"),
        (4100.0, "ALL", "k1"),
        (4080.0, "형       태", "k2"),
    ]
    band_tol = 6.0
    marks = _row([(250.0, 4200.0, "RG11A"), (550.0, 4200.0, "RG11B")])
    s0 = _row([(200.0, 4100.0, "X"), (200.0, 4070.0, "1")])
    s1 = _row([(400.0, 4100.0, "Y"), (400.0, 4070.0, "2")])
    s2 = _row([(600.0, 4100.0, "Z"), (600.0, 4070.0, "3")])
    strips = [s0, s1, s2]
    data = marks + s0 + s1 + s2
    infos = [
        {"index": 0, "x_center": 200.0, "entity_count": 2},
        {"index": 1, "x_center": 400.0, "entity_count": 2},
        {"index": 2, "x_center": 600.0, "entity_count": 2},
    ]
    m, dbg = _beam_vertical_infer_strip_member_map(data, tpl, strips, infos, band_tol)
    assert dbg.get("source") == "name_row_two_marks"
    assert m[0] == "RG11A" and m[2] == "RG11B"
    assert m[1] in ("RG11A", "RG11B")


def test_beam_vertical_merge_two_strips_int_and_cen_columns():
    """같은 세그먼트에 X로 나뉜 두 스트립 → int·cen 필드가 한 행으로 합쳐진다."""
    a = {
        "mark": "RG11",
        "int_top_bar": "26-UHD19",
        "ext_top_bar": "26-UHD19",
        "int_bot_bar": "4-UHD19",
        "ext_bot_bar": "4-UHD19",
        "beam_strip_index": 0,
        "beam_vertical_zone_display_label": "INT.(BOTH)",
    }
    b = {
        "mark": "",
        "cen_top_bar": "4-UHD19",
        "cen_bot_bar": "11-UHD19",
        "beam_strip_index": 1,
        "beam_vertical_zone_display_label": "CENTER",
    }
    m = _merge_beam_vertical_records_across_strips([a, b])
    assert m["mark"] == "RG11"
    assert m["cen_top_bar"] == "4-UHD19"
    assert m["int_top_bar"] == "26-UHD19"
    assert m["beam_vertical_merged_strip_indices"] == [0, 1]
    assert m.get("beam_vertical_zone_label_by_strip") == {"0": "INT.(BOTH)", "1": "CENTER"}


def test_beam_vertical_template_section_y_uses_neighbor_strip_for_int_column():
    """맨 왼쪽 열(INT)만 max(Y)가 낮을 때 옆 열 문자로 상단 Y를 올려 올바른 층 형태를 고른다."""
    template = [
        (45205.0, "형       태", "k1"),
        (41205.0, "형       태", "k2"),
        (37205.0, "형       태", "k3"),
    ]
    st0 = _row([(3973.0, 38941.0, "END"), (4000.0, 36000.0, "1")])
    st1 = _row([(6320.0, 46941.0, "RG13C"), (6320.0, 42504.0, "26-UHD19")])
    y = _beam_vertical_template_section_y_for_strip(
        template, st0, xc_focus=3973.0, x_gate=4500.0, neighbor_strips=[st1]
    )
    assert y == 45205.0


def test_beam_vertical_template_section_y_uses_strip_top_not_whole_median():
    """여러 층 형태 Y가 있을 때 스트립 전체 Y중앙이 아래로 가면 잘못된 층이 선택되지 않게 한다."""
    template = [
        (45205.0, "형       태", "형_태__1"),
        (41205.0, "형       태", "형_태__7"),
        (37205.0, "형       태", "형_태__13"),
    ]
    st = _row(
        [
            (7363.0, 46941.0, "ALL"),
            (7380.0, 44000.0, "26-UHD19"),
            (7360.0, 32000.0, "noise"),
        ]
    )
    y = _beam_vertical_template_section_y_for_strip(template, st, xc_focus=7363.0, x_gate=5000.0)
    assert y == 45205.0


def test_merge_beam_vertical_median_y_bounds_and_per_strip_maps():
    """병합 시 합집합 대신 lo/hi 중앙값으로 세로창을 안정화하고, 스트립별 bounds·앵커를 남긴다."""
    a = {
        "mark": "RG1",
        "beam_strip_index": 0,
        "row_data_anchor_y_bounds": [40000.0, 42200.0],
        "row_section_anchor_y": 45205.0,
    }
    b = {
        "mark": "RG1",
        "beam_strip_index": 1,
        "row_data_anchor_y_bounds": [36000.0, 38200.0],
        "row_section_anchor_y": 45205.0,
    }
    c = {
        "mark": "RG1",
        "beam_strip_index": 2,
        "row_data_anchor_y_bounds": [40100.0, 42300.0],
        "row_section_anchor_y": 41205.0,
    }
    m = _merge_beam_vertical_records_across_strips([a, b, c])
    # 스트립별로 형태 Y 기준 세로창 상한(필드 헤더 없을 때 ~1323)으로 먼저 클램프 후 lo/hi 중앙값
    assert m["row_data_anchor_y_bounds"] == [44543.5, 45866.5]
    assert m["beam_vertical_strip_row_y_bounds"] == {
        "0": [44543.5, 45866.5],
        "1": [44543.5, 45866.5],
        "2": [40543.5, 41866.5],
    }
    assert m["beam_vertical_strip_section_anchor_y"] == {
        "0": 45205.0,
        "1": 45205.0,
        "2": 41205.0,
    }


def test_beam_vertical_filter_drops_mm_near_shape_row():
    template = [
        (100.0, "형       태", "k_shape"),
        (90.0, "상  부  근", "k_top"),
    ]
    bands = [(99.5, "900"), (89.0, "7-D20")]
    out = _beam_vertical_filter_diagram_mm_bands(bands, template)
    assert len(out) == 1
    assert out[0][1] == "7-D20"


def test_beam_vertical_zone_spans_one_mark_multi_zone_records():
    """한 부호 아래 INT·END 등 구간이 여러 번이면 레코드를 부위별로 나눈다."""
    sub_t = [
        (100.0, "부 호", "k0"),
        (99.0, "INT.(BOTH)", "k1"),
        (98.0, "상 부 근", "k2"),
        (97.0, "END( INT. )", "k3"),
        (96.0, "상 부 근", "k4"),
    ]
    sub_dyn = {"k0": "B1", "k1": "", "k2": "10-D22", "k3": "", "k4": "4-D22"}
    parts = _split_beam_vertical_dyn_by_zone_spans(sub_dyn, sub_t)
    assert len(parts) == 2
    cfg = ExtractionConfig(beam_layout="vertical_blocks")
    r0 = _record_from_beam_vertical_dynamic(parts[0][0], parts[0][1], "unit", cfg)
    r1 = _record_from_beam_vertical_dynamic(parts[1][0], parts[1][1], "unit", cfg)
    assert r0["mark"] == "B1"
    assert r0["int_top_bar"] == "10-D22" and r0["ext_top_bar"] == "10-D22"
    assert r0.get("beam_vertical_zone_display_label") == "INT.(BOTH)"
    assert r1["mark"] == "B1"
    assert (r1.get("int_top_bar") or "").strip() == "4-D22"
    assert r1.get("beam_vertical_zone_display_label") == "END( INT. )"


def test_prune_beam_vertical_template_drops_sheet_title():
    from app.services.beam_extraction import _prune_beam_vertical_template

    raw = [
        (47340.0, "보 일람표-1(지하주차장)", "x0"),
        (46901.0, "부       호", "x1"),
        (46508.0, "INT.(BOTH)", "x2"),
    ]
    out = _prune_beam_vertical_template(raw)
    assert len(out) == 2
    assert "부" in out[0][1] and "INT" in out[1][1]
