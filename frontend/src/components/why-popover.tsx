"use client";
import { useEffect, useRef, useState, type ReactNode } from "react";
import { Info } from "lucide-react";

/** "Why?" button: reveals a short explanation popover (e.g. a post's fail reason).
 *  Opens upward/left-aligned so it never clips inside scrollable tables. */
export function WhyPopover({ label, children }: { label: string; children: ReactNode }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLSpanElement>(null);

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
    <span ref={ref} className="relative inline-flex align-middle">
      <button
        onClick={() => setOpen((o) => !o)}
        className="rounded-full p-0.5 text-zinc-400 transition hover:bg-zinc-100 hover:text-zinc-600 dark:hover:bg-zinc-800 dark:hover:text-zinc-300"
        aria-label={label}
        aria-expanded={open}
        title={label}
      >
        <Info className="h-3.5 w-3.5" />
      </button>
      {open && (
        <span
          role="note"
          className="animate-fade-up absolute bottom-full left-0 z-30 mb-2 w-64 rounded-xl border border-zinc-200 bg-white p-3 text-left text-xs font-normal normal-case shadow-xl dark:border-zinc-700 dark:bg-zinc-900"
        >
          {children}
        </span>
      )}
    </span>
  );
}
