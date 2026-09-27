"""Shared guards for multipart upload endpoints."""
from fastapi import HTTPException, Request

#: Fail-fast slack for the Content-Length pre-check below. The header
#: covers the whole multipart body (boundaries + part headers), not just
#: the file — without slack, a file of *exactly* max_bytes would 413 here
#: even though the streaming loop would have accepted it. Real overhead is
#: a few hundred bytes; 8 KiB is generous. Oversized bodies are still
#: caught either way — the streaming cap is authoritative.
_MULTIPART_OVERHEAD_SLACK = 8 * 1024


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
    if declared > max_bytes + _MULTIPART_OVERHEAD_SLACK:
        raise HTTPException(
            413, f"{what} exceeds the {max_bytes // (1024 * 1024)}MB limit"
        )
