"""Near-duplicate detection via perceptual hashing (dHash).

The md5 in the ingest/upload paths only catches byte-identical files.
The same clip re-downloaded under a different IG media id, cropped, or
re-encoded sails through — and reposting it burns reach and looks
bot-like. A 64-bit dHash of a quarter-point frame catches all of those
at a hamming distance <= 8.

Design rules (anti-interference):
- Pillow only (already a dependency) + the ffmpeg binary the pipeline
  already shells out to. No new packages, no network.
- frame_hash NEVER raises: any failure returns None and the caller
  skips the visual check (md5 still guards exact dupes).
- find_near_duplicate is sync (worker-safe) and takes an exclusion id
  so re-checks never flag the row being created.
"""
import os
import subprocess
import tempfile

NEAR_DUP_THRESHOLD = 8


def dhash_hex(img) -> str:
    """64-bit difference hash of a Pillow image → 16 hex chars."""
    g = img.convert("L").resize((9, 8))
    # get_flattened_data() replaced the deprecated Image.getdata() in
    # Pillow 12 (removal in 14); fall back for older Pillows.
    flat = getattr(g, "get_flattened_data", None)
    px = list(flat()) if flat is not None else list(g.getdata())
    bits = 0
    for y in range(8):
        for x in range(8):
            if px[y * 9 + x] > px[y * 9 + x + 1]:
                bits |= 1 << (y * 8 + x)
    return f"{bits:016x}"


def hamming(a: str, b: str) -> int:
    """Bit distance between two hex hashes."""
    return bin(int(a, 16) ^ int(b, 16)).count("1")


def frame_hash(video_path: str, at_fraction: float = 0.25) -> str | None:
    """dHash of one frame. Returns None on ANY failure (never raises)."""
    try:
        from PIL import Image

        from app.utils import ffmpeg as ff

        info = ff.probe_sync(video_path)
        duration = (info or {}).get("duration") or 0.0
        at = max(0.1, duration * at_fraction)
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            frame_path = tmp.name
        try:
            subprocess.run(
                ["ffmpeg", "-y", "-ss", str(at), "-i", video_path,
                 "-frames:v", "1", "-q:v", "3", frame_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=120,
            )
            if not os.path.exists(frame_path) or os.path.getsize(frame_path) == 0:
                return None
            with Image.open(frame_path) as img:
                return dhash_hex(img)
        finally:
            try:
                os.remove(frame_path)
            except OSError:
                pass
    except Exception:
        return None


def find_near_duplicate(session, phash: str, *, threshold: int = NEAR_DUP_THRESHOLD,
                        exclude_id: int | None = None):
    """Newest visually-identical video, or None. Sync, worker-safe."""
    from sqlalchemy import desc, select

    from app.models import Video

    q = select(Video.id, Video.phash).where(Video.phash.is_not(None))
    if exclude_id is not None:
        q = q.where(Video.id != exclude_id)
    q = q.order_by(desc(Video.id))
    for vid, stored in session.execute(q).all():
        try:
            if stored and hamming(phash, stored) <= threshold:
                return session.get(Video, vid)
        except (TypeError, ValueError):
            continue
    return None


def find_near_duplicate_id(phash: str, *, threshold: int = NEAR_DUP_THRESHOLD) -> int | None:
    """Fresh-session variant for async callers (upload endpoint): opens its
    own sync session in the caller's thread, returns the match id or None."""
    from app.database import SyncSessionLocal

    with SyncSessionLocal() as s:
        hit = find_near_duplicate(s, phash, threshold=threshold)
        return hit.id if hit is not None else None


def normalize_caption(text: str | None) -> str:
    """Canonical form for caption-equality checks."""
    import re

    if not text:
        return ""
    t = text.lower()
    t = re.sub(r"#\w+", " ", t)          # hashtags travel separately; ignore them
    t = re.sub(r"\s+", " ", t).strip()
    return t


def find_caption_duplicate(session, caption: str | None, *, min_len: int = 20,
                           exclude_id: int | None = None):
    """A video already carrying this exact (normalized) caption, or None."""
    if not caption or len(caption.strip()) < min_len:
        return None
    from sqlalchemy import select

    from app.models import Video

    want = normalize_caption(caption)
    if len(want) < min_len:
        return None
    q = select(Video.id, Video.source_caption).where(Video.source_caption.is_not(None))
    if exclude_id is not None:
        q = q.where(Video.id != exclude_id)
    for vid, stored in session.execute(q).all():
        if stored and normalize_caption(stored) == want:
            return session.get(Video, vid)
    return None
