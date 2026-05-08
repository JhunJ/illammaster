"""Unit tests (no DB): 클러스터링·텍스트 휴리스틱."""
import pytest

from app.services.schedule_extraction import (
    bridge_beam_sparse_row_clusters,
    _apply_column_member_story_parse_error_flag,
    _build_y_template_from_bands,
    _enrich_vertical_block_record,
    _is_probable_pure_data_value,
    _map_data_bands_to_template,
    _merge_narrow_adjacent_strips,
    _parse_column_name_story_and_mark,
    _pick_template_segment_for_y,
    _segment_template_rows,
    _slab_xy_variant,
    _strip_concrete_strength_snippets,
    _strip_mismatched_draw_mark_callouts,
    cluster_rows,
    merge_beam_sparse_row_clusters,
    parse_text_signals,
)


def test_merge_beam_sparse_row_clusters_joins_two_slightly_separated_cells():
    """Y가 y_tolerance를 넘겨 cluster_rows 가 두 줄로 쪼갠 뒤에도, 얇은 행이면 한 수평 묶음으로 병합."""
    row_a = [{"x": 0.0, "y": 100.0, "text": "A", "id": 1}]
    row_b = [{"x": 500.0, "y": 95.0, "text": "B", "id": 2}]
    out = merge_beam_sparse_row_clusters([row_a, row_b], y_tol=2.5, y_gap_max=12.0)
    assert len(out) == 1
    assert len(out[0]) == 2
    assert sorted(c["x"] for c in out[0]) == [0.0, 500.0]


def test_merge_beam_sparse_row_clusters_respects_large_vertical_gap():
    row_a = [{"x": 0.0, "y": 100.0, "text": "A", "id": 1}]
    row_b = [{"x": 0.0, "y": 40.0, "text": "B", "id": 2}]
    out = merge_beam_sparse_row_clusters([row_a, row_b], y_tol=2.5, y_gap_max=12.0)
    assert len(out) == 2


def test_merge_beam_sparse_row_clusters_chains_many_cells_along_x():
    """가로로 긴 한 줄이 Y만 살짝 다르게 쪼개져도, 얇은 조각이면 끝까지 한 묶음으로."""
    rows = [
        [{"x": float(i * 120), "y": 200.0 - i * 0.6, "text": str(i), "id": i}] for i in range(8)
    ]
    out = merge_beam_sparse_row_clusters(
        rows, y_tol=2.5, y_gap_max=20.0, max_glue_row_items=1, max_merged_row_items=256
    )
    assert len(out) == 1
    assert len(out[0]) == 8


def test_bridge_beam_sparse_row_clusters_merges_far_apart_same_band():
    """가로로 멀리 떨어진 부호가 Y만 비슷하면 한 행으로 가교 병합(사이는 1텍스트 이하만 허용)."""
    left = [{"x": 0.0, "y": 100.0, "text": "RG13B", "id": 1}]
    spacer = [{"x": 400.0, "y": 99.6, "text": "-", "id": 9}]
    right = [{"x": 1200.0, "y": 99.85, "text": "RG13B", "id": 2}]
    out = bridge_beam_sparse_row_clusters(
        [left, spacer, right],
        15.0,
        max_endpoint_items=4,
        max_intermediate_row_items=1,
        max_merged_row_items=64,
        max_bridge_passes=1,
    )
    # Y 정렬 후 왼쪽·오른쪽 부호가 인접해 병합되고, 아래쪽 얇은 spacer 행은 남는다.
    assert len(out) == 2
    wide = max(out, key=len)
    assert len(wide) == 2
    xs = sorted(c["x"] for c in wide)
    assert xs[0] == 0.0 and xs[1] == 1200.0


def test_bridge_beam_sparse_row_clusters_blocked_by_dense_middle():
    """두 끝 Y사이에(열린 구간) 조밀한 행이 있으면 가로로 멀어도 부호끼리 병합하지 않음."""
    left = [{"x": 0.0, "y": 100.0, "text": "A", "id": 1}]
    dense = [{"x": float(i * 20), "y": 99.75, "text": str(i), "id": i} for i in range(5)]
    right = [{"x": 800.0, "y": 99.5, "text": "B", "id": 2}]
    out = bridge_beam_sparse_row_clusters(
        [left, dense, right],
        18.0,
        max_endpoint_items=4,
        max_intermediate_row_items=1,
        max_merged_row_items=64,
        max_bridge_passes=3,
    )
    assert len(out) == 3


def test_parse_d_at_x():
    s = parse_text_signals("D22 @200 600x400 B2F~B1F")
    assert s.get("rebar_d") == 22
    assert s.get("spacing") == 200
    assert s.get("size_mm") == [600, 400]
    assert "floor_range" in s


