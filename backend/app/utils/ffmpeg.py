"""FFmpeg command builder + runner with progress parsing.

Both async (web API) and sync (Celery workers) runners are provided.
Celery must ONLY use the sync variants — never create an event loop in a task.
"""
import json
import logging
import os
import shlex
import subprocess

log = logging.getLogger("igfunnel.ffmpeg")

TARGET_W, TARGET_H = 720, 1280


def _parse_probe_json(raw: bytes) -> dict:
    info = json.loads(raw.decode() or "{}")
    streams = info.get("streams", [])
    video = next((s for s in streams if s.get("codec_type") == "video"), {})
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = float(info.get("format", {}).get("duration") or video.get("duration") or 0)
    raw_packets = video.get("nb_read_packets")
    video_packets: int | None = None
    if raw_packets is not None:
        try:
            video_packets = int(raw_packets)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            video_packets = None
    return {
        "duration": duration,
        "width": int(video.get("width") or 0),
        "height": int(video.get("height") or 0),
        "has_audio": audio is not None,
        # Populated only when probed with -count_packets. A header-only file
        # (streams listed, zero packets) passes every other check and then
        # dies in the encoder with "no packets" — reject it up front.
        "video_packets": video_packets,
    }


def _probe_args(path: str) -> list[str]:
    # -count_packets only demuxes (no decode) — fast, and exposes
    # header-only/broken files via nb_read_packets.
    return ["ffprobe", "-v", "quiet", "-print_format", "json",
            "-show_format", "-show_streams", "-count_packets", path]


def probe_sync(path: str) -> dict:
    """Blocking ffprobe (Celery-safe)."""
    proc = subprocess.run(
        _probe_args(path),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=60,
    )
    if proc.returncode != 0:
        raise RuntimeError("ffprobe failed — file is not a valid video")
    return _parse_probe_json(proc.stdout)


