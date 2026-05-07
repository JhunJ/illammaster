"""
보 flat 단면 하단 띠 텍스트(크기·상·하부근·스트럽·표피) — 프론트 `beamFlat*` 규칙과 동일한 키 구조로 서버 행에 채운다.
도면 미리보기 라벨 없이 `_flat_sorted_entities`(추출 행 텍스트)만 사용한다.
"""
from __future__ import annotations

import re
from typing import Any

# 프론트 `below_text_role_values` 와 동일한 한글 키
ROLE_KEYS_KO = ("크기", "상부근", "하부근", "스트럽", "표피철근")

_RE_MARK_DIM_PAREN = re.compile(r"\((\d{2,5})[xX×](\d{2,5})\)")


def beam_flat_parse_member_mark_dims(text: str) -> dict[str, int] | None:
    """RG11C(1000x900) → {width_mm, depth_mm}"""
    s = _RE_COMPACT_WS.sub("", text or "")
    if not s:
        return None
    m = _RE_MARK_DIM_PAREN.search(s)
    if not m:
        return None
    try:
        w, h = int(m.group(1)), int(m.group(2))
    except ValueError:
        return None
    return {"width_mm": w, "depth_mm": h}

_RE_COMPACT_WS = re.compile(r"\s+")
_RE_BXH = re.compile(r"\d{2,5}\s*[x×X]\s*\d{2,5}")
_RE_REBAR_VAL = re.compile(
    r"\d+\s*[-/]\s*(?:U?HD|SHD|D)\s*\d+|\d+\s*-\s*\d+\s*-\s*(?:U?HD|SHD|D)\s*\d+",
    re.I,
)


def beam_flat_below_text_role(t: str) -> str:
    """내부 역할 키: size | top | bot | stirrup | skin | ''"""
    s = _RE_COMPACT_WS.sub("", t or "")
    if not s:
        return ""
    if "크기" in s or "규격" in s:
        return "size"
    if "상부근" in s or "상단근" in s:
        return "top"
    if "하부근" in s or "하단근" in s:
        return "bot"
    if "스트럽" in s or "스터럽" in s or "띠근" in s:
        return "stirrup"
    if "표피철근" in s or "복근" in s or "측면근" in s:
        return "skin"
    return ""


def beam_flat_looks_like_dimension_text(t: str) -> bool:
    s = _RE_COMPACT_WS.sub(" ", (t or "").strip())
    if not s:
        return False
    if re.search(r"(U?HD|SHD|D)\s*\d+", s, re.I):
        return False
    if re.fullmatch(r"\d+(\.\d+)?", s):
        return True
    if not re.search(r"[A-Za-z가-힣]", s):
        return True
    if "±" in s:
        return True
    if re.search(r"\bR\d+(\.\d+)?\b", s, re.I):
        return True
    if re.search(r"[ØΦφ]", s):
        return True
    if _RE_BXH.search(s) and not _RE_REBAR_VAL.search(s):
        return True
    if re.fullmatch(r"\d+\s*-\s*\d+", s):
        return True
    if re.search(r"\b(mm|cm|m)\b", s, re.I):
        return True
    return False


def beam_flat_looks_like_rebar_value_text(t: str) -> bool:
    s = _RE_COMPACT_WS.sub("", (t or "").upper())
    if not s:
        return False
    if beam_flat_looks_like_dimension_text(t):
        return False
    if re.search(r"(FCK|FY|MPA|CONC|콘크리트|강도)", t, re.I):
        return False
    if _RE_REBAR_VAL.search(s):
        return True
    if re.search(r"(?:U?HD|SHD|D)\s*\d+", s) and re.search(r"\d", s):
        return True
    return False


def beam_flat_is_section_bxh_size_text(t: str) -> bool:
    s = _RE_COMPACT_WS.sub(" ", (t or "").strip())
    if not s or len(s) > 52:
        return False
    if beam_flat_looks_like_rebar_value_text(s):
        return False
    core = re.sub(r"\b(mm|cm|m)\b", "", s, flags=re.I).strip()
    return bool(_RE_BXH.search(core))


def _role_to_ko(role: str) -> str | None:
    return {
        "size": "크기",
        "top": "상부근",
        "bot": "하부근",
        "stirrup": "스트럽",
        "skin": "표피철근",
    }.get(role)


def _collect_below_hits(
    entities: list[dict[str, Any]],
    sec_cx: float,
    sec_cy: float,
    *,
    max_dx: float = 2400.0,
    max_dy: float = 3600.0,
) -> tuple[list[dict[str, Any]], str]:
    """primary: ty < refY (작은 y가 아래). 비면 큰 y 방향 시도."""

    def collect(is_down_small_y: bool) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        ref_y = sec_cy
        ref_x = sec_cx
        for e in entities:
            try:
                tx = float(e["x"])
                ty = float(e["y"])
            except (TypeError, KeyError, ValueError):
                continue
            if is_down_small_y:
                if ty >= ref_y:
                    continue
            else:
                if ty <= ref_y:
                    continue
            dx = abs(tx - ref_x)
            dy = abs(ref_y - ty)
            if dx > max_dx or dy > max_dy:
                continue
            raw = str(e.get("text") or "").replace("\n", " ")
            raw = _RE_COMPACT_WS.sub(" ", raw).strip()
            if not raw:
                continue
            t = raw[:80] + ("…" if len(raw) > 80 else "")
            out.append(
                {
                    "x": tx,
                    "y": ty,
                    "text": t,
                    "score": dy + dx * 0.16,
                    "dx": dx,
                    "dy": dy,
                }
            )
        out.sort(key=lambda h: h["score"])
        return out

    primary = collect(True)
    if primary:
        return primary, "smallY"
    alt = collect(False)
    return alt, "largeY"


