"use client";
import { Card, CardTitle } from "@/components/ui";
import { useFunnel } from "@/hooks/use-api";
import { fmt } from "@/lib/utils";
import { funnelConversions, funnelWidths, type FunnelStage } from "@/lib/dashboard";

const BAR_TONES = [
  "from-sky-500 to-sky-400",
  "from-violet-500 to-violet-400",
  "from-fuchsia-500 to-fuchsia-400",
  "from-amber-500 to-amber-400",
  "from-emerald-500 to-emerald-400",
];

/** Pipeline funnel: sources -> library -> ready -> scheduled -> posted,
 *  with stage-to-stage conversion rates. */
export function FunnelWidget() {
  const { data, isLoading } = useFunnel();
  const stages = (data?.stages ?? []) as FunnelStage[];
  const conversions = funnelConversions(stages);
  const widths = funnelWidths(stages);

  return (
    <Card className="animate-fade-up">
      <CardTitle>Pipeline funnel</CardTitle>
      {isLoading ? (
        <div className="space-y-3">
          {[0, 1, 2, 3, 4].map((i) => (
            <div key={i} className="skeleton h-9 rounded-lg" />
          ))}
        </div>
      ) : stages.length === 0 ? (
        <p className="text-sm text-zinc-500">No pipeline data yet.</p>
      ) : (
        <div className="space-y-2.5">
          {stages.map((s, i) => (
            <div key={s.key}>
              <div className="mb-1 flex items-baseline justify-between text-xs">
                <span className="font-medium text-zinc-600 dark:text-zinc-300">{s.label}</span>
                <span className="tnum font-bold text-zinc-900 dark:text-zinc-100">{fmt(s.count)}</span>
              </div>
              <div className="h-9 overflow-hidden rounded-lg bg-zinc-100 dark:bg-zinc-800">
                <div
                  className={`flex h-full items-center justify-end bg-gradient-to-r pr-2 transition-[width] duration-700 ${BAR_TONES[i % BAR_TONES.length]}`}
                  style={{ width: `${widths[i]}%` }}
                >
                  {conversions[i] !== null && (
                    <span className="tnum rounded bg-black/25 px-1.5 py-0.5 text-[10px] font-bold text-white">
                      {conversions[i]}%
                    </span>
                  )}
                </div>
              </div>
            </div>
          ))}
          <p className="pt-1 text-[11px] text-zinc-400">
            % badges show conversion from the previous stage.
          </p>
        </div>
      )}
    </Card>
  );
}
