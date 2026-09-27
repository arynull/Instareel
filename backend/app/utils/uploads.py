"""Shared guards for multipart upload endpoints."""
from fastapi import HTTPException, Request


def reject_oversize_content_length(request: Request, max_bytes: int, what: str) -> None:
    """Fail fast on a declared Content-Length over the cap, before streaming.

    The streaming loops still enforce the cap for chunked bodies or lying
    headers — this just avoids pointlessly reading up to max_bytes when the
    client already told us the body is too big.
    """
    clen = request.headers.get("content-length")
    if clen is None:
        return
    try:
        declared = int(clen)
    except (TypeError, ValueError):
        return  # malformed — the streaming cap still applies
    if declared > max_bytes:
        raise HTTPException(
            413, f"{what} exceeds the {max_bytes // (1024 * 1024)}MB limit"
        )
