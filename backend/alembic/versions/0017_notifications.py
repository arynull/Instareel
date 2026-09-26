"""Dashboard notifications: user-facing events with read state.

New table `notifications` (separate from append-only `system_logs`):
- ntype / severity / title / message / link for the bell panel
- dedup_key: recurring conditions (e.g. "beat down") notify once while
  unread instead of spamming every watchdog tick
- read_at: NULL = unread

Additive: no changes to existing tables.
"""

from alembic import op
import sqlalchemy as sa

revision = "0017_notifications"
down_revision = "0016_post_slot_for"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "notifications",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("ntype", sa.String(length=64), nullable=False),
        sa.Column("severity", sa.Enum("info", "success", "warning", "critical", name="notificationseverity"), nullable=False, server_default="info"),
        sa.Column("title", sa.String(length=256), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=512), nullable=True),
        sa.Column("dedup_key", sa.String(length=128), nullable=True),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_notifications_ntype", "notifications", ["ntype"])
    op.create_index("ix_notifications_dedup_key", "notifications", ["dedup_key"])
    op.create_index("ix_notifications_read_at", "notifications", ["read_at"])


def downgrade() -> None:
    op.drop_index("ix_notifications_read_at", table_name="notifications")
    op.drop_index("ix_notifications_dedup_key", table_name="notifications")
    op.drop_index("ix_notifications_ntype", table_name="notifications")
    op.drop_table("notifications")
    sa.Enum(name="notificationseverity").drop(op.get_bind(), checkfirst=True)
