"use client";
import { useEffect, useRef, useState } from "react";
import type { Account, Post, Video } from "@/types/models";
import { useBlobUrl } from "./blob";
import { comparePostRecency } from "./sort";
import { fetchPreviewToken, previewStreamUrl } from "./stream";
import { fmt } from "@/lib/utils";

function Reel({
  post,
  video,
  username,
  onPick,
}: {
  post: Post;
  video: Video | undefined;
  username: string;
  onPick: () => void;
}) {
  const rootRef = useRef<HTMLDivElement>(null);
  const videoRef = useRef<HTMLVideoElement>(null);
  const [inView, setInView] = useState(false);
  const [token, setToken] = useState<string | null>(null);
  const [tokenFailed, setTokenFailed] = useState(false);
  // Small poster while the stream token loads (thumbnail blobs are cheap;
  // the full video is never blob-downloaded — it streams via Range requests).
  // Lazy like the token: no fetch until the reel is on screen.
  const { url: poster } = useBlobUrl("thumbnail", inView ? (video?.id ?? null) : null);

  useEffect(() => {
    const el = rootRef.current;
    if (!el) return;
    const io = new IntersectionObserver(([entry]) => setInView(entry.isIntersecting), {
      threshold: 0.4,
    });
    io.observe(el);
    return () => io.disconnect();
  }, []);

  // Fetch the short-lived playback token lazily, only once the reel is
  // actually on screen — never for the whole list up front.
  useEffect(() => {
    if (!inView || !video || token || tokenFailed) return;
    let cancelled = false;
    fetchPreviewToken(video.id).then(
      (t) => {
        if (!cancelled) setToken(t);
      },
      () => {
        if (!cancelled) setTokenFailed(true);
      },
    );
    return () => {
      cancelled = true;
    };
  }, [inView, video, token, tokenFailed]);

  // Pause when scrolled away — the snap container keeps every reel mounted.
  useEffect(() => {
    if (!inView) videoRef.current?.pause();
  }, [inView]);

  const src = token && video ? previewStreamUrl(video.id, token) : null;

  return (
    <div ref={rootRef} className="relative h-full w-full shrink-0 snap-start snap-always bg-black">
      {src ? (
        <video
          ref={videoRef}
          src={src}
          poster={poster ?? undefined}
          className="h-full w-full object-contain"
          controls
          playsInline
          preload="metadata"
        />
      ) : (
        <div className="flex h-full items-center justify-center text-sm text-zinc-500">
          {!video ? (
            "No file yet"
          ) : tokenFailed ? (
            "Unavailable — reprocess the video"
          ) : poster ? (
            <img src={poster} alt="" className="h-full w-full object-contain opacity-60" />
          ) : (
            "Loading…"
          )}
        </div>
      )}
      {/* pointer-events-none on the wrapper: the native control bar at the
        bottom must stay tappable. Only the caption itself re-enables clicks
        for the inspector. */}
      <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-black/70 to-transparent p-3 pt-8 text-left text-white">
        <button onClick={onPick} className="pointer-events-auto block w-full text-left">
          <p className="text-sm font-bold">@{username}</p>
          {(post.caption || post.hashtags) && (
            <p className="line-clamp-2 text-xs opacity-90">
              {[post.caption, post.hashtags].filter(Boolean).join(" ")}
            </p>
          )}
          <p className="mt-1 flex gap-3 text-xs opacity-90">
            <span>▶ {fmt(post.views_7d ?? post.views_24h)}</span>
            {post.audio_track && <span>♪ {post.audio_track}</span>}
            {post.is_trial && <span>trial</span>}
          </p>
        </button>
      </div>
    </div>
  );
}

export function PhoneReels({
  account,
  posts,
  videos,
  onPickPost,
}: {
  account: Account;
  posts: Post[];
  videos: Video[];
  onPickPost: (post: Post) => void;
}) {
  const byVideo = new Map(videos.map((v) => [v.id, v]));
  const mine = posts
    .filter((p) => p.account_id === account.id && p.status === "posted")
    .sort(comparePostRecency);

  if (mine.length === 0) {
    return (
      <div className="flex h-full items-center justify-center p-8 text-center text-sm text-zinc-500">
        No posted reels yet — publish from the composer or wait for the schedule.
      </div>
    );
  }
  return (
    <div className="h-full snap-y snap-mandatory overflow-y-auto">
      {mine.map((p) => (
        <Reel key={p.id} post={p} video={byVideo.get(p.video_id)} username={account.username} onPick={() => onPickPost(p)} />
      ))}
    </div>
  );
}