async def probe(path: str) -> dict:
    import asyncio

    proc = await asyncio.create_subprocess_exec(
        *_probe_args(path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=60)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        await proc.wait()
        raise RuntimeError("ffprobe timed out after 60s")
    if proc.returncode != 0:
        raise RuntimeError("ffprobe failed — file is not a valid video")
    return _parse_probe_json(out)


def build_filter(
    effect_filter: str = "",
    custom_filters: str = "",
    color_grade: str = "",
    has_audio: bool = True,
) -> str:
    """Return the video filter_complex (single input, [outv] output)."""
    parts: list[str] = []
    # Center-crop to 9:16, whichever axis overflows: landscape (and exact
    # 9:16) crops the sides, narrower-than-9:16 portraits crop top/bottom.
    # A fixed crop=ih*9/16:... dies with "Invalid too big ... size" on the
    # narrow ones (real case: 638px tall but <358px wide). Commas inside
    # min() are backslash-escaped — bare commas separate filters.
    parts.append(
        "crop=min(iw\\,ih*9/16):min(ih\\,iw*16/9)"
        ":(iw-min(iw\\,ih*9/16))/2:(ih-min(ih\\,iw*16/9))/2,"
        f"scale={TARGET_W}:{TARGET_H}:force_original_aspect_ratio=increase,"
        f"crop={TARGET_W}:{TARGET_H}"
    )
    if effect_filter.strip():
        parts.append(effect_filter.strip())
    if color_grade.strip():
        parts.append(color_grade.strip())
    if custom_filters.strip():
        parts.append(custom_filters.strip())
    parts.append("format=yuv420p")
    video_chain = ",".join(p for p in parts if p)
    return f"[0:v]{video_chain}[outv]"


def build_command(
    src: str,
    dst: str,
    trim_start: float | None = None,
    trim_end: float | None = None,
    effect_filter: str = "",
    custom_filters: str = "",
    color_grade: str = "",
    has_audio: bool = True,
    trending_audio: str | None = None,
    music_volume: float = 0.4,
    duck_original: bool = False,
    loop_audio_to: float | None = None,
) -> list[str]:
    """Build the ffmpeg command. Trending-audio modes (music file must exist):

    - mix (default): original audio at full volume + trending track at
      ``music_volume``, looped to the video length.
    - duck/replace (``duck_original=True``, or video without audio): the
      trending track becomes the only audio (at ``music_volume``).
    - no trending file: previous behavior is unchanged.
    """
    filter_complex = build_filter(
        effect_filter, custom_filters, color_grade,
        has_audio,
    )
    music = trending_audio if trending_audio and os.path.exists(trending_audio) else None
    vol = max(0.0, float(music_volume or 0.0))
    use_mix = bool(music and has_audio and not duck_original)

    cmd = ["ffmpeg", "-y"]
    if trim_start:
        cmd += ["-ss", str(trim_start)]
    if trim_end and trim_start is not None and trim_end > trim_start:
        cmd += ["-t", str(trim_end - trim_start)]
    elif trim_end:
        cmd += ["-t", str(trim_end)]
    cmd += ["-i", src]
    next_idx = 1
    music_idx: int | None = None
    if music:
        if loop_audio_to and loop_audio_to > 0:
            # Loop the track and cut the input at exactly the video length.
            cmd += ["-stream_loop", "-1", "-t", str(loop_audio_to)]
        music_idx = next_idx
        cmd += ["-i", music]
        next_idx += 1
    lavfi_idx: int | None = None
    if not music and not has_audio:
        # Silent track input is appended here (not in audio_args) so its
        # index is known for the -map below. Without a map the stream is
        # dropped whenever any other -map exists (video-only output).
        cmd += ["-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo"]
        lavfi_idx = next_idx
        next_idx += 1

    audio_args: list[str]
    if use_mix and music_idx is not None:
        # loudnorm lives INSIDE the graph: a stream fed from -filter_complex
        # cannot also take -af (ffmpeg rejects mixing simple+complex filters).
        filter_complex += (
            f";[{music_idx}:a]volume={vol}[a1]"
            f";[0:a][a1]amix=inputs=2:duration=first:dropout_transition=0,loudnorm[aout]"
        )
        audio_args = ["-map", "[aout]", "-c:a", "aac", "-b:a", "128k"]
    elif music_idx is not None:
        # Trending track is the only audio (ducked original or silent video).
        audio_args = ["-map", f"{music_idx}:a", "-c:a", "aac", "-b:a", "128k",
                      "-af", f"volume={vol},loudnorm"]
        if not loop_audio_to:
            audio_args = ["-shortest"] + audio_args
    elif has_audio:
        audio_args = ["-map", "0:a?", "-c:a", "aac", "-b:a", "128k", "-af", "loudnorm"]
    else:
        audio_args = ["-map", f"{lavfi_idx}:a", "-shortest", "-c:a", "aac"] if lavfi_idx is not None else []

    cmd += [
        "-filter_complex", filter_complex,
        "-map", "[outv]",
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-movflags", "+faststart",
    ]
    cmd += audio_args
    cmd += [dst]
    log.info("FFmpeg: %s", " ".join(shlex.quote(c) for c in cmd))
    return cmd


def _parse_time_token(line: str, duration: float) -> float | None:
    if "time=" not in line or duration <= 0:
        return None
    try:
        t = line.split("time=")[1].split()[0]
        h, m, s = t.split(":")
        secs = int(h) * 3600 + int(m) * 60 + float(s)
        return max(0.0, min(100.0, secs / duration * 100))
    except Exception:
        return None


def run_sync_with_progress(cmd: list[str], duration: float, on_progress) -> None:
    """Blocking ffmpeg runner (Celery-safe). Reads stderr line by line via Popen.

    Bounded: a hung encode is killed after a duration-scaled timeout so one
    bad file can never wedge the (solo-pool) worker forever.
    """
    timeout = max(600.0, (duration or 0) * 10.0)
    proc = subprocess.Popen(
        cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True, errors="replace", bufsize=1
    )
    assert proc.stderr is not None
    stderr_tail: list[str] = []
    try:
        # A timer enforces the bound even while blocked reading stderr: on
        # expiry the process is killed and wait() raises TimeoutExpired.
        import threading

        def _kill():
            try:
                proc.kill()  # no-op if already exited (no zombie: wait() reaps)
            except OSError:
                pass

        timer = threading.Timer(timeout, _kill)
        timer.daemon = True
        timer.start()
        try:
            for line in proc.stderr:
                line = line.strip()
                if line:
                    stderr_tail.append(line[-500:])
                    pct = _parse_time_token(line, duration)
                    if pct is not None:
                        on_progress(pct, "processing")
        finally:
            timer.cancel()
        rc = proc.wait()
    finally:
        try:
            proc.stderr.close()
        except Exception:
            pass
    if rc != 0:
        raise RuntimeError("FFmpeg failed: " + " | ".join(stderr_tail[-25:]))


async def run_with_progress(cmd: list[str], duration: float, on_progress) -> None:
    """Run ffmpeg, parsing `time=` tokens from stderr to report 0-100%.

    Bounded like run_sync_with_progress: a hung encode is killed after a
    duration-scaled timeout so one bad file can never wedge the caller
    forever.
    """
    import asyncio

    timeout = max(600.0, (duration or 0) * 10.0)
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.PIPE
    )
    assert proc.stderr is not None
    stderr_tail: list[str] = []

    async def _drain() -> None:
        async for raw in proc.stderr:
            line = raw.decode(errors="replace").strip()
            if line:
                stderr_tail.append(line[-500:])
                pct = _parse_time_token(line, duration)
                if pct is not None:
                    await on_progress(pct, "processing")
        await proc.wait()

    try:
        await asyncio.wait_for(_drain(), timeout=timeout)
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except OSError:
            pass
        await proc.wait()
        raise RuntimeError(f"FFmpeg timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        raise RuntimeError("FFmpeg failed: " + " | ".join(stderr_tail[-25:]))
