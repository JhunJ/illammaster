"""
ezdxf濡?DXF ?뚯떛: modelspace ?뷀떚??LINE, LWPOLYLINE, POLYLINE, ARC, CIRCLE, TEXT, MTEXT, INSERT) +
釉붾줉 ?뺤쓽/諛곗튂/?띿꽦 異붿텧.
"""
import io
import logging
import re
from typing import Any

import ezdxf
from ezdxf.entities import DXFEntity

from app.services.fingerprint import (
    fingerprint_line,
    fingerprint_polyline,
    fingerprint_arc,
    fingerprint_circle,
    fingerprint_text,
    fingerprint_block_insert,
    get_tolerance_from_settings,
)
from app.utils.geom import (
    point_wkt,
    linestring_wkt,
    polygon_wkt,
    bbox_from_points,
    transform_wkt_with_matrix,
    transform_point_with_matrix,
    wkt_points_to_list,
)
from ezdxf.render.polyline import virtual_lwpolyline_entities, virtual_polyline_entities

logger = logging.getLogger(__name__)

SUPPORTED_ENTITY_TYPES = {
    "LINE",
    "LWPOLYLINE",
    "POLYLINE",
    "ARC",
    "CIRCLE",
    "ELLIPSE",
    "SPLINE",
    "3DFACE",
    "SOLID",
    "TEXT",
    "MTEXT",
    "INSERT",
    "POINT",
    "HATCH",
    "WIPEOUT",
    "ATTRIB",
}

TEXT_HALIGN_LABELS = {
    0: "LEFT",
    1: "CENTER",
    2: "RIGHT",
    3: "ALIGNED",
    4: "MIDDLE",
    5: "FIT",
}
TEXT_VALIGN_LABELS = {
    0: "BASELINE",
    1: "BOTTOM",
    2: "MIDDLE",
    3: "TOP",
}
MTEXT_ATTACHMENT_TO_HV = {
    1: (0, 3),  # top-left
    2: (1, 3),  # top-center
    3: (2, 3),  # top-right
    4: (0, 2),  # middle-left
    5: (1, 2),  # middle-center
    6: (2, 2),  # middle-right
    7: (0, 1),  # bottom-left
    8: (1, 1),  # bottom-center
    9: (2, 1),  # bottom-right
}


def _sanitize_instance_token(value: str) -> str:
    s = re.sub(r"[^A-Za-z0-9_.-]+", "_", (value or "BLOCK").strip())
    return s or "BLOCK"


def _make_insert_instance_key(name: str, occurrence: int, row: int | None = None, col: int | None = None) -> str:
    token = "{0}@{1}".format(_sanitize_instance_token(name), int(occurrence or 1))
    if row is not None and col is not None:
        token += "[r{0}c{1}]".format(int(row), int(col))
    return token


def _clone_block_hierarchy_path(path: list[dict] | None) -> list[dict]:
    out: list[dict] = []
    if not isinstance(path, list):
        return out
    for seg in path:
        if not isinstance(seg, dict):
            continue
        name = str(seg.get("name") or "").strip() or "BLOCK"
        key = str(seg.get("instance_key") or "").strip()
        if not key:
            continue
        out.append({"name": name, "instance_key": key})
    return out


def _merge_props_with_block_hierarchy(props: dict | None, block_hierarchy_path: list[dict] | None) -> dict:
    merged = dict(props or {})
    path = _clone_block_hierarchy_path(block_hierarchy_path)
    if path:
        merged["block_hierarchy_path"] = path
    return merged


def _props_mark_from_dimension(props: dict | None) -> dict:
    """단면 기하(column_section_geometry) 집계에서 제외 — 치수 전개 LINE/TEXT 등."""
    return {**(props or {}), "from_dimension": True}


def _get_render_context(doc):
    """doc?먯꽌 RenderContext ?앹꽦. layers媛 梨꾩썙吏??곹깭 諛섑솚."""
    try:
        from ezdxf.addons.drawing.properties import RenderContext
        ctx = RenderContext(doc)
        return ctx
    except Exception:
        return None


def _insert_props_to_aci(insert_props) -> int | None:
    """insert_props?먯꽌 ACI 異붿텧 (ByBlock fallback??."""
    if insert_props is None:
        return None
    if isinstance(insert_props, int):
        return insert_props
    if isinstance(insert_props, dict):
        return insert_props.get("pen")
    return getattr(insert_props, "pen", None)


def _hex_to_packed_rgb(hex_str: str) -> int:
    """#RRGGBB hex 臾몄옄?댁쓣 packed int (r<<16)|(g<<8)|b 濡?蹂??"""
    s = (hex_str or "").lstrip("#")
    if len(s) >= 6:
        try:
            r = int(s[0:2], 16)
            g = int(s[2:4], 16)
            b = int(s[4:6], 16)
            return (r << 16) | (g << 8) | b
        except ValueError:
            pass
    return 0


def resolve_color_to_aci(
    entity: DXFEntity,
    doc=None,
    layout=None,
    insert_props=None,
    render_ctx=None,
    insert_layer: str | None = None,
) -> int:
    """?쒖떆?섎뒗 理쒖쥌 ACI ?됱긽 諛섑솚. RenderContext ?ъ슜, ?ㅽ뙣 ??_color_int fallback."""
    if insert_layer is None and isinstance(insert_props, dict):
        insert_layer = insert_props.get("layer")
    # ByLayer(256)? RenderContext ???_color_int濡?吏곸젒 ?댁꽍 (CTB/?덉씠???ㅻ쾭?쇱씠???곹뼢 ?쒓굅)
    raw_val = _color_int_raw(entity)
    if raw_val == 256 and doc:
        result = _color_int(
            entity, doc=doc, layer_name=_layer(entity),
            insert_color_fallback=_insert_props_to_aci(insert_props),
            insert_layer=insert_layer,
        )
        return result
    if doc is None:
        return _color_int(
            entity,
            layer_name=_layer(entity),
            insert_color_fallback=_insert_props_to_aci(insert_props),
            insert_layer=insert_layer,
        )
    try:
        ctx = render_ctx or _get_render_context(doc)
        if ctx is None:
            return _color_int(
                entity, doc=doc, layer_name=_layer(entity),
                insert_color_fallback=_insert_props_to_aci(insert_props),
                insert_layer=insert_layer,
            )
        if layout and layout != getattr(ctx, "_current_layout", None):
            ctx.set_current_layout(layout)
        if insert_props is not None:
            from ezdxf.addons.drawing.properties import Properties
            ctx.inside_block_reference = True
            p = Properties()
            p.pen = _insert_props_to_aci(insert_props) or 7
            p.layer = insert_props.get("layer", "0") if isinstance(insert_props, dict) else "0"
            p.color = "#000000"
            ctx.current_block_reference_properties = p
        else:
            ctx.inside_block_reference = False
            ctx.current_block_reference_properties = None
        if getattr(entity.dxf, "hasattr", lambda _: False)("true_color"):
            try:
                hex_color = ctx.resolve_color(entity)
                packed = _hex_to_packed_rgb(hex_color[:7] if hex_color else "")
                if packed > 0:
                    return packed
            except Exception:
                pass
        aci = ctx.resolve_pen(entity)
        if 1 <= aci <= 255:
            return aci
        # 256 ?먮뒗 踰붿쐞 諛???_color_int濡??ы빐??
        return _color_int(
            entity, doc=doc, layer_name=_layer(entity),
            insert_color_fallback=_insert_props_to_aci(insert_props),
            insert_layer=insert_layer,
        )
    except Exception:
        return _color_int(
            entity, doc=doc, layer_name=_layer(entity),
            insert_color_fallback=_insert_props_to_aci(insert_props),
            insert_layer=insert_layer,
        )


def _entity_true_color_420(entity: DXFEntity) -> int | None:
    """DXF 그룹 420 TrueColor(32비트) — CAD Manage·와이어프레임은 plot/CTB 리맵 없이 이 값을 우선."""
    try:
        if hasattr(entity.dxf, "hasattr") and entity.dxf.hasattr("true_color"):
            t = entity.dxf.get("true_color")
            if t is not None:
                n = int(t)
                if n > 255:
                    return n
    except Exception:
        pass
    return None


def _color_meta(
    entity: DXFEntity,
    doc=None,
    layout=None,
    insert_props=None,
    render_ctx=None,
    insert_layer: str | None = None,
) -> dict:
    """color_raw=DXF 그룹 62/색필드 원시값, color=화면 표시용 정수(ACI 또는 packed RGB).
    명시 ACI(1–255)·TrueColor는 DXF 그대로 쓰고, RenderContext.resolve_pen(플롯 스타일)은 쓰지 않는다.
    ByLayer(256)만 레이어/블록 맥락에서 resolve_color_to_aci 로 보조."""
    color_raw = _color_int_raw(entity)
    tc420 = _entity_true_color_420(entity)

    if tc420 is not None:
        color_display = tc420
    elif color_raw == 256:
        color_display = 256
    elif isinstance(color_raw, int) and color_raw > 255:
        color_display = color_raw
    elif color_raw == 0:
        color_display = resolve_color_to_aci(
            entity,
            doc=doc,
            layout=layout,
            insert_props=insert_props,
            render_ctx=render_ctx,
            insert_layer=insert_layer,
        )
    elif isinstance(color_raw, int) and 1 <= color_raw <= 255:
        color_display = color_raw
    else:
        color_display = resolve_color_to_aci(
            entity,
            doc=doc,
            layout=layout,
            insert_props=insert_props,
            render_ctx=render_ctx,
            insert_layer=insert_layer,
        )
    return {
        "color": color_display,
        "color_raw": color_raw,
        "color_bylayer": color_raw == 256,
    }


def _color_int_raw(entity: DXFEntity) -> int:
    """?뷀떚?곗쓽 raw color 諛섑솚 (ByLayer=256, ByBlock=0 ?ы븿).
    DXF?먯꽌 group 62 誘몄?????湲곕낯? ByLayer(256)."""
    try:
        color = entity.dxf.get("color")
        if color is None:
            return 256  # DXF 湲곕낯: ByLayer
        if isinstance(color, int):
            return color
        if hasattr(color, "rgb"):
            r, g, b = color.rgb
            return (r << 16) | (g << 8) | b
    except Exception:
        pass
    return 256  # 誘명솗????ByLayer (0=ByBlock? 紐낆떆?곸씪 ?뚮쭔)


def _layer_color_to_aci(c) -> int:
    """?덉씠??get_color() 諛섑솚媛?-> 1-255 ACI ?먮뒗 packed RGB. 256/踰붿쐞諛뽰? 7."""
    if c is None:
        return 7
    if c == 256:
        return 7
    if hasattr(c, "rgb"):
        r, g, b = c.rgb
        packed = (int(r) << 16) | (int(g) << 8) | int(b)
        return packed if packed > 0 else 7
    try:
        ci = int(c)
        return ci if 1 <= ci <= 255 else 7
    except (TypeError, ValueError):
        return 7


def _extract_layer_colors(doc) -> dict[str, int]:
    """doc.layers ?쒗쉶?섏뿬 ?덉씠?대퀎 ?됱긽 ?섏쭛. ByLayer ?뷀떚???쒖떆??
    dxf.get?쇰줈 ?ㅼ젣 DXF 媛믪쓣 ?곗꽑 議고쉶 (洹몃９ 62/420 ?놁쑝硫?None). get_color()??fallback."""
    result: dict[str, int] = {}
    layers = getattr(doc, "layers", None)
    if not layers:
        return result
    for lay in layers:
        name = getattr(lay, "dxf", None) and getattr(lay.dxf, "name", None) or getattr(lay, "name", None)
        if not name:
            continue
        try:
            # 1) true_color (洹몃９ 420): DXF???덉쑝硫??곗꽑
            tc = lay.dxf.get("true_color") if hasattr(lay.dxf, "get") else getattr(lay.dxf, "true_color", None)
            if tc is not None and int(tc) != 0:
                result[str(name)] = int(tc)
                continue
            # 2) lay.rgb (true_color?먯꽌 ?뚯깮)
            if hasattr(lay, "rgb") and lay.rgb is not None:
                r, g, b = lay.rgb
                packed = (int(r) << 16) | (int(g) << 8) | int(b)
                if packed > 0:
                    result[str(name)] = packed
                    continue
            # 3) dxf.get("color"): 洹몃９ 62媛 DXF???덉쑝硫??대떦 媛??ъ슜 (?놁쑝硫?None)
            dxf_color = lay.dxf.get("color") if hasattr(lay.dxf, "get") else None
            if dxf_color is not None:
                aci = int(dxf_color)
                aci = abs(aci)
                if 1 <= aci <= 255:
                    result[str(name)] = aci
                    continue
                if aci == 256:
                    result[str(name)] = 7
                    continue
            # 4) getattr fallback (DXF 湲곕낯媛?7)
            dxf_color = getattr(lay.dxf, "color", None)
            if dxf_color is not None:
                aci = abs(int(dxf_color))
                result[str(name)] = aci if 1 <= aci <= 255 else 7
                continue
            # 5) 留덉?留?fallback
            c = lay.get_color()
            result[str(name)] = _layer_color_to_aci(c)
        except Exception:
            result[str(name)] = 7
    return result


