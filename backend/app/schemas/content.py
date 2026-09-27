import datetime as dt
import re
from typing import ClassVar

from pydantic import BaseModel, Field, field_validator, model_validator


class _NoNullsMixin(BaseModel):
    """Partial-update guard: explicit JSON null for a NON-nullable column
    would sail through validation and 500 at commit (IntegrityError).
    Reject it here with a 422 instead.

    Null stays meaningful for *nullable* columns (account_id,
    caption_template_id, pinned_video_id, preferred_effect, category):
    there it means "clear the field". Each Update schema declares its
    non-nullable fields via ``_non_nullable``.
    """

    _non_nullable: ClassVar[frozenset[str]] = frozenset()

    @model_validator(mode="after")
    def _reject_null_for_non_nullable(self):
        bad = sorted(
            f for f in self._non_nullable
            if f in self.model_fields_set and getattr(self, f) is None
        )
        if bad:
            raise ValueError(f"null is not allowed for: {', '.join(bad)}")
        return self


class ScheduleRuleIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    day_of_week: int = Field(default=-1, ge=-1, le=6)
    hour: int = Field(ge=0, le=23)
    minute: int = Field(default=0, ge=0, le=59)
    account_id: int | None = None
    is_active: bool = True
    preferred_effect: str | None = None
    caption_template_id: int | None = None
    prefer_source_caption: bool = True
    # Pinned video for one-shot scheduling (NULL = draw from the queue).
    pinned_video_id: int | None = None


class ScheduleRuleOut(ScheduleRuleIn):
    id: int
    created_at: dt.datetime
    # Display helpers for the pinned video (resolved by the API, not stored).
    pinned_video_label: str | None = None
    pinned_video_status: str | None = None


class CaptionIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    content: str = Field(min_length=1)
    category: str | None = None
    is_active: bool = True


class CaptionOut(CaptionIn):
    id: int
    use_count: int
    # Computed live per request (exact-caption attribution) — never stored.
    avg_engagement: float | None = None


class HashtagSetIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    tags: str = Field(min_length=1)
    is_active: bool = True


class HashtagSetOut(HashtagSetIn):
    id: int
    use_count: int


class BioIn(BaseModel):
    account_id: int
    # Every section is optional and independent — empty = don't touch on IG.
    text: str = ""
    link_url: str = ""
    full_name: str = Field(default="", max_length=128)

    @field_validator("link_url")
    @classmethod
    def _safe_link(cls, v: str) -> str:
        s = (v or "").strip()
        if s and not s.lower().startswith(("http://", "https://")):
            # Bare domains (t.me/...) render relative and broken — normalize.
            # Dangerous schemes never pass through to the rendered <a href>.
            if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", s, re.IGNORECASE):
                return "https://" + s
            raise ValueError("link_url must be an http(s) URL")
        return s
    make_private: bool | None = None


class BioApplyIn(BaseModel):
    """Sections to apply: any of bio, link, full_name, picture, privacy.
    Omitted/empty = apply every non-empty section (legacy full apply)."""
    fields: list[str] | None = None


class BioOut(BioIn):
    id: int
    profile_pic_path: str | None = None
    has_picture: bool = False
    last_applied: dt.datetime | None


class EffectIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    ffmpeg_filter: str = ""
    is_active: bool = True


class EffectOut(EffectIn):
    id: int
    use_count: int
    # Computed live per request (effect_preset attribution) — never stored.
    avg_engagement: float | None = None


class AudioIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str = ""
    music_volume: float = Field(default=0.4, ge=0.0, le=2.0)
    duck_original: bool = False
    is_active: bool = True


class AudioOut(AudioIn):
    id: int
    file_path: str
    duration: float | None
    use_count: int


class ProxySourceIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    url: str = Field(min_length=8, max_length=1024)
    default_protocol: str = "http"
    default_country: str = Field(default="", max_length=2)
    is_active: bool = True


class ProxySourceOut(ProxySourceIn):
    id: int
    last_fetch_at: dt.datetime | None
    last_added: int
    last_total: int



# --- Partial-update schemas -------------------------------------------------
# PUT endpoints apply body.model_dump(exclude_unset=True), so every field
# here is optional: a partial body touches only the keys it sends. (Using the
# *In create schemas for PUT made partial updates impossible — required
# fields like `name` 422'd when omitted.)
class ScheduleRuleUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset({
        "name", "day_of_week", "hour", "minute", "is_active", "prefer_source_caption",
    })
    name: str | None = Field(default=None, min_length=1, max_length=128)
    day_of_week: int | None = Field(default=None, ge=-1, le=6)
    hour: int | None = Field(default=None, ge=0, le=23)
    minute: int | None = Field(default=None, ge=0, le=59)
    account_id: int | None = None
    is_active: bool | None = None
    preferred_effect: str | None = None
    caption_template_id: int | None = None
    prefer_source_caption: bool | None = None
    pinned_video_id: int | None = None


class CaptionUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset({"name", "content", "is_active"})
    name: str | None = Field(default=None, min_length=1, max_length=128)
    content: str | None = Field(default=None, min_length=1)
    category: str | None = None
    is_active: bool | None = None


class HashtagSetUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset({"name", "tags", "is_active"})
    name: str | None = Field(default=None, min_length=1, max_length=128)
    tags: str | None = Field(default=None, min_length=1)
    is_active: bool | None = None


class BioUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset({"account_id", "text", "link_url", "full_name"})
    account_id: int | None = None
    text: str | None = None
    link_url: str | None = None
    full_name: str | None = Field(default=None, max_length=128)

    @field_validator("link_url")
    @classmethod
    def _safe_link(cls, v: str | None) -> str | None:
        if v is None:
            return None
        s = v.strip()
        if s and not s.lower().startswith(("http://", "https://")):
            # Bare domains (t.me/...) render relative and broken — normalize.
            # Dangerous schemes never pass through to the rendered <a href>.
            if re.match(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/.*)?$", s, re.IGNORECASE):
                return "https://" + s
            raise ValueError("link_url must be an http(s) URL")
        return s


class EffectUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset({"name", "description", "ffmpeg_filter", "is_active"})
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    ffmpeg_filter: str | None = None
    is_active: bool | None = None


class AudioUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset(
        {"name", "description", "music_volume", "duck_original", "is_active"})
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = None
    music_volume: float | None = Field(default=None, ge=0.0, le=2.0)
    duck_original: bool | None = None
    is_active: bool | None = None


class ProxySourceUpdate(_NoNullsMixin):
    _non_nullable: ClassVar[frozenset[str]] = frozenset(
        {"name", "url", "default_protocol", "default_country", "is_active"})
    name: str | None = Field(default=None, min_length=1, max_length=128)
    url: str | None = Field(default=None, min_length=8, max_length=1024)
    default_protocol: str | None = None
    default_country: str | None = Field(default=None, max_length=2)
    is_active: bool | None = None


class SettingUpdate(BaseModel):
    value: str = Field(min_length=1, max_length=5000)


class SettingOut(BaseModel):
    key: str
    value: str
    category: str
    is_sensitive: bool


class LogOut(BaseModel):
    id: int
    level: str
    category: str
    message: str
    details: dict | None
    timestamp: dt.datetime
