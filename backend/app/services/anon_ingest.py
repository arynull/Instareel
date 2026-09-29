"""Account-free ingestion primitives for PUBLIC Instagram pages.

Two building blocks, neither touching any IG account or session file:

- list_public_posts(): newest posts of a page via the public
  web_profile_info endpoint (the same JSON the web app itself uses).
- download_post(): one post's video + cover + caption via yt-dlp
  single-post extraction (mp4 + jpg thumbnail + info.json description).

Verified live 2026-09-25 (yt-dlp 2026.8.19, no login): full reel pull
works; profile/tag *playlist* extraction does NOT (IG obfuscates list
pages — "Unable to extract data"), and anonymous web_profile_info is
rate-limited by IP (often HTTP 429 from datacenter ranges) while
single-post extraction stays tolerant. Consequences, encoded in the
ingest task rather than hidden here:

- anonymous listing is best-effort (newest first page only); there is
  deliberately NO authenticated fallback — source ingest never touches
  an account session (bulk session usage is what Instagram flags as
  scraping), so a failed listing fails the run instead;
- every per-item download is anonymous via yt-dlp, so posting-account
  sessions are never burned on bulk bytes.
"""
import logging
import os

log = logging.getLogger("igfunnel.anon")

WEB_PROFILE_URL = "https://www.instagram.com/api/v1/users/web_profile_info/"
WEB_APP_ID = "936619743392459"  # public web client id, not a secret
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".webm", ".avi"}

# Kinds mirror instagram_service._classify plus listing-specific ones.
# "auth"   = needs a logged-in account (private page / login wall)
# "throttled" = back off, retryable later
# "not-found" = page does not exist, retrying is pointless


def classify_anon_error(exc: Exception) -> str:
    msg = f"{type(exc).__name__}: {exc}".lower()
    if any(m in msg for m in (
        "login required", "log in to", "private account",
        "not available", "isn't available", "content isn't",
        "checkpoint", "challenge",
    )):
        return "auth"
    if any(m in msg for m in (
        "429", "too many requests", "rate-limit", "rate limit",
        "slow down", "try again later", "timeout", "timed out",
    )):
        return "throttled"
    if "404" in msg or "not found" in msg or "does not exist" in msg:
        return "not-found"
    return "generic"


def list_public_posts(username: str, limit: int = 25,
                      proxy: "str | None" = None,
                      timeout: int = 15) -> "tuple[list[dict], str | None]":
    """Newest posts of a PUBLIC page without any account.

    Returns (items, error). Each item: shortcode / is_video /
    product_type / caption. Empty items + None error is impossible —
    empty page yields ([], None) only when the page genuinely has no posts.
    """
    import requests

    headers = {
        "User-Agent": BROWSER_UA,
        "X-IG-App-ID": WEB_APP_ID,
        "Accept": "application/json",
        "Accept-Language": "en-US,en;q=0.9",
        "X-Requested-With": "XMLHttpRequest",
        "Referer": f"https://www.instagram.com/{username}/",
    }
    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        r = requests.get(WEB_PROFILE_URL, params={"username": username},
                         headers=headers, proxies=proxies, timeout=timeout)
    except Exception as exc:
        kind = classify_anon_error(exc)
        return [], f"{kind}: anonymous listing failed: {exc}"
    if r.status_code == 404:
        return [], f"not-found: @{username} does not exist"
    if r.status_code == 429:
        return [], "throttled: anonymous listing rate-limited (HTTP 429) — retry later"
    if r.status_code in (400, 401, 403):
        return [], f"auth: anonymous listing refused (HTTP {r.status_code})"
    if r.status_code != 200:
        return [], f"generic: anonymous listing HTTP {r.status_code}"
    try:
        user = (r.json().get("data") or {}).get("user")
    except Exception:
        user = None
    if not user:
        return [], "auth: anonymous listing returned no profile"
    if user.get("is_private"):
        return [], f"private: @{username} is private — anonymous ingest only supports public pages"
    timeline = user.get("edge_owner_to_timeline_media") or {}
    edges = timeline.get("edges") or []
    items: "list[dict]" = []
    for e in edges:
        node = (e or {}).get("node") or {}
        sc = node.get("shortcode")
        if not sc:
            continue
        typename = node.get("__typename") or ""
        cap_edges = ((node.get("edge_media_to_caption") or {}).get("edges")) or []
        cap_text = ""
        if cap_edges:
            cap_text = ((cap_edges[0] or {}).get("node") or {}).get("text") or ""
        items.append({
            "shortcode": sc,
            "is_video": bool(node.get("is_video")) or typename == "GraphVideo",
            "product_type": (node.get("product_type") or "").lower(),
            "caption": (cap_text[:4000] or None),
        })
        if len(items) >= max(limit, 1):
            break
    return items, None


def download_post(shortcode: str, dest_dir: str,
                  proxy: "str | None" = None,
                  with_cover: bool = True,
                  socket_timeout: int = 30,
                  retries: int = 3) -> "tuple[dict | None, str | None]":
    """Download one PUBLIC post without any account.

    Returns (result, error) where result holds video / cover / caption
    paths (cover may be None). Raises nothing — all yt-dlp failures are
    classified into (None, "kind: detail").
    """
    from yt_dlp import YoutubeDL

    os.makedirs(dest_dir, exist_ok=True)
    url = f"https://www.instagram.com/p/{shortcode}/"
    params = {  # yt-dlp _Params; left unannotated for its strict stubs
        "format": "best[ext=mp4]/best",
        "outtmpl": os.path.join(dest_dir, "%(id)s.%(ext)s"),
        "writethumbnail": with_cover,
        "postprocessors": [{"key": "FFmpegThumbnailsConvertor", "format": "jpg"}],
        "writeinfojson": True,
        "quiet": True,
        "no_warnings": True,
        "no_playlist": True,
        "retries": retries,
        "socket_timeout": socket_timeout,
    }
    if proxy:
        params["proxy"] = proxy
    try:
        with YoutubeDL(params) as ydl:  # type: ignore[arg-type] — plain dict satisfies _Params at runtime
            ydl.extract_info(url, download=True)
    except Exception as exc:
        kind = classify_anon_error(exc)
        return None, f"{kind}: yt-dlp could not fetch {shortcode}: {exc}"
    video = cover = None
    for name in os.listdir(dest_dir):
        p = os.path.join(dest_dir, name)
        if not os.path.isfile(p):
            continue
        ext = os.path.splitext(name)[1].lower()
        if ext in VIDEO_EXTS and video is None:
            video = p
        elif ext in (".jpg", ".jpeg", ".png", ".webp") and cover is None:
            cover = p
    if video is None:
        return None, f"generic: yt-dlp finished {shortcode} with no video file"
    caption = None
    info_path = os.path.join(dest_dir, f"{shortcode}.info.json")
    if os.path.exists(info_path):
        try:
            import json

            with open(info_path, encoding="utf-8") as f:
                caption = (json.load(f).get("description") or "")[:4000] or None
        except Exception as exc:
            log.warning("unreadable info.json for %s: %s", shortcode, exc)
    return {"video": video, "cover": cover, "caption": caption}, None
