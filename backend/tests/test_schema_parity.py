"""Schema parity: create_all() must produce what the migrations produce.

Migration 0016 created ix_posts_slot_for; the model metadata must declare
the same index, otherwise fresh installs (create_all) drift from migrated
DBs and slot_for lookups lose their index.
"""
from app.database import Base
from app.models import Post  # noqa: F401  (registers the model)


def test_posts_slot_for_index_in_metadata():
    idx_names = {i.name for i in Post.__table__.indexes}
    assert "ix_posts_slot_for" in idx_names
    cols = next(i for i in Post.__table__.indexes if i.name == "ix_posts_slot_for").columns
    assert [c.name for c in cols] == ["slot_for"]


def test_create_all_emits_slot_for_index():
    from sqlalchemy import create_engine, inspect

    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    names = [i["name"] for i in inspect(engine).get_indexes("posts")]
    assert "ix_posts_slot_for" in names