def _split_label_value_suffix(s: str) -> tuple[str | None, str]:
    """'상부근 4-D16' / '크기 400 X 600' 형태에서 라벨과 값 꼬리."""
    t = s.strip()
    role = beam_flat_below_text_role(t)
    if not role:
        return None, t
    ko = _role_to_ko(role)
    if not ko:
        return None, t
    idx = t.find(ko)
    if idx < 0:
        return None, t
    tail = t[idx + len(ko) :].strip(" \t:：·.-")
    return role, tail


def beam_flat_fill_below_text_role_values_from_entities(
    entities: list[dict[str, Any]],
    section_geometry: dict[str, Any] | None,
    *,
    slab_x_lo: float | None = None,
    slab_x_hi: float | None = None,
) -> dict[str, Any]:
    """
    Returns:
      below_text_role_values: dict[str, str]  (한글 키)
      below_text_chain_values: list[str] (상·하·스트럽·표피 연쇄 후보)
      below_text_hit_count, below_text_match_count, below_text_dir
    """
    out: dict[str, Any] = {
        "below_text_role_values": {},
        "below_text_chain_values": [],
        "below_text_hit_count": 0,
        "below_text_match_count": 0,
        "below_text_dir": "",
    }
    sg = section_geometry if isinstance(section_geometry, dict) else {}
    bb = sg.get("search_bbox")
    if not isinstance(bb, (list, tuple)) or len(bb) < 4:
        bb = sg.get("search_bbox_loose")
    sec_cx = sec_cy = 0.0
    ok_center = False
    if isinstance(bb, (list, tuple)) and len(bb) >= 4:
        try:
            x0, y0, x1, y1 = float(bb[0]), float(bb[1]), float(bb[2]), float(bb[3])
            sec_cx = 0.5 * (x0 + x1)
            sec_cy = 0.5 * (y0 + y1)
            ok_center = True
        except (TypeError, ValueError):
            pass
    if not ok_center:
        return out

    slab_filter = (
        slab_x_lo is not None
        and slab_x_hi is not None
        and float(slab_x_hi) > float(slab_x_lo) + 1.0
    )
    ent_use: list[dict[str, Any]] = []
    for e in entities or []:
        if not isinstance(e, dict):
            continue
        try:
            xe = float(e["x"])
        except (TypeError, KeyError, ValueError):
            continue
        if slab_filter and (xe < float(slab_x_lo) or xe > float(slab_x_hi)):
            continue
        ent_use.append(e)

    hits, direction = _collect_below_hits(ent_use, sec_cx, sec_cy)
    out["below_text_hit_count"] = len(hits)
    out["below_text_dir"] = "smallY" if direction == "smallY" else "largeY"

    role_vals: dict[str, str] = {}
    chain: list[str] = []

    # 1) 같은 문자열에 라벨+값
    for h in hits:
        text = str(h.get("text") or "")
        role, tail = _split_label_value_suffix(text)
        if not role:
            continue
        if role == "size":
            if beam_flat_is_section_bxh_size_text(tail or text):
                role_vals["크기"] = (tail or text).strip()
        elif role == "skin":
            if tail and not beam_flat_looks_like_dimension_text(tail):
                role_vals["표피철근"] = tail.strip()
        elif tail and beam_flat_looks_like_rebar_value_text(tail):
            ko = _role_to_ko(role)
            if ko:
                role_vals[ko] = tail.strip()

    # 2) 크기: 라벨 없이 BxH (dx = 엔티티의 section 대비 — hits에 dx 있음)
    if "크기" not in role_vals:
        best: tuple[float, str] | None = None
        for h in hits:
            t = str(h.get("text") or "")
            if not beam_flat_is_section_bxh_size_text(t):
                continue
            try:
                dx = float(h.get("dx", 0.0))
                dy = float(h.get("dy", 0.0))
            except (TypeError, ValueError):
                continue
            sc = dy * 2.4 + dx * 0.12
            if best is None or sc < best[0]:
                best = (sc, t.strip())
        if best:
            role_vals["크기"] = best[1]

    # 3) 연쇄: 상→하→스트럽→표피 순 주근값 나열
    rebar_hits = [h for h in hits if beam_flat_looks_like_rebar_value_text(str(h.get("text") or ""))]
    for h in rebar_hits[:8]:
        chain.append(str(h.get("text") or "").strip())

    out["below_text_chain_values"] = chain[:6]
    # 연쇄로 빈 슬롯 보조
    if "상부근" not in role_vals and len(chain) > 0:
        role_vals["상부근"] = chain[0]
    if "하부근" not in role_vals and len(chain) > 1:
        role_vals["하부근"] = chain[1]
    if "스트럽" not in role_vals and len(chain) > 2:
        role_vals["스트럽"] = chain[2]
    if "표피철근" not in role_vals and len(chain) > 3:
        c3 = chain[3]
        if c3 and beam_flat_looks_like_rebar_value_text(c3):
            role_vals["표피철근"] = c3

    out["below_text_role_values"] = {k: role_vals[k] for k in ROLE_KEYS_KO if role_vals.get(k)}
    out["below_text_match_count"] = len(out["below_text_role_values"])
    return out
