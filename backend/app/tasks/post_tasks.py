"""Posting tasks: per-minute scheduler + single-post executor with retries.

Fully synchronous: no asyncio.run, no ThreadPoolExecutor, no event loop.
instagrapi calls are already blocking, so they run directly in the task.
"""
import datetime as dt
import logging
import random
import time

from app.tasks.celery_app import celery
from app.tasks.sync_helpers import publish_sync

log = logging.getLogger("igfunnel.tasks.post")


def fire_time_with_jitter(jitter_setting: object) -> dt.datetime:
    """Post fire time: now + a non-negative jitter of 0..jitter minutes.

    ``jitter_setting`` is the raw ``post_jitter_minutes`` setting value
    (unparseable/negative → default 5 / clamp 0). Jitter only delays: a
    negative jitter would set scheduled_for in the past, breaking the
    upcoming countdown and the slot's fire-time ordering for no benefit.

    Anti-detection note: the jitter is applied at *second* resolution, not
    whole minutes. Whole-minute fire times (06:00:00, 06:03:00, …) are a
    bot fingerprint — real users post at 06:03:27. Two rules firing in the
    same minute also naturally spread apart instead of landing on the same
    second.
    """
    try:
        jitter = max(0, int(jitter_setting if jitter_setting is not None else 5))
    except (TypeError, ValueError):
        jitter = 5
    return dt.datetime.now(dt.timezone.utc) + dt.timedelta(
        seconds=random.randint(0, jitter * 60)
    )