def test_parse_numeric_floor_range_in_member_name():
    s = parse_text_signals("-4 ~ -3C2A")
    assert s.get("floor_range") == ["-4", "-3"]
    assert parse_text_signals("-4C~-2C1").get("floor_range") is None


def test_merge_narrow_adjacent_strips():
    wide = [{"x": -16828.0, "y": 100.0, "text": "x"} for _ in range(14)]
    narrow = [{"x": -16062.0, "y": 100.0, "text": "f"} for _ in range(4)]
    out, log = _merge_narrow_adjacent_strips([wide, narrow])
    assert len(out) == 1
    assert len(out[0]) == 18
    assert log


def test_merge_narrow_adjacent_strips_fragment_pair():
    """엔티티 6+4처럼 둘 다 작을 때도 인접 조각은 한 스트립으로 합친다."""
    a = [{"x": 68508.0, "y": float(i), "text": "a"} for i in range(6)]
    b = [{"x": 68566.0, "y": float(i), "text": "b"} for i in range(4)]
    out, log = _merge_narrow_adjacent_strips([a, b])
    assert len(out) == 1
    assert len(out[0]) == 10
    assert any(m.get("merge_kind") == "fragment_pair" for m in log)


def test_parse_column_name_story_tilde():
    assert _parse_column_name_story_and_mark("-4 ~ -3C2A") == ("-4~-3", "C2A", ["-4", "-3"], None)
    assert _parse_column_name_story_and_mark("-4 - -3 C2A") == ("-4~-3", "C2A", ["-4", "-3"], None)
    assert _parse_column_name_story_and_mark("~ -1C2") == ("~-1", "C2", ["~", "-1"], None)
    assert _parse_column_name_story_and_mark("-2 C1") == ("-2", "C1", None, None)
    assert _parse_column_name_story_and_mark("-3C31") == ("-3", "C31", None, None)
    assert _parse_column_name_story_and_mark("2C5") == ("2", "C5", None, None)


def test_parse_column_name_trailing_lone_dim_mm():
    assert _parse_column_name_story_and_mark("-4C~-2C1 500") == (None, None, None, 500)
    assert _parse_column_name_story_and_mark("-2C21A 500") == ("-2", "C21A", None, 500)
    assert _parse_column_name_story_and_mark("-3C31 500") == ("-3", "C31", None, 500)


def test_split_x_gap_rejected_when_partition_too_wide_for_beam_tight():
    """최대 X간격이 데이터 열 사이면 x_gap을 버리고 비율 분할로 넘긴다(보 tight)."""
    from app.services.schedule_extraction import _split_column_label_and_data

    # 왼쪽에 x=100~3200까지 촘촘히 찍혀 있으면 x_gap이 '중간 대갭'으로 잡혀 라벨이 과하게 넓어진다.
    items = []
    for xi in range(100, 3201, 100):
        items.append({"x": float(xi), "y": 100.0, "text": "L", "layer": "0", "entity_type": "TEXT"})
    for x in (5200.0, 6400.0, 9600.0):
        items.append({"x": x, "y": 100.0, "text": "D", "layer": "0", "entity_type": "TEXT"})
    lab, data, mode, bx = _split_column_label_and_data(items, tight_label_column=True)
    assert "left_ratio" in mode
    assert bx is not None
    # x_gap이면 경계가 ~4200 근처로 밀리고 라벨이 과다해진다. 비율 분할이면 좌측 14% 안쪽.
    assert bx < 2500.0
    assert len(data) > len(lab)


def test_hdr_cluster_single_anchor_tightens():
    """행제목 매칭이 1건뿐이어도(한 줄만 인식) 경계를 왼쪽으로 당긴다."""
    from app.services.schedule_extraction import _split_column_label_and_data

    items = [
        {"x": 100.0, "y": 5000.0, "text": "부호", "layer": "0", "entity_type": "TEXT"},
        {"x": 8000.0, "y": 5000.0, "text": "RG11 (1000x900)", "layer": "0", "entity_type": "TEXT"},
        {"x": 9000.0, "y": 4990.0, "text": "26-UHD19", "layer": "0", "entity_type": "TEXT"},
    ]
    for xi in range(12000, 20001, 500):
        items.append({"x": float(xi), "y": 5000.0, "text": "x", "layer": "0", "entity_type": "TEXT"})
    _lab, _data, mode, bx = _split_column_label_and_data(items, tight_label_column=True)
    assert "hdr_cluster" in mode
    assert bx is not None and bx < 5000.0


