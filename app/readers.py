"""Read untrusted uploads without retaining or blindly extracting archives."""

import io
import json
import math
import stat
import struct
import zipfile
from pathlib import PurePosixPath
from typing import Any
from xml.etree.ElementTree import ParseError

import shapefile
from defusedxml import ElementTree
from defusedxml.common import DefusedXmlException
from pyproj import CRS
from pyproj.exceptions import CRSError
from shapely.errors import ShapelyError
from shapely.geometry import LineString, Point, Polygon, shape


class InvalidFile(ValueError):
    pass


def clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [clean(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return "[binary value]"
    return str(value)


def local(element):
    return element.tag.rsplit("}", 1)[-1]


def coordinates(element):
    node = next((e for e in element.iter() if local(e) == "coordinates"), None)
    if node is None or not node.text:
        raise InvalidFile("Geometry has no coordinates.")
    coords = [tuple(float(v) for v in item.split(",")) for item in node.text.split()]
    if any(len(c) not in {2, 3} or not all(math.isfinite(v) for v in c) for c in coords):
        raise InvalidFile("Invalid KML coordinates.")
    return coords


def kml_geometry(element):
    kind = local(element)
    if kind == "Point":
        return Point(coordinates(element)[0])
    if kind == "LineString":
        return LineString(coordinates(element))
    if kind == "Polygon":
        outer = next((e for e in element if local(e) == "outerBoundaryIs"), None)
        if outer is None:
            raise InvalidFile("Polygon has no outer boundary.")
        holes = [coordinates(e) for e in element if local(e) == "innerBoundaryIs"]
        return Polygon(coordinates(outer), holes)
    if kind == "MultiGeometry":
        from shapely.geometry import GeometryCollection, MultiLineString, MultiPoint, MultiPolygon

        parts = [kml_geometry(e) for e in element]
        if parts and all(p is not None and p.geom_type == "Polygon" for p in parts):
            return MultiPolygon(parts)
        if parts and all(p is not None and p.geom_type == "LineString" for p in parts):
            return MultiLineString(parts)
        if parts and all(p is not None and p.geom_type == "Point" for p in parts):
            return MultiPoint(parts)
        return GeometryCollection([p for p in parts if p is not None])
    return None


def read_kml(data: bytes):
    try:
        root = ElementTree.fromstring(data, forbid_dtd=True)
    except (ParseError, DefusedXmlException) as exc:
        raise InvalidFile("Malformed or unsafe KML XML.") from exc
    if local(root) != "kml":
        raise InvalidFile("XML root must be kml.")
    features = []
    for placemark in (e for e in root.iter() if local(e) == "Placemark"):
        properties = {
            local(e): e.text or "" for e in placemark if local(e) in {"name", "description"}
        }
        for e in placemark.iter():
            if local(e) == "Data":
                properties[e.get("name", "")] = next(
                    (v.text or "" for v in e if local(v) == "value"), ""
                )
            elif local(e) == "SimpleData":
                properties[e.get("name", "")] = e.text or ""
        geom = next(
            (
                e
                for e in placemark
                if local(e) in {"Point", "LineString", "Polygon", "MultiGeometry", "Track", "Model"}
            ),
            None,
        )
        error = None
        try:
            geometry = kml_geometry(geom) if geom is not None else None
        except (ValueError, IndexError, ShapelyError) as exc:
            geometry, error = None, f"Malformed feature geometry: {exc}"
        features.append((placemark.get("id", str(len(features))), geometry, properties, error))
    return CRS.from_epsg(4326), False, features


def read_shapefile(data: bytes, assume_crs: str | None, expansion_limit: int):
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if len(entries) > 1000 or sum(e.file_size for e in entries) > expansion_limit:
                raise InvalidFile("Archive exceeds member or expanded-size limit.")
            names = {}
            for entry in entries:
                path = PurePosixPath(entry.filename.replace("\\", "/"))
                if (
                    path.is_absolute()
                    or ".." in path.parts
                    or ":" in entry.filename
                    or stat.S_ISLNK(entry.external_attr >> 16)
                ):
                    raise InvalidFile("Unsafe archive path or symlink.")
                name = str(path).lower()
                if name in names:
                    raise InvalidFile("Duplicate archive member.")
                names[name] = entry
            datasets = [n for n in names if n.endswith(".shp")]
            if len(datasets) != 1:
                raise InvalidFile("ZIP must contain exactly one Shapefile dataset.")
            stem = datasets[0][:-4]
            for ext in (".shp", ".shx", ".dbf"):
                if stem + ext not in names:
                    raise InvalidFile(f"Shapefile is missing {ext}.")
            assumed = stem + ".prj" not in names
            if assumed and not assume_crs:
                raise InvalidFile("Missing .prj; supply assume_crs explicitly.")
            crs = CRS.from_user_input(
                assume_crs if assumed else archive.read(names[stem + ".prj"]).decode("utf-8-sig")
            )
            encoding = "utf-8"
            if stem + ".cpg" in names:
                encoding = archive.read(names[stem + ".cpg"]).decode().strip()
            reader = shapefile.Reader(
                shp=io.BytesIO(archive.read(names[stem + ".shp"])),
                shx=io.BytesIO(archive.read(names[stem + ".shx"])),
                dbf=io.BytesIO(archive.read(names[stem + ".dbf"])),
                encoding=encoding,
            )
            features = []
            for index, record in enumerate(reader.iterShapeRecords()):
                error = None
                try:
                    geometry = (
                        shape(record.shape.__geo_interface__) if record.shape.shapeType else None
                    )
                except (ValueError, IndexError, ShapelyError) as exc:
                    geometry, error = None, f"Malformed feature geometry: {exc}"
                features.append((str(index), geometry, clean(record.record.as_dict()), error))
            reader.close()
            return crs, assumed, features
    except InvalidFile:
        raise
    except (
        struct.error,
        EOFError,
        zipfile.BadZipFile,
        RuntimeError,
        KeyError,
        UnicodeError,
        CRSError,
        shapefile.ShapefileException,
        ValueError,
        LookupError,
        OSError,
    ) as exc:
        raise InvalidFile("Unreadable ZIP, Shapefile components or CRS.") from exc


def read_file(filename: str, data: bytes, assume_crs: str | None, expansion_limit: int):
    result = (
        read_kml(data)
        if filename.lower().endswith(".kml")
        else read_shapefile(data, assume_crs, expansion_limit)
    )
    if not result[2]:
        raise InvalidFile("Dataset contains no features.")
    # Ensure stored values can be represented by strict JSON.
    json.dumps(clean([f[2] for f in result[2]]), allow_nan=False)
    return result
