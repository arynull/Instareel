"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Clapperboard, Clock, FlaskConical, LayoutGrid } from "lucide-react";
import { api } from "@/lib/api";
import type { Account, Bio, IgProfile, Post, Video } from "@/types/models";
import { useBlobUrl } from "./blob";
import { comparePostRecency } from "./sort";

function Avatar({ url, username, size }: { url: string | null; username: string; size: string }) {
  if (url) {
    return <img src={url} alt={username} className={`${size} rounded-full object-cover`} />;
  }
  return (
    <div className={`${size} flex items-center justify-center rounded-full bg-zinc-200 text-zinc-600 dark:bg-zinc-800 dark:text-zinc-300`}>
      <span className="text-lg font-bold">{username.slice(0, 2).toUpperCase()}</span>
    </div>
  );
}

function GridThumb({
  videoId,
  badge,
  onPick,
}: {
  videoId: number | null;
  badge?: "scheduled" | "trial" | null;
  onPick: () => void;
}) {
  const { url, failed } = useBlobUrl("thumbnail", videoId);
  return (
    <button onClick={onPick} className="relative aspect-square w-full overflow-hidden bg-zinc-100 dark:bg-zinc-800">
      {url ? (
        <img src={url} alt="" className="h-full w-full object-cover" />
      ) : (
        <span className="flex h-full items-center justify-center text-lg text-zinc-400" title={failed ? "Thumbnail unavailable" : videoId === null ? "Video file missing" : undefined}>
          {videoId === null ? "⚠" : "▦"}
        </span>
      )}
      {badge === "scheduled" && (
        <span className="absolute right-1 top-1 rounded-full bg-black/60 p-1 text-white" title="Scheduled">
          <Clock className="h-3 w-3" />
        </span>
      )}
      {badge === "trial" && (
        <span className="absolute right-1 top-1 rounded-full bg-black/60 p-1 text-white" title="Trial reel">
          <FlaskConical className="h-3 w-3" />
        </span>
      )}
    </button>
  );
}

type GridTab = "posts" | "reels" | "trial";

