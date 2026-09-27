"""Dispatch dedup: track when a post was handed to the slow lane.

- posts.dispatched_at: aware UTC timestamp of the last execute_post dispatch
  (NULL = never dispatched). The per-minute scheduler tick atomically stamps
  only unstamped (or stale-stamped) due rows via UPDATE ... WHERE, so a post
  sitting in the slow queue is never enqueued a second time by the next tick.
  A stale stamp (older than dispatch_stale_minutes) means the dispatch was
  lost (broker/queue hiccup) and the tick re-dispatches it instead of losing
  the post.

Additive + nullable: old code reading the table keeps working, and every
existing row has dispatched_at NULL (treated as "never dispatched", i.e. the
next tick will dispatch it exactly once — same as the old behavior).
REQUIRED on existing DBs: `alembic upgrade head` (startup create_all only
creates missing tables, never new columns).
"""
revision = "0018_post_dispatched_at"
down_revision = "0017_notifications"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # Batch mode: SQLite can't ALTER tables freely, so the table is rebuilt
    # via copy-and-move (safe with existing rows — the column is nullable).
    with op.batch_alter_table("posts") as batch:
        batch.add_column(sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_posts_dispatched_at", ["dispatched_at"])


def downgrade() -> None:
    with op.batch_alter_table("posts") as batch:
        batch.drop_index("ix_posts_dispatched_at")
        batch.drop_column("dispatched_at")
