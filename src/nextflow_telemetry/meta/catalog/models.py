"""Source descriptors, not sample annotations or processing registrations."""
from __future__ import annotations

from enum import Enum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class SourceKind(str, Enum):
    git_repository = "git_repository"
    google_sheet = "google_sheet"
    zenodo = "zenodo"
    other = "other"


class Access(str, Enum):
    unknown = "unknown"
    public = "public"
    restricted = "restricted"


class ReviewStatus(str, Enum):
    unreviewed = "unreviewed"
    reviewed = "reviewed"
    retired = "retired"


class AssetRole(str, Enum):
    inventory = "inventory"
    sample_metadata = "sample_metadata"
    study_metadata = "study_metadata"
    profiles = "profiles"
    other = "other"


class Descriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Source(Descriptor):
    source_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    title: str = Field(min_length=1, max_length=300)
    kind: SourceKind
    url: str = Field(min_length=1, max_length=2000)
    description: str = ""
    maintainer: str | None = None
    access: Access = Access.unknown
    data_license: str | None = None
    review_status: ReviewStatus = ReviewStatus.unreviewed

    @field_validator("url")
    @classmethod
    def public_locator(cls, value: str) -> str:
        u = urlsplit(value)
        if u.scheme not in {"https", "http"} or not u.hostname or u.username or u.password:
            raise ValueError("use an HTTP(S) URL without embedded credentials")
        return value


class Asset(Descriptor):
    asset_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,79}$")
    locator: str = Field(min_length=1, max_length=2000)
    role: AssetRole
    description: str = ""
    access: Access = Access.unknown
    data_license: str | None = None


class SourceManifest(Descriptor):
    schema_version: int = Field(default=1, ge=1, le=1, strict=True)
    source: Source
    assets: list[Asset] = Field(min_length=1)

    @model_validator(mode="after")
    def unique_assets(self) -> SourceManifest:
        ids = [a.asset_id for a in self.assets]
        if len(ids) != len(set(ids)):
            raise ValueError("asset_id must be unique within the source")
        return self
