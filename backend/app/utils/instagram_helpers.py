"""Session persistence + anti-detection helpers for instagrapi."""
import logging
import os

log = logging.getLogger("igfunnel.instagram")

# Must track the newest instagrapi release: Instagram rejects logins from
# outdated app versions ("Your version of Instagram is out of date").
# Values below mirror instagrapi 3.0.2 config defaults (Instagram 446.x,
# Pixel 8 Pro / Android 14). Only the per-account `device`/`uuid` vary.
APP_VERSION = "446.0.0.49.77"
VERSION_CODE = "385211303"
BLOKS_VERSIONING_ID = "935a519904e9017324cdedb64a283a3c2c1a3d5b0bbc698b451f5aef72cc11df"
ANDROID_VERSION = 34
ANDROID_RELEASE = "14"
DPI = "480dpi"
RESOLUTION = "1344x2992"
MANUFACTURER = "Google/google"
MODEL = "Pixel 8 Pro"
CPU = "husky"


def session_path_for(username: str, media_root: str) -> str:
    d = os.path.join(media_root, "sessions")
    os.makedirs(d, exist_ok=True)
    try:
        # Session files hold auth cookies — the directory shouldn't be
        # listable by other users even if the umask is permissive (m7).
        os.chmod(d, 0o700)
    except OSError:
        pass
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in username)
    return os.path.join(d, f"{safe}.json")


def dump_session_settings(client, path: str) -> None:
    """instagrapi dump_settings + chmod 0o600 (m7).

    instagrapi writes with the process umask, which typically leaves the
    file world-readable. Session files contain long-lived auth cookies —
    restrict to owner-only right after every dump.
    """
    client.dump_settings(path)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def session_owner_info(payload: object) -> "tuple[str | None, str | None]":
    """Stable owner identity of an instagrapi session dump (pure — unit tested).

    Returns (ds_user_id, username). The numeric id survives username changes;
    the username comes from the ``ds_user`` cookie. Either may be None when
    the dump predates them or carries no cookies.
    """
    if not isinstance(payload, dict):
        return None, None
    uid: str | None = None
    auth = payload.get("authorization_data")
    if isinstance(auth, dict):
        raw_uid = auth.get("ds_user_id")
        if raw_uid is not None and str(raw_uid).strip():
            uid = str(raw_uid).strip()
    cookies = payload.get("cookies")
    items: list[tuple[str, str]] = []
    if isinstance(cookies, dict):
        items = [(str(k), str(v)) for k, v in cookies.items()]
    elif isinstance(cookies, list):
        for c in cookies:
            if isinstance(c, dict) and c.get("name") is not None:
                items.append((str(c.get("name")), str(c.get("value", ""))))
    uname: str | None = None
    sessionid: str | None = None
    for name, value in items:
        if name == "ds_user_id" and not uid and value.strip():
            uid = value.strip()
        elif name == "ds_user" and value.strip():
            uname = value.strip()
        elif name == "sessionid" and value.strip():
            sessionid = value.strip()
    if not uid and sessionid:
        # sessionid shape is "<ds_user_id>:<token>" — last-resort owner id
        # for dumps that carry no explicit ds_user_id cookie.
        uid = sessionid_owner_id(sessionid)
    return uid, uname


def sanitize_sessionid(raw: str) -> str:
    """Clean a pasted sessionid cookie (pure — unit tested).

    DevTools copies often carry surrounding quotes/whitespace, and the value
    is URL-encoded (``%3A`` instead of ``:``). instagrapi requires the raw
    ``<digits>:<token>`` shape, so decode percent-escapes here.
    """
    import re
    from urllib.parse import unquote

    s = (raw or "").strip()
    if len(s) >= 2 and s[0] == s[-1] and s[0] in ("'", '"'):
        s = s[1:-1].strip()
    s = unquote(s)
    return re.sub(r"\s+", "", s)


def sessionid_owner_id(sessionid: str) -> "str | None":
    r"""Leading user id of a sessionid (``^\d+``), or None if malformed."""
    import re

    m = re.search(r"^\d+", sessionid or "")
    return m.group() if m else None


def sessionid_looks_valid(sessionid: str) -> bool:
    """Mirror of instagrapi's own login_by_sessionid preconditions."""
    return isinstance(sessionid, str) and len(sessionid) > 30 and sessionid_owner_id(sessionid) is not None


def parse_cookies_file(text: str) -> dict:
    """Parse a browser cookie export into {name: value} (pure — unit tested).

    Accepts Netscape cookies.txt (tab-separated, skips comments/blank lines)
    and JSON objects (flat {name: value} or {"cookies": [...]}/{...} shapes
    as exported by common cookie-editor extensions).
    """
    import json

    cookies: dict[str, str] = {}
    stripped = (text or "").strip()
    if not stripped:
        return cookies
    if stripped[0] in ("{", "["):
        try:
            data = json.loads(stripped)
        except ValueError:
            return cookies
        if isinstance(data, dict):
            items = data.get("cookies", data)
            if isinstance(items, dict):
                for k, v in items.items():
                    if isinstance(v, dict) and "value" in v:
                        v = v["value"]
                    cookies[str(k)] = str(v)
            elif isinstance(items, list):
                for entry in items:
                    if isinstance(entry, dict) and "name" in entry:
                        cookies[str(entry["name"])] = str(entry.get("value", ""))
        elif isinstance(data, list):
            for entry in data:
                if isinstance(entry, dict) and "name" in entry:
                    cookies[str(entry["name"])] = str(entry.get("value", ""))
        return cookies
    for line in stripped.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) >= 7:
            cookies[parts[5]] = parts[6]
    return cookies


def extract_sessionid(cookies: dict) -> "str | None":
    """Sanitized sessionid from a cookie mapping, or None."""
    for key in ("sessionid", "SessionID", "SESSIONID"):
        if cookies.get(key):
            candidate = sanitize_sessionid(str(cookies[key]))
            if sessionid_looks_valid(candidate):
                return candidate
    return None


def device_settings_for(username: str) -> dict:
    """Deterministic per-account device fingerprint (stable across restarts).

    Hardware profile matches instagrapi defaults; only `device` and `uuid`
    vary per account so fingerprints stay consistent for each account.
    """
    import hashlib

    h = hashlib.md5(username.encode()).hexdigest()
    return {
        "app_version": APP_VERSION,
        "version_code": VERSION_CODE,
        "bloks_versioning_id": BLOKS_VERSIONING_ID,
        "android_version": ANDROID_VERSION,
        "android_release": ANDROID_RELEASE,
        "dpi": DPI,
        "resolution": RESOLUTION,
        "manufacturer": MANUFACTURER,
        "device": f"husky_{h[:4]}",
        "model": MODEL,
        "cpu": CPU,
        "uuid": h,
    }
