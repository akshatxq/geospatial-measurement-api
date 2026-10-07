"""Synchronous upload API with atomic SQLite persistence."""

import json
import logging
import os
import sqlite3
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from app.limits import BodyLimitMiddleware
from app.measurements import measure
from app.readers import InvalidFile, clean, read_file

logger = logging.getLogger(__name__)


class FileInfo(BaseModel):
    id: str
    filename: str
    feature_count: int
    crs: str | None
    crs_assumed: bool
    status: Literal["COMPLETED", "FAILED"]
    error: str | None = None


class Measurement(BaseModel):
    type: Literal["AREA", "LENGTH"]
    value: float
    unit: Literal["m2", "m"]
    projected_crs: str
    method: str


class Feature(BaseModel):
    index: int
    feature_id: str
    geometry_type: str | None
    geometry: dict[str, Any] | None = None
    crs: str
    properties: dict[str, Any]
    status: Literal["MEASURED", "NOT_APPLICABLE", "UNSUPPORTED", "FAILED"]
    measurement: Measurement | None
    reason: str | None
    warnings: list[str]
    is_valid: bool | None


class Summary(BaseModel):
    measured: int = 0
    not_applicable: int = 0
    unsupported: int = 0
    failed: int = 0
    total_area_m2: float = 0
    total_length_m: float = 0


class MeasurementsResponse(BaseModel):
    file_id: str
    total: int
    page: int
    page_size: int
    summary: Summary
    features: list[Feature]


def create_app(
    db_path: str | Path | None = None,
    max_upload_bytes: int | None = None,
    max_expanded_bytes: int | None = None,
) -> FastAPI:
    db_path = str(db_path or os.environ.get("DATABASE_PATH", "data.sqlite3"))
    upload_limit = max_upload_bytes or int(os.environ.get("MAX_UPLOAD_BYTES", 50 * 1024 * 1024))
    expansion_limit = max_expanded_bytes or int(
        os.environ.get("MAX_EXPANDED_BYTES", 100 * 1024 * 1024)
    )
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)

    def connect() -> sqlite3.Connection:
        connection = sqlite3.connect(db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    connection = connect()
    with connection:
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS files (id TEXT PRIMARY KEY, info TEXT NOT NULL,
                summary TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS features (file_id TEXT NOT NULL REFERENCES files(id),
                source_index INTEGER NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (file_id, source_index));
        """)
    connection.close()
    application = FastAPI(title="Geospatial File Measurement API", version="1.0.0")

    application.add_middleware(BodyLimitMiddleware, max_bytes=upload_limit + 1024 * 1024)

    def lookup(connection: sqlite3.Connection, file_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM files WHERE id = ?", (file_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "File not found.")
        return row

    def process(filename: str, data: bytes, assume_crs: str | None) -> FileInfo:
        try:
            crs, assumed, extracted = read_file(filename, data, assume_crs, expansion_limit)
        except InvalidFile as exc:
            raise HTTPException(422, str(exc)) from exc
        file_id = str(uuid.uuid4())
        info = FileInfo(
            id=file_id,
            filename=filename,
            feature_count=0,
            crs=crs.to_string(),
            crs_assumed=assumed,
            status="COMPLETED",
        )
        summary = Summary()
        features = []
        try:
            for index, (feature_id, geometry, properties, error) in enumerate(extracted):
                result = measure(geometry, crs)
                if error:
                    result.update(status="FAILED", reason=error)
                feature = Feature(
                    index=index,
                    feature_id=feature_id,
                    properties=clean(properties),
                    **clean(result),
                )
                features.append(feature)
                field = feature.status.lower()
                setattr(summary, field, getattr(summary, field) + 1)
                if feature.measurement:
                    field = (
                        "total_area_m2" if feature.measurement.type == "AREA" else "total_length_m"
                    )
                    setattr(summary, field, getattr(summary, field) + feature.measurement.value)
            info.feature_count = len(features)
            connection = connect()
            try:
                with connection:
                    connection.execute(
                        "INSERT INTO files VALUES (?, ?, ?)",
                        (file_id, info.model_dump_json(), summary.model_dump_json()),
                    )
                    connection.executemany(
                        "INSERT INTO features VALUES (?, ?, ?)",
                        [(file_id, f.index, f.model_dump_json()) for f in features],
                    )
            finally:
                connection.close()
        except Exception as exc:
            logger.exception("Processing failure for %s", file_id)
            info.status, info.error, info.feature_count = (
                "FAILED",
                "Internal processing failure.",
                0,
            )
            try:
                connection = connect()
                try:
                    with connection:
                        connection.execute(
                            "INSERT OR REPLACE INTO files VALUES (?, ?, ?)",
                            (file_id, info.model_dump_json(), Summary().model_dump_json()),
                        )
                finally:
                    connection.close()
            except sqlite3.Error:
                logger.exception("Unable to persist failed file %s", file_id)
            raise HTTPException(500, {"message": info.error, "file_id": file_id}) from exc
        return info

    @application.post("/api/files/", response_model=FileInfo, status_code=201)
    async def upload(file: UploadFile = File(...), assume_crs: str | None = Form(None)):
        filename = (file.filename or "").replace("\\", "/").split("/")[-1]
        filename = "".join(c for c in filename if c.isprintable())[:255]
        try:
            if not filename.lower().endswith((".kml", ".zip")):
                raise HTTPException(415, "Upload a .kml or .zip Shapefile.")
            data = bytearray()
            while chunk := await file.read(64 * 1024):
                data.extend(chunk)
                if len(data) > upload_limit:
                    raise HTTPException(413, "Upload exceeds size limit.")
            if not data:
                raise HTTPException(422, "Empty upload.")
            return await run_in_threadpool(process, filename, bytes(data), assume_crs)
        finally:
            await file.close()

    @application.get("/api/files/{file_id}/", response_model=FileInfo)
    def file_info(file_id: str):
        connection = connect()
        try:
            return json.loads(lookup(connection, file_id)["info"])
        finally:
            connection.close()

    @application.get("/api/files/{file_id}/measurements/", response_model=MeasurementsResponse)
    def measurements(
        file_id: str,
        page: int = Query(1, ge=1),
        page_size: int = Query(100, ge=1, le=1000),
        include_geometry: bool = True,
    ):
        connection = connect()
        try:
            row = lookup(connection, file_id)
            info = json.loads(row["info"])
            records = connection.execute(
                "SELECT payload FROM features WHERE file_id = ? "
                "ORDER BY source_index LIMIT ? OFFSET ?",
                (file_id, page_size, (page - 1) * page_size),
            ).fetchall()
            features = [json.loads(record["payload"]) for record in records]
            if not include_geometry:
                for feature in features:
                    feature["geometry"] = None
            return dict(
                file_id=file_id,
                total=info["feature_count"],
                page=page,
                page_size=page_size,
                summary=json.loads(row["summary"]),
                features=features,
            )
        finally:
            connection.close()

    return application


app = create_app()