def test_hdr_cluster_ignores_section_in_wide_left_slice():
    """SECTION 등 영문 표제는 X클러스터에 넣지 않아(전역 반복) hdr_cluster가 살아난다."""
    from app.services.schedule_extraction import _split_column_label_and_data

    items = []
    for yi, lab in enumerate(["부호", "형태", "상부근"]):
        items.append(
            {"x": 120.0, "y": float(5000 - yi * 40), "text": lab, "layer": "0", "entity_type": "TEXT"}
        )
    items.append({"x": 5000.0, "y": 4990.0, "text": "SECTION", "layer": "0", "entity_type": "TEXT"})
    for xi in range(8000, 12001, 400):
        items.append({"x": float(xi), "y": 5000.0, "text": "D", "layer": "0", "entity_type": "TEXT"})
    _lab, _data, mode, bx = _split_column_label_and_data(items, tight_label_column=True)
    assert "hdr_cluster" in mode
    assert bx is not None and bx < 6500.0


def test_demote_beam_zone_headers_moves_int_center_to_data():
    from app.services.schedule_extraction import _demote_beam_zone_column_headers_from_labels

    labels = [
        {"x": 100.0, "y": 1.0, "text": "부호", "layer": "0", "entity_type": "TEXT"},
        {"x": 420.0, "y": 2.0, "text": "INT.(BOTH)", "layer": "0", "entity_type": "TEXT"},
        {"x": 410.0, "y": 3.0, "text": "CENTER", "layer": "0", "entity_type": "TEXT"},
    ]
    data: list = []
    k, d = _demote_beam_zone_column_headers_from_labels(labels, data)
    assert len(k) == 1 and k[0]["text"] == "부호"
    assert len(d) == 2


def test_split_tight_beam_hdr_cluster_narrows_boundary():
    """좌측 행제목(부호·상부근 등) X로 경계를 좁혀 비율 분할보다 왼쪽에 주황선을 둔다."""
    from app.services.schedule_extraction import _split_column_label_and_data

    items = [
        {"x": 50.0, "y": 100.0, "text": "부       호", "layer": "0", "entity_type": "TEXT"},
        {"x": 55.0, "y": 90.0, "text": "형       태", "layer": "0", "entity_type": "TEXT"},
        {"x": 62.0, "y": 80.0, "text": "상  부  근", "layer": "0", "entity_type": "TEXT"},
        {"x": 400.0, "y": 100.0, "text": "noise", "layer": "0", "entity_type": "TEXT"},
        {"x": 2500.0, "y": 100.0, "text": "RG11 (1000x900)", "layer": "0", "entity_type": "TEXT"},
        {"x": 2600.0, "y": 90.0, "text": "INT.(BOTH)", "layer": "0", "entity_type": "TEXT"},
        {"x": 5200.0, "y": 100.0, "text": "7-D16", "layer": "0", "entity_type": "TEXT"},
    ]
    lab, data, mode, bx = _split_column_label_and_data(items, tight_label_column=True)
    assert "hdr_cluster" in mode
    assert bx is not None and bx < 400.0
    assert all(float(it["x"]) <= bx for it in lab)
    assert all(float(it["x"]) > bx for it in data)


def test_pure_data_value_excludes_member_banner_and_fck_for_left_template():
    assert _is_probable_pure_data_value("-4 ~ -2C1A")
    assert _is_probable_pure_data_value("fck = 35MPa")
    assert _is_probable_pure_data_value("FCK = 35 MPa")
    assert _is_probable_pure_data_value("-2 C1")
    assert _is_probable_pure_data_value("12-UHD19")
    assert _is_probable_pure_data_value("RG12")
    assert _is_probable_pure_data_value("RG13B")
    assert _is_probable_pure_data_value("RG11C")
    assert not _is_probable_pure_data_value("형       태")
    assert not _is_probable_pure_data_value("MAIN BAR")


def test_build_y_template_skips_fck_and_floor_range_titles():
    bands = [
        (100.0, "형       태"),
        (99.0, "fck = 35MPa"),
        (98.0, "-4 ~ -2C1A"),
        (97.0, "MAIN BAR"),
    ]
    rows = _build_y_template_from_bands(bands, dedupe_same_label=False)
    labs = [r[1] for r in rows]
    assert "fck = 35MPa" not in labs
    assert "-4 ~ -2C1A" not in labs
    assert any("형" in x for x in labs)
    assert any("MAIN" in x for x in labs)


