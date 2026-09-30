"""Per-rule slot identity: two rules may share the same slot.

- posts.rule_id: nullable FK to schedule_rules.id — which rule created this
  post (NULL for manual/API posts and rows created before this migration).
- uq_posts_account_slot is now (account_id, slot_for, rule_id): the grace
  window's exactly-once guarantee becomes per rule, so two rules at the same
  minute (e.g. two 04:30 rules on one account) each fire their own post
  instead of the second being silently swallowed as "already fired". The
  second-resolution fire jitter spreads their actual post times apart.

Additive + nullable: existing rows get rule_id NULL; SQLite/PG treat NULLs
as distinct in the unique index, so legacy rows never conflict with each
other or with new rows. Legacy NULL rows still count as "fired" for every
rule at that account+slot (see slot_already_fired), preserving the old
account-wide exactly-once behavior for pre-upgrade slots.
REQUIRED on existing DBs: `alembic upgrade head` (startup create_all only
creates missing tables, never new columns).
"""
revision = "0019_post_rule_id"
down_revision = "0018_post_dispatched_at"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa


def upgrade() -> None:
    # Batch mode: SQLite can't ALTER constraints in place, so the table is
    # rebuilt via copy-and-move (safe with existing rows — the column is
    # nullable and existing rows get NULL, which never conflicts).
    # NOTE: the FK needs an explicit name — batch mode rejects the unnamed
    # inline ForeignKey() form with "Constraint must have a name".
    with op.batch_alter_table("posts") as batch:
        batch.add_column(sa.Column("rule_id", sa.Integer(), nullable=True))
        batch.create_foreign_key("fk_posts_rule_id", "schedule_rules", ["rule_id"], ["id"])
        batch.drop_constraint("uq_posts_account_slot", type_="unique")
        batch.create_unique_constraint("uq_posts_account_slot", ["account_id", "slot_for", "rule_id"])


def downgrade() -> None:
    # SQLite can't drop constraints in place — batch mode rebuilds.
    with op.batch_alter_table("posts") as batch:
        batch.drop_constraint("uq_posts_account_slot", type_="unique")
        batch.drop_constraint("fk_posts_rule_id", type_="foreignkey")
        batch.drop_column("rule_id")
        batch.create_unique_constraint("uq_posts_account_slot", ["account_id", "slot_for"])
