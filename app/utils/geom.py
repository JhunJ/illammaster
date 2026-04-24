"""WKT/Shapely <-> PostGIS GeoAlchemy 헬퍼."""
import math
import re
from typing import Any

from geoalchemy2.elements import WKTElement


def ensure_polygon_rings_closed(wkt: str | None) -> str | None:
    """POLYGON/MULTIPOLYGON WKT에서 각 링이 시점=종점이 되도록 보정. PostGIS non-closed rings 오류 방지."""
    if not wkt or not wkt.strip() or "EMPTY" in (wkt or "").upper():
        return wkt
    u = (wkt or "").strip().upper()
    if not u.startswith("POLYGON"):
        return wkt
    has_z = " Z " in u or " Z(" in u
    try:
        idx_open = wkt.find("((")
        idx_close = wkt.rfind("))")
        if idx_open < 0 or idx_close < idx_open:
            return wkt
        prefix = wkt[: idx_open]  # "POLYGON Z " or "POLYGON " or "SRID=0;POLYGON "
        inner = wkt[idx_open + 2 : idx_close].strip()
        ring_parts = re.split(r"\)\s*,\s*\(", inner)
        out_rings = []
        for part in ring_parts:
            part = part.strip().strip("()").strip()
            if not part:
                continue
            pts = []
            for p in part.split(","):
                nums = re.findall(r"[-\d.eE]+", p.strip())
                if len(nums) >= 2:
                    x, y = float(nums[0]), float(nums[1])
                    z = float(nums[2]) if len(nums) >= 3 else 0.0
                    pts.append((x, y, z) if len(nums) >= 3 else (x, y))
            if not pts:
                continue
            # PostGIS는 시점=종점을 엄격히 요구. 마지막 점을 첫 점과 동일 좌표로 덮어써 부동소수점 오차 제거.
            if len(pts) >= 2:
                pts = list(pts)
                pts[-1] = pts[0]
            ring_str = "(" + ", ".join(" ".join(str(c) for c in p) for p in pts) + ")"
            out_rings.append(ring_str)
        if not out_rings:
            return wkt
        # POLYGON ((ring)) 형식: prefix + ( + ring + ) 만 필요. suffix 붙이면 ))) 가 되어 invalid
        return prefix + "(" + ", ".join(out_rings) + ")"
    except Exception:
        return wkt


def wkt_to_2d(wkt: str | None) -> str | None:
    """WKT에서 Z 차원 제거. PostGIS 2D 컬럼용. None/EMPTY는 그대로 반환."""
    if not wkt or "EMPTY" in (wkt or "").upper():
        return wkt
    u = (wkt or "").upper()
    if "Z" not in u or (" Z " not in u and " Z(" not in u and "Z (" not in u and not re.search(r"(POINT|LINESTRING|POLYGON)Z\b", u)):
        return wkt
    # POINT Z, POINTZ, LINESTRING Z 등 제거
    s = re.sub(r"\b(LINESTRING|POINT|POLYGON)\s*Z\s*", r"\1 ", wkt, flags=re.I)
    s = re.sub(r"\b(LINESTRING|POINT|POLYGON)Z\b\s*", r"\1 ", s, flags=re.I)
    # 좌표에서 세 번째(Z) 값 제거: "x y z" -> "x y" (음수·소수 포함)
    s = re.sub(r"([-\d.eE]+)\s+([-\d.eE]+)\s+[-\d.eE]+(?=\s*[,)])", r"\1 \2", s)
    return s


def to_wkt_element(geom: Any) -> WKTElement | None:
    """Shapely geometry -> WKTElement for GeoAlchemy."""
    if geom is None:
        return None
    if hasattr(geom, "wkt"):
        wkt_str = geom.wkt
    else:
        wkt_str = str(geom)
    geom_type = _geom_type_from_wkt(wkt_str)
    return WKTElement(wkt_str, srid=0)


def _geom_type_from_wkt(wkt_str: str) -> str:
    u = wkt_str.upper()
    if u.startswith("POINT"):
        return "POINT"
    if u.startswith("LINESTRING"):
        return "LINESTRING"
    if u.startswith("POLYGON"):
        return "POLYGON"
    if u.startswith("MULTI"):
        return "GEOMETRY"
    return "GEOMETRY"


def point_wkt(x: float, y: float, z: float | None = None) -> str:
    if z is not None:
        return f"POINT Z ({x} {y} {z})"
    return f"POINT ({x} {y})"


def linestring_wkt(points: list[tuple[float, ...]]) -> str:
    if not points:
        return "LINESTRING EMPTY"
    pts = ", ".join(" ".join(str(p) for p in pt) for pt in points)
    if len(points[0]) >= 3:
        return f"LINESTRING Z ({pts})"
    return f"LINESTRING ({pts})"


