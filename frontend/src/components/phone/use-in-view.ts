"use client";
import { useEffect, useRef, useState } from "react";

/** True once the element scrolls near the viewport, then stays true.
 *
 * Bounds eager work — e.g. thumbnail fetches — to what's actually visible
 * instead of firing hundreds of requests for a long grid up front. Without
 * this, a 200-item grid saturates the browser's ~6 connections-per-origin
 * pool and starves every other API call on the page (polls, preview
 * tokens), which is what made the phone view feel hung.
 */
export function useInView<T extends HTMLElement = HTMLDivElement>(rootMargin = "200px") {
  const ref = useRef<T | null>(null);
  const [inView, setInView] = useState(false);
  useEffect(() => {
    const el = ref.current;
    if (!el || typeof IntersectionObserver === "undefined") {
      // SSR / ancient browsers: load eagerly rather than never.
      setInView(true);
      return;
    }
    const io = new IntersectionObserver(
      ([entry]) => {
        if (entry.isIntersecting) {
          setInView(true);
          io.disconnect();
        }
      },
      { rootMargin },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [rootMargin]);
  return { ref, inView };
}