def test_story_parse_error_when_tilde_but_unparsable_member_name():
    rec = {"member_label": "-4C~-2C1", "mark": "-4C~-2C1"}
    _apply_column_member_story_parse_error_flag(rec)
    assert rec.get("story_parse_error")


def test_no_story_parse_error_when_proper_spaced_floor_range():
    rec = {
        "member_label": "-4 ~ -2 C1",
        "mark": "C1",
        "story_label": "-4~-2",
        "floor_range": ["-4", "-2"],
    }
    _apply_column_member_story_parse_error_flag(rec)
    assert not rec.get("story_parse_error")


def test_pick_template_segment_overlap_uses_nearest_block_mid():
    """인접 부재 블록의 Y 패딩이 겹칠 때, 세그먼트 중심에 가까운 블록을 고른다."""
    band_tol = 4.0
    template = [
        (1100.0, "부 호", "k0"),
        (1090.0, "형 태", "k1"),
        (1110.0, "부 호", "k2"),
        (1105.0, "형 태", "k3"),
    ]
    segs = _segment_template_rows(template, band_tol)
    assert len(segs) >= 2
    seg = _pick_template_segment_for_y(1110.0, template, segs, band_tol)
    assert 2 in seg and 3 in seg
    assert 0 not in seg


def test_segment_template_rows_splits_on_korean_giho_when_block_gap_small():
    """보·기둥 세로 일람이 '기호'만 쓰고 블록 사이 Y갭이 작아 갭 임계값으로는 안 갈라질 때."""
    band_tol = 4.0
    y = 200.0
    template: list[tuple[float, str, str]] = []
    for _ in range(4):
        template.append((y, "기 호", f"k{len(template)}")); y -= 5
        template.append((y, "형 태", f"k{len(template)}")); y -= 5
        template.append((y, "상 부 근", f"k{len(template)}")); y -= 5
        y -= 10  # 블록 사이: med*2.2 등으로는 한 세그먼트로 붙기 쉬운 간격
    segs = _segment_template_rows(template, band_tol)
    assert len(segs) == 4
    assert all(len(s) == 3 for s in segs)


def test_strip_mismatched_draw_callout_not_current_mark():
    s = _strip_mismatched_draw_mark_callouts("#32.C51 extra", "C42")
    assert "C51" not in s
    assert "extra" in s
    assert _strip_mismatched_draw_mark_callouts("#32.C42 note", "C42") == "C42 note"


def test_strip_concrete_strength_from_main_bar_cell():
    rest, bits = _strip_concrete_strength_snippets("fck = 35MPa 12-UHD19")
    assert "fck" in bits.lower() and "35" in bits
    assert "12-UHD19" in rest.replace(" ", "")
    r2, b2 = _strip_concrete_strength_snippets("12-UHD19 SHD10@250")
    assert not b2
    assert "12-UHD19" in r2


def test_enrich_vertical_story_label_from_floor_range_only():
    """floor_range 만 있을 때도 화면 '층' 컬럼(story_label)에 -4~-3 형으로 넣는다."""
    rec = {
        "column_field_titles": {"k0": "부 호"},
        "column_field_key_order": ["k0"],
        "cells": ["C22"],
        "floor_range": ["-4", "-3"],
    }
    _enrich_vertical_block_record(rec)
    assert rec.get("story_label") == "-4~-3"
    assert rec.get("mark") == "C22"


def test_enrich_splits_fck_into_concrete_strength():
    rec = {
        "column_field_titles": {"k0": "MAIN BAR"},
        "column_field_key_order": ["k0"],
        "cells": ["fck = 35MPa 16-UHD19"],
    }
    _enrich_vertical_block_record(rec)
    assert "UHD19" in (rec.get("MAIN_BAR") or "")
    assert "fck" in (rec.get("CONCRETE_STRENGTH") or "").lower()


def test_enrich_concrete_strength_from_korean_concrete_row():
    """`(1) 콘크리트` 전용 행에만 fck 가 있을 때 강도 칸으로 읽는다."""
    rec = {
        "column_field_titles": {
            "k0": "부 호",
            "k1": "(1) 콘크리트",
            "k2": "MAIN BAR",
        },
        "column_field_key_order": ["k0", "k1", "k2"],
        "cells": ["-2C1", "fck = 35MPa", "12-UHD19"],
    }
    _enrich_vertical_block_record(rec)
    assert "35" in (rec.get("CONCRETE_STRENGTH") or "")
    assert "fck" in (rec.get("CONCRETE_STRENGTH") or "").lower()
    assert "UHD19" in (rec.get("MAIN_BAR") or "")


