"use client";
import { Eclipse, Moon, Sun } from "lucide-react";
import { useTheme } from "next-themes";
import { cn } from "@/lib/utils";
import { THEME_ORDER, nextTheme, type ThemeName } from "@/lib/theme";

const META: Record<ThemeName, { icon: typeof Sun; label: string }> = {
  light: { icon: Sun, label: "Light theme" },
  dark: { icon: Moon, label: "Dark theme" },
  black: { icon: Eclipse, label: "True-black (OLED) theme" },
};

/** Cycles light → dark → true-black. Shows the icon of the *current* theme. */
export function ThemeToggle({ className }: { className?: string }) {
  const { theme, setTheme } = useTheme();
  const current: ThemeName = (THEME_ORDER as readonly string[]).includes(theme ?? "")
    ? (theme as ThemeName)
    : "dark";
  const next = nextTheme(current);
  const { icon: Icon, label } = META[current];
  return (
    <button
      onClick={() => setTheme(next)}
      className={cn("btn-ghost shrink-0 !px-2", className)}
      aria-label={`Theme: ${label}. Activate for ${META[next].label}`}
      title={`Theme: ${label} — click for ${META[next].label}`}
    >
      <Icon className="h-4 w-4" />
    </button>
  );
}