def debug_layer_colors_extraction(dxf_bytes: bytes) -> dict:
    """
    ?덉씠???됱긽 異붿텧 ?붾쾭源낆슜. DXF 諛붿씠?몃? ?뚯떛??doc/raw 異붿텧 寃곌낵?
    ?덉씠?대퀎 ?곸꽭(dxf.get, rgb, get_color ??瑜?諛섑솚.
    """
    result = {
        "layer_colors_final": {},
        "layer_colors_from_doc": {},
        "layer_colors_from_raw": {},
        "per_layer": [],
        "error": None,
    }
    try:
        doc = _load_dxf_document(dxf_bytes)
        layer_colors_doc = _extract_layer_colors(doc)
        raw_colors = _extract_layer_colors_from_raw(dxf_bytes)
        layer_colors_final = dict(layer_colors_doc)
        for name, raw_val in raw_colors.items():
            if raw_val is not None and layer_colors_final.get(name) == 7:
                layer_colors_final[name] = raw_val

        result["layer_colors_final"] = layer_colors_final
        result["layer_colors_from_doc"] = layer_colors_doc
        result["layer_colors_from_raw"] = {k: v for k, v in raw_colors.items() if v is not None}

        layers = getattr(doc, "layers", None) or []
        for lay in layers:
            name = getattr(lay, "dxf", None) and getattr(lay.dxf, "name", None) or getattr(lay, "name", None)
            if not name:
                continue
            info = {"layer": str(name)}
            try:
                info["dxf_get_color"] = lay.dxf.get("color") if hasattr(lay.dxf, "get") else None
                info["dxf_get_true_color"] = lay.dxf.get("true_color") if hasattr(lay.dxf, "get") else None
                info["getattr_dxf_color"] = getattr(lay.dxf, "color", None)
                info["rgb"] = lay.rgb if hasattr(lay, "rgb") else None
                info["get_color"] = lay.get_color() if hasattr(lay, "get_color") else None
                info["doc_result"] = layer_colors_doc.get(str(name))
                info["raw_result"] = raw_colors.get(str(name))
                info["final_result"] = layer_colors_final.get(str(name))
            except Exception as e:
                info["error"] = str(e)
            result["per_layer"].append(info)
    except Exception as e:
        result["error"] = str(e)
    return result


def _extract_layer_colors_from_raw(dxf_bytes: bytes) -> dict[str, int | None]:
    """Raw DXF LAYER ?뚯씠釉붿뿉??group 62/420 吏곸젒 ?뚯떛. ezdxf ?댁꽍 ?ㅽ뙣 ??fallback??"""
    result: dict[str, int | None] = {}
    BINARY_SENTINEL = b"AutoCAD Binary DXF\r\n\x1a\x00"
    try:
        if len(dxf_bytes) >= 22 and dxf_bytes[:22] == BINARY_SENTINEL:
            from ezdxf.lldxf.tagger import binary_tags_loader
            tagger = binary_tags_loader(dxf_bytes, errors="surrogateescape")
        else:
            import io
            from ezdxf.lldxf.tagger import ascii_tags_loader
            for enc in ("utf-8", "cp949"):
                try:
                    text = dxf_bytes.decode(enc, errors="surrogateescape")
                    break
                except UnicodeDecodeError:
                    continue
            else:
                return result
            tagger = list(ascii_tags_loader(io.StringIO(text), skip_comments=True))

        from ezdxf.lldxf.loader import load_dxf_structure
        from ezdxf.lldxf.const import DXFStructureError
        sections = load_dxf_structure(tagger, ignore_missing_eof=True)
    except Exception:
        return result

    tables = sections.get("TABLES", [])
    in_layer_table = False
    for entity in tables:
        if not entity or len(entity) < 1:
            continue
        t0 = entity[0]
        code0 = t0.code if hasattr(t0, "code") else (t0[0] if isinstance(t0, (tuple, list)) else 0)
        val0 = t0.value if hasattr(t0, "value") else (t0[1] if isinstance(t0, (tuple, list)) and len(t0) > 1 else t0)
        if code0 == 0 and val0 == "TABLE":
            in_layer_table = False
            for t in entity[1:]:
                c = t.code if hasattr(t, "code") else (t[0] if isinstance(t, (tuple, list)) else -1)
                v = t.value if hasattr(t, "value") else (t[1] if isinstance(t, (tuple, list)) and len(t) > 1 else None)
                if c == 2 and v == "LAYER":
                    in_layer_table = True
                    break
            continue
        if code0 == 0 and val0 == "ENDTAB":
            in_layer_table = False
            continue
        if in_layer_table and code0 == 0 and val0 == "LAYER":
            name_val = None
            color62 = None
            tc420 = None
            for t in entity:
                c = t.code if hasattr(t, "code") else (t[0] if isinstance(t, (tuple, list)) else -1)
                v = t.value if hasattr(t, "value") else (t[1] if isinstance(t, (tuple, list)) and len(t) > 1 else None)
                if c == 2:
                    name_val = str(v) if v is not None else None
                elif c == 62:
                    color62 = int(v) if v is not None else None
                elif c == 420:
                    tc420 = int(v) if v is not None else None
            if name_val:
                if tc420 is not None and tc420 != 0:
                    result[name_val] = tc420
                elif color62 is not None:
                    aci = abs(int(color62))
                    result[name_val] = aci if 1 <= aci <= 255 else (7 if aci == 256 else None)
                else:
                    result[name_val] = None
    return result


def _color_int(
    entity: DXFEntity,
    doc=None,
    layer_name: str | None = None,
    insert_color_fallback: int | None = None,
    insert_layer: str | None = None,
) -> int:
    """ACI ?먮뒗 RGB -> ?뺤닔. ByLayer(256)/ByBlock(0) ?댁꽍. resolve_color_to_aci ?ㅽ뙣 ??fallback."""
    raw = _color_int_raw(entity)
    if raw == 256 and doc:
        effective_layer = (
            insert_layer if ((layer_name or "") == "0" and insert_layer) else layer_name
        )
        if effective_layer:
            try:
                if effective_layer in doc.layers:
                    lay = doc.layers.get(effective_layer)
                    if hasattr(lay, "rgb") and lay.rgb is not None:
                        r, g, b = lay.rgb
                        packed = (int(r) << 16) | (int(g) << 8) | int(b)
                        if packed > 0:
                            return packed
                    c = lay.get_color()
                    res = _layer_color_to_aci(c)
                    return res
            except Exception:
                pass
        return 7
    if raw == 0 and insert_color_fallback is not None:
        return insert_color_fallback
    if raw == 256:
        return 7
    return raw


def _layer(entity: DXFEntity) -> str | None:
    return getattr(entity.dxf, "layer", None) or None


def _layer_is_on(doc, layer_name: str | None) -> bool:
    """레이어 테이블 기준 표시(켜짐). 없는 이름은 포함(호환)."""
    name = (layer_name or "0").strip() or "0"
    try:
        layers = getattr(doc, "layers", None)
        if not layers or name not in layers:
            return True
        lay = layers.get(name)
        if lay is None:
            return True
        if hasattr(lay, "is_on"):
            return bool(lay.is_on())
    except Exception:
        return True
    return True


def _effective_layer_for_visibility(layer_raw: str | None, insert_layer: str | None) -> str:
    """블록 내부 layer 0은 삽입 레이어를 따르는 경우가 많아, 가시성 판단에 동일 규칙 적용."""
    lr = (layer_raw or "0").strip() or "0"
    if lr == "0" and insert_layer:
        return (insert_layer or "0").strip() or "0"
    return lr


def _skip_entity_for_off_layer(
    doc,
    layer_raw: str | None,
    insert_layer: str | None,
    skip_off_layers: bool,
) -> bool:
    if not skip_off_layers:
        return False
    eff = _effective_layer_for_visibility(layer_raw, insert_layer)
    return not _layer_is_on(doc, eff)


def _linetype(entity: DXFEntity) -> str | None:
    return getattr(entity.dxf, "linetype", None) or None


def _is_invisible(entity: DXFEntity) -> bool:
    """DXF invisible flag(60) based visibility."""
    try:
        raw = entity.dxf.get("invisible", 0)
    except Exception:
        raw = getattr(entity.dxf, "invisible", 0)
    try:
        return int(raw or 0) != 0
    except (TypeError, ValueError):
        return bool(raw)


def _is_polyline_closed(entity) -> bool:
    """LWPOLYLINE/POLYLINE closed ?곹깭. entity.closed ?먮뒗 entity.is_closed ?ъ슜."""
    if hasattr(entity, "closed"):
        c = entity.closed
        return bool(c) if not callable(c) else bool(c())
    if hasattr(entity, "is_closed"):
        ic = entity.is_closed
        return bool(ic()) if callable(ic) else bool(ic)
    return bool(getattr(entity.dxf, "flags", 0) & 1)


def _point_3d(e, attr: str) -> tuple[float, float, float]:
    p = getattr(e.dxf, attr, None)
    if p is None:
        return (0.0, 0.0, 0.0)
    return (float(p[0]), float(p[1]), float(p[2]) if len(p) > 2 else 0.0)


def _entity_plain_text(entity: DXFEntity, dxftype: str) -> str:
    """TEXT/MTEXT/ATTRIB 표시 문자열. ezdxf plain_text()로 MTEXT 제어·인코딩 처리."""
    try:
        if dxftype in ("TEXT", "MTEXT", "ATTRIB") and hasattr(entity, "plain_text"):
            pt = entity.plain_text()
            if pt is not None:
                return str(pt).strip()
    except Exception:
        pass
    try:
        raw = entity.dxf.get("text")
        return (raw if raw is not None else "").strip()
    except Exception:
        return ""


def _entity_props(entity: DXFEntity) -> dict[str, Any]:
    """?먮낯 ?뺣낫 蹂댁〈??props."""
    return {
        "dxftype": entity.dxftype(),
    }


def _attach_lineweight(entity: DXFEntity, props: dict[str, Any]) -> dict[str, Any]:
    """DXF lineweight(그룹 370) → 뷰어 선 굵기용. TrueColor(420)은 프리뷰 색상용으로 보관."""
    p = dict(props)
    try:
        if entity.dxf.hasattr("lineweight"):
            lw = entity.dxf.lineweight
            if lw is not None:
                p["lineweight"] = int(lw)
    except Exception:
        pass
    try:
        tc = entity.dxf.get("true_color")
        if tc is not None and int(tc) > 255:
            p["true_color"] = int(tc)
    except Exception:
        pass
    return p


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return int(default)
        return int(value)
    except (TypeError, ValueError):
        return int(default)


def _mtext_paragraph_horizontal_aligns(raw: str) -> list[str]:
    """MTEXT 원문에서 단락(\\P·열 구분)마다 가로 자리맞추기(ql/qc/qr/qj/qd).

    ezdxf :class:`~ezdxf.tools.text.MTextParser`·:func:`~ezdxf.tools.text.plain_mtext`
    와 동일한 토큰 스트림을 사용해 ``plain_text(split=True)`` 줄 수와 맞춘다.
    """
    from ezdxf.enums import MTextParagraphAlignment
    from ezdxf.tools.text import MTextParser, TokenType, plain_mtext

    def _pa_str(a: Any) -> str:
        return {
            MTextParagraphAlignment.DEFAULT: "left",
            MTextParagraphAlignment.LEFT: "left",
            MTextParagraphAlignment.RIGHT: "right",
            MTextParagraphAlignment.CENTER: "center",
            MTextParagraphAlignment.JUSTIFIED: "justify",
            MTextParagraphAlignment.DISTRIBUTED: "distributed",
        }.get(a, "left")

    if not raw:
        return []
    parser = MTextParser(raw)
    on_break: list[str] = []
    for tok in parser:
        if tok.type in (TokenType.NEW_PARAGRAPH, TokenType.NEW_COLUMN):
            on_break.append(_pa_str(tok.ctx.paragraph.align))
    lines = plain_mtext(raw, split=True)
    n = len(lines)
    if n == 0:
        return []
    out = list(on_break)
    while len(out) < n:
        out.append(_pa_str(parser.ctx.paragraph.align))
    if len(out) > n:
        out = out[:n]
    return out


def _mtext_rotation_deg(entity: DXFEntity) -> float | None:
    rot = None
    try:
        rot_raw = entity.dxf.get("rotation")
        if rot_raw is not None:
            rot = float(rot_raw)
    except Exception:
        rot = None
    if rot is not None:
        return rot
    try:
        td = entity.dxf.get("text_direction")
        if td is not None:
            x = float(td[0]) if len(td) > 0 else 0.0
            y = float(td[1]) if len(td) > 1 else 0.0
            if abs(x) > 1e-12 or abs(y) > 1e-12:
                import math

                return math.degrees(math.atan2(y, x))
    except Exception:
        pass
    return None


