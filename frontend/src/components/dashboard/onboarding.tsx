"use client";
import { useState } from "react";
import Link from "next/link";
import { ArrowRight, Check, X } from "lucide-react";
import { Card } from "@/components/ui";
import { useAccounts, useProxies, useRules, useVideos } from "@/hooks/use-api";
import { cn } from "@/lib/utils";

const DISMISS_KEY = "igf:onboarding-dismissed";

function dismissed(): boolean {
  try {
    return localStorage.getItem(DISMISS_KEY) === "1";
  } catch {
    return false;
  }
}

/** First-run checklist: guides a fresh install through the four setup steps.
 *  Disappears once everything is done or the user dismisses it. */
export function OnboardingChecklist() {
  const [gone, setGone] = useState(() => dismissed());
  const { data: accounts } = useAccounts();
  const { data: proxies } = useProxies();
  const { data: videos } = useVideos();
  const { data: rules } = useRules();

  if (gone) return null;

  const steps = [
    {
      label: "Connect an Instagram account",
      done: (accounts ?? []).length > 0,
      href: "/dashboard/accounts",
    },
    {
      label: "Add proxies for safe scraping",
      done: (proxies ?? []).length > 0,
      href: "/dashboard/proxies",
    },
    {
      label: "Upload your first video",
      done: (videos ?? []).length > 0,
      href: "/dashboard/videos/upload",
    },
    {
      label: "Create a schedule rule",
      done: (rules ?? []).length > 0,
      href: "/dashboard/schedule",
    },
  ];

  if (steps.every((s) => s.done)) return null;

  const doneCount = steps.filter((s) => s.done).length;

  return (
    <Card className="relative overflow-hidden">
      <div
        className="pointer-events-none absolute inset-y-0 left-0 w-1"
        style={{ background: "var(--ig-gradient)" }}
        aria-hidden
      />
      <div className="flex items-start justify-between gap-2 pl-2">
        <div>
          <p className="font-semibold">Get set up</p>
          <p className="text-xs text-zinc-500">
            {doneCount} of {steps.length} steps complete — your first automated post is close.
          </p>
        </div>
        <button
          onClick={() => {
            try {
              localStorage.setItem(DISMISS_KEY, "1");
            } catch {
              /* ignore */
            }
            setGone(true);
          }}
          className="btn-ghost shrink-0 !px-2 !py-1"
          aria-label="Dismiss setup checklist"
        >
          <X className="h-4 w-4" />
        </button>
      </div>
      <div className="mt-3 space-y-1 pl-2">
        {steps.map((s) => (
          <Link
            key={s.label}
            href={s.href}
            className={cn(
              "group flex items-center gap-3 rounded-lg px-2 py-1.5 text-sm transition hover:bg-zinc-50 dark:hover:bg-zinc-800/60",
              s.done && "opacity-60"
            )}
          >
            <span
              className={cn(
                "flex h-5 w-5 shrink-0 items-center justify-center rounded-full border",
                s.done
                  ? "border-transparent bg-emerald-500 text-white"
                  : "border-zinc-300 text-transparent dark:border-zinc-600"
              )}
            >
              <Check className="h-3 w-3" />
            </span>
            <span className={cn("flex-1", s.done && "line-through")}>{s.label}</span>
            {!s.done && (
              <ArrowRight className="h-4 w-4 shrink-0 text-zinc-300 transition group-hover:translate-x-0.5 group-hover:text-zinc-500" />
            )}
          </Link>
        ))}
      </div>
      <div className="mt-3 h-1.5 overflow-hidden rounded-full bg-zinc-100 pl-2 dark:bg-zinc-800">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${(doneCount / steps.length) * 100}%`, background: "var(--ig-gradient)" }}
        />
      </div>
    </Card>
  );
}
