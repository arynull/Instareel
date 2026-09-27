"use client";
import { useEffect, useState } from "react";
import { PhoneFrame } from "@/components/phone-frame";
import { AccountSwitcher } from "@/components/phone/account-switcher";
import { PhoneActivity } from "@/components/phone/activity";
import { PhoneComposer } from "@/components/phone/composer";
import { PhoneProfile } from "@/components/phone/profile";
import { PhoneReels } from "@/components/phone/reels";
import { PhoneTabs, type PhoneTab } from "@/components/phone/tabs";
import { Card, EmptyState, Spinner } from "@/components/ui";
import { useAccounts, useAudios, useBios, useEffects, useLogs, usePosts, useVideos } from "@/hooks/use-api";
import { usePhone } from "@/stores/stores";
import { fmt, timeAgo } from "@/lib/utils";
import type { Account, Bio, LogEntry, Post, Video } from "@/types/models";

export default function PhonePage() {
  const { data: accountRows, isLoading: accountsLoading } = useAccounts();
  const { data: bioRows } = useBios();
  const { currentAccountId, setCurrentAccountId } = usePhone();
  // Scope post fetching to the visible account: the API's default limit=50
  // is global, so client-side filtering alone drops older accounts' posts.
  const scopedAccountId =
    currentAccountId ?? ((accountRows as Account[] | undefined)?.[0]?.id ?? null);
  const { data: postRows } = usePosts("", { accountId: scopedAccountId, limit: 200 });
  const { data: videoRows } = useVideos("", { limit: 200 });
  const { data: logRows } = useLogs();
  const { data: effectRows } = useEffects();
  const { data: audioRows } = useAudios();
  const [tab, setTab] = useState<PhoneTab>("profile");
  const [selected, setSelected] = useState<Post | null>(null);

  const accounts = (accountRows ?? []) as Account[];
  const bios = (bioRows ?? []) as Bio[];
  const posts = (postRows ?? []) as Post[];
  const videos = (videoRows ?? []) as Video[];
  const logs = (logRows ?? []) as LogEntry[];

  const current = accounts.find((a) => a.id === currentAccountId) ?? accounts[0];

  // Default to the first account once the list loads.
  useEffect(() => {
    if (accounts.length > 0 && currentAccountId === null) setCurrentAccountId(accounts[0].id);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [accounts.length]);
  const bio = current ? bios.find((b) => b.account_id === current.id) ?? null : null;
  const selectedVideo = selected ? videos.find((v) => v.id === selected.video_id) : undefined;

  // Switching accounts resets the whole phone state: tab back to profile,
  // selection cleared, reels scroll to top via key change below.
  function pickAccount(id: number) {
    if (id === current?.id) return;
    setCurrentAccountId(id);
    setSelected(null);
    setTab("profile");
  }

  // Keep selection valid when switching accounts.
  useEffect(() => {
    if (selected && current && selected.account_id !== current.id) setSelected(null);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [current?.id]);

  if (accountsLoading) return <Spinner />;
  if (accounts.length === 0) {
    return <EmptyState title="No accounts" hint="Add an IG account first — the phone mirrors your accounts." />;
  }
  if (!current) return <Spinner />;

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold tracking-tight">Phone view</h1>
      <div className="flex flex-wrap items-start justify-center gap-6">
        <div className="w-[375px] max-w-full">
          <AccountSwitcher
            accounts={accounts}
            currentId={current.id}
            onPick={pickAccount}
          />
          <PhoneFrame>
            <div className="flex h-full flex-col">
              <div className="min-h-0 flex-1">
                {tab === "profile" && (
                  <PhoneProfile account={current} bio={bio} posts={posts} videos={videos} onPickPost={setSelected} />
                )}
                {tab === "reels" && (
                  <PhoneReels account={current} posts={posts} videos={videos} onPickPost={setSelected} />
                )}
                {tab === "composer" && (
                  <PhoneComposer
                    account={current}
                    effects={((effectRows ?? []) as { name: string }[]).map((e) => e.name)}
                    audios={((audioRows ?? []) as { name: string }[]).map((a) => a.name)}
                  />
                )}
                {tab === "activity" && <PhoneActivity account={current} posts={posts} logs={logs} />}
              </div>
              <PhoneTabs tab={tab} onChange={setTab} />
            </div>
          </PhoneFrame>
        </div>

        <Card className="w-full max-w-md overflow-hidden">
          <div className="mb-3 min-w-0 truncate text-sm font-semibold text-zinc-900 dark:text-zinc-100" title={current.username}>Inspector · @{current.username}</div>
          {selected ? (
            <div className="space-y-1 text-sm">
              <p><span className="text-zinc-500">Post</span> #{selected.id} · <span className="text-zinc-500">{selected.status}</span>{selected.is_trial ? " · trial" : ""}</p>
              {(selected.caption || selected.hashtags) && (
                <p className="break-words whitespace-pre-wrap">{[selected.caption, selected.hashtags].filter(Boolean).join(" ")}</p>
              )}
              <p className="break-words"><span className="text-zinc-500">Audio</span> {selected.audio_track ?? "—"}</p>
              <p><span className="text-zinc-500">Effect</span> {selectedVideo?.effect_preset ?? "none"}</p>
              <p>
                <span className="text-zinc-500">Views</span> {fmt(selected.views_7d ?? selected.views_24h)}
                {" · "}<span className="text-zinc-500">Eng.</span> {selected.engagement_rate ?? 0}%
              </p>
              <p><span className="text-zinc-500">Posted</span> {selected.posted_at ? timeAgo(selected.posted_at) : "—"}</p>
              {selected.ig_permalink && (
                <a className="text-emerald-500 hover:underline" href={selected.ig_permalink} target="_blank">
                  Open reel ↗
                </a>
              )}
            </div>
          ) : (
            <p className="text-sm text-zinc-500">
              Tap any reel or grid item inside the phone to inspect it here — caption, audio, effect, stats and the live link.
            </p>
          )}
          <div className="mt-3 border-t border-zinc-100 pt-2 text-xs text-zinc-500 dark:border-zinc-800">
            <p>Account: {current.posts_today}/{current.max_daily_posts} posts today · {current.status}</p>
            <p>Bio config: {bio ? "set — edit it in Profile editor" : "none — create one in Profile editor"}</p>
          </div>
        </Card>
      </div>
    </div>
  );
}