@celery.task(name="tasks.post_tasks.check_and_post", bind=True, max_retries=0)
def check_and_post(self):
    """Beat entry: create scheduled posts from due rule slots, then fire due posts.

    Fresh posts are handed to the slow lane immediately via Celery ETA
    (countdown): the jittered fire time has *second* resolution, and the
    per-minute tick could only dispatch at minute granularity — ETA preserves
    the exact second. The row is stamped dispatched_at at creation so the
    tick's backstop below skips it; the atomic UPDATE ... WHERE there is the
    safety net for manual/API posts and lost ETA publishes, and it can never
    stamp the same row twice (no duplicate queue entries while a post waits
    in the slow queue).
    """
    from sqlalchemy.exc import IntegrityError

    from app.database import SyncSessionLocal
    from app.models import Post, PostStatus
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import log_event_sync, notify_sync

    try:
        # Backstop for a missed midnight reset_daily_counts: idempotent via
        # the date stamp, so this is a no-op on every tick except the first
        # one after a local day boundary (or after an outage).
        sched.ensure_daily_counts_reset()
        with SyncSessionLocal() as s:
            # (rule, slot) pairs — a slot stays fireable for
            # SCHEDULE_GRACE_MINUTES after its minute, so a brief
            # worker/beat outage at the exact minute doesn't lose it.
            slots = sched.due_rule_slots(s)
            created = 0
            used_video_ids: set[int] = set()

            def _notify_skip(rule, slot_utc, reason: str):
                """One warning per slot when a matched slot can't fire.

                A silently skipped slot is the worst outcome: the bell's
                "upcoming" countdown reaches zero and then nothing happens
                with no explanation. Deduped per rule+slot so the ticks
                inside the grace window notify exactly once.
                """
                from zoneinfo import ZoneInfo

                local_slot = slot_utc.astimezone(ZoneInfo(sched.settings.SCHEDULE_TZ))
                notify_sync(
                    "slot_skipped",
                    "warning",
                    f"Rule '{rule.name}' skipped its {local_slot:%H:%M} slot",
                    f"Scheduled slot {local_slot:%H:%M} for rule '{rule.name}' "
                    f"did not fire: {reason}.",
                    link="/dashboard/schedule",
                    dedup_key=f"slot_skip:{rule.id}:{slot_utc:%Y%m%d%H%M}",
                )

            def _dispatch_eta(post_id: int, when: dt.datetime):
                """Hand a fresh post to the slow lane at its jittered second.

                Celery ETA (countdown) preserves the second-resolution
                jitter — a .delay() here would leave the post for the tick's
                backstop and round the fire time up to the next minute tick.
                The row was stamped dispatched_at at creation so the backstop
                skips it; if the broker publish fails, the stamp is cleared
                and the next tick retries within a minute.
                """
                delay_s = max(
                    0.0,
                    (when - dt.datetime.now(dt.timezone.utc)).total_seconds(),
                )
                try:
                    execute_post.apply_async(args=[post_id], countdown=delay_s)
                except Exception:  # noqa: BLE001 — broker hiccup, tick retries
                    log.exception(
                        "ETA dispatch failed for post %s — tick backstop will retry",
                        post_id,
                    )
                    with SyncSessionLocal() as s2:
                        p = s2.get(Post, post_id)
                        if p is not None and p.status == PostStatus.scheduled:
                            p.dispatched_at = None
                            s2.commit()

            for rule, slot_utc in slots:
                if sched.slot_already_fired(s, rule, slot_utc):
                    # This slot already has its post row (any status) —
                    # without this, every tick in the grace window would
                    # queue another post for the same slot.
                    continue
                if sched.already_scheduled(s, rule, slot_utc):
                    _notify_skip(
                        rule, slot_utc,
                        "another post is already scheduled within ±10 min "
                        "for this account",
                    )
                    continue
                account = sched.eligible_account(s, rule.account_id)
                if not account:
                    _notify_skip(
                        rule, slot_utc,
                        sched.account_skip_reason(s, rule.account_id)
                        or "no eligible account",
                    )
                    continue
                if not sched.account_reachable(s, account):
                    # Its proxy is down and no spare is healthy — leave the
                    # slot for the next tick instead of queueing a doomed post.
                    hour = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d-%H")
                    notify_sync(
                        "proxy_pool_down",
                        "critical",
                        f"No working proxy for @{account.username}",
                        f"Rule '{rule.name}' slot skipped: the account's proxy is down "
                        "and no spare is healthy. The slot stays armed for the next tick.",
                        link="/dashboard/proxies",
                        dedup_key=f"proxy_pool:{account.id}:{hour}",
                    )
                    continue
                video, disposition = sched.resolve_rule_video(s, rule, used_video_ids)
                if disposition in ("retire_gone", "retire_posted"):
                    why = (
                        "pinned video was deleted"
                        if disposition == "retire_gone"
                        else "pinned video already posted elsewhere"
                    )
                    rule.is_active = False
                    s.commit()
                    log_event_sync("INFO", "schedule", f"Rule '{rule.name}' retired: {why}")
                    publish_sync("schedule_update", {"rule_id": rule.id, "is_active": False})
                    continue
                if disposition != "fire" or video is None:
                    if rule.pinned_video_id and disposition != "fire":
                        log_event_sync(
                            "INFO", "schedule",
                            f"Rule '{rule.name}' waiting: pinned video not postable yet",
                        )
                        _notify_skip(
                            rule, slot_utc,
                            "pinned video is not ready yet (still processing "
                            "or already queued)",
                        )
                    else:
                        _notify_skip(
                            rule, slot_utc,
                            "no processed video in the queue",
                        )
                    continue
                caption, tags = sched.resolve_fire_caption(
                    s, bool(rule.prefer_source_caption),
                    video.source_caption, rule.caption_template_id,
                )
                # Fire-time jitter comes from Settings (post_jitter_minutes),
                # not a hardcoded constant — the toggle actually does something.
                now = dt.datetime.now(dt.timezone.utc)
                when = fire_time_with_jitter(sched.get_setting(s, "post_jitter_minutes", "5"))
                post = Post(
                    video_id=video.id,
                    account_id=account.id,
                    rule_id=rule.id,
                    caption=caption,
                    hashtags=tags,
                    status=PostStatus.scheduled,
                    scheduled_for=when,
                    slot_for=slot_utc,
                    # ETA-dispatched just below: the tick backstop must
                    # skip this row (it only stamps unstamped/stale rows).
                    dispatched_at=now,
                    is_trial=bool(video.is_trial),
                )
                s.add(post)
                try:
                    s.flush()  # make the reservation visible to later rules in this tick
                    post_id = post.id  # capture before commit (expire_on_commit)
                    s.commit()  # per-rule commit: one bad rule can't void the whole tick
                except IntegrityError:
                    # Lost a race with a concurrent tick on the same slot —
                    # uq_posts_account_slot did its job. The slot is claimed;
                    # skip it instead of double-posting.
                    s.rollback()
                    log_event_sync(
                        "INFO", "schedule",
                        f"Rule '{rule.name}' slot {slot_utc:%H:%M} already taken "
                        "by a concurrent tick — skipping",
                    )
                    continue
                _dispatch_eta(post_id, when)
                if rule.pinned_video_id:
                    # One-shot fired: retire so tomorrow's tick doesn't re-post.
                    rule.is_active = False
                    s.commit()
                    log_event_sync("INFO", "schedule", f"Rule '{rule.name}' fired its pinned video and retired")
                    publish_sync("schedule_update", {"rule_id": rule.id, "is_active": False})
                used_video_ids.add(video.id)
                created += 1
                late_minutes = (dt.datetime.now(dt.timezone.utc) - slot_utc).total_seconds() / 60
                if late_minutes >= 1:
                    # The grace window recovered this slot after an outage at
                    # the exact minute — worth one info-level heads-up.
                    from zoneinfo import ZoneInfo

                    local_slot = slot_utc.astimezone(ZoneInfo(sched.settings.SCHEDULE_TZ))
                    notify_sync(
                        "slot_fired_late",
                        "info",
                        f"Rule '{rule.name}' fired {int(late_minutes)} min late",
                        f"The {local_slot:%H:%M} slot for rule '{rule.name}' was claimed "
                        f"{int(late_minutes)} min after its minute (grace window recovery) — "
                        "the post is queued.",
                        link="/dashboard/posts",
                    )

            # Backstop dispatch: due posts this tick didn't ETA-dispatch itself
            # (manual/API posts, or an ETA publish that failed and aged past
            # the stale window). The UPDATE ... WHERE is atomic: two ticks
            # racing can never stamp the same row twice, so a post waiting in
            # the slow queue is never enqueued a second time. A stale stamp
            # means the dispatch was lost (broker/queue hiccup) — re-dispatch
            # rather than lose the post; execute_post's atomic claim makes
            # the loser of any residual race a harmless no-op.
            from sqlalchemy import or_, update

            try:
                stale_minutes = max(
                    1, int(sched.get_setting(s, "dispatch_stale_minutes", "30"))
                )
            except (TypeError, ValueError):
                stale_minutes = 30
            now = dt.datetime.now(dt.timezone.utc)
            stale_cutoff = now - dt.timedelta(minutes=stale_minutes)
            due_ids = [
                r[0]
                for r in s.execute(
                    update(Post)
                    .where(
                        Post.status == PostStatus.scheduled,
                        Post.scheduled_for <= now,
                        or_(
                            Post.dispatched_at.is_(None),
                            Post.dispatched_at < stale_cutoff,
                        ),
                    )
                    .values(dispatched_at=now)
                    .returning(Post.id)
                ).all()
            ]
            s.commit()
            slots_matched = len(slots)
        for pid in due_ids:
            execute_post.delay(pid)
        return {"slots_matched": slots_matched, "created": created, "fired": len(due_ids)}
    except Exception as exc:  # noqa: BLE001
        # A failed tick used to die silently: beat kept firing, the
        # watchdog only watches gaps between runs, and nothing told the
        # admin that no post will fire until this is fixed (e.g. a missing
        # DB column after a skipped migration). Notify once per error class
        # — deduped while unread — so a persistent failure doesn't spam.
        log.exception("check_and_post failed")
        err = f"{type(exc).__name__}: {exc}".strip().rstrip(":")[:500]
        log_event_sync("ERROR", "schedule", f"Scheduler tick failed: {err}")
        notify_sync(
            "tick_error",
            "critical",
            "Scheduler tick failed",
            f"The posting scheduler hit an error and this tick did nothing: "
            f"{err}. No posts will fire until this is fixed.",
            link="/dashboard/logs",
            dedup_key=f"tick_error:{type(exc).__name__}",
        )
        return {"error": "tick failed"}


