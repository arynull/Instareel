"""IG account + proxy models."""
import datetime as dt
import enum

from sqlalchemy import DateTime, Enum, ForeignKey, Integer, String, Text, Boolean
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.models.base import TimestampMixin


class AccountStatus(str, enum.Enum):
    active = "active"
    cooldown = "cooldown"
    banned = "banned"
    challenge_required = "challenge_required"
    disabled = "disabled"


class ProxyProtocol(str, enum.Enum):
    http = "http"
    socks5 = "socks5"
    socks4 = "socks4"


class Proxy(Base, TimestampMixin):
    __tablename__ = "proxies"

    url: Mapped[str] = mapped_column(String(512), nullable=False)
    protocol: Mapped[ProxyProtocol] = mapped_column(Enum(ProxyProtocol), default=ProxyProtocol.http)
    username: Mapped[str | None] = mapped_column(String(255), nullable=True)
    password_enc: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    country: Mapped[str | None] = mapped_column(String(8), nullable=True)
    is_healthy: Mapped[bool] = mapped_column(Boolean, default=True)
    last_checked: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # Origin: "manual" for hand-added/imported rows, else the ProxySource
    # name. Only auto rows are ever purged; manual rows are immortal.
    source: Mapped[str | None] = mapped_column(String(128), nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    accounts: Mapped[list["Account"]] = relationship(back_populates="proxy")


class Account(Base, TimestampMixin):
    __tablename__ = "accounts"

    username: Mapped[str] = mapped_column(String(128), unique=True, nullable=False, index=True)
    # Stable Instagram numeric user id (ds_user_id). Usernames change; this
    # never does — used to verify uploaded sessions and auto-adopt renames.
    ig_user_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    password_enc: Mapped[str] = mapped_column(String(1024), nullable=False)
    proxy_id: Mapped[int | None] = mapped_column(ForeignKey("proxies.id"), nullable=True)
    status: Mapped[AccountStatus] = mapped_column(Enum(AccountStatus), default=AccountStatus.active)
    session_file_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    last_login: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_post: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    posts_today: Mapped[int] = mapped_column(Integer, default=0)
    max_daily_posts: Mapped[int] = mapped_column(Integer, default=3)
    cooldown_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    total_posts: Mapped[int] = mapped_column(Integer, default=0)
    total_views: Mapped[int] = mapped_column(Integer, default=0)
    total_likes: Mapped[int] = mapped_column(Integer, default=0)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    proxy: Mapped[Proxy | None] = relationship(back_populates="accounts")
    posts: Mapped[list["Post"]] = relationship(back_populates="account", cascade="all, delete-orphan")
    bio_configs: Mapped[list["BioConfig"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    # M6: deleting an account removes its schedule rules and video sources
    # instead of orphaning them (with FK enforcement on, the DB would refuse
    # the delete otherwise). Source items cascade via VideoSource.items.
    schedule_rules: Mapped[list["ScheduleRule"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
    video_sources: Mapped[list["VideoSource"]] = relationship(
        back_populates="account", cascade="all, delete-orphan"
    )
