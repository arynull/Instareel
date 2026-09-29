"use client";
import { useEffect, useRef, useState } from "react";
import { Check, Columns2, Rows3, SwatchBook } from "lucide-react";
import { ACCENTS, type AccentName, type Density } from "@/lib/display-prefs";
import { useDisplay } from "@/stores/display";
import { cn } from "@/lib/utils";

const DENSITIES: { id: Density; label: string; hint: string; icon: typeof Rows3 }[] = [
  { id: "comfortable", label: "Comfortable", hint: "Roomy spacing", icon: Rows3 },
  { id: "compact", label: "Compact", hint: "Fit more on screen", icon: Columns2 },
];

/** Accent color + density picker, shown in the top bar next to the theme toggle. */
export function DisplaySettings() {
  const { accent, density, setAccent, setDensity } = useDisplay();
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") setOpen(false);
    };
    window.addEventListener("pointerdown", onDown);
    window.addEventListener("keydown", onKey);
    return () => {
      window.removeEventListener("pointerdown", onDown);
      window.removeEventListener("keydown", onKey);
    };
  }, [open ]);

  return (
    <div ref={ref} className="relative shrink-0">
      <button
        onClick={() => setOpen((o) => !o)}
        className="btn-ghost !px-2"
        aria-label="Display settings"
        aria-expanded={open}
        title="Display settings — accent color and density"
      >
        <SwatchBook className="h-4 w-4" />
      </button>
      {open && (
        <div
          className="card animate-fade-up absolute right-0 z-50 mt-2 w-60 !p-3 shadow-xl"
          role="dialog"
          aria-label="Display settings"
        >
          <p className="px-1 pb-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Accent color
          </p>
          <div className="grid grid-cols-4 gap-1.5">
            {(Object.keys(ACCENTS) as AccentName[]).map((name) => (
              <button
                key={name}
                onClick={() => setAccent(name)}
                className={cn(
                  "flex flex-col items-center gap-1 rounded-lg p-2 transition hover:bg-zinc-100 dark:hover:bg-zinc-800",
                  accent === name && "bg-zinc-100 dark:bg-zinc-800"
                )}
                title={ACCENTS[name].label}
                aria-pressed={accent === name}
              >
                <span
                  className="flex h-7 w-7 items-center justify-center rounded-full"
                  style={{ backgroundColor: ACCENTS[name].brand }}
                >
                  {accent === name && <Check className="h-4 w-4 text-white" />}
                </span>
                <span className="text-[10px] font-medium text-zinc-500">{ACCENTS[name].label}</span>
              </button>
            ))}
          </div>
          <p className="px-1 pb-2 pt-3 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
            Density
          </p>
          <div className="grid grid-cols-2 gap-1.5">
            {DENSITIES.map(({ id, label, hint, icon: Icon }) => (
              <button
                key={id}
                onClick={() => setDensity(id)}
                className={cn(
                  "flex items-center gap-2 rounded-lg border p-2 text-left transition",
                  density === id
                    ? "border-transparent bg-zinc-100 dark:bg-zinc-800"
                    : "border-zinc-200 hover:bg-zinc-50 dark:border-zinc-700 dark:hover:bg-zinc-800/50"
                )}
                aria-pressed={density === id}
              >
                <Icon className="h-4 w-4 shrink-0 text-zinc-400" />
                <span>
                  <span className="block text-xs font-semibold">{label}</span>
                  <span className="block text-[10px] text-zinc-400">{hint}</span>
                </span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  );
}
