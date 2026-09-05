"""Versioned Pydantic models for point-in-time snapshot manifests."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import PurePosixPath
from typing import Literal, Self
from urllib.parse import parse_qsl, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

MANIFEST_SCHEMA_VERSION: Literal["1.1"] = "1.1"


class CaptureKind(StrEnum):
    """Kinds of perishable inputs captured during Phase -1."""

    SALARIES = "salaries"
    PROJECTIONS = "projections"
    OWNERSHIP = "ownership"
    ODDS = "odds"
    WEATHER = "weather"
    NEWS = "news"
    STANDINGS = "standings"
    INACTIVES = "inactives"
    STATS = "stats"


def _require_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamp must include a timezone")
    return value.astimezone(UTC)


class SnapshotFile(BaseModel):
    """The immutable provenance and integrity record for one captured file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    path: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    original_filename: str
    observed_at: datetime
    source: str
    kind: CaptureKind

    @field_validator("path")
    @classmethod
    def validate_relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
            raise ValueError("path must be a safe POSIX path relative to the capture directory")
        return path.as_posix()

    @field_validator("original_filename")
    @classmethod
    def validate_original_filename(cls, value: str) -> str:
        if not value or "/" in value or "\\" in value or value in {".", ".."}:
            raise ValueError("original_filename must be a base filename")
        return value

    @field_validator("observed_at")
    @classmethod
    def normalize_observed_at(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        source = value.strip()
        if not source:
            raise ValueError("source must not be empty")
        return source

    @model_validator(mode="after")
    def validate_storage_path(self) -> Self:
        path = PurePosixPath(self.path)
        if len(path.parts) != 2 or path.parts[0] != self.kind.value:
            raise ValueError("path must have the form '<kind>/<original_filename>'")
        if path.name != self.original_filename:
            raise ValueError("path filename must match original_filename")
        return self


class SnapshotRequest(BaseModel):
    """Sanitized request/response provenance for a fetched artifact."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    url: str
    observed_at: datetime
    attempts: int = Field(ge=1, le=3)
    status_code: int = Field(ge=100, le=599)
    response_headers: dict[str, str] = Field(default_factory=dict)
    file_path: str
    stadium: str | None = None
    stadium_table_version: str | None = None
    kickoff_at: datetime | None = None
    forecast_model_run_at: datetime | None = None
    forecast_lead_time_seconds: int | None = Field(default=None, ge=0)

    @field_validator("source")
    @classmethod
    def validate_source(cls, value: str) -> str:
        source = value.strip()
        if not source:
            raise ValueError("source must not be empty")
        return source

    @field_validator("url")
    @classmethod
    def reject_credentials_in_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("url must be an absolute HTTP(S) URL")
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
            if (
                key.casefold() in {"apikey", "api_key", "key", "token"}
                and query_value != "REDACTED"
            ):
                raise ValueError("request credentials must be redacted")
        return value

    @field_validator("observed_at", "kickoff_at", "forecast_model_run_at")
    @classmethod
    def normalize_timestamps(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _require_utc(value)

    @field_validator("file_path")
    @classmethod
    def validate_file_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or "\\" in value or path.is_absolute() or ".." in path.parts:
            raise ValueError("file_path must be a safe relative POSIX path")
        return path.as_posix()

    @model_validator(mode="after")
    def validate_forecast_metadata(self) -> Self:
        forecast_values = (
            self.stadium,
            self.stadium_table_version,
            self.kickoff_at,
            self.forecast_model_run_at,
            self.forecast_lead_time_seconds,
        )
        if any(value is not None for value in forecast_values) and not all(
            value is not None for value in forecast_values
        ):
            raise ValueError("weather requests require complete forecast provenance")
        return self


class SnapshotError(BaseModel):
    """A visible degraded-mode record for a failed input or HTTP request."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    occurred_at: datetime
    attempts: int = Field(ge=0, le=3)
    error_type: str
    message: str
    request_url: str | None = None
    response_headers: dict[str, str] = Field(default_factory=dict)

    @field_validator("source", "error_type", "message")
    @classmethod
    def reject_empty_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("error text fields must not be empty")
        return normalized

    @field_validator("occurred_at")
    @classmethod
    def normalize_occurred_at(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @field_validator("request_url")
    @classmethod
    def reject_credentials_in_request_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return SnapshotRequest.reject_credentials_in_url(value)


class SnapshotManifest(BaseModel):
    """Schema-versioned manifest written at the root of every capture."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0", "1.1"] = MANIFEST_SCHEMA_VERSION
    season: int = Field(ge=1)
    week: int = Field(ge=1, le=99)
    captured_at: datetime
    files: tuple[SnapshotFile, ...] = ()
    requests: tuple[SnapshotRequest, ...] = ()
    errors: tuple[SnapshotError, ...] = ()

    @field_validator("captured_at")
    @classmethod
    def normalize_captured_at(cls, value: datetime) -> datetime:
        return _require_utc(value)

    @model_validator(mode="after")
    def reject_duplicate_paths(self) -> Self:
        paths = [file.path for file in self.files]
        if len(paths) != len(set(paths)):
            raise ValueError("manifest contains duplicate file paths")
        file_paths = set(paths)
        for request in self.requests:
            if request.file_path not in file_paths:
                raise ValueError("request file_path must reference a manifested file")
        return self
