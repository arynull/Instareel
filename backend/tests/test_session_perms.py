"""Session-file permission hardening (m7 follow-up).

dump_session_settings() chmods at write time, but files already on disk
(from before m7, or any other writer) stay loose until something fixes
them. harden_session_dir() runs at API startup (lifespan) and worker ready.
"""
import os
import stat

from app.utils.instagram_helpers import harden_session_dir


def _mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_hardens_existing_loose_files(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    loose = d / "user1.json"
    loose.write_text("{}")
    os.chmod(loose, 0o644)
    os.chmod(d, 0o755)

    harden_session_dir(str(tmp_path))

    assert _mode(d) == 0o700
    assert _mode(str(loose)) == 0o600


def test_already_tight_files_untouched_and_idempotent(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    f = d / "user2.json"
    f.write_text("{}")
    os.chmod(f, 0o600)

    harden_session_dir(str(tmp_path))
    harden_session_dir(str(tmp_path))  # second run: no-op, no error

    assert _mode(str(f)) == 0o600
    assert _mode(d) == 0o700


def test_missing_dir_does_not_raise(tmp_path):
    harden_session_dir(str(tmp_path / "does-not-exist"))  # no sessions/ at all


def test_symlink_not_followed(tmp_path):
    d = tmp_path / "sessions"
    d.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    os.chmod(outside, 0o644)
    os.symlink(outside, d / "link.json")

    harden_session_dir(str(tmp_path))

    # The link itself was skipped (never chmod through it onto another file).
    assert _mode(str(outside)) == 0o644
