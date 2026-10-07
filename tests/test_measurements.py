import pytest
from pyproj import CRS, Transformer
from shapely.geometry import (
    GeometryCollection,
    LineString,
    MultiLineString,
    MultiPoint,
    MultiPolygon,
    Point,
    Polygon,
    box,
)
from shapely.ops import transform

from app.measurements import measure

UTM = CRS.from_epsg(32643)
SQUARE = box(500000, 2000000, 501000, 2001000)


@pytest.mark.parametrize(
    "geometry,kind,value",
    [
        (SQUARE, "AREA", 1_000_000),
        (LineString([(500000, 2000000), (501000, 2000000)]), "LENGTH", 1000),
        (
            Polygon(
                SQUARE.exterior.coords, [box(500100, 2000100, 500200, 2000200).exterior.coords]
            ),
            "AREA",
            990_000,
        ),
        (MultiPolygon([SQUARE, box(502000, 2000000, 503000, 2001000)]), "AREA", 2_000_000),
        (
            MultiLineString(
                [[(500000, 2000000), (501000, 2000000)], [(501000, 2000000), (501000, 2002000)]]
            ),
            "LENGTH",
            3000,
        ),
    ],
)
def test_metric_and_geographic(geometry, kind, value):
    for geom, crs in [
        (geometry, UTM),
        (
            transform(Transformer.from_crs(UTM, 4326, always_xy=True).transform, geometry),
            CRS.from_epsg(4326),
        ),
    ]:
        result = measure(geom, crs)
        assert result["status"] == "MEASURED"
        assert result["measurement"]["type"] == kind
        assert result["measurement"]["value"] == pytest.approx(value, rel=1e-7)
        assert result["measurement"]["projected_crs"] == "EPSG:32643"


@pytest.mark.parametrize(
    "geom,status",
    [
        (None, "UNSUPPORTED"),
        (Polygon(), "UNSUPPORTED"),
        (Point(77, 20), "NOT_APPLICABLE"),
        (MultiPoint([(77, 20), (77.1, 20.1)]), "NOT_APPLICABLE"),
        (GeometryCollection([Point(77, 20)]), "UNSUPPORTED"),
        (Polygon([(77, 20), (78, 21), (77, 21), (78, 20), (77, 20)]), "FAILED"),
        (box(179, 10, -179, 11), "UNSUPPORTED"),
        (box(10, 85, 11, 86), "UNSUPPORTED"),
        (box(70, 10, 80, 20), "UNSUPPORTED"),
    ],
)
def test_graceful_geometry(geom, status):
    result = measure(geom, CRS.from_epsg(4326))
    assert result["status"] == status
    assert result["reason"]
    assert result["measurement"] is None


def test_south_and_feet():
    south = measure(box(18, -34, 18.01, -33.99), CRS.from_epsg(4326))
    assert south["measurement"]["projected_crs"] == "EPSG:32734"
    feet = CRS.from_proj4("+proj=utm +zone=43 +datum=WGS84 +units=ft +type=crs")
    geometry = transform(Transformer.from_crs(UTM, feet, always_xy=True).transform, SQUARE)
    assert measure(geometry, feet)["measurement"]["value"] == pytest.approx(1e6, rel=1e-7)


def test_latitude_and_z():
    equator = measure(box(75, 0, 75.01, 0.01), CRS.from_epsg(4326))
    high = measure(box(75, 60, 75.01, 60.01), CRS.from_epsg(4326))
    assert high["measurement"]["value"] / equator["measurement"]["value"] == pytest.approx(
        0.5, abs=0.01
    )
    line = LineString([(75, 10, 0), (75.001, 10, 1000)])
    assert measure(line, CRS.from_epsg(4326))["warnings"]
