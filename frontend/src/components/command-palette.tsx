"use client";
import { useEffect, useMemo, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { useQueryClient } from "@tanstack/react-query";
import { Command, CornerDownLeft, RefreshCw, Search, Smartphone, Upload } from "lucide-react";
import { NAV } from "@/components/nav";
import { nextTheme } from "@/lib/theme";
import { filterActions } from "@/lib/palette";
import { loadRecentIds, recordRecentId } from "@/lib/palette-recents";
import { cn } from "@/lib/utils";

interface Action {
  id: string;
  label: string;
  hint?: string;
  icon: (typeof Search);
  run: () => void;
}

/** Programmatic opener (e.g. the TopBar search button). */
export function openCommandPalette() {
  window.dispatchEvent(new CustomEvent("igf:open-palette"));
}

/** ⌘K / Ctrl+K command palette: jump anywhere, flip theme, open health. */
export function CommandPalette() {
  const router = useRouter();
  const queryClient = useQueryClient();
  const { theme, setTheme } = useTheme();
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const [cursor, setCursor] = useState(0);
  const [recents, setRecents] = useState<string[]>(() => loadRecentIds());
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setOpen((o) => !o);
      } else if (e.key === "Escape") {
        setOpen(false);
      }
    };
    window.addEventListener("keydown", onKey);
    const onOpen = () => setOpen(true);
    window.addEventListener("igf:open-palette", onOpen);
    return () => {
      window.removeEventListener("keydown", onKey);
      window.removeEventListener("igf:open-palette", onOpen);
    };
  }, []);

  useEffect(() => {
    if (open) {
      setQuery("");
      setCursor(0);
      // wait a frame so the input exists before focusing
      requestAnimationFrame(() => inputRef.current?.focus());
    }
  }, [open ]);

  const actions: Action[] = useMemo(
    () => [
      {
        id: "action:refresh-all",
        label: "Refresh all data",
        hint: "refetch every dashboard query",
        icon: RefreshCw,
        run: () => {
          queryClient.invalidateQueries();
        },
      },
      {
        id: "action:upload-video",
        label: "Upload video",
        hint: "new video → processing queue",
        icon: Upload,
        run: () => router.push("/dashboard/videos/upload"),
      },
      {
        id: "action:phone-composer",
        label: "Open phone composer",
        hint: "compose a post in phone view",
        icon: Smartphone,
        run: () => router.push("/dashboard/phone"),
      },
      ...NAV.map(({ href, label, icon }) => ({
        id: `nav:${href}`,
        label: `Go to ${label}`,
        hint: href,
        icon,
        run: () => router.push(href),
      })),
      {
        id: "action:theme",
        label: "Toggle theme",
        hint: "light → dark → true-black",
        icon: Command,
        run: () => setTheme(nextTheme(theme ?? "dark")),
      },
    ],
    [router, queryClient, setTheme, theme]
  );

  const actionById = useMemo(() => new Map(actions.map((a) => [a.id, a])), [actions]);

  const filtered = useMemo(
    () => filterActions(actions, query),
    [actions, query]
  );

  const recentActions = useMemo(
    () => recents.map((id) => actionById.get(id)).filter((a): a is Action => !!a),
    [recents, actionById]
  );

  // Sections: recents (only when the query is empty), then the matches.
  const sections = useMemo(() => {
    const recentIds = new Set(recentActions.map((a) => a.id));
    if (query.trim() === "" && recentActions.length > 0) {
      return [
        { title: "Recent", items: recentActions },
        { title: "All commands", items: filtered.filter((a) => !recentIds.has(a.id)) },
      ];
    }
    return [{ title: null as string | null, items: filtered }];
  }, [query, recentActions, filtered]);

  const flat = useMemo(() => sections.flatMap((s) => s.items), [sections]);

  useEffect(() => setCursor(0), [query]);
  const clamped = Math.min(cursor, Math.max(0, flat.length - 1));

  function choose(a: Action) {
    setRecents(recordRecentId(a.id));
    setOpen(false);
    a.run();
  }

  if (!open) return null;

  let row = -1;

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center bg-black/50 p-4 pt-[12vh] backdrop-blur-sm"
      onClick={() => setOpen(false)}
      role="dialog"
      aria-modal="true"
      aria-label="Command palette"
    >
      <div
        className="card animate-fade-up w-full max-w-lg overflow-hidden !p-0 shadow-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-zinc-200 px-4 dark:border-zinc-800">
          <Search className="h-4 w-4 shrink-0 text-zinc-400" />
          <input
            ref={inputRef}
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "ArrowDown") {
                e.preventDefault();
                setCursor((c) => Math.min(c + 1, flat.length - 1));
              } else if (e.key === "ArrowUp") {
                e.preventDefault();
                setCursor((c) => Math.max(c - 1, 0));
              } else if (e.key === "Enter" && flat[clamped]) {
                choose(flat[clamped]);
              }
            }}
            placeholder="Type a command or search pages…"
            className="w-full bg-transparent py-3 text-sm outline-none placeholder:text-zinc-400"
          />
          <kbd className="shrink-0 rounded border border-zinc-200 px-1.5 py-0.5 text-[10px] text-zinc-400 dark:border-zinc-700">
            esc
          </kbd>
        </div>
        <div className="max-h-72 overflow-y-auto p-2">
          {flat.length === 0 ? (
            <p className="px-3 py-6 text-center text-sm text-zinc-500">No matches for “{query}”.</p>
          ) : (
            sections.map((s, si) => (
              <div key={s.title ?? `s${si}`}>
                {s.title && (
                  <p className="px-3 pb-1 pt-2 text-[11px] font-semibold uppercase tracking-wide text-zinc-400">
                    {s.title}
                  </p>
                )}
                {s.items.map((a) => {
                  row += 1;
                  const i = row;
                  return (
                    <button
                      key={a.id}
                      onMouseEnter={() => setCursor(i)}
                      onClick={() => choose(a)}
                      className={cn(
                        "flex w-full items-center gap-3 rounded-lg px-3 py-2 text-left text-sm transition",
                        i === clamped ? "bg-zinc-100 dark:bg-zinc-800" : "text-zinc-600 dark:text-zinc-300"
                      )}
                    >
                      <a.icon className="h-4 w-4 shrink-0 text-zinc-400" />
                      <span className="min-w-0 flex-1 truncate font-medium">{a.label}</span>
                      {a.hint && <span className="shrink-0 text-xs text-zinc-400">{a.hint}</span>}
                      {i === clamped && <CornerDownLeft className="h-3.5 w-3.5 shrink-0 text-zinc-400" />}
                    </button>
                  );
                })}
              </div>
            ))
          )}
        </div>
        <div className="border-t border-zinc-200 px-4 py-2 text-[11px] text-zinc-400 dark:border-zinc-800">
          <kbd className="rounded border border-zinc-200 px-1 dark:border-zinc-700">↑↓</kbd> navigate
          {" · "}
          <kbd className="rounded border border-zinc-200 px-1 dark:border-zinc-700">↵</kbd> open
          {" · "}
          <kbd className="rounded border border-zinc-200 px-1 dark:border-zinc-700">⌘K</kbd> toggle
        </div>
      </div>
    </div>
  );
}
