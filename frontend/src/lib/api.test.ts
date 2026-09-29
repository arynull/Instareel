import { beforeEach, describe, expect, it, vi, type Mock } from "vitest";

// Mock only axios.post (used by doRefresh); axios.create keeps building a
// real instance so the module's actual interceptors are what's under test.
vi.mock("axios", async (importOriginal) => {
  const actual = await importOriginal<typeof import("axios")>();
  return {
    ...actual,
    default: {
      ...actual.default,
      post: vi.fn(),
    },
  };
});

import axios from "axios";

const mockPost = axios.post as Mock;

function installBrowserShims() {
  const store = new Map<string, string>();
  const localStorageShim = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => {
      store.set(k, v);
    },
    removeItem: (k: string) => {
      store.delete(k);
    },
  };
  const location = { href: "http://localhost/dashboard/phone" };
  (globalThis as Record<string, unknown>).window = { location, __API_URL: "" };
  (globalThis as Record<string, unknown>).localStorage = localStorageShim;
  return { store, location };
}

function err401(config: unknown) {
  return new axios.AxiosError("Request failed with status code 401", "ERR_BAD_REQUEST", config as never, undefined, {
    status: 401,
    data: { detail: "expired" },
    headers: {},
    config,
  } as never);
}

describe("auth interceptor", () => {
  beforeEach(() => {
    vi.resetModules();
    mockPost.mockReset();
  });

  it("rejects the original request when token refresh fails (never resolves undefined)", async () => {
    const { store, location } = installBrowserShims();
    store.set("access_token", "expired-access");
    store.set("refresh_token", "expired-refresh");
    const { api } = await import("@/lib/api");
    api.defaults.adapter = ((config: unknown) => Promise.reject(err401(config))) as never;
    mockPost.mockRejectedValue(err401({ url: "/api/v1/auth/refresh" }));

    // Contract: a failed refresh must reject the original request (not
    // resolve it), redirect to /login, and clear both tokens — so no
    // caller ever proceeds as if the dead session were fine.
    await expect(api.get("/auth/me")).rejects.toThrow("401");
    expect(location.href).toBe("/login");
    expect(store.has("access_token")).toBe(false);
    expect(store.has("refresh_token")).toBe(false);
  });

  it("retries once with the fresh token after a successful refresh", async () => {
    const { store } = installBrowserShims();
    store.set("access_token", "expired-access");
    store.set("refresh_token", "valid-refresh");
    const { api } = await import("@/lib/api");
    const seenAuth: (string | undefined)[] = [];
    let calls = 0;
    api.defaults.adapter = ((config: unknown) => {
      calls += 1;
      const c = config as { headers?: Record<string, string> };
      seenAuth.push(c.headers?.Authorization);
      if (calls === 1) return Promise.reject(err401(config));
      return Promise.resolve({
        data: { username: "admin" },
        status: 200,
        statusText: "OK",
        headers: {},
        config,
      });
    }) as never;
    mockPost.mockResolvedValue({ data: { access_token: "new-access", refresh_token: "new-refresh" } });

    const { data } = await api.get("/auth/me");
    expect(data).toEqual({ username: "admin" });
    expect(store.get("access_token")).toBe("new-access");
    expect(store.get("refresh_token")).toBe("new-refresh");
    expect(seenAuth[1]).toBe("Bearer new-access");
  });
});
