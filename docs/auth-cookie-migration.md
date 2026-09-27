# Auth migration plan: localStorage Bearer → httpOnly cookies + CSRF

Status: **plan only — not implemented**. Do not fold into a general batch;
implement as one dedicated, reviewed change with the regression suite below.

## Why

Today the dashboard keeps `access_token` + `refresh_token` in `localStorage`
(`frontend/src/lib/api.ts`) and sends `Authorization: Bearer`. Any XSS in the
dashboard (IG-sourced captions/tags render in the UI) can exfiltrate both
tokens. httpOnly cookies remove the tokens from JS reach entirely.

## Design

### Cookies (set by backend on `/api/v1/auth/login`)

| Cookie | httpOnly | SameSite | Path | Max-Age | Secure |
|---|---|---|---|---|---|
| `access_token` | ✅ | Lax | `/` | `JWT_EXPIRE_MINUTES` | per `COOKIE_SECURE` |
| `refresh_token` | ✅ | Lax | `/api/v1/auth` | `JWT_REFRESH_DAYS` | per `COOKIE_SECURE` |
| `csrf_token` | ❌ (JS must read it) | Lax | `/` | session | per `COOKIE_SECURE` |

- New setting `COOKIE_SECURE` (default `false`): the stock deployment is
  plain HTTP on LAN — `Secure` cookies would never be sent there. Set
  `true` when terminating TLS in front of nginx.
- `refresh_token` is path-scoped to `/api/v1/auth` so it is only ever sent
  to the auth endpoints, never to the general API.

### CSRF

`SameSite=Lax` already blocks cross-site `fetch`/XHR mutations in modern
browsers. Belt-and-braces for old browsers: double-submit token.

- `csrf_token` = 32 random bytes (hex), minted at login, rotated at login.
- Unsafe methods (`POST/PUT/PATCH/DELETE`) require header
  `X-CSRF-Token` == `csrf_token` cookie, compared with
  `hmac.compare_digest`. Checked in **middleware** (not per-route): if the
  request carries the `access_token` cookie (i.e. is cookie-authenticated),
  the header must match; requests without that cookie (Bearer fallback,
  `/login`, public GETs) skip the check.
- `/refresh` is cookie-authenticated → needs the CSRF header too; the
  dashboard JS reads the non-httpOnly `csrf_token` cookie and sends it.

### Backend changes

1. `POST /auth/login`: on success, `Set-Cookie` the three cookies **and**
   keep returning `TokenOut` JSON for one transition release (old cached
   frontend keeps working).
2. `get_current_admin`: accept the `access_token` cookie first, fall back
   to the `Authorization: Bearer` header (kept permanently — cheap, and
   external scripts/users may rely on it; it does not weaken the XSS story
   since JS still can't *read* the cookie).
3. `POST /auth/refresh`: read the refresh token from the cookie; keep the
   existing single-use jti claim/rotation exactly as-is; set the new pair
   as cookies.
4. **New** `POST /auth/logout`: blacklist the access token's `jti` until
   its `exp` (existing `token_blacklist` module) **and** claim the refresh
   token's `jti` without reissuing; clear all three cookies
   (`Set-Cookie` with `Max-Age=0`).
5. CSRF middleware as described above. `403` on mismatch/missing.
6. `COOKIE_SECURE` setting; cookie flags centralized in one helper
   (`app/core/cookies.py`: `set_auth_cookies(response, ...)`,
   `clear_auth_cookies(response)`).

### WebSocket auth plan

The browser sends cookies on the WS handshake automatically (same-origin
through nginx `/ws`), so no token-in-JS is needed at all:

- `ws_feed`: read `access_token` from the handshake `cookie` header first;
  validate like `get_current_admin`. Fall back to the current first-frame
  `{"token": ...}` message for one transition release, then remove.
- Keep the 5s auth window and the per-IP pending-auth cap unchanged.
- Keep sending 4401 on token expiry; the client reconnects (cookies refresh
  via the normal 401→refresh flow, no token plumbing in `use-realtime.ts`).

### Frontend changes

- `lib/api.ts`: `axios` `withCredentials: true`; **delete** all
  `localStorage` token code and the `Authorization` header injection;
  interceptor adds `X-CSRF-Token` (read from the `csrf_token` cookie) on
  unsafe methods; refresh = `POST /api/v1/auth/refresh` with **no body**;
  logout button = `POST /api/v1/auth/logout` (+ local state reset).
- `use-realtime.ts`: stop reading/sending the token; rely on cookies.
- `login/page.tsx`: no token storage; on 200, redirect to dashboard.

### Rollout

Flag-day is acceptable (backend + frontend ship in one compose), but the
transition fallbacks (TokenOut JSON kept, Bearer accepted, WS first-frame
accepted) mean an old cached frontend keeps working until hard-refresh.
No DB migration. Deploy = rebuild backend + frontend, one
`docker compose up -d --build`, then hard-refresh the dashboard.
Add `COOKIE_SECURE=false` (default) to `.env.example` with a comment.

## Regression suite (must all pass before merge)

Backend (pytest, TestClient):
1. `POST /login` sets `access_token` + `refresh_token` as httpOnly,
   `SameSite=Lax`; `csrf_token` is NOT httpOnly.
2. API call with **only** the cookie (no `Authorization` header) → 200.
3. `PUT` with cookie but no `X-CSRF-Token` → 403; wrong value → 403;
   correct → 200. `GET` with cookie and no CSRF header → 200.
4. `Bearer` header without cookies still authenticates (fallback).
5. `POST /refresh` with the cookie rotates: new pair issued, replaying the
   old refresh cookie → 401.
6. `POST /logout` clears the cookies (check `Set-Cookie` expirations) and
   the logged-out access cookie → 401 afterwards.
7. WS handshake with the cookie authenticates without any first frame
   (extend `test_upload_guards.py`-style fakes).

Frontend (`tsc --noEmit` + manual):
8. No `localStorage` token reads/writes remain; dashboard works after login
   with cookies only; 401 → silent refresh → retry still works.
9. Logout button clears UI state and the server rejects the old cookies.

## Out of scope

- Remember-me / multiple sessions: still single-admin, unchanged.
- Changing JWT lifetimes or the single-use refresh rotation: unchanged.
- `Secure` by default: would break the stock HTTP LAN deployment.