def _ring_closed(ring: list[tuple[float, ...]], tol: float = 1e-9) -> bool:
    if len(ring) < 2 or len(ring[0]) < 2 or len(ring[-1]) < 2:
        return True
    return abs(ring[0][0] - ring[-1][0]) <= tol and abs(ring[0][1] - ring[-1][1]) <= tol


def polygon_wkt(exterior: list[tuple[float, ...]]) -> str:
    if not exterior:
        return "POLYGON EMPTY"
    ring = list(exterior)
    if len(ring) >= 2 and not _ring_closed(ring):
        ring.append(ring[0])
    pts = ", ".join(" ".join(str(p) for p in pt) for pt in ring)
    if len(ring[0]) >= 3:
        return f"POLYGON Z (({pts}))"
    return f"POLYGON (({pts}))"


def transform_block_point_to_world(
    lx: float, ly: float,
    base_x: float, base_y: float,
    insert_x: float, insert_y: float,
    scale_x: float = 1.0, scale_y: float = 1.0,
    rotation_deg: float = 0.0,
) -> tuple[float, float]:
    """블록 내부 좌표 (lx,ly) 를 삽입점·기준점·스케일·회전으로 월드 좌표로 변환.
    CAD 규칙: T = Translate(insert) * Rotate(rot) * Scale(sx,sy) * Translate(-base)
    즉 (lx-base_x)*sx, (ly-base_y)*sy 로 스케일 후 회전, insert 더함.
    scale_z는 2D 평면 도면에서 사용하지 않음(출력은 항상 z=0).
    """
    dx = (lx - base_x) * scale_x
    dy = (ly - base_y) * scale_y
    if rotation_deg != 0.0:
        r = math.radians(rotation_deg)
        c, s = math.cos(r), math.sin(r)
        dx, dy = dx * c - dy * s, dx * s + dy * c
    return (insert_x + dx, insert_y + dy)


def wkt_points_to_list(wkt: str) -> list[tuple[float, ...]]:
    """WKT POINT 또는 LINESTRING에서 좌표 리스트 추출. [(x,y,z)], 빈 리스트 가능."""
    if not wkt or not wkt.strip():
        return []
    wkt = wkt.strip()
    if wkt.upper().startswith("SRID="):
        idx = wkt.find(";")
        if idx >= 0:
            wkt = wkt[idx + 1 :].strip()
    m = re.search(r"\(([^)]*)\)", wkt)
    if not m:
        return []
    inner = m.group(1).strip()
    points = []
    for part in re.split(r",\s*", inner):
        nums = re.findall(r"[-\d.eE]+", part)
        if len(nums) >= 2:
            x, y = float(nums[0]), float(nums[1])
            z = float(nums[2]) if len(nums) >= 3 else 0.0
            points.append((x, y, z))
    return points


def transform_block_wkt_to_world(
    geom_wkt: str,
    base_x: float, base_y: float,
    insert_x: float, insert_y: float,
    scale_x: float = 1.0, scale_y: float = 1.0,
    rotation_deg: float = 0.0,
    _wkt_cache: dict[str, list[tuple[float, ...]]] | None = None,
) -> str | None:
    """블록 내부 WKT(POINT/LINESTRING/POLYGON)를 월드 좌표 WKT로 변환.
    각 점에 transform_block_point_to_world 적용. POLYGON은 링별 변환 후 POLYGON으로 반환. _wkt_cache 있으면 파싱 결과 재사용."""
    geom_upper = (geom_wkt or "").strip().upper()
    if geom_upper.startswith("POLYGON") and "((" in geom_wkt and "))" in geom_wkt:
        idx_open = geom_wkt.find("((")
        idx_close = geom_wkt.rfind("))")
        inner = geom_wkt[idx_open + 2 : idx_close].strip()
        ring_parts = re.split(r"\)\s*,\s*\(", inner)
        rings_out = []
        for part in ring_parts:
            part = part.strip().strip("()").strip()
            if not part:
                continue
            pts = []
            for p in part.split(","):
                nums = re.findall(r"[-\d.eE]+", p.strip())
                if len(nums) >= 2:
                    x, y = float(nums[0]), float(nums[1])
                    z = float(nums[2]) if len(nums) >= 3 else 0.0
                    pts.append((x, y, z))
            if not pts:
                continue
            transformed = [
                transform_block_point_to_world(
                    p[0], p[1], base_x, base_y, insert_x, insert_y,
                    scale_x, scale_y, rotation_deg,
                )
                for p in pts
            ]
            ring = [(t[0], t[1], 0.0) for t in transformed]
            if len(ring) >= 2 and not _ring_closed(ring):
                ring.append(ring[0])
            rings_out.append(ring)
        if not rings_out:
            return None
        if len(rings_out) == 1:
            return polygon_wkt(rings_out[0])
        ring_strs = [
            "(" + ", ".join(" ".join(str(c) for c in p) for p in ring) + ")"
            for ring in rings_out
        ]
        return "POLYGON Z (" + ", ".join(ring_strs) + ")"
    if _wkt_cache is not None and geom_wkt in _wkt_cache:
        pts = _wkt_cache[geom_wkt]
    else:
        pts = wkt_points_to_list(geom_wkt)
        if _wkt_cache is not None:
            _wkt_cache[geom_wkt] = pts
    if not pts:
        return None
    transformed = [
        transform_block_point_to_world(
            p[0], p[1], base_x, base_y, insert_x, insert_y,
            scale_x, scale_y, rotation_deg,
        )
        for p in pts
    ]
    if len(transformed) == 1:
        return point_wkt(transformed[0][0], transformed[0][1], 0.0)
    return linestring_wkt([(t[0], t[1], 0.0) for t in transformed])