def _extract_text_alignment(
    entity: DXFEntity,
    dxftype: str,
    insert_pos: tuple[float, float, float],
) -> tuple[tuple[float, float, float], dict[str, Any]]:
    """TEXT/MTEXT/ATTRIB ?뺣젹 愿??props? ?뚮뜑 湲곗??먯쓣 異붿텧."""
    ins = (float(insert_pos[0]), float(insert_pos[1]), float(insert_pos[2]))
    anchor = ins
    props: dict[str, Any] = {
        "insert_x": ins[0],
        "insert_y": ins[1],
        "insert_z": ins[2],
    }

    if dxftype in ("TEXT", "ATTRIB"):
        halign = _safe_int(getattr(entity.dxf, "halign", None), 0)
        valign = _safe_int(getattr(entity.dxf, "valign", None), 0)
        has_align_point = False
        try:
            has_align_point = bool(entity.dxf.hasattr("align_point"))
        except Exception:
            try:
                has_align_point = entity.dxf.get("align_point") is not None
            except Exception:
                has_align_point = False
        align_pos = _point_3d(entity, "align_point") if has_align_point else ins
        if has_align_point and (halign != 0 or valign != 0):
            anchor = align_pos

        props.update(
            {
                "halign": halign,
                "valign": valign,
                "alignment": "{0}/{1}".format(
                    TEXT_HALIGN_LABELS.get(halign, str(halign)),
                    TEXT_VALIGN_LABELS.get(valign, str(valign)),
                ),
                "text_align_x": float(anchor[0]),
                "text_align_y": float(anchor[1]),
                "text_align_z": float(anchor[2]),
            }
        )
        if has_align_point:
            props["align_x"] = float(align_pos[0])
            props["align_y"] = float(align_pos[1])
            props["align_z"] = float(align_pos[2])

        try:
            rot_raw = entity.dxf.get("rotation")
            if rot_raw is not None:
                props["rotation"] = float(rot_raw)
        except Exception:
            pass
        try:
            width_raw = entity.dxf.get("width")
            if width_raw is not None:
                props["width_factor"] = float(width_raw)
        except Exception:
            pass
        try:
            oblique_raw = entity.dxf.get("oblique")
            if oblique_raw is not None:
                props["oblique"] = float(oblique_raw)
        except Exception:
            pass
        try:
            style_raw = entity.dxf.get("style")
            if style_raw:
                props["style_name"] = str(style_raw)
        except Exception:
            pass
        return anchor, props

    if dxftype == "MTEXT":
        attachment_point = _safe_int(getattr(entity.dxf, "attachment_point", None), 1)
        halign, valign = MTEXT_ATTACHMENT_TO_HV.get(attachment_point, (0, 3))
        props.update(
            {
                "attachment_point": attachment_point,
                "halign": halign,
                "valign": valign,
                "alignment": "ATTACH_{0}".format(attachment_point),
                "text_align_x": ins[0],
                "text_align_y": ins[1],
                "text_align_z": ins[2],
            }
        )
        rot = _mtext_rotation_deg(entity)
        if rot is not None:
            props["rotation"] = float(rot)
        try:
            raw_m = str(getattr(entity, "text", "") or "")
            pal = _mtext_paragraph_horizontal_aligns(raw_m)
            if pal:
                props["mtext_line_aligns"] = pal
        except Exception:
            pass
        try:
            mw = entity.dxf.get("width")
            if mw is not None and float(mw) > 0:
                props["mtext_width"] = float(mw)
        except Exception:
            pass
        try:
            lsf = entity.dxf.get("line_spacing_factor")
            if lsf is not None:
                props["line_spacing_factor"] = float(lsf)
        except Exception:
            pass
        try:
            style_raw = entity.dxf.get("style")
            if style_raw:
                props["style_name"] = str(style_raw)
        except Exception:
            pass
        return anchor, props

    props.update({"text_align_x": ins[0], "text_align_y": ins[1], "text_align_z": ins[2]})
    return anchor, props


def _matrix_rotation_deg(matrix) -> float:
    try:
        import math

        vx = matrix.transform_direction((1.0, 0.0, 0.0))
        return float(math.degrees(math.atan2(float(vx[1]), float(vx[0]))))
    except Exception:
        return 0.0


def _safe_float_or_none(value: Any) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _transform_xyz_props(props: dict[str, Any], prefix: str, matrix) -> None:
    x = _safe_float_or_none(props.get(prefix + "_x"))
    y = _safe_float_or_none(props.get(prefix + "_y"))
    z = _safe_float_or_none(props.get(prefix + "_z"))
    if x is None or y is None:
        return
    tz = z if z is not None else 0.0
    tx, ty, tz2 = transform_point_with_matrix(x, y, tz, matrix)
    props[prefix + "_x"] = float(tx)
    props[prefix + "_y"] = float(ty)
    props[prefix + "_z"] = float(tz2)


def _transform_text_props_with_matrix(props: dict | None, matrix) -> dict:
    out = dict(props or {})
    _transform_xyz_props(out, "insert", matrix)
    _transform_xyz_props(out, "text_align", matrix)
    _transform_xyz_props(out, "align", matrix)
    rot = _safe_float_or_none(out.get("rotation"))
    if rot is not None:
        out["rotation"] = float(rot + _matrix_rotation_deg(matrix))
    return out


def _iter_insert_instances(insert_entity: DXFEntity):
    mcount = int(getattr(insert_entity, "mcount", 1) or 1)
    if mcount > 1 and hasattr(insert_entity, "multi_insert"):
        row_count = max(1, int(getattr(insert_entity.dxf, "row_count", 1) or 1))
        col_count = max(1, int(getattr(insert_entity.dxf, "column_count", 1) or 1))
        idx = 0
        for virtual_insert in insert_entity.multi_insert():
            row = idx // col_count
            col = idx % col_count
            yield virtual_insert, row, col
            idx += 1
        return
    yield insert_entity, 0, 0


def _vec3_tuple(value: Any) -> tuple[float, float, float] | None:
    try:
        x = float(value[0])
        y = float(value[1])
        z = float(value[2]) if len(value) > 2 else 0.0
        return (x, y, z)
    except Exception:
        try:
            x = float(getattr(value, "x"))
            y = float(getattr(value, "y"))
            z = float(getattr(value, "z", 0.0) or 0.0)
            return (x, y, z)
        except Exception:
            return None


def _points_close(p1: tuple[float, float, float], p2: tuple[float, float, float], tol: float = 1e-9) -> bool:
    return (
        abs(float(p1[0]) - float(p2[0])) <= tol
        and abs(float(p1[1]) - float(p2[1])) <= tol
        and abs(float(p1[2]) - float(p2[2])) <= tol
    )


def _arc_points_from_angles(
    center: tuple[float, float, float],
    radius: float,
    start_angle_deg: float,
    end_angle_deg: float,
) -> list[tuple[float, float, float]]:
    """Create ARC sample points in CCW direction, including start/end points."""
    import math

    try:
        cx, cy, cz = float(center[0]), float(center[1]), float(center[2])
        r = float(radius)
        sa = float(start_angle_deg)
        ea = float(end_angle_deg)
    except Exception:
        return []

    if r <= 0.0:
        return []

    sa = sa % 360.0
    ea = ea % 360.0
    span = ea - sa
    if span <= 0.0:
        span += 360.0
    # ARC with identical start/end can represent a full-turn arc.
    if span <= 1e-9:
        span = 360.0

    # Keep a stable density close to circle sampling while avoiding huge point counts.
    segs = max(8, int(math.ceil(span / 12.0)))
    segs = min(256, segs)
    points: list[tuple[float, float, float]] = []
    for i in range(segs + 1):
        t = float(i) / float(segs)
        ang = sa + span * t
        rad = math.radians(ang)
        points.append((cx + r * math.cos(rad), cy + r * math.sin(rad), cz))
    return points


def _flatten_virtual_line_arc_chain(
    segments: list[Any],
) -> list[tuple[float, float, float]]:
    """ezdxf 가상 LINE/ARC 엔티티 목록을 하나의 연속 꼭짓점 리스트로 합친다 (bulge 호 포함)."""
    pts: list[tuple[float, float, float]] = []
    for ve in segments:
        try:
            dt = ve.dxftype()
        except Exception:
            continue
        if dt == "LINE":
            p1 = _point_3d(ve, "start")
            p2 = _point_3d(ve, "end")
            if not pts:
                pts.append(p1)
            elif not _points_close(pts[-1], p1, 1e-4):
                pts.append(p1)
            pts.append(p2)
        elif dt == "ARC":
            c = _point_3d(ve, "center")
            try:
                r = float(ve.dxf.radius)
                sa = float(ve.dxf.start_angle)
                ea = float(ve.dxf.end_angle)
            except Exception:
                continue
            arc_pts = _arc_points_from_angles(c, r, sa, ea)
            if len(arc_pts) < 2:
                continue
            if not pts:
                pts.extend(arc_pts)
            elif _points_close(pts[-1], arc_pts[0], 1e-3):
                pts.extend(arc_pts[1:])
            else:
                pts.extend(arc_pts)
    return pts


def _polyline_vertices_skip_faces(entity: DXFEntity) -> list[tuple[float, float, float]]:
    """POLYLINE 정점만 수집 (PolyFace의 face record 제외)."""
    out: list[tuple[float, float, float]] = []
    try:
        for v in entity.vertices:
            if getattr(v, "is_face_record", False):
                continue
            out.append(_point_3d(v, "location"))
    except Exception:
        return []
    return out


def _lwpolyline_points_world(entity: DXFEntity) -> list[tuple[float, float, float]]:
    """LWPOLYLINE: bulge를 반영해 LINE/ARC로 펼친 뒤 샘플 점. 실패 시 get_points(xy)."""
    try:
        chain = list(virtual_lwpolyline_entities(entity))
        pts = _flatten_virtual_line_arc_chain(chain)
        if len(pts) >= 2:
            return pts
    except Exception as e:
        logger.debug("virtual_lwpolyline_entities failed: %s", e)
    try:
        raw = list(entity.get_points("xy"))
        elev = getattr(entity.dxf, "elevation", None)
        ez = float(elev) if elev is not None else 0.0
        return [(p[0], p[1], ez) for p in raw]
    except Exception:
        return []


def _polyline_points_world(entity: DXFEntity) -> list[tuple[float, float, float]]:
    """POLYLINE: 2D bulge/3D 직선은 virtual로 펼침. PolyFace/PolyMesh는 빈 리스트(별도 처리)."""
    try:
        if getattr(entity, "is_poly_face_mesh", False) or getattr(entity, "is_polygon_mesh", False):
            return []
        chain = list(virtual_polyline_entities(entity))
        pts = _flatten_virtual_line_arc_chain(chain)
        if len(pts) >= 2:
            return pts
    except Exception as e:
        logger.debug("virtual_polyline_entities failed: %s", e)
    return _polyline_vertices_skip_faces(entity)


def _linearized_points_for_entity(
    entity: DXFEntity,
    dxftype: str,
) -> tuple[list[tuple[float, float, float]], bool]:
    import math

    points: list[tuple[float, float, float]] = []
    closed = False

    if dxftype == "ELLIPSE":
        try:
            for v in entity.flattening(0.5, segments=16):
                p = _vec3_tuple(v)
                if p is not None:
                    points.append(p)
        except Exception as e:
            logger.debug("ELLIPSE flattening failed: %s", e)
            return [], False
        closed = len(points) > 1 and _points_close(points[0], points[-1])
        if not closed:
            try:
                start = float(entity.dxf.start_param)
                end = float(entity.dxf.end_param)
                span = abs(end - start)
                if abs(span - (2.0 * math.pi)) < 1e-6:
                    closed = True
            except Exception:
                pass
        if closed and points and not _points_close(points[0], points[-1]):
            points.append(points[0])
        return points, closed

    if dxftype == "SPLINE":
        try:
            for v in entity.flattening(0.5, segments=8):
                p = _vec3_tuple(v)
                if p is not None:
                    points.append(p)
        except Exception as e:
            logger.debug("SPLINE flattening failed: %s", e)
            return [], False
        try:
            closed = bool(getattr(entity, "closed", False))
        except Exception:
            closed = False
        if closed and points and not _points_close(points[0], points[-1]):
            points.append(points[0])
        return points, closed

    if dxftype in ("3DFACE", "SOLID"):
        raw_vertices: list[Any] = []
        if hasattr(entity, "wcs_vertices"):
            try:
                raw_vertices = list(entity.wcs_vertices())
            except Exception:
                raw_vertices = []
        if not raw_vertices:
            for attr in ("vtx0", "vtx1", "vtx2", "vtx3"):
                try:
                    if entity.dxf.hasattr(attr):
                        raw_vertices.append(entity.dxf.get(attr))
                except Exception:
                    continue
        for rv in raw_vertices:
            p = _vec3_tuple(rv)
            if p is None:
                continue
            if points and _points_close(points[-1], p):
                continue
            points.append(p)
        if len(points) > 1 and _points_close(points[0], points[-1]):
            points = points[:-1]
        if len(points) >= 3:
            points.append(points[0])
            closed = True
        return points, closed

    return [], False


