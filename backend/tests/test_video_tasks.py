"""m8: process_video single-flight lock + pre-flight disk-space check."""
import os

import pytest

from app.tasks import video_tasks
from app.tasks.video_tasks import (
    _acquire_process_lock,
    _ensure_disk_space,
    _release_process_lock,
)


class _FakeRedis:
    """Minimal SET NX EX / DELETE semantics."""

    def __init__(self):
        self.keys = {}

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.keys:
            return None
        self.keys[key] = value
        return True

    def delete(self, key):
        return self.keys.pop(key, None) is not None


@pytest.fixture()
def fake_redis(monkeypatch):
    fake = _FakeRedis()
    monkeypatch.setattr(video_tasks, "_redis_client", lambda: fake)
    return fake


def test_process_lock_single_flight(fake_redis):
    assert _acquire_process_lock(42) is True
    assert _acquire_process_lock(42) is False  # second run: locked out
    assert _acquire_process_lock(43) is True  # other video unaffected
    _release_process_lock(42)
    assert _acquire_process_lock(42) is True


def test_duplicate_task_skips_without_touching_db(monkeypatch):
    """A task that loses the lock race returns 'skipped' before any DB/FFmpeg work."""
    monkeypatch.setattr(video_tasks, "_acquire_process_lock", lambda vid: False)
    result = video_tasks.process_video_task.run(999)
    assert result["status"] == "skipped"
    assert result["reason"] == "already processing"


def test_lock_released_on_failure(monkeypatch, fake_redis):
    """Even when processing blows up, the lock is released (finally).

    The DB failure hits in the audio-resolution block, before FFmpeg —
    no need to stub the processor itself."""
    monkeypatch.setattr("app.database.SyncSessionLocal",
                        lambda: _DeadSession())
    result = video_tasks.process_video_task.run(77)
    assert result["status"] == "failed"
    assert _acquire_process_lock(77) is True  # lock was released


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

    def commit(self):
        raise RuntimeError("db down")


def test_ensure_disk_space_ok(monkeypatch, tmp_path):
    import shutil

    f = tmp_path / "raw.mp4"
    f.write_bytes(b"x" * 1024)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _Usage(free=10 * 1024**3))
    _ensure_disk_space(str(f))  # no raise


def test_ensure_disk_space_floor(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _Usage(free=100 * 1024**2))  # 100 MiB < 1 GiB floor
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        _ensure_disk_space(None)


def test_ensure_disk_space_scales_with_input(monkeypatch, tmp_path):
    import shutil

    f = tmp_path / "raw.mp4"
    f.write_bytes(b"x" * 1024)
    # Pretend the file is 4 GiB: need 8 GiB, have 5 GiB -> raise.
    monkeypatch.setattr(os.path, "getsize", lambda p: 4 * 1024**3)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: _Usage(free=5 * 1024**3))
    with pytest.raises(RuntimeError, match="Insufficient disk space"):
        _ensure_disk_space(str(f))


class _Usage:
    def __init__(self, free):
        self.free = free
