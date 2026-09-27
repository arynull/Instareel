"""process_video single-flight lock (ownership tokens) + idempotent claim.

Hermetic: Redis is faked (SET NX / Lua compare-and-delete+refresh /
EXISTS), the DB is temp-file SQLite.
"""
import pytest

from app.models import Video, VideoStatus
from app.tasks import video_tasks
from app.tasks.video_tasks import (
    _acquire_process_lock,
    _claim_for_processing,
    _ensure_disk_space,
    _refresh_process_lock,
    _release_process_lock,
    process_lock_held,
)


class _FakeRedis:
    """SET NX EX / EXISTS plus the two compare-and-X Lua scripts."""

    def __init__(self):
        self.keys = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def exists(self, key):
        return 1 if key in self.keys else 0

    def eval(self, script, numkeys, key, *args):
        owned = self.keys.get(key) == args[0]
        if not owned:
            return 0
        if "expire" in script:
            return 1  # TTL bookkeeping omitted — presence is the lock
        del self.keys[key]
        return 1

    def delete(self, key):
        return self.keys.pop(key, None) is not None


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(video_tasks, "_redis_client", lambda: fake)
    return fake


@pytest.fixture()
def db(monkeypatch, tmp_path):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    import app.database as db_module
    import app.models  # noqa: F401 — register tables before create_all
    from app.database import Base

    engine = create_engine(f"sqlite:///{tmp_path}/v.db")
    Base.metadata.create_all(engine)
    maker = sessionmaker(bind=engine, expire_on_commit=False)
    monkeypatch.setattr(db_module, "SyncSessionLocal", maker)
    return maker


def _make_video(maker, status):
    with maker() as s:
        v = Video(
            original_filename="t.mp4",
            raw_path="/tmp/raw-t.mp4",
            md5_hash="d" + str(status),
            status=status,
        )
        s.add(v)
        s.commit()
        return v.id


def _status_of(maker, vid):
    with maker() as s:
        return s.get(Video, vid).status


# ---- lock ownership ----


def test_process_lock_single_flight(fake_redis):
    token, verified = _acquire_process_lock(42)
    assert token and verified
    assert _acquire_process_lock(42) == (None, True)  # second run: locked out
    token3, _ = _acquire_process_lock(43)
    assert token3  # other video unaffected
    _release_process_lock(42, token)
    token4, _ = _acquire_process_lock(42)
    assert token4 and token4 != token


def test_lock_release_needs_ownership_token(fake_redis):
    """A run must never release a lock it doesn't own (stale blind DELETE)."""
    token, _ = _acquire_process_lock(42)
    _release_process_lock(42, "wrong-token")
    assert _acquire_process_lock(42) == (None, True)  # still held
    assert process_lock_held(42) is True
    _release_process_lock(42, token)
    assert process_lock_held(42) is False


def test_lock_refresh_keeps_ownership(fake_redis):
    token, _ = _acquire_process_lock(42)
    assert _refresh_process_lock(42, token) is True
    assert _refresh_process_lock(42, "wrong-token") is False


