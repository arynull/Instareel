"""Schedule grace window: track which rule slot each post was created for.

- posts.slot_for: the rule's wall-clock slot minute (aware UTC), NULL for
  manual/API posts. Lets the scheduler prove a slot already fired, so the
  grace window can't queue a second post for the same slot.
- uq_posts_account_slot: one post row per (account, slot), ever. Backstop for
  two beat ticks racing on the same slot — the loser gets IntegrityError and
  the task skips gracefully instead of double-posting.

Additive + nullable: old code reading the table keeps working, and every
existing row has slot_for NULL (NULLs never conflict in the unique index).
REQUIRED on existing DBs: `alembic upgrade head` (startup create_all only
creates missing tables, never new columns).
"""
revision = "0016_post_slot_for"
down_revision = "0015_video_phash"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # Batch mode: SQLite can't ALTER constraints in place, so the table is
    # rebuilt via copy-and-move (safe with existing rows — the column is
    # nullable and existing rows get NULL, which never conflicts).
    with op.batch_alter_table("posts") as batch:
        batch.add_column(sa.Column("slot_for", sa.DateTime(timezone=True), nullable=True))
        batch.create_index("ix_posts_slot_for", ["slot_for"])
        batch.create_unique_constraint("uq_posts_account_slot", ["account_id", "slot_for"])


def downgrade() -> None:
    # SQLite can't drop constraints/indexes in place — batch mode rebuilds.
    with op.batch_alter_table("posts") as batch:
        batch.drop_constraint("uq_posts_account_slot", type_="unique")
        batch.drop_index("ix_posts_slot_for")
        batch.drop_column("slot_for")
