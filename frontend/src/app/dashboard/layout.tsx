"use client";
import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import { Header, MobileNav, Sidebar, TopBar } from "@/components/layout";
import { CommandPalette } from "@/components/command-palette";
import { ShortcutHelp } from "@/components/shortcut-help";
import { PageTransition } from "@/components/motion";
import { Toaster } from "@/components/toast";
import { Spinner } from "@/components/ui";
import { api } from "@/lib/api";
import { useAuth } from "@/stores/stores";
import { useRealtimeFeed } from "@/hooks/use-realtime";

export default function DashboardLayout({ children }: { children: React.ReactNode }) {
  const router = useRouter();
  const { setAuth } = useAuth();
  const [mounted, setMounted] = useState(false);
  const [verified, setVerified] = useState(false);
  const [verifyFailed, setVerifyFailed] = useState(false);

  useEffect(() => {
    setMounted(true);
    const token = localStorage.getItem("access_token");
    if (!token) {
      router.replace("/login");
      return;
    }
    // Presence isn't validity: confirm the token with the API once, else an
    // expired token renders a broken dashboard of failing queries.
    // A hard timeout keeps a never-settling verify request from becoming an
    // eternal spinner (refresh used to sit on "loading" forever).
    let done = false;
    const timer = setTimeout(() => {
      done = true;
      setVerifyFailed(true);
    }, 15000);
    api.get("/auth/me").then(
      () => {
        if (done) return;
        clearTimeout(timer);
        if (!useAuth.getState().username) setAuth(localStorage.getItem("username") ?? "admin");
        setVerified(true);
      },
      () => {
        if (done) return;
        clearTimeout(timer);
        router.replace("/login");
      },
    );
    return () => clearTimeout(timer);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Only read localStorage after mount so the server-rendered HTML (empty)
  // matches the first client render — avoids React hydration mismatches.
  const token = mounted ? localStorage.getItem("access_token") : null;
  useRealtimeFeed(!!token && verified);

  if (!mounted || !token) return null;
  if (!verified) {
    if (verifyFailed) {
      return (
        <div className="flex min-h-screen flex-col items-center justify-center gap-3 p-6 text-center">
          <p className="text-sm text-zinc-600 dark:text-zinc-300">
            Couldn&apos;t reach the server — check your connection and try again.
          </p>
          <button className="btn-primary" onClick={() => window.location.reload()}>
            Retry
          </button>
        </div>
      );
    }
    return <div className="flex min-h-screen items-center justify-center"><Spinner /></div>;
  }

  return (
    <div className="flex min-h-screen">
      <Sidebar />
      <div className="flex min-w-0 flex-1 flex-col">
        <Header />
        <TopBar />
        <main className="flex-1 space-y-6 p-4 pb-20 md:p-6 md:pb-6"><PageTransition>{children}</PageTransition></main>
      </div>
      <Toaster />
      <CommandPalette />
      <ShortcutHelp />
      <MobileNav />
    </div>
  );
}
