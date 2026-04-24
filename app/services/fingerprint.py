"""
엔티티/블록 삽입의 fingerprint 생성.
정규화된 지오메트리 + 주요 속성(타입/레이어/색상/linetype) 기반 해시.
fp_tolerance_mm(기본 1.0)로 좌표 라운딩.
"""
import hashlib
import json
import logging
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_TOLERANCE_MM = 1.0


def _round_val(v: float, tolerance: float) -> float:
    if tolerance <= 0:
        return v
    return round(v / tolerance) * tolerance


def _round_point(x: float, y: float, z: float, tol: float) -> tuple[float, float, float]:
    return (_round_val(x, tol), _round_val(y, tol), _round_val(z, tol))


def _hash_str(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()[:32]


def fingerprint_line(p1: tuple[float, float, float], p2: tuple[float, float, float],
                     entity_type: str, layer: str | None, color: int | None, linetype: str | None,
                     tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> str:
    """LINE: (p1,p2) 정렬(작은 점이 앞) 후 라운딩."""
    tol = tolerance_mm
    a = _round_point(p1[0], p1[1], p1[2] if len(p1) > 2 else 0.0, tol)
    b = _round_point(p2[0], p2[1], p2[2] if len(p2) > 2 else 0.0, tol)
    if a > b:
        a, b = b, a
    payload = f"LINE|{a}|{b}|{layer or ''}|{color or 0}|{linetype or ''}"
    return _hash_str(payload)


def fingerprint_polyline(points: list[tuple[float, float, float]], closed: bool,
                          entity_type: str, layer: str | None, color: int | None, linetype: str | None,
                          tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> str:
    """폴리라인: 모든 버텍스 라운딩. 방향성은 MVP에서 유지(순서 정규화 생략 가능)."""
    tol = tolerance_mm
    rounded = [_round_point(p[0], p[1], p[2] if len(p) > 2 else 0.0, tol) for p in points]
    payload = f"{entity_type}|{closed}|{rounded}|{layer or ''}|{color or 0}|{linetype or ''}"
    return _hash_str(payload)


def fingerprint_arc(center: tuple[float, float, float], radius: float, start_angle: float, end_angle: float,
                    entity_type: str, layer: str | None, color: int | None, linetype: str | None,
                    tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> str:
    c = _round_point(center[0], center[1], center[2] if len(center) > 2 else 0.0, tolerance_mm)
    r = _round_val(radius, tolerance_mm)
    sa = _round_val(start_angle, 0.01)
    ea = _round_val(end_angle, 0.01)
    payload = f"{entity_type}|{c}|{r}|{sa}|{ea}|{layer or ''}|{color or 0}|{linetype or ''}"
    return _hash_str(payload)


def fingerprint_circle(center: tuple[float, float, float], radius: float,
                       entity_type: str, layer: str | None, color: int | None, linetype: str | None,
                       tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> str:
    c = _round_point(center[0], center[1], center[2] if len(center) > 2 else 0.0, tolerance_mm)
    r = _round_val(radius, tolerance_mm)
    payload = f"CIRCLE|{c}|{r}|{layer or ''}|{color or 0}|{linetype or ''}"
    return _hash_str(payload)


def fingerprint_text(position: tuple[float, float, float], text: str,
                     entity_type: str, layer: str | None, color: int | None, linetype: str | None,
                     tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> str:
    pos = _round_point(position[0], position[1], position[2] if len(position) > 2 else 0.0, tolerance_mm)
    payload = f"{entity_type}|{pos}|{text}|{layer or ''}|{color or 0}|{linetype or ''}"
    return _hash_str(payload)


def fingerprint_block_insert(block_name: str, insert_point: tuple[float, float, float],
                             rotation: float, scale_x: float, scale_y: float, scale_z: float,
                             layer: str | None, color: int | None,
                             attr_summary: list[tuple[str, str]] | None,
                             tolerance_mm: float = DEFAULT_TOLERANCE_MM) -> str:
    """block_name + insert_point + rotation + scale + layer/color + attribute 요약."""
    tol = tolerance_mm
    pt = _round_point(insert_point[0], insert_point[1], insert_point[2] if len(insert_point) > 2 else 0.0, tol)
    rot = _round_val(rotation, 0.01)
    sx, sy, sz = _round_val(scale_x, 0.01), _round_val(scale_y, 0.01), _round_val(scale_z, 0.01)
    attrs = sorted(attr_summary or [], key=lambda x: x[0])
    payload = f"INSERT|{block_name}|{pt}|{rot}|{sx}|{sy}|{sz}|{layer or ''}|{color or 0}|{attrs}"
    return _hash_str(payload)


def get_tolerance_from_settings(settings: dict | None) -> float:
    if not settings:
        return DEFAULT_TOLERANCE_MM
    return float(settings.get("fp_tolerance_mm", DEFAULT_TOLERANCE_MM))
