"use client";
import { useEffect, useState } from "react";
import { useTheme } from "next-themes";
import { X } from "lucide-react";
import { nextTheme } from "@/lib/theme";

const SHORTCUTS: { keys: string[]; label: string }[] = [
  { keys: ["⌘K", "Ctrl K"], label: "Open the command palette" },
  { keys: ["?"], label: "Show / hide this help" },
  { keys: ["T"], label: "Cycle theme (light → dark → true-black)" },
  { keys: ["↑", "↓", "↵"], label: "Navigate the command palette" },
  { keys: ["Esc"], label: "Close dialogs" },
];

function isTypingTarget(t: EventTarget | null): boolean {
  const el = t as HTMLElement | null;
  return (
    !!el &&
    (el.tagName === "INPUT" ||
      el.tagName === "TEXTAREA" ||
      el.tagName === "SELECT" ||
      el.isContentEditable)
  );
}

/** Global keyboard shortcuts: ? opens this overlay, T cycles the theme. */
export function ShortcutHelp() {
  const [open, setOpen] = useState(false);
  const { theme, setTheme } = useTheme();

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.metaKey || e.ctrlKey || e.altKey || isTypingTarget(e.target)) return;
      if (e.key === "?") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key.toLowerCase() === "t") {
        setTheme(nextTheme(theme ?? "dark"));
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [theme, setTheme]);

  if (!open) return null;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-[15vh] backdrop-blur-sm"
      onClick={() => setOpen(false)}
      role="dialog"
      aria-modal="true"
      aria-label="Keyboard shortcuts"
    >
      <div
        className="card animate-fade-up w-full max-w-sm !p-0 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center justify-between border-b border-zinc-200 px-4 py-3 dark:border-zinc-800">
          <p className="text-sm font-semibold">Keyboard shortcuts</p>
          <button
            onClick={() => setOpen(false)}
            className="btn-ghost !px-2 !py-1"
            aria-label="Close shortcuts help"
          >
            <X className="h-4 w-4" />
          </button>
        </div>
        <div className="space-y-1 p-3">
          {SHORTCUTS.map((s) => (
            <div key={s.label} className="flex items-center justify-between gap-3 px-1 py-1.5 text-sm">
              <span className="text-zinc-600 dark:text-zinc-300">{s.label}</span>
              <span className="flex shrink-0 gap-1">
                {s.keys.map((k) => (
                  <kbd
                    key={k}
                    className="rounded-md border border-zinc-200 bg-zinc-50 px-1.5 py-0.5 text-[11px] font-semibold text-zinc-500 dark:border-zinc-700 dark:bg-zinc-800 dark:text-zinc-400"
                  >
                    {k}
                  </kbd>
                ))}
              </span>
            </div>
          ))}
        </div>
      </div>
    </div>
  );
}
