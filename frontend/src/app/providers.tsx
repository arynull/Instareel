"use client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ThemeProvider, useTheme } from "next-themes";
import { useEffect, useState } from "react";

/**
 * next-themes 0.3 applies the `value` mapping verbatim to <html>'s classList
 * and throws on whitespace ("dark black"). The black theme therefore gets its
 * own single class, and this component keeps the `dark` class alongside it —
 * every Tailwind `dark:` utility and `html.dark.black` rule keeps working in
 * true-black mode. Runs after next-themes' own effect, so the re-added class
 * is never wiped by a theme switch.
 */
function DarkClassSync() {
  const { resolvedTheme } = useTheme();
  useEffect(() => {
    document.documentElement.classList.toggle("dark", resolvedTheme === "black");
  }, [resolvedTheme]);
  return null;
}

export function Providers({ children }: { children: React.ReactNode }) {
  const [client] = useState(() => new QueryClient({
    defaultOptions: { queries: { retry: 1, staleTime: 5000 } },
  }));
  return (
    <ThemeProvider
      attribute="class"
      defaultTheme="dark"
      enableSystem={false}
      value={{ light: "light", dark: "dark", black: "black" }}
    >
      <DarkClassSync />
      <QueryClientProvider client={client}>{children}</QueryClientProvider>
    </ThemeProvider>
  );
}
