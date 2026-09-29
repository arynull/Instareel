"use client";
import { useEffect, useRef, useState } from "react";
import { countUpValue } from "@/lib/count-up";

/**
 * Animates a number toward `target` over `duration` ms with an ease-out
 * curve. Animates on mount (from 0) and on every target change.
 * Honors prefers-reduced-motion by jumping straight to the target.
 * Returns the current display value.
 */
export function useCountUp(target: number, duration = 800): number {
  const [value, setValue] = useState(0);
  const fromRef = useRef(0);
  const rafRef = useRef(0);

  useEffect(() => {
    const reduce =
      typeof window !== "undefined" &&
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    const from = fromRef.current;
    if (reduce || from === target) {
      fromRef.current = target;
      setValue(target);
      return;
    }
    const start = performance.now();
    const tick = (now: number) => {
      const p = Math.min(1, (now - start) / duration);
      const v = countUpValue(from, target, p);
      fromRef.current = v;
      setValue(v);
      if (p < 1) rafRef.current = requestAnimationFrame(tick);
    };
    rafRef.current = requestAnimationFrame(tick);
    return () => cancelAnimationFrame(rafRef.current);
  }, [target, duration]);

  return value;
}
