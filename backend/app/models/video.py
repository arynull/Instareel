"""Video + Post models."""
import datetime as dt
import enum

from sqlalchemy import BigInteger, DateTime, Enum, Float, ForeignKey, Index, Integer, String, Text, Boolean, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin


class VideoStatus(str, enum.Enum):
    uploaded = "uploaded"
    processing = "processing"
    processed = "processed"
    posting = "posting"
    posted = "posted"
    failed = "failed"
    archived = "archived"


class PostStatus(str, enum.Enum):
    scheduled = "scheduled"
    posting = "posting"
    posted = "posted"
    failed = "failed"
    deleted = "deleted"
    shadowbanned_check = "shadowbanned_check"


class Video(Base, TimestampMixin):
    __tablename__ = "videos"

    original_filename: Mapped[str] = mapped_column(String(512), nullable=False)
    raw_path: Mapped[str] = mapped_column(String(1024), nullable=False)
    processed_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    # Admin-uploaded cover: preferred over the auto-extracted frame at post time.
    custom_thumbnail_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    duration: Mapped[float | None] = mapped_column(Float, nullable=True)
    file_size: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    md5_hash: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    # Perceptual hash (64-bit dHash of a middle frame, hex). Catches
    # near-duplicates the md5 misses: same clip re-uploaded under a
    # different media id, cropped, or re-encoded. NULL = pre-0015 row.
    phash: Mapped[str | None] = mapped_column(String(16), nullable=True, index=True)
    status: Mapped[VideoStatus] = mapped_column(Enum(VideoStatus), default=VideoStatus.uploaded)
    # Original caption from a video source (IG page ingest) — posted verbatim
    # when the firing schedule rule prefers source captions.
    source_caption: Mapped[str | None] = mapped_column(Text, nullable=True)

    effect_preset: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Name of the AudioTrack mixed in at processing time (resolved like effect_preset).
    audio_track: Mapped[str | None] = mapped_column(String(128), nullable=True)
    # Trial Reel: shown to non-followers first (explore engine). Falls back
    # to a regular reel automatically when IG rejects trial for the account.
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)
    trial_strategy: Mapped[str] = mapped_column(String(16), default="manual")
    custom_filters: Mapped[str | None] = mapped_column(Text, nullable=True)
    trim_start: Mapped[float | None] = mapped_column(Float, nullable=True)
    trim_end: Mapped[float | None] = mapped_column(Float, nullable=True)

    processed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    post: Mapped["Post | None"] = relationship(back_populates="video", uselist=False)


class Post(Base, TimestampMixin):
    __tablename__ = "posts"
    # One post row per schedule slot, ever: the grace window lets several
    # beat ticks see the same slot, so (account_id, slot_for) is unique.
    # SQLite/PG treat NULLs as distinct, so manual/API posts (slot_for NULL)
    # never conflict with each other or with scheduler posts.
    __table_args__ = (
        UniqueConstraint("account_id", "slot_for", name="uq_posts_account_slot"),
        # Mirrors migration 0016: without this, fresh create_all() DBs lack
        # the index that migrated DBs have (schema drift).
        Index("ix_posts_slot_for", "slot_for"),
    )

    video_id: Mapped[int] = mapped_column(ForeignKey("videos.id"), nullable=False)
    account_id: Mapped[int] = mapped_column(ForeignKey("accounts.id"), nullable=False)
    ig_media_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    ig_permalink: Mapped[str | None] = mapped_column(String(512), nullable=True)
    caption: Mapped[str] = mapped_column(Text, default="")
    hashtags: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[PostStatus] = mapped_column(Enum(PostStatus), default=PostStatus.scheduled)
    scheduled_for: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Schedule slot (aware UTC) this post was created for — the rule's
    # wall-clock minute in SCHEDULE_TZ. NULL for manual/API posts.
    # Powers the grace-window dedup: one slot → at most one post row.
    slot_for: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # When this post was handed to the slow lane (execute_post dispatched).
    # Powers the dispatch dedup: the per-minute tick atomically stamps only
    # unstamped rows, so a post waiting in the slow queue is never enqueued
    # twice. A stale stamp (older than dispatch_stale_minutes) means the
    # dispatch was lost (broker/queue hiccup) and the tick re-dispatches.
    dispatched_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    views_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    views_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes_24h: Mapped[int | None] = mapped_column(Integer, nullable=True)
    likes_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments_7d: Mapped[int | None] = mapped_column(Integer, nullable=True)
    engagement_rate: Mapped[float | None] = mapped_column(Float, nullable=True)
    last_analytics_check: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    fail_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    retry_count: Mapped[int] = mapped_column(Integer, default=0)
    is_trial: Mapped[bool] = mapped_column(Boolean, default=False)

    video: Mapped[Video] = relationship(back_populates="post")
    account: Mapped["Account"] = relationship(back_populates="posts")
