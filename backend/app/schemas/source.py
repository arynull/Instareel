"""Video source schemas."""
import datetime as dt
import re

from pydantic import BaseModel, Field, field_validator

_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


class SourceIn(BaseModel):
    username: str = Field(min_length=1, max_length=32)
    max_items: int = Field(default=20, ge=1, le=200)
    reels_only: bool = True
    with_covers: bool = True
    auto_process: bool = True
    delay_min_s: float = Field(default=8.0, ge=0, le=300)
    delay_max_s: float = Field(default=20.0, ge=0, le=300)

    @field_validator("username")
    @classmethod
    def _clean_username(cls, v: str) -> str:
        u = (v or "").strip().lstrip("@").lower()
        if not _USERNAME_RE.match(u):
            raise ValueError("invalid Instagram username")
        return u

    @field_validator("delay_max_s")
    @classmethod
    def _delays_ordered(cls, v: float, info) -> float:
        if info.data.get("delay_min_s") is not None and v < info.data["delay_min_s"]:
            raise ValueError("delay_max_s must be >= delay_min_s")
        return v


class SourceUpdate(BaseModel):
    max_items: int = Field(default=20, ge=1, le=200)
    reels_only: bool = True
    with_covers: bool = True
    auto_process: bool = True
    delay_min_s: float = Field(default=8.0, ge=0, le=300)
    delay_max_s: float = Field(default=20.0, ge=0, le=300)

    @field_validator("delay_max_s")
    @classmethod
    def _delays_ordered(cls, v: float, info) -> float:
        if info.data.get("delay_min_s") is not None and v < info.data["delay_min_s"]:
            raise ValueError("delay_max_s must be >= delay_min_s")
        return v


class SourceOut(BaseModel):
    id: int
    username: str
    account_id: int | None
    status: str
    max_items: int
    reels_only: bool
    with_covers: bool
    auto_process: bool
    delay_min_s: float
    delay_max_s: float
    has_cursor: bool = False
    fetched: int = 0
    downloaded: int = 0
    skipped: int = 0
    failed_count: int = 0
    total_items: int = 0
    pending_items: int = 0
    last_error: str | None = None
    current_stage: str | None = None
    started_at: dt.datetime | None = None
    finished_at: dt.datetime | None = None
    created_at: dt.datetime | None = None


class SourceItemOut(BaseModel):
    id: int
    media_pk: str
    shortcode: str | None = None
    media_type: str | None = None
    status: str
    video_id: int | None = None
    error: str | None = None
    created_at: dt.datetime | None = None
