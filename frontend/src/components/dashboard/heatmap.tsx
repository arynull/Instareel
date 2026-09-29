"use client";
import { useState } from "react";
import { Flame } from "lucide-react";
import { Card, CardTitle } from "@/components/ui";
import { useAccounts, useHeatmap } from "@/hooks/use-api";
import { fmt } from "@/lib/utils";
import { bestCells, cellLabel, heatIntensity } from "@/lib/dashboard";

const DOW_FALLBACK = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];

/** 7x24 engagement heatmap: when each account's audience actually watches. */
export function HeatmapWidget() {
  const { data: accounts } = useAccounts();
  const [accountId, setAccountId] = useState<number | "">("");
  const { data, isLoading } = useHeatmap(accountId);
  const cells = data?.cells ?? [];
  const dowLabels = data?.dow_labels ?? DOW_FALLBACK;
  const maxAvg = data?.max_avg_views ?? 0;
  const top = bestCells(cells, 3);

  const byDowHour = new Map(cells.map((c) => [`${c.dow}:${c.hour}`, c]));

  return (
    <Card className="animate-fade-up">
      <div className="mb-3 flex flex-wrap items-center gap-2">
        <CardTitle>Best time to post</CardTitle>
        <select
          className="input ml-auto !w-auto !py-1 text-xs"
          value={accountId}
          onChange={(e) => setAccountId(e.target.value === "" ? "" : Number(e.target.value))}
          aria-label="Heatmap account"
        >
          <option value="">All accounts</option>
          {((accounts ?? []) as { id: number; username: string }[]).map((a) => (
            <option key={a.id} value={a.id}>@{a.username}</option>
          ))}
        </select>
      </div>

      {isLoading ? (
        <div className="skeleton h-44 rounded-lg" />
      ) : cells.length === 0 || (data?.total_posts ?? 0) === 0 ? (
        <p className="text-sm text-zinc-500">No posted history yet — post a few reels first.</p>
      ) : (
        <>
          {!data?.personalized && accountId !== "" && (
            <p className="mb-2 text-xs text-amber-600">
              Not enough history for this account yet — showing global pattern.
            </p>
          )}
          <div className="overflow-x-auto">
            <div className="min-w-[560px]">
              <div className="mb-1 grid grid-cols-[2.2rem_repeat(24,1fr)] gap-[3px] text-[9px] text-zinc-400">
                <span />
                {Array.from({ length: 24 }, (_, h) => (
                  <span key={h} className="tnum text-center">{h % 3 === 0 ? `${h}h` : ""}</span>
                ))}
              </div>
              {dowLabels.map((day, dow) => (
                <div key={day} className="mb-[3px] grid grid-cols-[2.2rem_repeat(24,1fr)] gap-[3px]">
                  <span className="pr-1 text-right text-[10px] font-medium leading-4 text-zinc-500">{day}</span>
                  {Array.from({ length: 24 }, (_, hour) => {
                    const c = byDowHour.get(`${dow}:${hour}`);
                    const intensity = heatIntensity(c?.avg_views ?? 0, maxAvg);
                    return (
                      <div
                        key={hour}
                        title={c ? `${cellLabel(c, dowLabels)} · ${fmt(c.avg_views)} avg views · ${c.posts} posts` : ""}
                        className="h-4 rounded-[3px] transition-transform hover:scale-125 bg-zinc-100 dark:bg-zinc-800/60"
                        style={intensity > 0 ? { backgroundColor: `rgb(16 185 129 / ${0.15 + intensity * 0.85})` } : undefined}
                      />
                    );
                  })}
                </div>
              ))}
            </div>
          </div>
          <div className="mt-2 flex items-center gap-1.5 text-[10px] text-zinc-400">
            <span>Less</span>
            {[0, 0.25, 0.5, 0.75, 1].map((i) => (
              <span
                key={i}
                className="h-3 w-3 rounded-[3px]"
                style={{ backgroundColor: i === 0 ? undefined : `rgb(16 185 129 / ${0.15 + i * 0.85})` }}
              />
            ))}
            <span>More</span>
            <span className="ml-auto">avg views · {data?.tz_label} time</span>
          </div>
          {top.length > 0 && (
            <div className="mt-3 flex flex-wrap gap-2">
              {top.map((c, i) => (
                <span
                  key={`${c.dow}:${c.hour}`}
                  className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-2.5 py-1 text-xs font-bold text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                >
                  <Flame className="h-3 w-3" />
                  #{i + 1} {cellLabel(c, dowLabels)}
                  <span className="tnum font-medium opacity-80">{fmt(c.avg_views)}</span>
                </span>
              ))}
            </div>
          )}
        </>
      )}
    </Card>
  );
}