def _geometry_from_linearized_entity(
    entity: DXFEntity,
    dxftype: str,
) -> dict[str, Any] | None:
    points, closed = _linearized_points_for_entity(entity, dxftype)
    if not points:
        return None
    geom_wkt = linestring_wkt(points)
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
    bbox_wkt = bbox_from_points(points)
    return {
        "points": points,
        "closed": closed,
        "geom_wkt": geom_wkt,
        "centroid_wkt": centroid_wkt,
        "bbox_wkt": bbox_wkt,
    }


def _polygon_signed_area_2d(points: list[tuple[float, ...]]) -> float:
    """2D ?대━怨ㅼ쓽 遺???덈뒗 硫댁쟻. 諛섏떆怨??묒닔, ?쒓퀎=?뚯닔. ?먯씠 3媛?誘몃쭔?대㈃ 0."""
    if len(points) < 3:
        return 0.0
    n = len(points)
    area = 0.0
    for i in range(n):
        j = (i + 1) % n
        area += points[i][0] * points[j][1] - points[j][0] * points[i][1]
    return area * 0.5


# ?댁튂 寃쎄퀎濡??덉슜??理쒕? 硫댁쟻/醫뚰몴 (?댁긽移샕룹삤瑜??곗씠???쒖쇅)
MAX_HATCH_AREA = 1e12
MAX_HATCH_COORD = 1e9


def _hatch_fallback_candidates_from_edge_paths(entity: DXFEntity) -> list[list[tuple[float, float, float]]]:
    """from_hatch flattening ?ㅽ뙣 ??EdgePath ArcEdge(?뱁엳 0~360?? 蹂댁젙 ?꾨낫 ?앹꽦."""
    import math

    out: list[list[tuple[float, float, float]]] = []
    try:
        hatch_paths = list(entity.paths)
    except Exception:
        return out

    for path in hatch_paths:
        edges = getattr(path, "edges", None)
        if not edges:
            continue
        edges = list(edges)
        if len(edges) != 1:
            continue
        edge = edges[0]
        if type(edge).__name__.upper() != "ARCEDGE":
            continue

        center = getattr(edge, "center", None)
        radius = getattr(edge, "radius", None)
        if center is None or radius is None:
            continue
        try:
            cx = float(center[0])
            cy = float(center[1])
            r = float(radius)
        except Exception:
            continue
        if r <= 0.0:
            continue

        try:
            sa = float(getattr(edge, "start_angle", 0.0) or 0.0)
            ea = float(getattr(edge, "end_angle", 0.0) or 0.0)
        except Exception:
            sa, ea = 0.0, 360.0
        ccw = bool(getattr(edge, "ccw", True))

        span = (ea - sa) if ccw else (sa - ea)
        while span < 0.0:
            span += 360.0
        if span <= 1e-6:
            span = 360.0
        if span > 360.0:
            span = span % 360.0
            if span <= 1e-6:
                span = 360.0

        segs = max(32, int(span / 6.0))
        points: list[tuple[float, float, float]] = []
        for i in range(segs + 1):
            t = float(i) / float(segs)
            ang = sa + (span * t if ccw else -span * t)
            rad = math.radians(ang)
            points.append((cx + r * math.cos(rad), cy + r * math.sin(rad), 0.0))
        if points and not _points_close(points[0], points[-1]):
            points.append(points[0])
        if len(points) >= 4:
            out.append(points)

    return out


def _hatch_to_geom_item(entity: DXFEntity, doc=None, layer_name=None) -> dict | None:
    """HATCH ?뷀떚?곕? {entity_type, geom_wkt, color, props} 濡?蹂?? from_hatch濡?寃쎄퀎 異붿텧.
    ?щ윭 寃쎄퀎 以??멸낸(硫댁쟻??媛?????좏슚 寃쎄퀎)留??ъ슜?섍퀬, 鍮꾩젙?곸쟻?쇰줈 ??寃쎄퀎???쒖쇅."""
    try:
        from ezdxf.path import from_hatch
        meta = _color_meta(entity, doc=doc, layout=None, insert_props=None)
        color = meta["color"]
        paths = list(from_hatch(entity))
        if not paths:
            logger.debug("_hatch_to_geom_item: no paths (layer=%s)", layer_name or "0")
            return None
        solid_fill = bool(getattr(entity.dxf, "solid_fill", 1))
        pattern_name = str(getattr(entity.dxf, "pattern_name", "SOLID") or "SOLID")
        candidates: list[tuple[list[tuple[float, ...]], float]] = []

        def add_candidate(points: list[tuple[float, ...]]) -> None:
            if len(points) < 3:
                return
            if any(abs(c) > MAX_HATCH_COORD for p in points for c in p[:2]):
                return
            area = abs(_polygon_signed_area_2d(points))
            if area > MAX_HATCH_AREA or area <= 0:
                return
            candidates.append((points, area))

        for path in paths:
            p = path
            if p.has_sub_paths:
                for sub in p.sub_paths():
                    try:
                        sub.close()
                        vertices = list(sub.flattening(0.01, segments=4))
                        if len(vertices) < 3:
                            continue
                        points = [
                            (float(v.x), float(v.y), float(v.z) if hasattr(v, "z") and v.z is not None else 0.0)
                            for v in vertices
                        ]
                        add_candidate(points)
                    except Exception:
                        continue
            else:
                try:
                    p.close()
                    vertices = list(p.flattening(0.01, segments=4))
                    if len(vertices) < 3:
                        continue
                    points = [
                        (float(v.x), float(v.y), float(v.z) if hasattr(v, "z") and v.z is not None else 0.0)
                        for v in vertices
                    ]
                    add_candidate(points)
                except Exception:
                    continue

        if not candidates:
            for points in _hatch_fallback_candidates_from_edge_paths(entity):
                add_candidate(points)
        if not candidates:
            logger.debug("_hatch_to_geom_item: no valid candidates (layer=%s, paths=%s)", layer_name or "0", len(paths))
            return None
        best_points, _ = max(candidates, key=lambda x: x[1])
        geom_wkt = polygon_wkt(best_points)
        return {
            "entity_type": "HATCH",
            "geom_wkt": geom_wkt,
            "color": color,
            "layer": (layer_name or "0"),
            "props": {
                "solid_fill": solid_fill,
                "pattern_name": pattern_name,
                "color_raw": meta["color_raw"],
                "color_bylayer": meta["color_bylayer"],
            },
        }
    except Exception as e:
        logger.debug("_hatch_to_geom_item: exception (layer=%s): %s", layer_name or "0", e)
        return None
    return None


def _wipeout_to_geom_item(entity: DXFEntity, doc=None, layer_name=None) -> dict | None:
    """WIPEOUT??POLYGON WKT濡?蹂?? 酉곗뼱?먯꽌 諛곌꼍??梨꾩?(mask)?쇰줈 ?ъ슜."""
    try:
        points: list[tuple[float, float, float]] = []
        if hasattr(entity, "boundary_path_wcs"):
            for v in entity.boundary_path_wcs():
                p = _vec3_tuple(v)
                if p is not None:
                    points.append(p)
        if len(points) < 3:
            return None
        if not _points_close(points[0], points[-1]):
            points.append(points[0])
        meta = _color_meta(entity, doc=doc, layout=None, insert_props=None)
        return {
            "entity_type": "WIPEOUT",
            "geom_wkt": polygon_wkt(points),
            "color": meta["color"],
            "props": {
                "is_wipeout": True,
                "color_raw": meta["color_raw"],
                "color_bylayer": meta["color_bylayer"],
            },
        }
    except Exception as e:
        logger.debug("_wipeout_to_geom_item: exception (layer=%s): %s", layer_name or "0", e)
        return None


def _block_entity_to_geom_item(
    entity: DXFEntity,
    doc=None,
    insert_color_fallback: int | None = None,
    insert_layer: str | None = None,
) -> list[dict]:
    """블록 안 단일 DXF 엔티티 → 0개 이상의 {entity_type, geom_wkt, color, props} (PolyFace 등은 복수)."""
    import math
    if _is_invisible(entity):
        return []
    dxftype = entity.dxftype()
    if dxftype == "HATCH":
        h = _hatch_to_geom_item(entity, doc=doc, layer_name=_layer(entity))
        return [h] if h else []
    if dxftype == "WIPEOUT":
        w = _wipeout_to_geom_item(entity, doc=doc, layer_name=_layer(entity))
        return [w] if w else []
    if dxftype not in (
        "LINE",
        "LWPOLYLINE",
        "POLYLINE",
        "ARC",
        "CIRCLE",
        "ELLIPSE",
        "SPLINE",
        "3DFACE",
        "SOLID",
        "TEXT",
        "MTEXT",
        "POINT",
    ):
        return []
    layer_raw = _layer(entity) or "0"
    layer_name = (insert_layer or "0") if layer_raw == "0" and insert_layer else layer_raw
    insert_props = None
    if insert_color_fallback is not None or insert_layer:
        insert_props = {
            "pen": insert_color_fallback if insert_color_fallback is not None else 7,
            "layer": insert_layer or "0",
        }
    meta = _color_meta(entity, doc=doc, insert_props=insert_props, insert_layer=insert_layer)
    color = meta["color"]
    cprops = {
        "color_raw": meta["color_raw"],
        "color_bylayer": meta["color_bylayer"],
        "block_def_layer": layer_raw,
    }
    try:
        if dxftype == "LINE":
            p1 = _point_3d(entity, "start")
            p2 = _point_3d(entity, "end")
            return [{"entity_type": "LINE", "geom_wkt": linestring_wkt([p1, p2]), "color": color, "layer": (layer_name or "0"), "props": cprops}]
        if dxftype == "LWPOLYLINE":
            points = _lwpolyline_points_world(entity)
            if len(points) < 2:
                return []
            closed = _is_polyline_closed(entity)
            if closed and points and not _points_close(points[0], points[-1], 1e-5):
                points = list(points) + [points[0]]
            return [{"entity_type": "LWPOLYLINE", "geom_wkt": linestring_wkt(points), "color": color, "layer": (layer_name or "0"), "props": cprops}]
        if dxftype == "POLYLINE":
            if getattr(entity, "is_poly_face_mesh", False) or getattr(entity, "is_polygon_mesh", False):
                out: list[dict] = []
                try:
                    for vf in virtual_polyline_entities(entity):
                        if vf.dxftype() != "3DFACE":
                            continue
                        geo = _geometry_from_linearized_entity(vf, "3DFACE")
                        if not geo:
                            continue
                        out.append(
                            {
                                "entity_type": "3DFACE",
                                "geom_wkt": geo["geom_wkt"],
                                "color": color,
                                "layer": (layer_name or "0"),
                                "props": cprops,
                            }
                        )
                except Exception as e:
                    logger.debug("block POLYLINE mesh: %s", e)
                if out:
                    return out
            points = _polyline_points_world(entity)
            if len(points) < 2:
                return []
            closed = _is_polyline_closed(entity)
            if closed and points and not _points_close(points[0], points[-1], 1e-5):
                points = list(points) + [points[0]]
            return [{"entity_type": "POLYLINE", "geom_wkt": linestring_wkt(points), "color": color, "layer": (layer_name or "0"), "props": cprops}]
        if dxftype == "ARC":
            center = _point_3d(entity, "center")
            radius = float(entity.dxf.radius)
            start_angle = float(entity.dxf.start_angle)
            end_angle = float(entity.dxf.end_angle)
            points = _arc_points_from_angles(center, radius, start_angle, end_angle)
            if len(points) < 2:
                return []
            return [{"entity_type": "ARC", "geom_wkt": linestring_wkt(points), "color": color, "layer": (layer_name or "0"), "props": cprops}]
        if dxftype == "CIRCLE":
            center = _point_3d(entity, "center")
            radius = float(entity.dxf.radius)
            n = 32
            points = [(center[0] + radius * math.cos(2 * math.pi * i / n), center[1] + radius * math.sin(2 * math.pi * i / n), center[2]) for i in range(n + 1)]
            return [{"entity_type": "CIRCLE", "geom_wkt": linestring_wkt(points), "color": color, "layer": (layer_name or "0"), "props": cprops}]
        if dxftype in ("ELLIPSE", "SPLINE", "3DFACE", "SOLID"):
            geo = _geometry_from_linearized_entity(entity, dxftype)
            if not geo:
                return []
            return [
                {
                    "entity_type": dxftype,
                    "geom_wkt": geo["geom_wkt"],
                    "color": color,
                    "layer": (layer_name or "0"),
                    "props": cprops,
                }
            ]
        if dxftype == "TEXT":
            pos = _point_3d(entity, "insert")
            anchor, align_props = _extract_text_alignment(entity, "TEXT", pos)
            text = _entity_plain_text(entity, "TEXT")
            height = entity.dxf.get("height")
            props = {}
            if height is not None:
                props["height"] = float(height)
            if text:
                props["text"] = text
            return [
                {
                    "entity_type": "TEXT",
                    "geom_wkt": point_wkt(anchor[0], anchor[1], anchor[2]),
                    "color": color,
                    "layer": (layer_name or "0"),
                    "props": {**props, **align_props, **cprops},
                }
            ]
        if dxftype == "MTEXT":
            pos = _point_3d(entity, "insert")
            anchor, align_props = _extract_text_alignment(entity, "MTEXT", pos)
            text = (entity.dxf.get("text") or "").strip()
            char_height = entity.dxf.get("char_height")
            props = {}
            if char_height is not None:
                props["char_height"] = float(char_height)
            else:
                mh = entity.dxf.get("height")
                if mh is not None:
                    props["height"] = float(mh)
            if text:
                props["text"] = text
            return [
                {
                    "entity_type": "MTEXT",
                    "geom_wkt": point_wkt(anchor[0], anchor[1], anchor[2]),
                    "color": color,
                    "layer": (layer_name or "0"),
                    "props": {**props, **align_props, **cprops},
                }
            ]
        if dxftype == "POINT":
            pos = _point_3d(entity, "location")
            return [{"entity_type": "POINT", "geom_wkt": point_wkt(pos[0], pos[1], pos[2]), "color": color, "layer": (layer_name or "0"), "props": cprops}]
    except Exception:
        return []
    return []