export function PhoneProfile({
  account,
  bio,
  posts,
  videos,
  onPickPost,
}: {
  account: Account;
  bio: Bio | null;
  posts: Post[];
  videos: Video[];
  onPickPost: (post: Post) => void;
}) {
  const router = useRouter();
  const [live, setLive] = useState<IgProfile | null>(null);
  const [liveFailed, setLiveFailed] = useState(false);
  const [gridTab, setGridTab] = useState<GridTab>("posts");
  const [copied, setCopied] = useState(false);
  const [shareError, setShareError] = useState(false);

  useEffect(() => {
    if (!bio) {
      setLive(null);
      return;
    }
    let cancelled = false;
    setLiveFailed(false);
    api
      .get(`/bios/${bio.id}/current`)
      .then(({ data }) => {
        if (!cancelled) setLive(data as IgProfile);
      })
      .catch(() => {
        if (!cancelled) setLiveFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [bio]);

  const byVideo = new Map(videos.map((v) => [v.id, v]));
  const mine = posts
    .filter((p) => p.account_id === account.id && (p.status === "posted" || p.status === "scheduled"))
    .sort(comparePostRecency);
  const gridItems =
    gridTab === "reels"
      ? mine.filter((p) => p.status === "posted" && !p.is_trial)
      : gridTab === "trial"
        ? mine.filter((p) => p.is_trial)
        : mine;

  const usingLive = live !== null;
  // Live values ONLY — falling back to the staged config here once showed
  // typed-but-unapplied drafts as if they were on Instagram. Drafts get
  // their own explicit badges below instead.
  const displayName = live?.full_name || account.username;
  const biography = live?.biography || "";
  const link = live?.external_url || "";
  const followers = live?.follower_count ?? null;
  const following = live?.following_count ?? null;
  const postCount = live?.media_count ?? mine.filter((p) => p.status === "posted").length;
  // Staged values that differ from what's actually on Instagram.
  const pending: string[] = [];
  if (usingLive && bio) {
    if ((bio.text || "") !== (live?.biography || "")) pending.push("bio");
    if ((bio.link_url || "") !== (live?.external_url || "")) pending.push("link");
    if ((bio.full_name || "") !== (live?.full_name || "")) pending.push("name");
    if (bio.has_picture) pending.push("picture");
    if (bio.make_private !== null && bio.make_private !== undefined && bio.make_private !== live?.is_private) pending.push("privacy");
  }

  async function shareProfile() {
    const url = `https://www.instagram.com/${account.username}/`;
    try {
      await navigator.clipboard.writeText(url);
      setCopied(true);
      setShareError(false);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
      setShareError(true);
      setTimeout(() => setShareError(false), 3000);
    }
  }

  return (
    <div className="flex h-full min-w-0 flex-col overflow-x-hidden overflow-y-auto">
      <div className="flex min-w-0 items-center justify-center gap-1 border-b border-zinc-200 py-2 text-sm font-bold dark:border-zinc-800">
        <span title={account.username} className="min-w-0 max-w-full truncate px-2">@{account.username}</span>
        {live?.is_private ? <span title="Private" className="shrink-0">🔒</span> : null}
      </div>
      <div className="flex min-w-0 items-center gap-4 px-4 pt-3">
        <Avatar url={live?.profile_pic_url ?? null} username={account.username} size="h-16 w-16 shrink-0" />
        <div className="flex min-w-0 flex-1 justify-around text-center">
          <div className="min-w-0">
            <p className="truncate font-bold">{postCount}</p>
            <p className="text-xs text-zinc-500">posts</p>
          </div>
          <div className="min-w-0">
            <p className="truncate font-bold">{followers ?? "—"}</p>
            <p className="text-xs text-zinc-500">followers</p>
          </div>
          <div className="min-w-0">
            <p className="truncate font-bold">{following ?? "—"}</p>
            <p className="text-xs text-zinc-500">following</p>
          </div>
        </div>
      </div>
      <div className="min-w-0 px-4 pb-2 pt-1 text-[13px]">
        <p className="break-words font-semibold">{displayName}</p>
        {biography && <p className="whitespace-pre-wrap break-words">{biography}</p>}
        {link && (/^https?:\/\//i.test(link) ? (
          <a href={link} target="_blank" rel="noreferrer" title={link} className="block truncate text-sky-600 dark:text-sky-400">
            {link}
          </a>
        ) : (
          <span className="block truncate text-zinc-500" title={link}>{link}</span>
        ))}
        {bio === null && <p className="text-zinc-500">No profile config — create one in Profile.</p>}
        {!usingLive && bio !== null && !liveFailed && (
          <p className="text-xs text-amber-600">Loading live profile… showing nothing until Instagram answers.</p>
        )}
        {!usingLive && bio !== null && (
          <p className="break-words text-xs text-amber-600">
            Draft on file{bio.text ? `: “${bio.text.slice(0, 80)}${bio.text.length > 80 ? "…" : ""}”` : ""} — not on Instagram until you Apply it.
          </p>
        )}
        {usingLive && pending.length > 0 && (
          <p className="text-xs text-amber-600">Unapplied draft changes: {pending.join(", ")}.</p>
        )}
        {liveFailed && <p className="text-xs text-amber-600">Live data unavailable (session/proxy).</p>}
      </div>
      <div className="flex gap-2 px-4 pb-2">
        <button
          className="flex-1 rounded-lg bg-sky-500 py-1.5 text-[13px] font-semibold text-white"
          onClick={() => router.push("/dashboard/bios")}
        >
          Edit profile
        </button>
        <button
          className="flex-1 rounded-lg bg-zinc-200 py-1.5 text-[13px] font-semibold text-zinc-800 dark:bg-zinc-800 dark:text-zinc-100"
          onClick={shareProfile}
        >
          {copied ? "Link copied ✓" : shareError ? "Copy failed ✗" : "Share profile"}
        </button>
      </div>
      <div className="flex justify-around border-t border-zinc-200 dark:border-zinc-800">
        {(
          [
            { id: "posts", icon: LayoutGrid, label: "Posts" },
            { id: "reels", icon: Clapperboard, label: "Reels" },
            { id: "trial", icon: FlaskConical, label: "Trial" },
          ] as const
        ).map(({ id, icon: Icon, label }) => (
          <button
            key={id}
            aria-label={label}
            onClick={() => setGridTab(id)}
            className={`flex-1 py-1.5 ${gridTab === id ? "text-zinc-900 dark:text-white" : "text-zinc-400"}`}
          >
            <Icon className="mx-auto h-5 w-5" />
          </button>
        ))}
      </div>
      <div className="grid flex-1 grid-cols-3 gap-px bg-zinc-200 content-start dark:bg-zinc-800">
        {gridItems.map((p) => {
          const v = byVideo.get(p.video_id);
          return (
            <GridThumb
              key={p.id}
              videoId={v ? v.id : null}
              badge={p.status === "scheduled" ? "scheduled" : p.is_trial ? "trial" : null}
              onPick={() => onPickPost(p)}
            />
          );
        })}
      </div>
    </div>
  );
}
