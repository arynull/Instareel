"use client";
import { useState } from "react";
import { useRouter } from "next/navigation";
import { Clapperboard } from "lucide-react";
import { api } from "@/lib/api";
import { useAuth } from "@/stores/stores";

export default function LoginPage() {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const router = useRouter();
  const { setAuth } = useAuth();

  async function submit(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      // Same-origin: Next.js rewrites /api/* to FastAPI (no CORS).
      const { data } = await api.post(`/auth/login`, { username, password });
      localStorage.setItem("access_token", data.access_token);
      localStorage.setItem("refresh_token", data.refresh_token);
      setAuth(username);
      router.push("/dashboard");
    } catch (err) {
      // Never leak backend internals (env var names, stack details) to an
      // unauthenticated visitor. A response means the server rejected the
      // credentials; no response means we never reached the server.
      const reached = (err as { response?: unknown })?.response !== undefined;
      setError(reached ? "Invalid username or password." : "Cannot reach the server — check your network connection and try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="flex min-h-screen items-center justify-center p-4">
      <form onSubmit={submit} className="card w-full max-w-sm space-y-4 p-8">
        <div className="flex items-center gap-2">
          <Clapperboard className="h-7 w-7 text-emerald-500" />
          <h1 className="text-xl font-extrabold tracking-tight">IG Funnel</h1>
        </div>
        <p className="text-sm text-zinc-500">Sign in to the admin dashboard.</p>
        <div>
          <label className="label" htmlFor="login-username">Username</label>
          <input id="login-username" className="input" value={username} onChange={(e) => setUsername(e.target.value)} autoComplete="username" required />
        </div>
        <div>
          <label className="label" htmlFor="login-password">Password</label>
          <input id="login-password" className="input" type="password" value={password} onChange={(e) => setPassword(e.target.value)} autoComplete="current-password" required />
        </div>
        {error && <p className="text-sm text-red-500">{error}</p>}
        <button className="btn-primary w-full" disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
      </form>
    </main>
  );
}