def _block_to_geom_items(
    block,
    doc,
    _seen: set[str] | None = None,
    _depth: int = 0,
    _insert_layer: str | None = None,
    _insert_color_fallback: int | None = None,
    skip_off_layers: bool = False,
) -> list[dict]:
    """釉붾줉 ?대? ?뷀떚?곕? {entity_type, geom_wkt, color} 由ъ뒪?몃줈 蹂??
    以묒꺽 INSERT???ш??곸쑝濡???컻. ?쒗솚 李몄“ 諛⑹?(_seen), 源딆씠 ?쒗븳(20).
    array INSERT(row_count x column_count)??媛?蹂듭젣蹂?insert point濡?蹂??
    _seen? 蹂듭궗 ?놁씠 add/discard濡??ъ궗??蹂듭궗 鍮꾩슜 ?쒓굅)."""
    if _depth > 20:
        return []
    _seen = _seen or set()
    block_name = getattr(block, "name", "") or ""
    if block_name in _seen:
        return []
    _seen.add(block_name)
    items: list[dict] = []
    insert_name_counts: dict[str, int] = {}
    try:
        for ent in block:
            if _is_invisible(ent):
                continue
            dxftype = ent.dxftype()
            if dxftype == "INSERT":
                nested_name = getattr(ent.dxf, "name", "") or ""
                nested_block = doc.blocks.get(nested_name) if getattr(doc, "blocks", None) else None
                if not nested_block:
                    continue
                insert_name_counts[nested_name] = insert_name_counts.get(nested_name, 0) + 1
                occ = insert_name_counts[nested_name]
                for instance_insert, row, col in _iter_insert_instances(ent):
                    if skip_off_layers and _skip_entity_for_off_layer(
                        doc, _layer(instance_insert), _insert_layer, True,
                    ):
                        continue
                    parent_insert_props = None
                    if _insert_color_fallback is not None or _insert_layer:
                        parent_insert_props = {
                            "pen": _insert_color_fallback if _insert_color_fallback is not None else 7,
                            "layer": _insert_layer or "0",
                        }
                    child_layer_raw = _layer(instance_insert) or "0"
                    child_insert_layer = (_insert_layer or "0") if child_layer_raw == "0" and _insert_layer else child_layer_raw
                    child_insert_color = resolve_color_to_aci(
                        instance_insert,
                        doc=doc,
                        insert_props=parent_insert_props,
                        insert_layer=_insert_layer,
                    )
                    nested_items = _block_to_geom_items(
                        nested_block,
                        doc,
                        _seen,
                        _depth + 1,
                        _insert_layer=child_insert_layer,
                        _insert_color_fallback=child_insert_color,
                        skip_off_layers=skip_off_layers,
                    )
                    seg_key = _make_insert_instance_key(nested_name, occ, row, col)
                    seg_name = (nested_name or "BLOCK").strip() or "BLOCK"
                    current_seg = {"name": seg_name, "instance_key": seg_key}
                    try:
                        matrix = instance_insert.matrix44()
                    except Exception:
                        matrix = None
                    for item in nested_items:
                        twkt = transform_wkt_with_matrix(item["geom_wkt"], matrix=matrix)
                        if twkt:
                            item_props = _transform_text_props_with_matrix(item.get("props"), matrix)
                            child_path = _clone_block_hierarchy_path(item_props.get("block_hierarchy_path"))
                            full_path = [dict(current_seg)]
                            for seg in child_path:
                                ck = str(seg.get("instance_key") or "").strip()
                                if not ck:
                                    continue
                                full_path.append({
                                    "name": str(seg.get("name") or "").strip() or "BLOCK",
                                    "instance_key": current_seg["instance_key"] + "/" + ck,
                                })
                            item_props = _merge_props_with_block_hierarchy(item_props, full_path)
                            items.append({**item, "geom_wkt": twkt, "props": item_props})
            else:
                if skip_off_layers and _skip_entity_for_off_layer(doc, _layer(ent), _insert_layer, True):
                    continue
                for direct in _block_entity_to_geom_item(
                    ent,
                    doc=doc,
                    insert_color_fallback=_insert_color_fallback,
                    insert_layer=_insert_layer,
                ):
                    if direct:
                        direct["props"] = _merge_props_with_block_hierarchy(direct.get("props"), None)
                        items.append(direct)
    finally:
        _seen.discard(block_name)
    return items


def _geom_item_to_entity_dict(item: dict, linetype: str | None = None) -> dict | None:
    geom_wkt = item.get("geom_wkt")
    if not geom_wkt:
        return None
    entity_type = str(item.get("entity_type") or "").strip().upper() or "LINE"
    layer = (item.get("layer") or "0")
    color = item.get("color")
    props = dict(item.get("props") or {})
    out_linetype = item.get("linetype")
    if out_linetype is None:
        out_linetype = linetype

    centroid_wkt = None
    bbox_wkt = None
    if entity_type in ("TEXT", "MTEXT", "ATTRIB", "POINT"):
        centroid_wkt = geom_wkt
    else:
        pts = wkt_points_to_list(geom_wkt)
        if pts:
            xs = [p[0] for p in pts]
            ys = [p[1] for p in pts]
            zs = [p[2] if len(p) > 2 else 0.0 for p in pts]
            centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), sum(zs) / len(zs))
            bbox_wkt = bbox_from_points(pts)

    return {
        "entity_type": entity_type,
        "layer": layer,
        "color": color,
        "linetype": out_linetype,
        "geom_wkt": geom_wkt,
        "centroid_wkt": centroid_wkt,
        "bbox_wkt": bbox_wkt,
        "props": props,
        "fingerprint": None,
    }


def _vec3_to_xyz(v) -> tuple[float, float, float] | None:
    if v is None:
        return None
    try:
        return (float(v.x), float(v.y), float(v.z))
    except Exception:
        return None


def _ensure_dimension_text_midpoint(dim_entity: DXFEntity) -> None:
    """ezdxf DIMENSION.__virtual_entities__ 는 text_midpoint 가 None 이면 .z 접근으로 실패한다."""
    try:
        if dim_entity.dxftype() != "DIMENSION":
            return
        dxf = dim_entity.dxf
        if getattr(dxf, "text_midpoint", None) is not None:
            return
        from ezdxf.math import Vec3

        dp2 = getattr(dxf, "defpoint2", None)
        dp3 = getattr(dxf, "defpoint3", None)
        dp = getattr(dxf, "defpoint", None)
        ins = getattr(dxf, "insert", None)
        cand = None
        if dp2 is not None and dp3 is not None:
            mid = (Vec3(dp2) + Vec3(dp3)) * 0.5
            if dp is not None:
                mid = mid.replace(z=float(Vec3(dp).z))
            cand = mid
        elif ins is not None:
            cand = Vec3(ins)
        elif dp is not None:
            cand = Vec3(dp)
        if cand is not None:
            dim_entity.dxf.text_midpoint = cand
    except Exception:
        pass


