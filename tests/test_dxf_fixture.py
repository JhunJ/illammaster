"""DXF 픽스처: ezdxf로 최소 도면 생성 후 parse_dxf 호출."""
import io

import ezdxf
import pytest

from app.services.dxf_parser import parse_dxf


def test_parse_dxf_minimal_text():
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    msp.add_text("D22 @200 600x400", dxfattribs={"height": 2.5, "insert": (0, 0)})
    buf = io.StringIO()
    doc.write(buf)
    dxf_bytes = buf.getvalue().encode("utf-8")
    entities, block_defs, block_inserts, block_attrs, layer_colors = parse_dxf(dxf_bytes, {})
    texts = [e for e in entities if e.get("entity_type") in ("TEXT", "MTEXT")]
    assert len(texts) >= 1
    assert "D22" in (texts[0].get("props") or {}).get("text", "")


def test_parse_dxf_mtext_paragraph_aligns():
    """MTEXT \\p...; 단락 가로 정렬(ql/qc/qr)이 props.mtext_line_aligns 로 남는지."""
    doc = ezdxf.new("R2010")
    msp = doc.modelspace()
    raw = "\\pqc;L1\\P\\pql;L2"
    msp.add_mtext(raw, dxfattribs={"char_height": 2.5, "insert": (0, 0), "width": 40})
    buf = io.StringIO()
    doc.write(buf)
    dxf_bytes = buf.getvalue().encode("utf-8")
    entities, *_rest = parse_dxf(dxf_bytes, {})
    mts = [e for e in entities if e.get("entity_type") == "MTEXT"]
    assert len(mts) == 1
    props = mts[0].get("props") or {}
    assert props.get("mtext_line_aligns") == ["center", "left"]
    assert props.get("mtext_width") == 40.0
    assert "L1" in props.get("text", "") and "L2" in props.get("text", "")


def test_parse_dxf_skip_off_layers():
    doc = ezdxf.new("R2010")
    doc.layers.add("ON_L")
    doc.layers.add("OFF_L")
    doc.layers.get("OFF_L").off()
    msp = doc.modelspace()
    msp.add_line((0, 0), (10, 0), dxfattribs={"layer": "ON_L"})
    msp.add_line((0, 5), (10, 5), dxfattribs={"layer": "OFF_L"})
    buf = io.StringIO()
    doc.write(buf)
    dxf_bytes = buf.getvalue().encode("utf-8")

    all_lines = [e for e in parse_dxf(dxf_bytes, {})[0] if e.get("entity_type") == "LINE"]
    assert len(all_lines) == 2

    kept = [e for e in parse_dxf(dxf_bytes, {"skip_off_layers": True})[0] if e.get("entity_type") == "LINE"]
    assert len(kept) == 1
    assert kept[0].get("layer") == "ON_L"
