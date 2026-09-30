"""Unit tests for scripts/migrate_sqlite_to_postgres.py helpers.

The script itself is exercised end-to-end against real PostgreSQL manually;
these tests pin the subtle behaviors that broke in practice:

  * JSON columns: copy_table reads via textual SQL, which bypasses the JSON
    type's result deserializer — SQLite hands back the raw JSON string. The
    helper must json.loads()-decode it, otherwise the target stores a
    double-encoded JSON string (regression seen on real PG: details became
    '"{\\"a\\": ...}"' and details->'a' returned NULL).
"""
import importlib.util
import os

from sqlalchemy import Column, Integer, JSON, MetaData, Table, create_engine

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
SCRIPT = os.path.join(REPO, "scripts", "migrate_sqlite_to_postgres.py")


def _load_script():
    spec = importlib.util.spec_from_file_location("pg_data_migrate", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _tables():
    src_meta, dst_meta = MetaData(), MetaData()
    cols = lambda m: [
        Column("id", Integer, primary_key=True),
        Column("payload", JSON, nullable=True),
    ]
    src = Table("t", src_meta, *cols(src_meta))
    dst = Table("t", dst_meta, *cols(dst_meta))
    return src, dst


def test_copy_table_decodes_json_text():
    mod = _load_script()
    src_t, dst_t = _tables()
    se, de = create_engine("sqlite://"), create_engine("sqlite://")
    src_t.metadata.create_all(se)
    dst_t.metadata.create_all(de)
    with se.begin() as c:
        c.execute(src_t.insert(), {"id": 1, "payload": {"a": [1, 2, {"b": "c"}]}})
        c.execute(src_t.insert(), {"id": 2, "payload": None})

    with se.connect() as s, de.connect() as d:
        n = mod.copy_table(s, d, dst_t, [], ["payload"], 100)
        d.commit()
    assert n == 2

    with de.connect() as d:
        rows = {
            r["id"]: r["payload"]
            for r in d.execute(dst_t.select()).mappings().all()
        }
    # Real object, not a double-encoded string; NULL stays NULL.
    assert rows[1] == {"a": [1, 2, {"b": "c"}]}
    assert isinstance(rows[1], dict)
    assert rows[2] is None


def test_copy_table_localizes_naive_datetimes():
    """Naive SQLite timestamps are the app's UTC wall-clock; the payload
    sent to Postgres must carry tzinfo=UTC, otherwise timestamptz would
    interpret it in the server's timezone."""
    import datetime as dt
    from unittest.mock import patch

    from sqlalchemy import DateTime

    mod = _load_script()
    src_meta, dst_meta = MetaData(), MetaData()
    src = Table(
        "t", src_meta,
        Column("id", Integer, primary_key=True),
        Column("ts", DateTime(timezone=True)),
    )
    dst = Table(
        "t", dst_meta,
        Column("id", Integer, primary_key=True),
        Column("ts", DateTime(timezone=True)),
    )
    se, de = create_engine("sqlite://"), create_engine("sqlite://")
    src.metadata.create_all(se)
    dst.metadata.create_all(de)
    naive = dt.datetime(2026, 9, 30, 12, 0, 0)
    with se.begin() as c:
        c.execute(src.insert().values(id=1, ts=naive))

    sent = []
    with se.connect() as s, de.connect() as d:
        orig_execute = d.execute

        def spy(stmt, params=None, *a, **k):
            if params:
                sent.extend(params if isinstance(params, list) else [params])
            return orig_execute(stmt, params, *a, **k)

        with patch.object(d, "execute", spy):
            n = mod.copy_table(s, d, dst, ["ts"], [], 100)
        d.commit()

    assert n == 1
    ts_val = sent[0]["ts"]
    assert isinstance(ts_val, dt.datetime)
    assert ts_val.tzinfo == dt.timezone.utc
    assert ts_val.replace(tzinfo=None) == naive
