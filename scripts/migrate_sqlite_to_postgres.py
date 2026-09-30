#!/usr/bin/env python3
"""One-time SQLite -> PostgreSQL data migration for Instareel.

Copies every table (in FK-dependency order) from a SQLite app.db into a
PostgreSQL database whose schema was built by `alembic upgrade head`.

USAGE (one-time, on the server):
    The copy must run where the hostname `postgres` resolves, i.e. inside
    the compose network (the compose file publishes no PG port to the host).
    Stack stopped so nothing writes mid-copy:

    1. docker compose down
       docker compose --profile postgres up -d postgres
    2. docker compose run --rm -e DATABASE_URL='postgresql+asyncpg://igfunnel:<pw>@postgres:5432/igfunnel' \
           backend alembic upgrade head        # build the PG schema at head
    3. ./scripts/backup.sh                    # safety snapshot of the sqlite DB
    4. docker compose run --rm -v ./scripts:/scripts \
           -e DATABASE_URL='postgresql+psycopg2://igfunnel:<pw>@postgres:5432/igfunnel' \
           backend python3 /scripts/migrate_sqlite_to_postgres.py /data/app.db
       (or pass --pg-url explicitly; --help for all options;
        <pw> = POSTGRES_PASSWORD from .env)
    5. Point DATABASE_URL/SYNC_DATABASE_URL at Postgres in .env,
       docker compose up -d, and verify the dashboard.

SAFETY:
  * Refuses to run when the target PG database has ANY rows (unless --force).
  * Refuses when the target has no alembic_version at head (schema not built).
  * Never touches the source SQLite file (opens read-only).
  * Copies in batches inside ONE transaction for the whole run; any failure
    rolls everything back, leaving the target untouched (--no-verify still
    rolls back on copy errors).
  * After the copy, resets every `id` sequence to MAX(id) and re-counts all
    tables; any mismatch aborts.

ASSUMPTIONS (documented, verified against the models):
  * All DateTime columns are timezone-aware; the app always writes UTC, but
    SQLite stores them tz-naive. The copy reads via textual SQL, so values
    arrive as ISO strings (or naive datetimes); both are localized to UTC.
  * All enums are `str` enums; SQLite stores the value string, which Postgres
    native enums accept.
  * The copy reads via textual SQL, which bypasses the JSON type's result
    deserializer — SQLite hands back the raw JSON string, so JSON columns are
    explicitly json.loads()-decoded before insert (otherwise Postgres would
    store a double-encoded JSON string). BigInteger columns round-trip
    unchanged.

Requires: sqlalchemy, psycopg2-binary (both in backend/requirements.txt).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect, text  # noqa: E402


def _ensure_app_importable() -> None:
    """Put the backend package on sys.path.

    Repo checkout: this file is <repo>/scripts/..., code is <repo>/backend.
    Backend container (Dockerfile WORKDIR /code): code is /code/app and the
    image sets no PYTHONPATH, so plain `python3 /scripts/...` can't see it.
    First candidate that actually contains an `app/` package wins.
    """
    candidates = [
        Path(__file__).resolve().parent.parent / "backend",
        Path("/code"),
    ]
    for cand in candidates:
        if (cand / "app").is_dir():
            sys.path.insert(0, str(cand))
            return


_ensure_app_importable()

BATCH = 2000


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Copy Instareel data SQLite -> PostgreSQL.")
    p.add_argument("sqlite_path", help="Path to the source app.db (opened read-only)")
    p.add_argument(
        "--pg-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="Target Postgres URL (sync driver). Defaults to $DATABASE_URL; "
        "must use postgresql+psycopg2:// (not asyncpg).",
    )
    p.add_argument("--batch", type=int, default=BATCH, help="Rows per INSERT batch")
    p.add_argument("--force", action="store_true",
                   help="Allow copying into a non-empty target (DANGEROUS)")
    p.add_argument("--no-verify", action="store_true",
                   help="Skip the post-copy row-count verification")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    if not args.pg_url.startswith("postgresql+psycopg2://"):
        print("ERROR: --pg-url must be a SYNC url: postgresql+psycopg2://...", file=sys.stderr)
        return 2
    src_path = Path(args.sqlite_path)
    if not src_path.exists():
        print(f"ERROR: source not found: {src_path}", file=sys.stderr)
        return 2

    # Import here so the script fails fast with a clear message when run
    # outside the project venv. NOTE: app.database builds its engines at
    # import time from settings.DATABASE_URL — which this script never uses
    # (it builds its own sqlite/psycopg2 engines). Point it at a dummy async
    # URL first so a sync pg URL passed via the environment can't break the
    # import ("The asyncio extension requires an async driver").
    os.environ["DATABASE_URL"] = "sqlite+aiosqlite:////tmp/igfunnel-migrate-dummy.db"
    try:
        import app.models  # noqa: F401,E402 — registers all tables on Base.metadata
        from app.database import Base  # noqa: E402
        from app.core.db_gate import code_heads  # noqa: E402
    except ImportError as e:
        print(f"ERROR: cannot import app models ({e}); run with the project venv.", file=sys.stderr)
        return 2

    src = create_engine(f"sqlite:///file:{src_path}?mode=ro&uri=true")
    dst = create_engine(args.pg_url)

    expected_heads = code_heads()

    with dst.connect() as c:
        insp = inspect(c)
        if not insp.has_table("alembic_version"):
            print("ERROR: target has no alembic_version table — run "
                  "`alembic upgrade head` against Postgres first.", file=sys.stderr)
            return 2
        target_heads = {r[0] for r in c.execute(text("SELECT version_num FROM alembic_version"))}
        if target_heads != expected_heads:
            print(f"ERROR: target is at {sorted(target_heads)}, code expects "
                  f"{sorted(expected_heads)} — upgrade the PG schema first.", file=sys.stderr)
            return 2
        tables = [t for t in Base.metadata.sorted_tables if t.name != "alembic_version"]
        non_empty = []
        pre_counts: dict[str, int] = {}
        for t in tables:
            if insp.has_table(t.name):
                n = c.execute(text(f'SELECT COUNT(*) FROM "{t.name}"')).scalar()
                pre_counts[t.name] = n
                if n:
                    non_empty.append((t.name, n))
        if non_empty and not args.force:
            print("ERROR: target already has data — refusing to copy:", file=sys.stderr)
            for name, n in non_empty:
                print(f"  {name}: {n} rows", file=sys.stderr)
            print("Re-run with --force only if you know what you are doing.", file=sys.stderr)
            return 2

    # Column roles per table, resolved once from the metadata.
    tz_cols: dict[str, list[str]] = {}
    json_cols: dict[str, list[str]] = {}
    for t in tables:
        tz_cols[t.name] = [
            col.name for col in t.columns
            if col.type.__class__.__name__ == "DateTime" and getattr(col.type, "timezone", False)
        ]
        # The copy reads via textual SQL, which bypasses the JSON type's
        # result deserializer: SQLite hands back the raw JSON string. Decode
        # it here, otherwise Postgres stores a double-encoded JSON string.
        json_cols[t.name] = [
            col.name for col in t.columns
            if col.type.__class__.__name__ == "JSON"
        ]

    total = 0
    counts: dict[str, int] = {}
    with src.connect() as s, dst.connect() as d:
        trans = d.begin()
        try:
            for t in tables:
                n = copy_table(s, d, t, tz_cols[t.name], json_cols[t.name], args.batch)
                counts[t.name] = n
                total += n
                print(f"  {t.name}: {n} rows")
            reset_sequences(d, tables)
            if not args.no_verify:
                verify_counts(s, d, counts, pre_counts)
            trans.commit()
        except Exception:
            trans.rollback()
            print("ABORTED: transaction rolled back, target untouched.", file=sys.stderr)
            raise
    print(f"DONE: {total} rows copied across {len(tables)} tables; sequences reset.")
    return 0


def copy_table(src_conn, dst_conn, table, tz_columns: list[str],
               json_columns: list[str], batch: int) -> int:
    cols = [c.name for c in table.columns]
    collist = ", ".join(f'"{c}"' for c in cols)
    n = 0
    offset = 0
    while True:
        rows = src_conn.execute(
            text(f'SELECT {collist} FROM "{table.name}" LIMIT :b OFFSET :o'),
            {"b": batch, "o": offset},
        ).mappings().all()
        if not rows:
            break
        payload = []
        for r in rows:
            d = dict(r)
            for c in tz_columns:
                v = d[c]
                # Textual SELECT bypasses the DateTime result deserializer, so
                # SQLite hands back the stored ISO string, not a datetime.
                if isinstance(v, str):
                    v = dt.datetime.fromisoformat(v)
                if isinstance(v, dt.datetime):
                    if v.tzinfo is None:
                        v = v.replace(tzinfo=dt.timezone.utc)
                    d[c] = v
            for c in json_columns:
                v = d[c]
                if isinstance(v, str):
                    d[c] = json.loads(v)
            payload.append(d)
        dst_conn.execute(table.insert(), payload)
        n += len(payload)
        offset += batch
    return n


def reset_sequences(dst_conn, tables) -> None:
    """Point every `id` sequence at MAX(id) so new inserts don't collide.

    Empty tables get setval(seq, 1, false) so the first nextval() returns 1
    (setval(seq, 0) is out of bounds for int4/int8 sequences on Postgres).
    """
    for t in tables:
        pk = list(t.primary_key.columns)
        if len(pk) == 1 and pk[0].name == "id" and pk[0].type.__class__.__name__ in ("Integer", "BigInteger"):
            dst_conn.execute(text(
                'SELECT setval(pg_get_serial_sequence(:tbl, \'id\'), '
                'COALESCE((SELECT MAX(id) FROM "{}"), 1), '
                '(SELECT COUNT(*) FROM "{}") > 0)'.format(t.name, t.name)
            ), {"tbl": t.name})


def verify_counts(src_conn, dst_conn, counts: dict[str, int],
                  pre_counts: dict[str, int] | None = None) -> None:
    """Every table must hold exactly (pre-existing rows + copied rows).

    pre_counts covers --force runs against a non-empty target; without it a
    legitimately pre-seeded table would always fail verification.
    """
    pre_counts = pre_counts or {}
    bad = []
    for name, expected in counts.items():
        got = dst_conn.execute(text(f'SELECT COUNT(*) FROM "{name}"')).scalar()
        want = pre_counts.get(name, 0) + expected
        if got != want:
            bad.append((name, want, got))
    if bad:
        raise RuntimeError(f"row-count mismatch after copy: {bad}")
    print(f"  verified: {len(counts)} tables, counts match")


if __name__ == "__main__":
    sys.exit(main())