@celery.task(name="tasks.post_tasks.execute_post", bind=True, max_retries=3)
def execute_post(self, post_id: int):
    from celery.exceptions import Retry

    from app.config import settings
    from app.core.security import decrypt_secret
    from app.database import SyncSessionLocal
    from app.models import Account, AccountStatus, Post, PostStatus, Proxy, Video, VideoStatus
    from app.services.instagram_service import InstagramService
    from app.tasks import sync_helpers as sched
    from app.tasks.sync_helpers import log_event_sync, notify_sync, publish_sync
    from app.utils.instagram_helpers import session_path_for

    def set_status(status: PostStatus, **fields):
        """Move the post out of 'posting'.

        Returns False when the row is no longer ours — e.g. the
        stale-posting reaper already failed it while this worker was stuck.
        The caller must then NOT treat the outcome as its own (no notify,
        no account touch, no retry): the reaper owns the row now.
        """
        with SyncSessionLocal() as s:
            post = s.get(Post, post_id)
            if not post or post.status != PostStatus.posting:
                return False
            post.status = status
            for k, v in fields.items():
                setattr(post, k, v)
            s.commit()
        publish_sync("post_status_update", {"post_id": post_id, "status": status.value})
        return True

    def _rotate_proxy_for_cooldown(s, account) -> str:
        """Move the account to a spare proxy (country-stable when possible).

        Throttling and action blocks are usually IP-flavored: retrying from
        the same flagged egress IP just burns the next posts. Returns a
        human note for the log line.
        """
        own = s.get(Proxy, account.proxy_id) if account.proxy_id else None
        spare = sched.pick_spare_proxy(
            s,
            exclude_id=account.proxy_id,
            prefer_country=own.country if own else None,
        )
        if spare is not None and spare.id != account.proxy_id:
            account.proxy_id = spare.id
            return " — rotated proxy"
        return " — no spare proxy"

    def action_block_cooldown_hours(s) -> int:
        """Cooldown after an Instagram action block (setting, default 24h)."""
        from app.tasks.sync_helpers import get_setting

        try:
            return max(1, int(get_setting(s, "action_block_cooldown_hours", "24")))
        except (TypeError, ValueError):
            return 24

    def touch_account(ok: bool, err: str = ""):
        with SyncSessionLocal() as s:
            post = s.get(Post, post_id)
            account = s.get(Account, post.account_id) if post else None
            if not account:
                return
            now = dt.datetime.now(dt.timezone.utc)
            note = ""
            kind = ""
            if ok:
                account.last_post = now
                account.posts_today += 1
                account.total_posts += 1
            else:
                kind = err.split(":")[0]
                if kind == "challenge":
                    account.status = AccountStatus.challenge_required
                elif kind == "throttled":
                    from app.tasks.sync_helpers import throttle_cooldown_hours

                    hours = throttle_cooldown_hours(post.retry_count if post else 0)
                    account.status = AccountStatus.cooldown
                    account.cooldown_until = now + dt.timedelta(hours=hours)
                    note = f"{_rotate_proxy_for_cooldown(s, account)}, cooldown {hours}h"
                elif kind == "action_blocked":
                    # Instagram action block (feedback_required): temporary,
                    # lifts on its own — cool down instead of demanding a
                    # manual session refresh (challenge_required would be
                    # wrong here). Not retried: it won't clear in minutes.
                    hours = action_block_cooldown_hours(s)
                    account.status = AccountStatus.cooldown
                    account.cooldown_until = now + dt.timedelta(hours=hours)
                    note = (
                        f"{_rotate_proxy_for_cooldown(s, account)}, "
                        f"action-block cooldown {hours}h"
                    )
                else:
                    # Any other repeated failure (auth, proxy, IG 500s):
                    # the Health Guard parks the account after a streak
                    # instead of burning the next posts on it.
                    from app.services.account_health import maybe_park_account_sync

                    if maybe_park_account_sync(s, account, now):
                        kind = "auto_park"
                        note = " — auto-parked 6h after 5 consecutive failures"
            username = account.username
            account_id = account.id
            status_changed = kind in ("challenge", "throttled", "action_blocked", "auto_park")
            new_status = account.status
            s.commit()
            if note:
                if kind == "action_blocked":
                    log_event_sync(
                        "WARNING", "account", f"Account @{username} action-blocked{note}"
                    )
                elif kind == "auto_park":
                    log_event_sync("WARNING", "account", f"Account @{username}{note}")
                else:
                    log_event_sync(
                        "WARNING", "account", f"Account @{username} throttled{note}"
                    )
            if status_changed:
                publish_sync("account_status_change",
                             {"account_id": account_id, "status": new_status.value})

    try:
        # Single-flight claim: concurrent workers, beat redelivery and celery
        # retries can never upload the same post twice.
        with SyncSessionLocal() as s:
            outcome = sched.claim_post(s, post_id)
        if outcome == "missing":
            return {"post_id": post_id, "status": "missing"}
        if outcome == "busy":
            return {"post_id": post_id, "status": "already-handled"}
        publish_sync("post_status_update", {"post_id": post_id, "status": "posting"})

        with SyncSessionLocal() as s:
            post = s.get(Post, post_id)
            account = s.get(Account, post.account_id) if post else None
            video = s.get(Video, post.video_id) if post else None
            if post is None or account is None or video is None:
                # Stale references (account/video deleted after scheduling) —
                # fail the post explicitly instead of crashing on None.
                missing = [n for n, o in (("post", post), ("account", account), ("video", video)) if o is None]
                if post is not None:
                    post.status = PostStatus.failed
                    post.fail_reason = f"Stale reference: missing {', '.join(missing)}"
                    s.commit()
                publish_sync("post_status_update", {"post_id": post_id, "status": "failed"})
                return {"post_id": post_id, "status": "failed", "error": f"missing {', '.join(missing)}"}
            sibling = sched.find_blocking_sibling(s, post_id, video.id)
            if sibling is not None:
                # Another post row targets the same video and is in flight or
                # done — abort instead of double-uploading. Fail-closed: in the
                # narrow double-claim race both abort and the next tick
                # re-queues the video exactly once via the reservation.
                post.status = PostStatus.failed
                post.fail_reason = (
                    f"Superseded: video already handled by post #{sibling.id} ({sibling.status.value})"
                )
                s.commit()
                publish_sync("post_status_update", {"post_id": post_id, "status": "failed"})
                return {"post_id": post_id, "status": "failed", "error": "duplicate-superseded"}
            caption, tags, retries = post.caption, post.hashtags, post.retry_count
            video_id = video.id
            want_trial = bool(video.is_trial)
            trial_strategy = (video.trial_strategy or "manual") if want_trial else "manual"
            username, password = account.username, decrypt_secret(account.password_enc)
            video_path = video.processed_path or video.raw_path
            # Own proxy if healthy, else best spare (country-stable) — never
            # the raw name or a dead proxy. Keep the id so the outcome below
            # feeds back into that proxy's health streak.
            from app.services.proxy_service import proxy_url_for

            _proxy = sched.resolve_proxy(s, account)
            proxy_url = proxy_url_for(_proxy)
            proxy_id = _proxy.id if _proxy is not None else None

        # Anti-detection pre-post delay (blocking sleep — task is sync).
        time.sleep(random.uniform(settings.IG_PRE_POST_DELAY_MIN, settings.IG_PRE_POST_DELAY_MAX))

        # Re-verify after the sleep: the admin may have deleted the post or
        # moved it out of 'posting' while we waited — never upload then.
        # The proxy is re-resolved too: it may have died during the sleep.
        with SyncSessionLocal() as s:
            post = s.get(Post, post_id)
            if post is None:
                return {"post_id": post_id, "status": "missing"}
            if post.status != PostStatus.posting:
                return {"post_id": post_id, "status": "already-handled"}
            account = s.get(Account, post.account_id)
            if account is None:
                set_status(PostStatus.failed, fail_reason="Stale reference: missing account")
                return {"post_id": post_id, "status": "failed", "error": "missing account"}
            _proxy = sched.resolve_proxy(s, account)
            proxy_url = proxy_url_for(_proxy)
            proxy_id = _proxy.id if _proxy is not None else None

        svc = InstagramService(proxy_url=proxy_url, session_path=session_path_for(username, settings.MEDIA_ROOT))
        full_caption = (caption + "\n" + tags).strip()
        from app.services.video_processor import resolve_post_thumbnail_sync

        thumb_path = resolve_post_thumbnail_sync(video_id)
        media_id, permalink, error = svc.upload_reel(
            username, password, video_path, full_caption,
            trial=want_trial, trial_strategy=trial_strategy,
            thumbnail_path=thumb_path,
        )

        if error:
            kind = error.split(":")[0]
            if kind in ("throttled", "login_required") and retries < 3:
                # Park it back as scheduled (not failed) so the celery retry
                # re-claims it cleanly instead of tripping over a failed row.
                countdown = 2 ** retries * 60
                if not set_status(
                    PostStatus.scheduled,
                    fail_reason=error[:2000],
                    retry_count=retries + 1,
                    scheduled_for=dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=countdown),
                ):
                    # Lost a race with the stale-posting reaper — it owns the
                    # row now; don't resurrect it with a retry.
                    return {"post_id": post_id, "status": "reaped", "error": error}
                touch_account(False, error)
                log_event_sync("ERROR", "post", f"Post {post_id} to @{username} failed: {error}")
                if kind == "login_required":
                    # Retries can't fix a dead session — tell the admin once
                    # per day so they refresh it instead of burning attempts.
                    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                    notify_sync(
                        "account_session_invalid",
                        "warning",
                        f"@{username} session invalid",
                        f"Instagram rejected the session for @{username} (login required). "
                        "Refresh the session on the Accounts page to resume posting.",
                        link="/dashboard/accounts",
                        dedup_key=f"session:{username}:{day}",
                    )
                raise self.retry(exc=RuntimeError(error), countdown=countdown)
            touch_account(False, error)
            if proxy_id is not None and (
                kind == "throttled" or sched.looks_like_proxy_error(error)
            ):
                # Throttle is IP reputation by definition; transport-looking
                # failures belong to the egress proxy too — feed both into its
                # shared health streak (checker observations count the same).
                with SyncSessionLocal() as s:
                    _p = s.get(Proxy, proxy_id)
                    if _p is not None:
                        sched.record_proxy_check(s, _p, False, error=error)
            # Trial configure failures surface as generic 500s (no "trial" in
            # the text, so the pre-publish fallback can't catch them) — and a
            # blind auto-retry as regular could double-post. Point the admin
            # at the safe manual retry instead.
            fail_note = (
                error + " [trial reel was ON — regular reels may still work; retry with trial off]"
                if want_trial else error
            )
            if set_status(PostStatus.failed, fail_reason=fail_note[:2000], retry_count=retries + 1):
                log_event_sync("ERROR", "post", f"Post {post_id} to @{username} failed: {fail_note}")
                if kind == "action_blocked":
                    # Dedicated warning instead of the generic post_failed
                    # critical: the post failed because Instagram restricted
                    # the account (already cooling down with a rotated proxy)
                    # — the admin needs context, not a second alarm for the
                    # same event.
                    day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
                    notify_sync(
                        "action_blocked",
                        "warning",
                        f"@{username}: Instagram action block",
                        f"Instagram temporarily blocked actions for @{username} "
                        "(feedback_required). The account is cooling down and its "
                        "proxy was rotated — no manual session refresh needed; "
                        "posting resumes automatically.",
                        link="/dashboard/accounts",
                        dedup_key=f"action_block:{username}:{day}",
                    )
                else:
                    notify_sync(
                        "post_failed",
                        "critical",
                        f"Post to @{username} failed",
                        fail_note[:500],
                        link="/dashboard/posts",
                    )
                return {"post_id": post_id, "status": "failed", "error": error}
            # Lost a race with the stale-posting reaper — it already failed
            # the row and notified; don't double-notify or touch the account.
            return {"post_id": post_id, "status": "reaped", "error": error}

        if not set_status(
            PostStatus.posted,
            ig_media_id=media_id,
            ig_permalink=permalink,
            posted_at=dt.datetime.now(dt.timezone.utc),
        ):
            # Lost a race with the stale-posting reaper (upload took 45+ min):
            # the row is failed and the admin was notified — don't resurrect it.
            return {"post_id": post_id, "status": "reaped", "url": permalink}
        touch_account(True)
        # Archive the video by captured id so it is never posted twice, even
        # if the post row itself was deleted in the meantime.
        with SyncSessionLocal() as s:
            v = s.get(Video, video_id)
            if v is not None and v.status != VideoStatus.posted:
                v.status = VideoStatus.posted
            if proxy_id is not None:
                _p = s.get(Proxy, proxy_id)
                if _p is not None:
                    # A clean upload through this proxy heals its streak.
                    sched.record_proxy_check(s, _p, True)
            s.commit()
        log_event_sync("INFO", "post", f"Posted to @{username}", {"post_id": post_id, "url": permalink})
        notify_sync(
            "post_posted",
            "success",
            f"Posted to @{username}",
            f"Video posted to @{username}." + (f" {permalink}" if permalink else ""),
            link="/dashboard/posts",
        )
        return {"post_id": post_id, "status": "posted", "url": permalink}
    except Retry:
        raise
    except Exception as exc:  # noqa: BLE001
        log.exception("execute_post %s failed", post_id)
        try:
            set_status(PostStatus.failed, fail_reason=str(exc)[:2000])
        except Exception:
            pass
        return {"post_id": post_id, "status": "failed", "error": str(exc)}
