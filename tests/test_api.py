import io
import zipfile

import pytest
import shapefile
from fastapi.testclient import TestClient
from pyproj import CRS, Transformer

from app.main import create_app


def kml(content):
    return (
        f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document>{content}</Document></kml>'.encode()
    )


def placemark(geometry, name="sample"):
    return f'<Placemark id="{name}"><name>{name}</name>{geometry}</Placemark>'


POINT = "<Point><coordinates>75,20,0</coordinates></Point>"
LINE = "<LineString><coordinates>75,20 75.01,20</coordinates></LineString>"


def archive(prj=True, missing=None, extra=None):
    shp, shx, dbf = io.BytesIO(), io.BytesIO(), io.BytesIO()
    writer = shapefile.Writer(shp=shp, shx=shx, dbf=dbf, shapeType=shapefile.POLYGON)
    writer.field("name", "C")
    writer.poly(
        [
            [
                (500000, 2000000),
                (500000, 2001000),
                (501000, 2001000),
                (501000, 2000000),
                (500000, 2000000),
            ]
        ]
    )
    writer.record("square")
    writer.close()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for ext, data in [
            ("shp", shp.getvalue()),
            ("shx", shx.getvalue()),
            ("dbf", dbf.getvalue()),
        ]:
            if ext != missing:
                z.writestr("folder/survey." + ext, data)
        if prj:
            z.writestr("folder/survey.prj", CRS.from_epsg(32643).to_wkt())
        if extra:
            z.writestr(extra, b"test")
    return buffer.getvalue()


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path / "test.sqlite3")) as client:
        yield client


def upload(client, content, filename="survey.kml", **kwargs):
    return client.post("/api/files/", files={"file": (filename, content)}, **kwargs)


def test_endpoints_pagination_and_persistence(client):
    data = kml(
        "<Folder>"
        + placemark(POINT, "point")
        + "</Folder><Folder>"
        + placemark(LINE, "line")
        + placemark("", "empty")
        + "</Folder>"
    )
    response = upload(client, data)
    assert response.status_code == 201, response.text
    info = response.json()
    assert info["feature_count"] == 3
    assert info["crs"] == "EPSG:4326"
    assert info["status"] == "COMPLETED"
    base = f"/api/files/{info['id']}/"
    assert client.get(base).json() == info
    measured = client.get(base + "measurements/?page_size=1&include_geometry=false").json()
    assert measured["total"] == 3
    assert measured["summary"]["measured"] == 1
    assert measured["summary"]["not_applicable"] == 1
    assert measured["summary"]["unsupported"] == 1
    assert measured["features"][0]["geometry"] is None
    assert measured["features"][0]["properties"] == {"name": "point"}
    page2 = client.get(base + "measurements/?page=2&page_size=1").json()
    assert page2["features"][0]["index"] == 1
    assert page2["features"][0]["geometry"]["type"] == "LineString"
    assert page2["summary"] == measured["summary"]
    assert client.get(base + "measurements/?page=0").status_code == 422
    assert client.get(base + "measurements/?page_size=1001").status_code == 422


def test_real_square_kml(client):
    transformer = Transformer.from_crs(32643, 4326, always_xy=True)
    points = [
        transformer.transform(x, y)
        for x, y in [
            (500000, 2000000),
            (501000, 2000000),
            (501000, 2001000),
            (500000, 2001000),
            (500000, 2000000),
        ]
    ]
    coords = " ".join(f"{x},{y}" for x, y in points)
    data = kml(
        placemark(
            "<Polygon><outerBoundaryIs><LinearRing><coordinates>"
            + coords
            + "</coordinates></LinearRing></outerBoundaryIs></Polygon>"
        )
    )
    response = upload(client, data)
    assert response.status_code == 201
    result = client.get(f"/api/files/{response.json()['id']}/measurements/").json()
    assert result["features"][0]["measurement"]["value"] == pytest.approx(1e6, rel=1e-7)


