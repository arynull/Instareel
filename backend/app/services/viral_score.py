"""Pre-flight viral score: warn-only quality gate before posting.

Scores a processed (or uploaded) video 0-100 from signals the pipeline
already owns — duration, resolution/orientation, audio, cover, caption —
and returns a verdict plus actionable suggestions. It NEVER blocks: the
Post-now button only shows a warning, the admin always decides.

Weights (documented so tuning is a diff, not archaeology):
- duration 25: reels sweet spot is 7-30s (completion rate drives reach).
- format 20: 9:16 vertical, >=720p. Landscape is a reach killer.
- audio 15: named trending track > any baked audio > silent.
- cover 10: custom cover (CTR) > auto frame > none.
- caption 20: source/templated caption (10) + hashtags present (10).
- finish 10: effect or grade applied (polish signal, small weight).

Design rules (anti-interference):
- score_video_meta is pure (dict in → dict out), fully unit-tested.
- The endpoint probes the file best-effort in a thread; a dead file
  still scores from the stored row (low format points, honest hint).
"""
DURATION_IDEAL = (7.0, 30.0)
DURATION_OK = (3.0, 60.0)


def score_video_meta(meta: dict) -> dict:
    """Pure scorer. meta keys: duration, width, height, has_audio,
    audio_track, caption, hashtags, has_effect, has_custom_cover."""
    duration = meta.get("duration") or 0.0
    width = meta.get("width") or 0
    height = meta.get("height") or 0
    breakdown: list[dict] = []
    suggestions: list[str] = []

    # Duration (25)
    if DURATION_IDEAL[0] <= duration <= DURATION_IDEAL[1]:
        d_pts = 25
    elif DURATION_OK[0] <= duration < DURATION_IDEAL[0]:
        d_pts = 15
        suggestions.append("Under 7s — will flop without a strong hook")
    elif DURATION_IDEAL[1] < duration <= DURATION_OK[1]:
        d_pts = 15
        suggestions.append("Over 30s — trim it or make sure it holds attention")
    else:
        d_pts = 5
        suggestions.append(f"Duration {duration:.0f}s is outside the reels sweet spot")
    breakdown.append({"key": "duration", "label": "Duration", "points": d_pts, "max": 25})

    # Format (20)
    f_pts, portrait = 0, height >= width and height > 0
    if portrait and height >= 1280:
        f_pts = 20
    elif portrait and height >= 720:
        f_pts = 14
    elif portrait:
        f_pts = 8
        suggestions.append("Low resolution — quality gets penalized on Explore")
    else:
        f_pts = 4
        suggestions.append("Landscape — vertical reels get several times more reach")
    breakdown.append({"key": "format", "label": "Vertical format", "points": f_pts, "max": 20})

    # Audio (15)
    if meta.get("audio_track"):
        a_pts = 15
    elif meta.get("has_audio"):
        a_pts = 9
        suggestions.append("No trending audio — layer a track from Audio onto it")
    else:
        a_pts = 0
        suggestions.append("Silent — silent reels almost always flop")
    breakdown.append({"key": "audio", "label": "Audio", "points": a_pts, "max": 15})

    # Cover (10)
    if meta.get("has_custom_cover"):
        c_pts = 10
    elif meta.get("has_cover"):
        c_pts = 6
        suggestions.append("Auto cover — a custom cover with a face/text boosts CTR")
    else:
        c_pts = 0
        suggestions.append("No cover — always set a cover")
    breakdown.append({"key": "cover", "label": "Cover", "points": c_pts, "max": 10})

    # Caption (20)
    cap_pts = 10 if (meta.get("caption") or "").strip() else 0
    if not cap_pts:
        suggestions.append("Empty caption — pick a source caption or template")
    tags = meta.get("hashtags") or ""
    tag_count = len([t for t in tags.replace(",", " ").split() if t.startswith("#")])
    tag_pts = 10 if tag_count >= 3 else (5 if tag_count > 0 else 0)
    if not tag_pts:
        suggestions.append("No hashtags — add 3-5 relevant hashtags")
    breakdown.append({"key": "caption", "label": "Caption & hashtags", "points": cap_pts + tag_pts, "max": 20})

    # Finish (10)
    fin_pts = 10 if meta.get("has_effect") else 4
    if not meta.get("has_effect"):
        suggestions.append("No effect/grade — give it a light polish")
    breakdown.append({"key": "finish", "label": "Finishing", "points": fin_pts, "max": 10})

    total = sum(b["points"] for b in breakdown)
    verdict = "ready" if total >= 75 else ("needs-work" if total >= 50 else "risky")
    return {"score": total, "verdict": verdict, "breakdown": breakdown, "suggestions": suggestions}


async def score_video(db, video_id: int) -> dict:
    """Gather row + best-effort probe, then score. Async (API use)."""
    import asyncio

    from fastapi import HTTPException

    from app.models import Video
    from app.utils import ffmpeg as ff

    v = await db.get(Video, video_id)
    if not v:
        raise HTTPException(404, "Video not found")
    width = height = 0
    has_audio = False
    probe_ok = False
    try:
        src = v.processed_path or v.raw_path
        info = await asyncio.wait_for(
            asyncio.to_thread(ff.probe_sync, src), timeout=60,
        )
        width, height = info.get("width") or 0, info.get("height") or 0
        has_audio = bool(info.get("has_audio"))
        probe_ok = True
    except Exception:
        pass
    # What will actually post: the newest post row's caption/hashtags when
    # one exists (Post-now/scheduler fill them in), else the harvested one.
    from sqlalchemy import desc, select

    from app.models import Post

    latest = (
        await db.execute(
            select(Post.caption, Post.hashtags)
            .where(Post.video_id == video_id)
            .order_by(desc(Post.id))
            .limit(1)
        )
    ).first()
    caption = (latest[0] if latest and latest[0] else None) or v.source_caption or ""
    hashtags = (latest[1] if latest else None) or ""
    out = score_video_meta({
        "duration": v.duration or 0.0,
        "width": width, "height": height, "has_audio": has_audio,
        "audio_track": v.audio_track,
        "caption": caption,
        "hashtags": hashtags,
        "has_effect": bool(v.effect_preset or v.custom_filters),
        "has_custom_cover": bool(v.custom_thumbnail_path),
        "has_cover": bool(v.custom_thumbnail_path or v.thumbnail_path),
    })
    if not probe_ok:
        out["suggestions"] = ["Video file could not be probed — it may be corrupt"] + out["suggestions"]
    out["video_id"] = v.id
    return out
