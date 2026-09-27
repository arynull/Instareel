"""instagrapi wrapper: login w/ session reuse, reel upload, bio edit, media info."""
import asyncio
import logging
import random

log = logging.getLogger("igfunnel.instagram")

CHALLENGE_MARKERS = ("challenge_required", "checkpoint_required", "feedback_required")
THROTTLE_MARKERS = ("throttled", "slow down", "try again later", "rate limit")


def _classify(exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(m in msg for m in CHALLENGE_MARKERS):
        return "challenge"
    if "login_required" in msg or "login required" in msg:
        return "login_required"
    if any(m in msg for m in THROTTLE_MARKERS):
        return "throttled"
    return "generic"


def _feed_with_retry(cl) -> "tuple[bool, str]":
    """Timeline check that survives network blips without a fresh login.

    A fresh login is the strongest automation signal, so a transient network
    error must not trigger one: auth failures return immediately, anything
    else gets one retry after 5s. Returns (ok, kind).
    """
    import time

    try:
        cl.get_timeline_feed()
        return True, ""
    except Exception as exc:
        kind = _classify(exc)
        if kind in ("challenge", "login_required"):
            return False, kind
        time.sleep(5)
        try:
            cl.get_timeline_feed()
            return True, ""
        except Exception as exc2:
            return False, _classify(exc2)


def _safe_filename(settings_path: str) -> str:
    """Return the raw JSON of a session file (or ''), for diagnostics."""
    try:
        with open(settings_path, encoding="utf-8") as f:
            return f.read(200)
    except OSError:
        return ""


def _persisted_mismatches(sent: dict, actual: dict) -> list:
    """Sections we asked IG to save but it silently dropped.

    edit_profile returns status ok even when it ignores fields (e.g. links
    on restricted accounts). Pure function so it can be unit-tested.
    sent keys: biography, external_url, full_name (as sent); actual: the
    account_info dict re-read afterwards.
    """
    problems = []
    if sent.get("biography") and (actual.get("biography") or "").strip() != sent["biography"].strip():
        problems.append("biography")
    if sent.get("external_url"):
        want = str(sent["external_url"]).rstrip("/")
        got = str(actual.get("external_url") or "").rstrip("/")
        if got != want:
            problems.append("link")
    if sent.get("full_name"):
        want = str(sent["full_name"]).strip()[:64]
        if (actual.get("full_name") or "").strip() != want:
            problems.append("full name")
    return problems


class InstagramService:
    def __init__(self, proxy_url: str | None = None, session_path: str | None = None):
        self.proxy_url = proxy_url
        self.session_path = session_path

    def _make_client(self, username: str, session_first: bool = True,
                     request_timeout: float = 30):
        from instagrapi import Client
        from app.utils.instagram_helpers import device_settings_for
        from app.utils.instagram_helpers import dump_session_settings

        cl = Client(request_timeout=request_timeout)
        cl.delay_range = [1, 3]
        if self.proxy_url:
            cl.set_proxy(self.proxy_url)
        loaded = False
        if session_first and self.session_path:
            # Load the saved session BEFORE set_device so the file's own
            # device/uuid/cookies (not our generic fingerprint) win.
            try:
                cl.load_settings(self.session_path)
                loaded = bool(cl.settings)
                if loaded:
                    log.info("Loaded session file for %s", username)
            except FileNotFoundError:
                log.info("No session file yet for %s (path: %s)", username, self.session_path)
            except Exception as exc:
                log.warning("Session file for %s unreadable (%s); starting fresh", username, exc)
        if not loaded:
            # No usable session: fall back to the deterministic fingerprint.
            cl.set_device(device_settings_for(username))
        # load_settings()/init() restores request_timeout from the session
        # file (dumped with instagrapi's 1s default) — enforce ours after.
        cl.request_timeout = request_timeout
        return cl

    def login(self, username: str, password: str) -> tuple[bool, str]:
        """Blocking (run in thread). Returns (ok, detail)."""
        import os

        cl = self._make_client(username)
        try:
            if self.session_path and os.path.exists(self.session_path):
                ok, _ = _feed_with_retry(cl)  # cheap session validity check
                if ok:
                    log.info("Reused session for %s", username)
                    return True, "session_reused"
                log.info("Stored session invalid for %s — logging in fresh", username)
            cl.login(username, password)
            if self.session_path:
                dump_session_settings(cl, self.session_path)
            return True, "logged_in"
        except Exception as exc:
            kind = _classify(exc)
            log.warning("Login failed for %s (%s): %s", username, kind, exc)
            return False, f"{kind}: {exc}"

    def check_session(self, username: str) -> "tuple[bool, str]":
        """Session validity plus a human-readable reason (surfaced in the UI).

        Returns (True, "ok") or (False, "<kind>: <hint>") — the old bool-only
        contract hid WHY a proxy route fails, forcing log-diving.
        """
        cl = self._make_client(username)
        try:
            ok, kind = _feed_with_retry(cl)
            if ok:
                log.info("Session check for %s: valid", username)
                return True, "ok"
            hint = {
                "challenge": "Instagram demands verification on this route — complete it in the app/browser, then re-test.",
                "login_required": "Session rejected via this route (IP change or killed session) — refresh the session.",
                "throttled": "This egress IP is rate-limited by Instagram — use another proxy.",
            }.get(kind, "Network/proxy path failed — check the proxy itself (Test button).")
            log.info("Session check for %s: invalid (%s)", username, kind)
            return False, f"{kind}: {hint}"
        except Exception as exc:
            kind = _classify(exc)
            log.warning("Session check for %s failed (%s): %s", username, kind, exc)
            return False, f"{kind}: {exc}"

    def upload_reel(
        self, username: str, password: str, video_path: str, caption: str,
        trial: bool = False, trial_strategy: str = "manual",
        thumbnail_path: "str | None" = None,
    ) -> tuple[str | None, str | None, str]:
        """Blocking. Returns (media_id, permalink, error).

        With trial=True the reel first goes to non-followers (explore
        engine). If Instagram rejects the trial mode itself (ineligible
        account), it falls back to a regular reel instead of failing —
        the fallback only triggers on trial-specific errors at configure
        time, never after a publish, so no double post is possible.
        """
        cl = self._make_client(username)
        try:
            ok, _ = _feed_with_retry(cl)
            if not ok:
                cl.login(username, password)
                if self.session_path:
                    try:
                        dump_session_settings(cl, self.session_path)
                    except Exception:
                        pass
            # instagrapi generates the cover via MoviePy when thumbnail is
            # omitted — not installed here, so always pass an explicit file.
            from pathlib import Path as _Path

            thumb = _Path(thumbnail_path) if thumbnail_path else None
            # Random pre-post delay is applied by the caller (needs async sleep).
            if trial:
                try:
                    media = cl.clip_upload(
                        video_path, caption=caption, thumbnail=thumb, trial=True,
                        trial_graduation_strategy=trial_strategy or "manual",
                    )
                except Exception as texc:
                    if "trial" not in f"{type(texc).__name__}: {texc}".lower():
                        raise
                    log.warning("Trial upload rejected for %s — falling back to regular reel: %s", username, texc)
                    media = cl.clip_upload(video_path, caption=caption, thumbnail=thumb)
            else:
                media = cl.clip_upload(video_path, caption=caption, thumbnail=thumb)
            media_id = str(getattr(media, "id", "") or getattr(media, "pk", ""))
            code = getattr(media, "code", None)
            permalink = f"https://www.instagram.com/reel/{code}/" if code else None
            if not media_id:
                # Upload "succeeded" with no media id — must NOT be marked posted.
                return None, None, "generic: upload returned no media id"
            return media_id, permalink, ""
        except Exception as exc:
            kind = _classify(exc)
            return None, None, f"{kind}: {exc}"

    def apply_bio(self, username: str, password: str, biography: str, external_url: str) -> str:
        """Legacy bio-only entry — delegates to apply_profile (one code path)."""
        return self.apply_profile(username, password, biography=biography, external_url=external_url)

    def apply_profile(
        self,
        username: str,
        password: str,
        biography: str = "",
        external_url: str = "",
        full_name: str = "",
        make_private: "bool | None" = None,
        picture_path: "str | None" = None,
    ) -> str:
        """Apply profile fields in one session. Returns "" or "kind: error".

        Only non-empty fields are sent (empty = don't touch). A missing
        picture file fails upfront, before anything reaches Instagram.
        Username/email/phone changes are deliberately unsupported (they
        desync our session files and need human confirmation flows).
        """
        import os

        if picture_path and not os.path.exists(picture_path):
            return f"picture: file not found ({picture_path})"
        cl = self._make_client(username)
        try:
            ok, _ = _feed_with_retry(cl)
            if not ok:
                cl.login(username, password)
                if self.session_path:
                    try:
                        dump_session_settings(cl, self.session_path)
                    except Exception:
                        pass
            edit: dict = {}
            if (biography or "").strip():
                edit["biography"] = biography
            if (external_url or "").strip():
                edit["external_url"] = external_url
            if (full_name or "").strip():
                edit["full_name"] = full_name.strip()[:64]
            if edit:
                cl.account_edit(**edit)
            if picture_path:
                from pathlib import Path

                cl.account_change_picture(Path(picture_path))
            if make_private is not None:
                if make_private:
                    cl.account_set_private()
                else:
                    cl.account_set_public()
            if self.session_path and (edit or picture_path or make_private is not None):
                try:
                    dump_session_settings(cl, self.session_path)
                except Exception:
                    pass
            # Read-back: IG answers ok even when it silently drops fields.
            # Never report success for something that isn't on the profile.
            # (A failed re-read is not proof of a drop — only a fresh,
            # contradictory snapshot counts.)
            if edit:
                try:
                    after = cl.account_info().dict()
                except Exception:
                    return ""
                dropped = _persisted_mismatches(edit, after)
                if dropped:
                    return (
                        "unpersisted: Instagram accepted the edit but did not save "
                        + ", ".join(dropped)
                    )
            return ""
        except Exception as exc:
            return f"{_classify(exc)}: {exc}"

    def remove_live_picture(self, username: str, password: str) -> str:
        """Delete the CURRENT Instagram profile photo. Returns "" or "kind: error".

        instagrapi has no wrapper for this; the official app calls
        POST accounts/remove_profile_picture/ (confirmed across the
        python/js/c# private-API clients), so we sign it the same way.
        """
        cl = self._make_client(username)
        try:
            ok, _ = _feed_with_retry(cl)
            if not ok:
                cl.login(username, password)
                if self.session_path:
                    try:
                        dump_session_settings(cl, self.session_path)
                    except Exception:
                        pass
            # POST with signed action data (same shape as set_private/_public):
            # a bare private_request() sends GET, which IG answers with 405.
            if not cl.user_id:
                return "login_required: no user id in session"
            data = cl.with_action_data({"_uid": str(cl.user_id), "_uuid": cl.uuid})
            res = cl.private_request("accounts/remove_profile_picture/", data)
            if not res or res.get("status") != "ok":
                return f"generic: unexpected response {res}"
            if self.session_path:
                try:
                    dump_session_settings(cl, self.session_path)
                except Exception:
                    pass
            return ""
        except Exception as exc:
            return f"{_classify(exc)}: {exc}"

    def read_profile(self, username: str) -> dict:
        """Read-only IG-side profile snapshot (bio/full name/url/privacy/pic).

        Raises on failure — callers turn it into a 502 with the reason.
        """
        cl = self._make_client(username)
        ok, kind = _feed_with_retry(cl)
        if not ok:
            raise RuntimeError(f"session invalid ({kind}) — refresh the session first")
        info = cl.account_info().dict()
        return {
            "username": info.get("username"),
            "full_name": info.get("full_name") or "",
            "biography": info.get("biography") or "",
            "external_url": info.get("external_url") or "",
            "is_private": bool(info.get("is_private")),
            "profile_pic_url": info.get("profile_pic_url") or "",
            "follower_count": info.get("follower_count"),
            "following_count": info.get("following_count"),
            "media_count": info.get("media_count"),
        }

    def media_info(self, username: str, media_id: str) -> dict:
        cl = self._make_client(username)
        try:
            info = cl.media_info(media_id).dict()
            return {
                "like_count": info.get("like_count", 0),
                "comment_count": info.get("comment_count", 0),
                "view_count": info.get("view_count") or info.get("play_count") or 0,
            }
        except Exception as exc:
            log.warning("media_info failed for %s: %s", media_id, exc)
            return {}


async def random_pre_post_delay(min_s: int, max_s: int) -> None:
    await asyncio.sleep(random.uniform(min_s, max_s))


def classify_error(exc: Exception) -> str:
    return _classify(exc)
