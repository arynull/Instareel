"""Pydantic schemas shared across resources."""
import datetime as dt
import re

from pydantic import BaseModel, Field, field_validator

# Instagram usernames: 1-30 chars, letters/digits/period/underscore only.
# Validated at creation so a typo ("my page", "@user") fails fast with a
# clear 422 instead of creating an account that can never log in.
_IG_USERNAME_RE = re.compile(r"^[A-Za-z0-9._]{1,30}$")


def validate_ig_username(v: str) -> str:
    v = v.strip()
    if not _IG_USERNAME_RE.match(v):
        raise ValueError(
            "Instagram usernames are 1-30 chars: letters, numbers, periods, "
            "underscores only (no @, no spaces)"
        )
    return v


class AccountCreate(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1)
    proxy_id: int | None = None
    max_daily_posts: int = Field(default=3, ge=1, le=20)
    notes: str | None = None

    @field_validator("username")
    @classmethod
    def _username_charset(cls, v: str) -> str:
        return validate_ig_username(v)


class AccountUpdate(BaseModel):
    max_daily_posts: int | None = Field(default=None, ge=1, le=20)
    notes: str | None = None
    status: str | None = None
    proxy_id: int | str | None = None


class AccountOut(BaseModel):
    id: int
    username: str
    ig_user_id: str | None = None
    proxy_id: int | None
    status: str
    last_login: dt.datetime | None
    last_post: dt.datetime | None
    posts_today: int
    max_daily_posts: int
    cooldown_until: dt.datetime | None
    total_posts: int
    total_views: int
    total_likes: int
    notes: str | None
    has_session: bool = False
    created_at: dt.datetime
    updated_at: dt.datetime


class ProxyCreate(BaseModel):
    url: str
    protocol: str = "http"
    username: str | None = None
    password: str | None = None
    country: str | None = None


class ProxyUpdate(BaseModel):
    url: str | None = None
    protocol: str | None = None
    username: str | None = None
    password: str | None = None
    country: str | None = None
    is_active: bool | None = None


class ProxyOut(BaseModel):
    id: int
    url: str
    protocol: str
    username: str | None
    country: str | None
    is_healthy: bool
    last_checked: dt.datetime | None
    fail_count: int
    latency_ms: int | None
    last_error: str | None = None
    source: str | None = None
    is_active: bool
    created_at: dt.datetime
