"use client";
import { create } from "zustand";
import {
  readAccent,
  readDensity,
  writeAccent,
  writeDensity,
  type AccentName,
  type Density,
} from "@/lib/display-prefs";

function applyToDom(accent: AccentName, density: Density) {
  if (typeof document === "undefined") return;
  document.documentElement.dataset.accent = accent;
  document.documentElement.dataset.density = density;
}

interface DisplayState {
  accent: AccentName;
  density: Density;
  setAccent: (a: AccentName) => void;
  setDensity: (d: Density) => void;
}

/** Dashboard display preferences: accent color + density.
 *  Persisted to localStorage, applied to <html> as data attributes so CSS
 *  and charts can react. The pre-hydration script in app/layout.tsx sets
 *  the same attributes before first paint to avoid a flash. */
export const useDisplay = create<DisplayState>((set) => {
  const accent = readAccent();
  const density = readDensity();
  applyToDom(accent, density);
  return {
    accent,
    density,
    setAccent: (a) => {
      writeAccent(a);
      applyToDom(a, useDisplay.getState().density);
      set({ accent: a });
    },
    setDensity: (d) => {
      writeDensity(d);
      applyToDom(useDisplay.getState().accent, d);
      set({ density: d });
    },
  };
});
