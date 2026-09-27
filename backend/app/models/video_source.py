"""Video sources: ingest reels from IG pages (listing + download + process)."""
import datetime as dt
import enum

from sqlalchemy import Boolean, DateTime, Enum, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin


class SourceStatus(str, enum.Enum):
    idle = "idle"           # never ran, or stopped/finished and ready again
    running = "running"     # celery task active
    stopping = "stopping"   # stop requested; task finishes current item then parks
    completed = "completed" # last run hit the end (cursor exhausted or max reached)
    failed = "failed"       # last run aborted (auth/challenge/guard) — resumable


class SourceItemStatus(str, enum.Enum):
    pending = "pending"         # listed, not attempted
    downloading = "downloading" # task working on it now
    downloaded = "downloaded"   # Video row created (processing continues async)
    skipped = "skipped"         # photo/album/duplicate — with reason in error
    failed = "failed"           # download/validation failed — retryable


class VideoSource(Base, TimestampMixin):
    __tablename__ = "video_sources"

    # The IG page to ingest (username without @).
    username: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    # Which of OUR accounts' sessions downloads (NULL = auto: first usable).
    account_id: Mapped[int | None] = mapped_column(ForeignKey("accounts.id"), nullable=True)
    status: Mapped[SourceStatus] = mapped_column(Enum(SourceStatus), default=SourceStatus.idle)

    max_items: Mapped[int] = mapped_column(Integer, default=20)
    reels_only: Mapped[bool] = mapped_column(Boolean, default=True)
    with_covers: Mapped[bool] = mapped_column(Boolean, default=True)
    auto_process: Mapped[bool] = mapped_column(Boolean, default=True)
    delay_min_s: Mapped[float] = mapped_column(default=8.0)
    delay_max_s: Mapped[float] = mapped_column(default=20.0)

    # Pagination resume cursor (None = start from newest).
    end_cursor: Mapped[str | None] = mapped_column(String(512), nullable=True)
    fetched: Mapped[int] = mapped_column(Integer, default=0)
    downloaded: Mapped[int] = mapped_column(Integer, default=0)
    skipped: Mapped[int] = mapped_column(Integer, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    current_stage: Mapped[str | None] = mapped_column(String(256), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    items: Mapped[list["SourceItem"]] = relationship(
        back_populates="source", cascade="all, delete-orphan"
    )

    account: Mapped["Account | None"] = relationship(back_populates="video_sources")


class SourceItem(Base, TimestampMixin):
    __tablename__ = "source_items"
    __table_args__ = (UniqueConstraint("source_id", "media_pk", name="uq_source_item"),)

    source_id: Mapped[int] = mapped_column(ForeignKey("video_sources.id"), nullable=False, index=True)
    # IG media pk — unique per source so resume never re-downloads.
    media_pk: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    shortcode: Mapped[str | None] = mapped_column(String(64), nullable=True)
    media_type: Mapped[str | None] = mapped_column(String(16), nullable=True)
    status: Mapped[SourceItemStatus] = mapped_column(Enum(SourceItemStatus), default=SourceItemStatus.pending)
    video_id: Mapped[int | None] = mapped_column(ForeignKey("videos.id"), nullable=True)
    error: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    source: Mapped[VideoSource] = relationship(back_populates="items")