def transform_point_with_matrix(
    x: float,
    y: float,
    z: float = 0.0,
    matrix: Any = None,
) -> tuple[float, float, float]:
    """Transform a 3D point by ezdxf Matrix44. Returns input on failure."""
    if matrix is None:
        return (float(x), float(y), float(z))
    try:
        out = matrix.transform((float(x), float(y), float(z)))
        return (float(out[0]), float(out[1]), float(out[2]) if len(out) > 2 else 0.0)
    except Exception:
        return (float(x), float(y), float(z))


def _parse_polygon_rings(wkt: str) -> list[list[tuple[float, float, float]]]:
    idx_open = wkt.find("((")
    idx_close = wkt.rfind("))")
    if idx_open < 0 or idx_close < idx_open:
        return []
    inner = wkt[idx_open + 2 : idx_close].strip()
    ring_parts = re.split(r"\)\s*,\s*\(", inner)
    rings: list[list[tuple[float, float, float]]] = []
    for part in ring_parts:
        part = part.strip().strip("()").strip()
        if not part:
            continue
        pts: list[tuple[float, float, float]] = []
        for p in part.split(","):
            nums = re.findall(r"[-\d.eE]+", p.strip())
            if len(nums) >= 2:
                x, y = float(nums[0]), float(nums[1])
                z = float(nums[2]) if len(nums) >= 3 else 0.0
                pts.append((x, y, z))
        if pts:
            rings.append(pts)
    return rings


def transform_wkt_with_matrix(
    geom_wkt: str | None,
    matrix: Any = None,
    _wkt_cache: dict[str, list[tuple[float, ...]]] | None = None,
) -> str | None:
    """Transform POINT/LINESTRING/POLYGON WKT by ezdxf Matrix44."""
    if not geom_wkt or not geom_wkt.strip():
        return None
    wkt = geom_wkt.strip()
    if wkt.upper().startswith("SRID="):
        idx = wkt.find(";")
        if idx >= 0:
            wkt = wkt[idx + 1 :].strip()
    upper = wkt.upper()

    if upper.startswith("POLYGON") and "((" in wkt and "))" in wkt:
        rings = _parse_polygon_rings(wkt)
        out_rings: list[list[tuple[float, float, float]]] = []
        for ring in rings:
            transformed = [transform_point_with_matrix(p[0], p[1], p[2], matrix) for p in ring]
            if len(transformed) >= 2 and not _ring_closed(transformed):
                transformed.append(transformed[0])
            out_rings.append(transformed)
        if not out_rings:
            return None
        if len(out_rings) == 1:
            return polygon_wkt(out_rings[0])
        ring_strs = [
            "(" + ", ".join(" ".join(str(c) for c in p) for p in ring) + ")"
            for ring in out_rings
        ]
        return "POLYGON Z (" + ", ".join(ring_strs) + ")"

    if _wkt_cache is not None and wkt in _wkt_cache:
        pts = _wkt_cache[wkt]
    else:
        pts = wkt_points_to_list(wkt)
        if _wkt_cache is not None:
            _wkt_cache[wkt] = pts
    if not pts:
        return None
    transformed = [transform_point_with_matrix(p[0], p[1], p[2], matrix) for p in pts]
    if len(transformed) == 1:
        return point_wkt(transformed[0][0], transformed[0][1], transformed[0][2])
    return linestring_wkt(transformed)


def bbox_from_points(points: list[tuple[float, ...]]) -> str | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    minx, maxx = min(xs), max(xs)
    miny, maxy = min(ys), max(ys)
    return polygon_wkt([
        (minx, miny), (maxx, miny), (maxx, maxy), (minx, maxy), (minx, miny),
    ])
