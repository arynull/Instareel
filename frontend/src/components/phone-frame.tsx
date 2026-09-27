"use client";
import { useEffect, useState } from "react";
import type { ReactNode } from "react";
import { BatteryFull, Signal, Wifi } from "lucide-react";

/** Android-style phone shell. Pure layout — all data comes from props/children. */
export function PhoneFrame({ children }: { children: ReactNode }) {
  const [now, setNow] = useState(() => new Date());
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30000);
    return () => clearInterval(t);
  }, []);
  const time = now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  return (
    <div className="mx-auto w-[375px] max-w-full overflow-hidden rounded-[2.75rem] border-[10px] border-zinc-900 bg-black shadow-2xl dark:border-zinc-700">
      <div className="relative bg-white dark:bg-black">
        {/* notch + status bar */}
        <div className="absolute left-1/2 top-2 z-10 h-6 w-32 -translate-x-1/2 rounded-full bg-zinc-900 dark:bg-zinc-800" />
        <div className="flex items-center justify-between px-6 pt-3 text-[11px] font-semibold text-zinc-900 dark:text-zinc-100">
          <span>{time}</span>
          <span className="flex items-center gap-1">
            <Signal className="h-3 w-3" />
            <Wifi className="h-3 w-3" />
            <BatteryFull className="h-3.5 w-3.5" />
          </span>
        </div>
        {/* screen */}
        <div className="h-[640px] overflow-hidden bg-white text-zinc-900 dark:bg-black dark:text-zinc-100">
          {children}
        </div>
        {/* gesture bar */}
        <div className="flex justify-center bg-white pb-2 pt-1 dark:bg-black">
          <div className="h-1 w-28 rounded-full bg-zinc-300 dark:bg-zinc-700" />
        </div>
      </div>
    </div>
  );
}
