"""서버 flat 하단 텍스트 스키마(below_text_role_values)."""

from app.services.beam_flat_below_text import (
    beam_flat_fill_below_text_role_values_from_entities,
    beam_flat_parse_member_mark_dims,
)


def test_beam_flat_parse_member_mark_dims():
    assert beam_flat_parse_member_mark_dims("RG11 (1000x900)") == {"width_mm": 1000, "depth_mm": 900}
    assert beam_flat_parse_member_mark_dims("no dims") is None


def test_beam_flat_fill_below_text_from_entities():
    ents = [
        {"x": 1000.0, "y": 100.0, "text": "상부근 4-D16"},
        {"x": 1010.0, "y": 80.0, "text": "하부근 3-D16"},
    ]
    sg = {"search_bbox": [900.0, 200.0, 1100.0, 400.0]}
    out = beam_flat_fill_below_text_role_values_from_entities(ents, sg)
    assert out["below_text_dir"] in ("smallY", "largeY")
    vals = out["below_text_role_values"]
    assert "상부근" in vals or "하부근" in vals