def _dimension_fallback_entity_dicts(
    dim_entity: DXFEntity,
    layer: str,
    color,
    linetype: str | None,
    props: dict,
    tol: float,
) -> list[dict]:
    """virtual_entities 가 비었거나 전부 스킵될 때 defpoint 계열로 최소 치수선·문자를 만든다."""
    out: list[dict] = []
    try:
        dxf = dim_entity.dxf
        p2 = _vec3_to_xyz(getattr(dxf, "defpoint2", None))
        p3 = _vec3_to_xyz(getattr(dxf, "defpoint3", None))
        if not (p2 and p3):
            p2 = _vec3_to_xyz(getattr(dxf, "defpoint", None))
            p3 = _vec3_to_xyz(getattr(dxf, "defpoint2", None))
        if p2 and p3:
            geom_wkt = linestring_wkt([p2, p3])
            centroid_wkt = point_wkt((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2, (p2[2] + p3[2]) / 2)
            bbox_wkt = bbox_from_points([p2, p3])
            fp = fingerprint_line(p2, p3, "LINE", layer, color, linetype, tol)
            out.append({
                "entity_type": "LINE",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": _props_mark_from_dimension({**props, "dimension_fallback": True}),
                "fingerprint": fp,
            })
        anchor = _vec3_to_xyz(getattr(dxf, "defpoint", None))
        if anchor is None and p2 and p3:
            anchor = ((p2[0] + p3[0]) / 2, (p2[1] + p3[1]) / 2, (p2[2] + p3[2]) / 2)
        ms = getattr(dxf, "actual_measurement", None)
        if ms is not None:
            label = str(ms).strip()
        else:
            label = str(getattr(dxf, "text", "") or "").strip()
        if anchor and label:
            geom_wkt = point_wkt(anchor[0], anchor[1], anchor[2])
            text_props = _props_mark_from_dimension({**props, "text": label, "dimension_fallback": True})
            fp = fingerprint_text(anchor, label, "TEXT", layer, color, linetype, tol)
            out.append({
                "entity_type": "TEXT",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": geom_wkt,
                "bbox_wkt": None,
                "props": text_props,
                "fingerprint": fp,
            })
    except Exception:
        return []
    return out


def _virtual_entity_to_dict(
    virtual_entity: DXFEntity,
    doc=None,
    insert_color_fallback: int | None = None,
    insert_layer: str | None = None,
) -> dict | None:
    """INSERT.virtual_entities()濡??섏삩 ?뷀떚???대? WCS)瑜?entities ??ぉ dict濡?蹂??
    insert_color_fallback: ByBlock ?댁꽍??ACI. insert_layer: 釉붾줉 ??layer 0???뷀떚?곗쓽 ByLayer ?댁꽍??INSERT ?덉씠??
    """
    import math
    if _is_invisible(virtual_entity):
        return None
    dxftype = virtual_entity.dxftype()
    if dxftype not in SUPPORTED_ENTITY_TYPES:
        return None
    layer_raw = _layer(virtual_entity) or "0"
    layer = (insert_layer or "0") if layer_raw == "0" and insert_layer else layer_raw
    insert_props = None
    if insert_color_fallback is not None or insert_layer:
        insert_props = {
            "pen": insert_color_fallback if insert_color_fallback is not None else 7,
            "layer": insert_layer or "0",
        }
    meta = _color_meta(virtual_entity, doc=doc, insert_props=insert_props, insert_layer=insert_layer)
    color = meta["color"]
    linetype = _linetype(virtual_entity)
    props = _attach_lineweight(
        virtual_entity,
        {
            **_entity_props(virtual_entity),
            "color_raw": meta["color_raw"],
            "color_bylayer": meta["color_bylayer"],
            "block_def_layer": layer_raw,
        },
    )
    try:
        if dxftype == "LINE":
            p1 = _point_3d(virtual_entity, "start")
            p2 = _point_3d(virtual_entity, "end")
            geom_wkt = linestring_wkt([p1, p2])
            pts = [p1, p2]
            centroid_wkt = point_wkt((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, (p1[2] + p2[2]) / 2)
            bbox_wkt = bbox_from_points(pts)
            return {"entity_type": "LINE", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": centroid_wkt, "bbox_wkt": bbox_wkt, "props": props, "fingerprint": None}
        if dxftype == "LWPOLYLINE":
            points = _lwpolyline_points_world(virtual_entity)
            if len(points) < 2:
                return None
            closed = _is_polyline_closed(virtual_entity)
            if closed and points and not _points_close(points[0], points[-1], 1e-5):
                points = list(points) + [points[0]]
            geom_wkt = linestring_wkt(points)
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
            bbox_wkt = bbox_from_points(points)
            return {"entity_type": "LWPOLYLINE", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": centroid_wkt, "bbox_wkt": bbox_wkt, "props": props, "fingerprint": None}
        if dxftype == "POLYLINE":
            if getattr(virtual_entity, "is_poly_face_mesh", False) or getattr(virtual_entity, "is_polygon_mesh", False):
                try:
                    for vf in virtual_polyline_entities(virtual_entity):
                        if vf.dxftype() != "3DFACE":
                            continue
                        geo = _geometry_from_linearized_entity(vf, "3DFACE")
                        if not geo:
                            continue
                        return {
                            "entity_type": "3DFACE",
                            "layer": layer,
                            "color": color,
                            "linetype": linetype,
                            "geom_wkt": geo["geom_wkt"],
                            "centroid_wkt": geo["centroid_wkt"],
                            "bbox_wkt": geo["bbox_wkt"],
                            "props": props,
                            "fingerprint": None,
                        }
                except Exception as e:
                    logger.debug("virtual POLYLINE mesh: %s", e)
                return None
            points = _polyline_points_world(virtual_entity)
            if len(points) < 2:
                return None
            closed = _is_polyline_closed(virtual_entity)
            if closed and points and not _points_close(points[0], points[-1], 1e-5):
                points = list(points) + [points[0]]
            geom_wkt = linestring_wkt(points)
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
            bbox_wkt = bbox_from_points(points)
            return {"entity_type": "POLYLINE", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": centroid_wkt, "bbox_wkt": bbox_wkt, "props": props, "fingerprint": None}
        if dxftype == "ARC":
            center = _point_3d(virtual_entity, "center")
            radius = float(virtual_entity.dxf.radius)
            start_angle = float(virtual_entity.dxf.start_angle)
            end_angle = float(virtual_entity.dxf.end_angle)
            points = _arc_points_from_angles(center, radius, start_angle, end_angle)
            if len(points) < 2:
                return None
            geom_wkt = linestring_wkt(points)
            centroid_wkt = point_wkt(center[0], center[1], center[2])
            bbox_wkt = bbox_from_points(points)
            return {"entity_type": "ARC", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": centroid_wkt, "bbox_wkt": bbox_wkt, "props": props, "fingerprint": None}
        if dxftype == "CIRCLE":
            center = _point_3d(virtual_entity, "center")
            radius = float(virtual_entity.dxf.radius)
            n = 32
            points = [(center[0] + radius * math.cos(2 * math.pi * i / n), center[1] + radius * math.sin(2 * math.pi * i / n), center[2]) for i in range(n + 1)]
            geom_wkt = linestring_wkt(points)
            centroid_wkt = point_wkt(center[0], center[1], center[2])
            bbox_wkt = bbox_from_points(points)
            return {"entity_type": "CIRCLE", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": centroid_wkt, "bbox_wkt": bbox_wkt, "props": props, "fingerprint": None}
        if dxftype in ("ELLIPSE", "SPLINE", "3DFACE", "SOLID"):
            geo = _geometry_from_linearized_entity(virtual_entity, dxftype)
            if not geo:
                return None
            return {
                "entity_type": dxftype,
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geo["geom_wkt"],
                "centroid_wkt": geo["centroid_wkt"],
                "bbox_wkt": geo["bbox_wkt"],
                "props": props,
                "fingerprint": None,
            }
        if dxftype == "TEXT":
            pos = _point_3d(virtual_entity, "insert")
            anchor, align_props = _extract_text_alignment(virtual_entity, "TEXT", pos)
            text = _entity_plain_text(virtual_entity, "TEXT")
            geom_wkt = point_wkt(anchor[0], anchor[1], anchor[2])
            text_props = {**props, **align_props, "text": text}
            h = virtual_entity.dxf.get("height")
            if h is not None:
                text_props["height"] = float(h)
            return {"entity_type": "TEXT", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": geom_wkt, "bbox_wkt": None, "props": text_props, "fingerprint": None}
        if dxftype == "MTEXT":
            pos = _point_3d(virtual_entity, "insert")
            anchor, align_props = _extract_text_alignment(virtual_entity, "MTEXT", pos)
            text = _entity_plain_text(virtual_entity, "MTEXT")
            geom_wkt = point_wkt(anchor[0], anchor[1], anchor[2])
            text_props = {**props, **align_props, "text": text}
            ch = virtual_entity.dxf.get("char_height")
            if ch is not None:
                text_props["char_height"] = float(ch)
            else:
                h = virtual_entity.dxf.get("height")
                if h is not None:
                    text_props["height"] = float(h)
            return {"entity_type": "MTEXT", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": geom_wkt, "bbox_wkt": None, "props": text_props, "fingerprint": None}
        if dxftype == "POINT":
            pos = _point_3d(virtual_entity, "location")
            geom_wkt = point_wkt(pos[0], pos[1], pos[2])
            return {"entity_type": "POINT", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": geom_wkt, "bbox_wkt": None, "props": props, "fingerprint": None}
        if dxftype == "ATTRIB":
            pos = _point_3d(virtual_entity, "insert")
            anchor, align_props = _extract_text_alignment(virtual_entity, "ATTRIB", pos)
            text = _entity_plain_text(virtual_entity, "ATTRIB")
            geom_wkt = point_wkt(anchor[0], anchor[1], anchor[2])
            text_props = {**props, **align_props, "text": text}
            h = virtual_entity.dxf.get("height")
            if h is not None:
                text_props["height"] = float(h)
            return {"entity_type": "ATTRIB", "layer": layer, "color": color, "linetype": linetype, "geom_wkt": geom_wkt, "centroid_wkt": geom_wkt, "bbox_wkt": None, "props": text_props, "fingerprint": None}
        if dxftype == "HATCH":
            item = _hatch_to_geom_item(virtual_entity, doc=doc, layer_name=layer)
            if not item:
                return None
            points = wkt_points_to_list(item["geom_wkt"])
            centroid_wkt = None
            bbox_wkt = None
            if points:
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
                bbox_wkt = bbox_from_points(points)
            return {
                "entity_type": "HATCH",
                "layer": layer,
                "color": item.get("color", color),
                "linetype": linetype,
                "geom_wkt": item["geom_wkt"],
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": {**(item.get("props") or {}), **props},
                "fingerprint": None,
            }
        if dxftype == "WIPEOUT":
            item = _wipeout_to_geom_item(virtual_entity, doc=doc, layer_name=layer)
            if not item:
                return None
            points = wkt_points_to_list(item["geom_wkt"])
            centroid_wkt = None
            bbox_wkt = None
            if points:
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
                bbox_wkt = bbox_from_points(points)
            return {
                "entity_type": "WIPEOUT",
                "layer": layer,
                "color": item.get("color", color),
                "linetype": linetype,
                "geom_wkt": item["geom_wkt"],
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": {**(item.get("props") or {}), **props},
                "fingerprint": None,
            }
    except Exception:
        return None
    return None


def _explode_insert_into_entities(
    insert_entity: DXFEntity,
    entities: list,
    doc=None,
    temp_key: int | None = None,
    block_hierarchy_path: list[dict] | None = None,
    _depth: int = 0,
    skip_off_layers: bool = False,
    mark_from_dimension: bool = False,
) -> int:
    """INSERT??virtual_entities()瑜??ш??곸쑝濡?蹂?섑빐 entities??異붽?. 以묒꺽 INSERT쨌DIMENSION ???ы븿. 諛섑솚: 異붽???媛쒖닔."""
    if _is_invisible(insert_entity):
        return 0
    if skip_off_layers and _skip_entity_for_off_layer(doc, _layer(insert_entity), None, True):
        return 0
    if _depth > 50:
        return 0
    virtual_container_types = {"DIMENSION", "LEADER", "MLEADER", "MULTILEADER"}
    count = 0
    hierarchy_path = _clone_block_hierarchy_path(block_hierarchy_path)
    nested_name_counts: dict[str, int] = {}
    insert_color = resolve_color_to_aci(insert_entity, doc=doc, insert_props=None)
    insert_layer = insert_entity.dxf.get("layer", "0") or "0"
    try:
        vir_list = list(insert_entity.virtual_entities())
        for v in vir_list:
            if _is_invisible(v):
                continue
            if getattr(v, "dxftype", None) and v.dxftype() == "INSERT":
                nested_name = getattr(v.dxf, "name", "") or "BLOCK"
                nested_name_counts[nested_name] = nested_name_counts.get(nested_name, 0) + 1
                occ = nested_name_counts[nested_name]
                token = _make_insert_instance_key(nested_name, occ)
                prefix = hierarchy_path[-1]["instance_key"] if hierarchy_path else ""
                instance_key = token if not prefix else (prefix + "/" + token)
                child_path = hierarchy_path + [{"name": nested_name, "instance_key": instance_key}]
                count += _explode_insert_into_entities(
                    v,
                    entities,
                    doc=doc,
                    temp_key=temp_key,
                    block_hierarchy_path=child_path,
                    _depth=_depth + 1,
                    skip_off_layers=skip_off_layers,
                    mark_from_dimension=mark_from_dimension,
                )
            elif getattr(v, "virtual_entities", None) and v.dxftype() in virtual_container_types:
                nested_processed = 0
                sub_mark = mark_from_dimension or (v.dxftype() == "DIMENSION")
                if v.dxftype() == "DIMENSION":
                    _ensure_dimension_text_midpoint(v)
                try:
                    nested_entities = list(v.virtual_entities())
                except Exception:
                    nested_entities = []
                for v2 in nested_entities:
                    if _is_invisible(v2):
                        continue
                    if getattr(v2, "dxftype", None) and v2.dxftype() == "INSERT":
                        nested_name = getattr(v2.dxf, "name", "") or "BLOCK"
                        nested_name_counts[nested_name] = nested_name_counts.get(nested_name, 0) + 1
                        occ = nested_name_counts[nested_name]
                        token = _make_insert_instance_key(nested_name, occ)
                        prefix = hierarchy_path[-1]["instance_key"] if hierarchy_path else ""
                        instance_key = token if not prefix else (prefix + "/" + token)
                        child_path = hierarchy_path + [{"name": nested_name, "instance_key": instance_key}]
                        added = _explode_insert_into_entities(
                            v2,
                            entities,
                            doc=doc,
                            temp_key=temp_key,
                            block_hierarchy_path=child_path,
                            _depth=_depth + 1,
                            skip_off_layers=skip_off_layers,
                            mark_from_dimension=sub_mark,
                        )
                        count += added
                        nested_processed += added
                    else:
                        ed = _virtual_entity_to_dict(v2, doc=doc, insert_color_fallback=insert_color, insert_layer=insert_layer)
                        if ed and (not skip_off_layers or _layer_is_on(doc, (ed.get("layer") or "0"))):
                            ed["props"] = _merge_props_with_block_hierarchy(ed.get("props"), hierarchy_path)
                            if sub_mark:
                                ed["props"] = _props_mark_from_dimension(ed.get("props"))
                            if temp_key is not None:
                                ed["_temp_insert_key"] = temp_key
                            entities.append(ed)
                            count += 1
                            nested_processed += 1
                if nested_processed == 0:
                    ed = _virtual_entity_to_dict(v, doc=doc, insert_color_fallback=insert_color, insert_layer=insert_layer)
                    if ed and (not skip_off_layers or _layer_is_on(doc, (ed.get("layer") or "0"))):
                        ed["props"] = _merge_props_with_block_hierarchy(ed.get("props"), hierarchy_path)
                        if sub_mark:
                            ed["props"] = _props_mark_from_dimension(ed.get("props"))
                        if temp_key is not None:
                            ed["_temp_insert_key"] = temp_key
                        entities.append(ed)
                        count += 1
            else:
                ed = _virtual_entity_to_dict(v, doc=doc, insert_color_fallback=insert_color, insert_layer=insert_layer)
                if ed and (not skip_off_layers or _layer_is_on(doc, (ed.get("layer") or "0"))):
                    ed["props"] = _merge_props_with_block_hierarchy(ed.get("props"), hierarchy_path)
                    if mark_from_dimension:
                        ed["props"] = _props_mark_from_dimension(ed.get("props"))
                    if temp_key is not None:
                        ed["_temp_insert_key"] = temp_key
                    entities.append(ed)
                    count += 1
    except Exception as e:
        logger.warning("INSERT virtual_entities failed for %s (depth=%s): %s", getattr(insert_entity.dxf, "name", ""), _depth, e)
    return count


def parse_dxf_stats(dxf_bytes: bytes) -> dict:
    """?붾쾭源? DXF ?뚯씪 ???덉씠?꾩썐蹂꽷룻??낅퀎 ?뷀떚??媛쒖닔留?諛섑솚 (?뚯떛 ?놁쓬)."""
    from collections import Counter
    doc = _load_dxf_document(dxf_bytes)
    layout_stats = []
    for layout in doc.layouts:
        type_counts = Counter(e.dxftype() for e in layout)
        layout_stats.append({"name": layout.name, "total": sum(type_counts.values()), "by_type": dict(type_counts)})
    return {
        "layout_names": [l["name"] for l in layout_stats],
        "layouts": layout_stats,
        "block_def_count": sum(1 for b in doc.blocks if not b.name.startswith("*")),
    }


def _load_dxf_document(dxf_bytes: bytes):
    """DXF 諛붿씠?몄뿉??ezdxf Document 濡쒕뱶. Binary DXF / ASCII DXF ?먮룞 ?먮퀎.
    ASCII DXF???꾩떆 ?뚯씪濡?????readfile() ?ъ슜 ???몄퐫???먮룞 媛먯?(ANSI_949 ??.
    """
    import tempfile
    import os
    from ezdxf.lldxf.const import DXFStructureError

    BINARY_DXF_SENTINEL = b"AutoCAD Binary DXF\r\n\x1a\x00"
    if len(dxf_bytes) >= 22 and dxf_bytes[:22] == BINARY_DXF_SENTINEL:
        from ezdxf.lldxf.tagger import binary_tags_loader
        from ezdxf.document import Drawing
        loader = binary_tags_loader(dxf_bytes, errors="surrogateescape")
        return Drawing.load(loader)

    # ASCII DXF: ?꾩떆 ?뚯씪濡??????readfile() ?ъ슜. ODA媛 ANSI_949濡??대낫?대㈃ UTF-8濡??쎌쑝硫?源⑥쭚 ??CP949 ?ъ떆??
    tmp = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".dxf", delete=False) as f:
            tmp = f.name
            f.write(dxf_bytes)
        for encoding_try in (None, "cp949"):
            try:
                if encoding_try is None:
                    doc = ezdxf.readfile(tmp, errors="surrogateescape")
                else:
                    with open(tmp, "rt", encoding=encoding_try, errors="surrogateescape") as fp:
                        doc = ezdxf.read(fp)
                # ??⑸웾 DXF?몃뜲 紐⑤뜽?ㅽ럹?댁뒪媛 鍮꾩뼱?덉쑝硫??몄퐫??臾몄젣 媛????CP949 ?ъ떆??
                if encoding_try is None and len(dxf_bytes) > 100000:
                    if len(doc.modelspace()) == 0:
                        raise DXFStructureError("empty layouts, retry with cp949")
                return doc
            except DXFStructureError as e:
                if "retry with cp949" in str(e) and encoding_try != "cp949":
                    logger.info("DXF has 0 entities with default encoding, retrying with cp949")
                    continue
                if encoding_try == "cp949":
                    raise
                logger.warning("Normal DXF load failed, trying ezdxf.recover.readfile...")
                from ezdxf.recover import readfile as recover_readfile
                doc, _auditor = recover_readfile(tmp, errors="surrogateescape")
                return doc
    finally:
        if tmp and os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def parse_dxf(
    dxf_bytes: bytes,
    settings: dict | None = None,
) -> tuple[list[dict], list[dict], list[dict], list[dict], dict[str, int]]:
    """
    DXF 諛붿씠???뚯떛.
    諛섑솚: (entities, block_defs, block_inserts, block_attrs, layer_colors)
    媛???ぉ? DB???ｌ쓣 ???ъ슜??dict (geom? WKT 臾몄옄?대줈, ?섏쨷??WKTElement濡?蹂??.
    layer_colors: ?덉씠?대챸 -> ACI/packed ?됱긽 (ByLayer ?뷀떚???쒖떆??
    """
    tol = get_tolerance_from_settings(settings)
    skip_off_layers = bool((settings or {}).get("skip_off_layers"))
    doc = _load_dxf_document(dxf_bytes)

    layer_colors = _extract_layer_colors(doc)
    raw_colors = _extract_layer_colors_from_raw(dxf_bytes)
    for layer_name, raw_val in raw_colors.items():
        if raw_val is not None and layer_colors.get(layer_name) == 7:
            layer_colors[layer_name] = raw_val
    render_ctx = _get_render_context(doc)
    entities: list[dict] = []
    block_defs: list[dict] = []
    block_inserts: list[dict] = []
    block_attrs: list[dict] = []

    # ?덉씠?꾩썐 INSERT?먯꽌 李몄“?섎뒗 釉붾줉紐??섏쭛 + 以묒꺽 釉붾줉源뚯? ?먯뇙 ??李몄“?섎뒗 釉붾줉留?_block_to_geom_items 怨꾩궛
    def _layout_insert_block_names(doc):
        names = set()
        for layout in doc.layouts:
            for entity in layout:
                if entity.dxftype() == "INSERT":
                    n = getattr(entity.dxf, "name", "") or ""
                    if n:
                        names.add(n)
        return names

    def _transitive_block_refs(doc, initial: set):
        """BFS: ?덉씠?꾩썐 INSERT?먯꽌 李몄“?섎뒗 釉붾줉 + 洹?釉붾줉??李몄“?섎뒗 釉붾줉??媛?釉붾줉 1?뚮쭔 ?쒗쉶?섎ŉ ?섏쭛."""
        blocks = getattr(doc, "blocks", None)
        if not blocks:
            return set(initial)
        seen = set(initial)
        queue = list(initial)
        while queue:
            name = queue.pop()
            block = blocks.get(name)
            if not block:
                continue
            for ent in block:
                if ent.dxftype() == "INSERT":
                    n = getattr(ent.dxf, "name", "") or ""
                    if n and n not in seen:
                        seen.add(n)
                        queue.append(n)
        return seen

    referenced_blocks = _layout_insert_block_names(doc)
    referenced_blocks = _transitive_block_refs(doc, referenced_blocks)
    # ?듬챸 釉붾줉(*U...)??INSERT?먯꽌 李몄“?섎㈃ doc.blocks fallback?쇰줈 泥섎━?섎?濡??쒖쇅?섏? ?딆쓬

    # 釉붾줉 ?뺤쓽 ?섏쭛: 李몄“?섎뒗 釉붾줉留?怨꾩궛 (?대쫫 以묐났 ?쒓굅, * ?듬챸 釉붾줉 ?쒖쇅)
    seen_def_names: set[str] = set()
    for block in doc.blocks:
        if block.name.startswith("*"):  # *MODEL_SPACE ???쒖쇅, *U... ?듬챸 釉붾줉? ?ы븿
            if not block.name.startswith("*U"):
                continue
        if block.name not in referenced_blocks:
            continue
        if block.name in seen_def_names:
            continue
        seen_def_names.add(block.name)
        base = block.base_point if hasattr(block, "base_point") else (0, 0, 0)
        base_pt = (float(base[0]), float(base[1]), float(base[2]) if len(base) > 2 else 0.0)
        block_entities = _block_to_geom_items(block, doc, skip_off_layers=skip_off_layers)
        block_defs.append({
            "name": block.name,
            "base_point_wkt": point_wkt(base_pt[0], base_pt[1], base_pt[2]),
            "base_x": base_pt[0],
            "base_y": base_pt[1],
            "props": {"entities": block_entities},
        })

    # INSERT 泥섎━ ???ш퀎?걔톎oc.blocks 議고쉶 諛⑹?: block_name -> entity_items(釉붾줉 濡쒖뺄 醫뚰몴)
    insert_temp_key_counter = [0]
    skip_types: dict[str, int] = {}

    def next_insert_temp_key():
        insert_temp_key_counter[0] += 1
        return insert_temp_key_counter[0]

    # WKT ?뚯떛 罹먯떆: ?숈씪 geom_wkt 諛섎났 ?뚯떛 諛⑹? (媛숈? 釉붾줉 ?ㅼ닔 INSERT ???④낵)
    wkt_parse_cache: dict[str, list[tuple[float, ...]]] = {}
    # INSERT matrix expansion cache: (block_name, insert_layer, insert_color) -> local geom items
    block_local_items_cache: dict[tuple[str, str, int], list[dict]] = {}

    # 紐⑤뜽?ㅽ럹?댁뒪 + 紐⑤뱺 ?섏씠?쇱뒪?섏씠???덉씠?꾩썐?먯꽌 ?뷀떚???섏쭛 (?쇰? DWG???덉씠?꾩썐?먮쭔 ?덉쓬)
    insert_count = 0
    virtual_entities_count = 0

    def iter_entities():
        for layout in doc.layouts:
            for entity in layout:
                yield entity

    for entity in iter_entities():
        if _is_invisible(entity):
            continue
        dxftype = entity.dxftype()
        # 모델/레이아웃 공간의 치수는 DIMENSION 엔티티로만 있고 LINE+TEXT로 풀리지 않은 경우가 많다.
        # INSERT 안 치수는 virtual_entities()로 이미 전개되지만, 루트 DIMENSION은 여기서 전개해야 한다.
        if dxftype == "DIMENSION":
            layer = _layer(entity)
            if _skip_entity_for_off_layer(doc, layer, None, skip_off_layers):
                continue
            meta = _color_meta(entity, doc=doc, layout=None, render_ctx=render_ctx)
            color = meta["color"]
            linetype = _linetype(entity)
            props = _attach_lineweight(
                entity,
                {**_entity_props(entity), "color_raw": meta["color_raw"], "color_bylayer": meta["color_bylayer"]},
            )
            _ensure_dimension_text_midpoint(entity)
            try:
                nested = list(entity.virtual_entities())
            except Exception as e:
                logger.debug("DIMENSION virtual_entities failed: %s", e)
                nested = []
            added_here = 0
            for v in nested:
                if _is_invisible(v):
                    continue
                if getattr(v, "dxftype", None) and v.dxftype() == "INSERT":
                    n = _explode_insert_into_entities(
                        v,
                        entities,
                        doc=doc,
                        temp_key=None,
                        block_hierarchy_path=None,
                        _depth=0,
                        skip_off_layers=skip_off_layers,
                        mark_from_dimension=True,
                    )
                    added_here += n
                    virtual_entities_count += n
                    continue
                ed = _virtual_entity_to_dict(
                    v,
                    doc=doc,
                    insert_color_fallback=None,
                    insert_layer=None,
                )
                if not ed or not ed.get("geom_wkt"):
                    continue
                dim_props = _props_mark_from_dimension(ed.get("props"))
                entities.append(
                    {
                        "entity_type": str(ed.get("entity_type") or "LINE").upper(),
                        "layer": ed.get("layer") or layer,
                        "color": ed.get("color"),
                        "linetype": ed.get("linetype"),
                        "geom_wkt": ed["geom_wkt"],
                        "centroid_wkt": ed.get("centroid_wkt"),
                        "bbox_wkt": ed.get("bbox_wkt"),
                        "props": dim_props,
                        "fingerprint": ed.get("fingerprint"),
                    }
                )
                added_here += 1
                virtual_entities_count += 1
            if added_here == 0:
                for fb in _dimension_fallback_entity_dicts(entity, layer, color, linetype, props, tol):
                    if skip_off_layers and not _layer_is_on(doc, fb.get("layer") or layer):
                        continue
                    entities.append(fb)
                    virtual_entities_count += 1
            continue

        if dxftype not in SUPPORTED_ENTITY_TYPES:
            skip_types[dxftype] = skip_types.get(dxftype, 0) + 1
            continue
        layer = _layer(entity)
        if _skip_entity_for_off_layer(doc, layer, None, skip_off_layers):
            continue
        meta = _color_meta(entity, doc=doc, layout=None, render_ctx=render_ctx)
        color = meta["color"]
        linetype = _linetype(entity)
        props = _attach_lineweight(
            entity,
            {**_entity_props(entity), "color_raw": meta["color_raw"], "color_bylayer": meta["color_bylayer"]},
        )

        if dxftype == "LINE":
            p1 = _point_3d(entity, "start")
            p2 = _point_3d(entity, "end")
            geom_wkt = linestring_wkt([p1, p2])
            pts = [p1, p2]
            centroid_wkt = point_wkt((p1[0] + p2[0]) / 2, (p1[1] + p2[1]) / 2, (p1[2] + p2[2]) / 2)
            bbox_wkt = bbox_from_points(pts)
            fp = fingerprint_line(p1, p2, "LINE", layer, color, linetype, tol)
            entities.append({
                "entity_type": "LINE",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": props,
                "fingerprint": fp,
            })

        elif dxftype == "LWPOLYLINE":
            points = _lwpolyline_points_world(entity)
            if len(points) < 2:
                continue
            closed = _is_polyline_closed(entity)
            if closed and points and not _points_close(points[0], points[-1], 1e-5):
                points = list(points) + [points[0]]
            geom_wkt = linestring_wkt(points)
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
            bbox_wkt = bbox_from_points(points)
            fp = fingerprint_polyline(points, closed, "LWPOLYLINE", layer, color, linetype, tol)
            entities.append({
                "entity_type": "LWPOLYLINE",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": props,
                "fingerprint": fp,
            })

        elif dxftype == "POLYLINE":
            if getattr(entity, "is_poly_face_mesh", False) or getattr(entity, "is_polygon_mesh", False):
                try:
                    n_faces = 0
                    for vf in virtual_polyline_entities(entity):
                        if vf.dxftype() != "3DFACE":
                            continue
                        geo = _geometry_from_linearized_entity(vf, "3DFACE")
                        if not geo:
                            continue
                        fpts = geo["points"]
                        fclosed = bool(geo["closed"])
                        fp = fingerprint_polyline(fpts, fclosed, "3DFACE", layer, color, linetype, tol)
                        entities.append({
                            "entity_type": "3DFACE",
                            "layer": layer,
                            "color": color,
                            "linetype": linetype,
                            "geom_wkt": geo["geom_wkt"],
                            "centroid_wkt": geo["centroid_wkt"],
                            "bbox_wkt": geo["bbox_wkt"],
                            "props": props,
                            "fingerprint": fp,
                        })
                        n_faces += 1
                    if n_faces:
                        continue
                except Exception as e:
                    logger.debug("POLYLINE mesh to 3DFACE failed: %s", e)
            points = _polyline_points_world(entity)
            if len(points) < 2:
                continue
            closed = _is_polyline_closed(entity)
            if closed and points and not _points_close(points[0], points[-1], 1e-5):
                points = list(points) + [points[0]]
            geom_wkt = linestring_wkt(points)
            xs, ys = [p[0] for p in points], [p[1] for p in points]
            centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
            bbox_wkt = bbox_from_points(points)
            fp = fingerprint_polyline(points, closed, "POLYLINE", layer, color, linetype, tol)
            entities.append({
                "entity_type": "POLYLINE",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": props,
                "fingerprint": fp,
            })

        elif dxftype == "ARC":
            center = _point_3d(entity, "center")
            radius = float(entity.dxf.radius)
            start_angle = float(entity.dxf.start_angle)
            end_angle = float(entity.dxf.end_angle)
            points = _arc_points_from_angles(center, radius, start_angle, end_angle)
            if len(points) < 2:
                continue
            geom_wkt = linestring_wkt(points)
            centroid_wkt = point_wkt(center[0], center[1], center[2])
            bbox_wkt = bbox_from_points(points)
            fp = fingerprint_arc(center, radius, start_angle, end_angle, "ARC", layer, color, linetype, tol)
            entities.append({
                "entity_type": "ARC",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": {**props, "radius": radius, "start_angle": start_angle, "end_angle": end_angle},
                "fingerprint": fp,
            })

        elif dxftype == "CIRCLE":
            center = _point_3d(entity, "center")
            radius = float(entity.dxf.radius)
            import math
            n = 32
            points = []
            for i in range(n + 1):
                rad = 2 * math.pi * i / n
                x = center[0] + radius * math.cos(rad)
                y = center[1] + radius * math.sin(rad)
                points.append((x, y, center[2]))
            geom_wkt = linestring_wkt(points)
            centroid_wkt = point_wkt(center[0], center[1], center[2])
            bbox_wkt = bbox_from_points(points)
            fp = fingerprint_circle(center, radius, "CIRCLE", layer, color, linetype, tol)
            entities.append({
                "entity_type": "CIRCLE",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": props,
                "fingerprint": fp,
            })

        elif dxftype in ("ELLIPSE", "SPLINE", "3DFACE", "SOLID"):
            geo = _geometry_from_linearized_entity(entity, dxftype)
            if not geo:
                continue
            points = geo["points"]
            closed = bool(geo["closed"])
            fp = fingerprint_polyline(points, closed, dxftype, layer, color, linetype, tol)
            entities.append({
                "entity_type": dxftype,
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geo["geom_wkt"],
                "centroid_wkt": geo["centroid_wkt"],
                "bbox_wkt": geo["bbox_wkt"],
                "props": props,
                "fingerprint": fp,
            })

        elif dxftype == "TEXT":
            pos = _point_3d(entity, "insert")
            anchor, align_props = _extract_text_alignment(entity, "TEXT", pos)
            text = _entity_plain_text(entity, "TEXT")
            geom_wkt = point_wkt(anchor[0], anchor[1], anchor[2])
            centroid_wkt = geom_wkt
            bbox_wkt = None
            text_props = {**props, **align_props, "text": text}
            h = entity.dxf.get("height")
            if h is not None:
                text_props["height"] = float(h)
            fp = fingerprint_text(anchor, text, "TEXT", layer, color, linetype, tol)
            entities.append({
                "entity_type": "TEXT",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": text_props,
                "fingerprint": fp,
            })

        elif dxftype == "MTEXT":
            pos = _point_3d(entity, "insert")
            anchor, align_props = _extract_text_alignment(entity, "MTEXT", pos)
            text = _entity_plain_text(entity, "MTEXT")
            geom_wkt = point_wkt(anchor[0], anchor[1], anchor[2])
            centroid_wkt = geom_wkt
            bbox_wkt = None
            text_props = {**props, **align_props, "text": text}
            ch = entity.dxf.get("char_height")
            if ch is not None:
                text_props["char_height"] = float(ch)
            else:
                h = entity.dxf.get("height")
                if h is not None:
                    text_props["height"] = float(h)
            fp = fingerprint_text(anchor, text, "MTEXT", layer, color, linetype, tol)
            entities.append({
                "entity_type": "MTEXT",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": text_props,
                "fingerprint": fp,
            })

        elif dxftype == "POINT":
            pos = _point_3d(entity, "location")
            geom_wkt = point_wkt(pos[0], pos[1], pos[2])
            centroid_wkt = geom_wkt
            bbox_wkt = None
            fp = fingerprint_text(pos, "", "POINT", layer, color, linetype, tol)
            entities.append({
                "entity_type": "POINT",
                "layer": layer,
                "color": color,
                "linetype": linetype,
                "geom_wkt": geom_wkt,
                "centroid_wkt": centroid_wkt,
                "bbox_wkt": bbox_wkt,
                "props": props,
                "fingerprint": fp,
            })

        elif dxftype == "HATCH":
            item = _hatch_to_geom_item(entity, doc=doc, layer_name=layer)
            if item:
                geom_wkt = item["geom_wkt"]
                points = wkt_points_to_list(geom_wkt)
                centroid_wkt = None
                bbox_wkt = None
                if points:
                    xs, ys = [p[0] for p in points], [p[1] for p in points]
                    centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
                    bbox_wkt = bbox_from_points(points)
                hatch_color = item.get("color", color)
                fp = fingerprint_polyline(points, True, "HATCH", layer, hatch_color, linetype, tol) if points else None
                entities.append({
                    "entity_type": "HATCH",
                    "layer": layer,
                    "color": hatch_color,
                    "linetype": linetype,
                    "geom_wkt": geom_wkt,
                    "centroid_wkt": centroid_wkt,
                    "bbox_wkt": bbox_wkt,
                    "props": {**props, **(item.get("props") or {})},
                    "fingerprint": fp,
                })

        elif dxftype == "WIPEOUT":
            item = _wipeout_to_geom_item(entity, doc=doc, layer_name=layer)
            if item:
                geom_wkt = item["geom_wkt"]
                points = wkt_points_to_list(geom_wkt)
                centroid_wkt = None
                bbox_wkt = None
                if points:
                    xs, ys = [p[0] for p in points], [p[1] for p in points]
                    centroid_wkt = point_wkt(sum(xs) / len(xs), sum(ys) / len(ys), 0.0)
                    bbox_wkt = bbox_from_points(points)
                wipeout_color = item.get("color", color)
                fp = fingerprint_polyline(points, True, "WIPEOUT", layer, wipeout_color, linetype, tol) if points else None
                entities.append({
                    "entity_type": "WIPEOUT",
                    "layer": layer,
                    "color": wipeout_color,
                    "linetype": linetype,
                    "geom_wkt": geom_wkt,
                    "centroid_wkt": centroid_wkt,
                    "bbox_wkt": bbox_wkt,
                    "props": {**props, **(item.get("props") or {})},
                    "fingerprint": fp,
                })
        elif dxftype == "INSERT":
            insert_count += 1
            block_name = entity.dxf.name
            insert_pt = _point_3d(entity, "insert")
            rotation = float(getattr(entity.dxf, "rotation", 0) or 0)
            scale_x = float(getattr(entity.dxf, "xscale", 1) or 1)
            scale_y = float(getattr(entity.dxf, "yscale", 1) or 1)
            scale_z = float(getattr(entity.dxf, "zscale", 1) or 1)
            temp_key = next_insert_temp_key()
            virtual_entities_count += 1
            added = 0
            matrix_added = 0

            block = None
            try:
                if getattr(doc, "blocks", None):
                    block = doc.blocks.get(block_name)
            except Exception:
                block = None

            if block is not None:
                try:
                    insert_color = resolve_color_to_aci(entity, doc=doc, layout=None, render_ctx=render_ctx)
                    try:
                        insert_color_key = int(insert_color)
                    except Exception:
                        insert_color_key = 7
                    cache_key = (str(block_name or ""), str(layer or "0"), insert_color_key)
                    local_items = block_local_items_cache.get(cache_key)
                    if local_items is None:
                        local_items = _block_to_geom_items(
                            block,
                            doc,
                            _insert_layer=cache_key[1],
                            _insert_color_fallback=cache_key[2],
                            skip_off_layers=skip_off_layers,
                        )
                        block_local_items_cache[cache_key] = local_items

                    if local_items:
                        for instance_insert, _row, _col in _iter_insert_instances(entity):
                            try:
                                matrix = instance_insert.matrix44()
                            except Exception:
                                matrix = None
                            for item in local_items:
                                twkt = transform_wkt_with_matrix(
                                    item.get("geom_wkt"),
                                    matrix=matrix,
                                    _wkt_cache=wkt_parse_cache,
                                )
                                if not twkt:
                                    continue
                                out_props = _transform_text_props_with_matrix(item.get("props"), matrix)
                                out_item = {
                                    **item,
                                    "geom_wkt": twkt,
                                    "props": out_props,
                                }
                                out_ent = _geom_item_to_entity_dict(out_item, linetype=linetype)
                                if not out_ent:
                                    continue
                                out_ent["_temp_insert_key"] = temp_key
                                entities.append(out_ent)
                                matrix_added += 1
                except Exception as e:
                    logger.warning("INSERT %s matrix expansion failed: %s", block_name, e)

            added += matrix_added
            if added == 0:
                try:
                    for virtual_insert, _row, _col in _iter_insert_instances(entity):
                        added += _explode_insert_into_entities(
                            virtual_insert,
                            entities,
                            doc=doc,
                            temp_key=temp_key,
                            skip_off_layers=skip_off_layers,
                        )
                except Exception as e:
                    logger.warning("INSERT %s explode failed: %s", block_name, e)

            if added == 0 and len(block_inserts) < 2:
                logger.warning("INSERT %s: expansion added 0 entities (nested or empty?)", block_name)
            attrs = []
            for attrib in entity.attribs:
                tag = getattr(attrib.dxf, "tag", "") or ""
                value = getattr(attrib.dxf, "value", "") or ""
                attrs.append((tag, str(value)))
            fp = fingerprint_block_insert(
                block_name, insert_pt, rotation, scale_x, scale_y, scale_z,
                layer, color, attrs, tol,
            )
            insert_point_wkt = point_wkt(insert_pt[0], insert_pt[1], insert_pt[2])
            block_inserts.append({
                "block_name": block_name,
                "layer": layer,
                "color": color,
                "insert_point_wkt": insert_point_wkt,
                "rotation": rotation,
                "scale_x": scale_x,
                "scale_y": scale_y,
                "scale_z": scale_z,
                "transform": {},
                "props": props,
                "fingerprint": fp,
                "_temp_insert_key": temp_key,
            })
            for a in attrs:
                block_attrs.append({
                    "_temp_insert_key": temp_key,
                    "tag": a[0],
                    "value": a[1],
                    "props": {},
                })

    hatch_count = sum(1 for e in entities if e.get("entity_type") == "HATCH")
    logger.info(
        "parse_dxf entities_out=%s hatch_count=%s insert_count=%s virtual_entities_count=%s",
        len(entities), hatch_count, insert_count, virtual_entities_count,
    )

    # INSERT?먯꽌 李몄“??釉붾줉紐?以?block_def???녿뒗 寃껋? placeholder ?뺤쓽 異붽? (酉곗뼱/DB ?곕룞??
    for bi in block_inserts:
        name = bi.get("block_name") or ""
        if name and name not in seen_def_names:
            seen_def_names.add(name)
            block_defs.append({
                "name": name,
                "base_point_wkt": point_wkt(0.0, 0.0, 0.0),
                "base_x": 0.0,
                "base_y": 0.0,
                "props": {"entities": []},
            })

    logger.info(
        "parse_dxf: entities=%s, block_inserts=%s, block_defs=%s, skipped_types=%s",
        len(entities),
        len(block_inserts),
        len(block_defs),
        dict(skip_types) if skip_types else None,
    )
    return entities, block_defs, block_inserts, block_attrs, layer_colors

