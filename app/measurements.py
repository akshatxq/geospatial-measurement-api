"""Horizontal measurements in per-feature UTM coordinates."""

import math

from pyproj import CRS, Transformer
from pyproj.exceptions import ProjError
from shapely import force_2d
from shapely.errors import ShapelyError
from shapely.geometry import mapping
from shapely.geometry.base import BaseGeometry
from shapely.ops import transform
from shapely.validation import explain_validity


def measure(geometry: BaseGeometry | None, source_crs: CRS) -> dict:
    result = {
        "geometry_type": geometry.geom_type if geometry is not None else None,
        "geometry": mapping(geometry) if geometry is not None else None,
        "crs": source_crs.to_string(),
        "status": "UNSUPPORTED",
        "measurement": None,
        "reason": None,
        "warnings": [],
        "is_valid": geometry.is_valid if geometry is not None else None,
    }
    if geometry is None or geometry.is_empty:
        result["reason"] = "Missing or empty geometry."
        return result
    if not geometry.is_valid:
        result.update(status="FAILED", reason=explain_validity(geometry))
        return result
    if geometry.geom_type in {"Point", "MultiPoint"}:
        result.update(status="NOT_APPLICABLE", reason="Points have no area or length.")
        return result
    if geometry.geom_type not in {"Polygon", "MultiPolygon", "LineString", "MultiLineString"}:
        result["reason"] = "Geometry type is not supported for measurement."
        return result
    try:
        wgs = transform(
            Transformer.from_crs(source_crs, 4326, always_xy=True).transform, force_2d(geometry)
        )
        west, south, east, north = wgs.bounds
        if not all(math.isfinite(v) for v in wgs.bounds):
            raise ValueError("Coordinates cannot be transformed to WGS84.")
        if west < -180 or east > 180 or south < -90 or north > 90:
            raise ValueError("Coordinates are outside valid WGS84 bounds.")
        if south < -80 or north > 84 or east - west > 6 or north - south > 6:
            result["reason"] = "Polar, antimeridian or extensive geometry exceeds local UTM limits."
            return result
        lon, lat = wgs.centroid.coords[0]
        zone = min(60, max(1, math.floor((lon + 180) / 6) + 1))
        target = CRS.from_epsg((32600 if lat >= 0 else 32700) + zone)
        projected = transform(Transformer.from_crs(4326, target, always_xy=True).transform, wgs)
        area = geometry.geom_type in {"Polygon", "MultiPolygon"}
        value = projected.area if area else projected.length
        if not math.isfinite(value):
            raise ValueError("Projection produced a non-finite measurement.")
        result.update(
            status="MEASURED",
            measurement={
                "type": "AREA" if area else "LENGTH",
                "value": value,
                "unit": "m2" if area else "m",
                "projected_crs": target.to_string(),
                "method": "projected_utm",
            },
        )
        if geometry.has_z:
            result["warnings"].append("Altitude ignored; measurement is horizontal in 2D.")
    except (ValueError, RuntimeError, ProjError, ShapelyError) as exc:
        result.update(status="FAILED", reason=str(exc))
    return result