def test_shapefile_and_missing_crs(client):
    response = upload(client, archive(), "survey.zip")
    assert response.status_code == 201, response.text
    result = client.get(f"/api/files/{response.json()['id']}/measurements/").json()
    assert result["features"][0]["measurement"]["value"] == pytest.approx(1e6, rel=1e-7)
    assert upload(client, archive(prj=False), "survey.zip").status_code == 422
    assumed = upload(client, archive(prj=False), "survey.zip", data={"assume_crs": "EPSG:32643"})
    assert assumed.status_code == 201
    assert assumed.json()["crs_assumed"]
    assert (
        upload(client, archive(prj=False), "survey.zip", data={"assume_crs": "bad"}).status_code
        == 422
    )


@pytest.mark.parametrize(
    "data,filename,status",
    [
        (b"", "empty.kml", 422),
        (b"broken", "broken.kml", 422),
        (b"<notkml/>", "bad.kml", 422),
        (kml(""), "empty.kml", 422),
        (b"bad", "bad.zip", 422),
        (b"x", "bad.txt", 415),
        (b'<!DOCTYPE kml [<!ENTITY x SYSTEM "file:///etc/passwd">]><kml>&x;</kml>', "bad.kml", 422),
    ],
)
def test_invalid_uploads(client, data, filename, status):
    assert upload(client, data, filename).status_code == status


@pytest.mark.parametrize(
    "data",
    [
        archive(missing="shx"),
        archive(missing="dbf"),
        archive(extra="../escape"),
        archive(extra="C:/escape"),
        archive(extra="second.shp"),
    ],
)
def test_bad_archives(client, data):
    assert upload(client, data, "bad.zip").status_code == 422


def test_limits(tmp_path):
    with TestClient(create_app(tmp_path / "tiny.sqlite3", max_upload_bytes=10)) as client:
        assert upload(client, kml(placemark(POINT))).status_code == 413
    with TestClient(create_app(tmp_path / "expand.sqlite3", max_expanded_bytes=10)) as client:
        assert upload(client, archive(), "large.zip").status_code == 422


def test_bad_feature_does_not_abort(client):
    response = upload(
        client,
        kml(
            placemark(POINT)
            + placemark("<LineString><coordinates>garbage</coordinates></LineString>", "bad")
        ),
    )
    assert response.status_code == 201
    result = client.get(f"/api/files/{response.json()['id']}/measurements/").json()
    assert result["summary"]["failed"] == 1
    assert result["summary"]["not_applicable"] == 1


def test_unknown(client):
    assert client.get("/api/files/not-a-uuid/").status_code == 404
    assert client.get("/api/files/not-a-uuid/measurements/").status_code == 404
    assert client.post("/api/files/").status_code == 422


def test_body_limit_before_multipart(tmp_path):
    with TestClient(create_app(tmp_path / "body.sqlite3", max_upload_bytes=10)) as client:
        response = client.post(
            "/api/files/",
            content=b"a" * (1024 * 1024 + 11),
            headers={"content-type": "multipart/form-data; boundary=test"},
        )
        assert response.status_code == 413


def test_file_level_failure_is_persisted(client, monkeypatch):
    import app.main as main

    def broken(*args):
        raise RuntimeError("private internal details")

    monkeypatch.setattr(main, "measure", broken)
    response = upload(client, kml(placemark(POINT)))
    assert response.status_code == 500
    file_id = response.json()["detail"]["file_id"]
    assert "private" not in response.text
    assert client.get(f"/api/files/{file_id}/").json()["status"] == "FAILED"
    result = client.get(f"/api/files/{file_id}/measurements/").json()
    assert result["total"] == 0
    assert result["features"] == []


def test_persistence_after_restart(tmp_path):
    path = tmp_path / "persistent.sqlite3"
    with TestClient(create_app(path)) as client:
        file_id = upload(client, kml(placemark(POINT))).json()["id"]
    with TestClient(create_app(path)) as client:
        assert client.get(f"/api/files/{file_id}/").json()["feature_count"] == 1
        assert client.get(f"/api/files/{file_id}/measurements/").json()["total"] == 1