def test_enrich_name_strips_trailing_dim_and_sets_story():
    rec = {
        "column_field_titles": {"k0": "부 호"},
        "column_field_key_order": ["k0"],
        "cells": ["-2C21A 500"],
    }
    _enrich_vertical_block_record(rec)
    assert rec.get("member_label") == "-2C21A"
    assert rec.get("mark") == "C21A"
    assert rec.get("story_label") == "-2"
    assert rec.get("SIZE") == "500"


def test_enrich_jammed_floor_tilde_keeps_story_parse_error():
    """`-4C~-2C1` 는 층범위로 자동 해석하지 않고, 안내 오류만 남긴다."""
    rec = {
        "column_field_titles": {"k0": "부 호"},
        "column_field_key_order": ["k0"],
        "cells": ["-4C~-2C1 500"],
    }
    _enrich_vertical_block_record(rec)
    assert rec.get("member_label") == "-4C~-2C1"
    assert rec.get("mark") == "-4C~-2C1"
    assert rec.get("SIZE") == "500"
    assert not rec.get("story_label")
    assert not rec.get("floor_range")
    assert rec.get("story_parse_error")


def test_map_data_bands_attaches_distant_numeric_floor_line():
    """층 표기가 부호 템플릿 Y와 멀어도 부호 칸에 합류해야 한다."""
    band_tol = 45.0
    template = [
        (10000.0, "부 호", "k0"),
        (9600.0, "형 태", "k1"),
        (9000.0, "MAIN BAR", "k2"),
    ]
    bands = [
        (13300.0, "-4 ~ -3"),
        (10020.0, "C22"),
    ]
    dyn = _map_data_bands_to_template(bands, template, band_tol)
    assert dyn.get("k0") and "C22" in dyn["k0"]
    assert "-4" in dyn["k0"] and "-3" in dyn["k0"]


def test_map_data_bands_beam_vertical_rejects_far_y_noise_in_tall_segment():
    """보 세로: 세그먼트 세로가 길어도 템플릿 행 간격 밖 Y는 붙이지 않는다(기둥용 span*loose는 과대)."""
    band_tol = 6.0
    template: list[tuple[float, str, str]] = []
    y = 1000.0
    for i in range(17):
        lab = "부 호" if i == 0 else ("형 태" if i == 3 else ("상 부 근" if i == 8 else f"r{i}"))
        template.append((y, lab, f"k{i}"))
        y -= 50.0
    bands = [(983.0, "RG11"), (50.0, "FAR-NOISE")]
    dyn_beam = _map_data_bands_to_template(bands, template, band_tol, beam_vertical_schedule=True)
    dyn_col = _map_data_bands_to_template(bands, template, band_tol, beam_vertical_schedule=False)
    joined_beam = " ".join(str(v) for v in dyn_beam.values())
    joined_col = " ".join(str(v) for v in dyn_col.values())
    assert "RG11" in joined_beam
    assert "FAR-NOISE" not in joined_beam
    assert "FAR-NOISE" in joined_col


def test_thickness_paren_slash():
    a = parse_text_signals("600(400) wall")
    assert a.get("thickness_pair") == [600, 400]
    b = parse_text_signals("600/400")
    assert b.get("thickness_pair") == [600, 400]


def test_cluster_rows():
    items = [
        {"text": "A", "x": 0, "y": 100, "layer": "0", "id": 1, "entity_type": "TEXT"},
        {"text": "B", "x": 50, "y": 100, "layer": "0", "id": 2, "entity_type": "TEXT"},
        {"text": "C", "x": 0, "y": 50, "layer": "0", "id": 3, "entity_type": "TEXT"},
    ]
    rows = cluster_rows(items, y_tol=5.0)
    assert len(rows) == 2
    texts_row0 = [r["text"] for r in rows[0]]
    assert "A" in texts_row0 and "B" in texts_row0


def test_slab_layout_guess():
    assert _slab_xy_variant(["600x400", "D13"]) != "unknown"


def test_build_beam_flat_member_table_rows_strips_paren_dims_from_mark():
    """부재별표 행: `RG11 (1000x900)` → 표시 마크 RG11, 치수 dict·width/depth 보강."""
    from app.services.schedule_extraction import build_beam_flat_member_table_rows

    rows = [
        {
            "mark": "RG11 (1000x900)",
            "name": "",
            "beam_section_geometry_zones": None,
        }
    ]
    out = build_beam_flat_member_table_rows(rows)
    assert len(out) == 1
    assert out[0]["member_mark_text"] == "RG11"
    assert out[0]["member_mark_dims"] == {"width_mm": 1000, "depth_mm": 900}
    assert out[0]["width_mm"] == 1000
    assert out[0]["depth_mm"] == 900