def test_acquire_fail_open_when_redis_down(monkeypatch):
    class _Down:
        def set(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(video_tasks, "_redis_client", lambda: _Down())
    token, verified = _acquire_process_lock(42)
    assert token and verified is False


# ---- idempotent claim ----


def test_claim_uploaded_moves_to_processing(db, fake_redis):
    vid = _make_video(db, VideoStatus.uploaded)
    assert _claim_for_processing(vid, True) == "process"
    assert _status_of(db, vid) == VideoStatus.processing


def test_claim_processed_skips(db, fake_redis):
    vid = _make_video(db, VideoStatus.processed)
    assert _claim_for_processing(vid, True) == "skip_processed"
    assert _status_of(db, vid) == VideoStatus.processed


def test_claim_stale_processing_reclaimed_with_verified_lock(db, fake_redis):
    """Previous holder died after claiming (no lock key): reclaim."""
    vid = _make_video(db, VideoStatus.processing)
    assert _claim_for_processing(vid, True) == "process"


def test_claim_processing_not_stolen_without_verified_lock(db, monkeypatch):
    """Fail-open mode: a processing row may belong to a live run — skip."""

    class _Down:
        def set(self, *a, **k):
            raise ConnectionError("down")

    monkeypatch.setattr(video_tasks, "_redis_client", lambda: _Down())
    vid = _make_video(db, VideoStatus.processing)
    token, verified = _acquire_process_lock(vid)
    assert verified is False
    assert _claim_for_processing(vid, verified) == "skip_active"
    assert _status_of(db, vid) == VideoStatus.processing


def test_claim_gone_video(db, fake_redis):
    assert _claim_for_processing(999999, True) == "gone"


def test_crash_then_reacquire_reclaims(db, fake_redis):
    """Worker died mid-run: the lock TTL expired (key dropped), status is
    stuck at processing. The next run re-acquires and reclaims."""
    vid = _make_video(db, VideoStatus.uploaded)
    token, _ = _acquire_process_lock(vid)
    assert _claim_for_processing(vid, True) == "process"
    # ...crash: thread dead, TTL expired, lock gone, DB row stuck...
    fake_redis.keys.pop(f"lock:process_video:{vid}")
    assert process_lock_held(vid) is False
    token2, verified2 = _acquire_process_lock(vid)
    assert token2 and verified2
    assert _claim_for_processing(vid, True) == "process"


# ---- task-level ----


def test_duplicate_task_skips_without_touching_db(monkeypatch):
    """A task that loses the lock race returns 'skipped' before any DB/FFmpeg work."""
    monkeypatch.setattr(video_tasks, "_acquire_process_lock", lambda vid: (None, True))
    result = video_tasks.process_video_task.run(999)
    assert result["status"] == "skipped"
    assert result["reason"] == "already processing"


def test_completed_video_duplicate_task_skips(db, fake_redis, monkeypatch):
    """A task queued twice that runs after completion must NOT transcode again."""
    vid = _make_video(db, VideoStatus.processed)

    def _boom(*a, **k):
        raise AssertionError("FFmpeg must not run for an already-processed video")

    monkeypatch.setattr("app.services.video_processor.process_video_sync", _boom)
    result = video_tasks.process_video_task.run(vid)
    assert result["status"] == "skipped"
    assert result["reason"] == "already processed"


def test_stale_claim_task_processes(db, fake_redis, monkeypatch):
    """End-to-end: stale processing claim + expired lock → task reclaims and runs."""
    vid = _make_video(db, VideoStatus.processing)
    calls = []

    def _fake_process(vid_arg, *a, **k):
        calls.append(vid_arg)
        # Mimic the real pipeline's DB commit.
        with db() as s:
            s.get(Video, vid_arg).status = VideoStatus.processed
            s.commit()

    monkeypatch.setattr(
        "app.services.video_processor.process_video_sync", _fake_process
    )
    monkeypatch.setattr("app.tasks.sync_helpers.resolve_audio", lambda s, name: None)
    monkeypatch.setattr("app.tasks.sync_helpers.pick_audio", lambda s: None)
    monkeypatch.setattr("app.tasks.sync_helpers.publish_sync", lambda *a, **k: None)
    monkeypatch.setattr("app.tasks.sync_helpers.log_event_sync", lambda *a, **k: None)
    monkeypatch.setattr(video_tasks, "_ensure_disk_space", lambda p: None)

    result = video_tasks.process_video_task.run(vid)
    assert result["status"] == "processed"
    assert calls, "the FFmpeg pipeline should have run"
    assert _status_of(db, vid) == VideoStatus.processed
    # Lock released with ownership: re-acquirable afterwards.
    token, _ = _acquire_process_lock(vid)
    assert token


def test_lock_released_on_failure(monkeypatch, fake_redis):
    """Even when processing blows up, the lock is released (finally).

    The DB failure hits in the claim block, before FFmpeg —
    no need to stub the processor itself.
    """
    monkeypatch.setattr("app.database.SyncSessionLocal", lambda: _DeadSession())
    result = video_tasks.process_video_task.run(77)
    assert result["status"] == "failed"
    token, _ = _acquire_process_lock(77)
    assert token  # lock was released


def test_failing_run_does_not_release_foreign_lock(monkeypatch, fake_redis, db):
    """A crashing run must not release a *different* run's lock.

    Run A acquires; the key is then taken over by run B (simulating TTL
    expiry + re-acquire). A's finally, using its own token, must leave
    B's lock alone.
    """
    vid = _make_video(db, VideoStatus.uploaded)
    token_a, _ = _acquire_process_lock(vid)
    # B takes over (TTL of A's lock "expired")
    fake_redis.keys[f"lock:process_video:{vid}"] = "token-b"
    _release_process_lock(vid, token_a)  # A's finally with its own token
    assert fake_redis.keys[f"lock:process_video:{vid}"] == "token-b"


class _DeadSession:
    """Session factory whose sessions raise on use — proves the failure path
    copes (mark() swallows DB errors)."""

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, *a, **k):
        raise RuntimeError("db down")

    def execute(self, *a, **k):
        raise RuntimeError("db down")


def test_ensure_disk_space_ok(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path))
    # /tmp here is a small tmpfs — shrink the floor for the test.
    monkeypatch.setattr(video_tasks, "_MIN_FREE_DISK_BYTES", 0)
    f = tmp_path / "in.mp4"
    f.write_bytes(b"x" * 1024)
    _ensure_disk_space(str(f))  # must not raise


def test_ensure_disk_space_refuses(tmp_path, monkeypatch):
    from app.config import settings

    monkeypatch.setattr(settings, "MEDIA_ROOT", str(tmp_path))
    monkeypatch.setattr("shutil.disk_usage", lambda p: _Usage(0))
    f = tmp_path / "in.mp4"
    f.write_bytes(b"x")
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        _ensure_disk_space(str(f))


class _Usage:
    def __init__(self, free):
        self.free = free
