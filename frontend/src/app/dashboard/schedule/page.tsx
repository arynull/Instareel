"use client";
import { Fragment, useState } from "react";
import { Card, CardTitle, EmptyState, Field, QueryFailed, Spinner } from "@/components/ui";
import { toast } from "@/components/toast";
import { useAccounts, useApiMutation, useCaptions, useEffects, useRules, useTimezone, useVideos } from "@/hooks/use-api";
import { api } from "@/lib/api";
import { dayLabel } from "@/lib/utils";
import type { Account, Caption, Effect, ScheduleRule, Video } from "@/types/models";

const HOURS = Array.from({ length: 24 }, (_, h) => h);

export default function SchedulePage() {
  const { data: rules, isLoading, isError, refetch } = useRules();
  const { data: accounts } = useAccounts();
  const { data: captions } = useCaptions();
  const { data: effects } = useEffects();
  const { data: processed } = useVideos("processed");
  const readyVideos = ((processed ?? []) as Video[]).filter((v) => v.status === "processed");
  const videoLabel = (v: Video) => {
    // <option> text can't wrap or truncate via CSS — shorten in JS.
    const base = `#${v.id} ${v.original_filename}${v.effect_preset ? ` (${v.effect_preset})` : ""}`;
    return base.length > 42 ? `${base.slice(0, 41)}…` : base;
  };
  const create = useApiMutation("post", [["rules"]], "Rule added");
  const remove = useApiMutation("delete", [["rules"]], "Rule deleted");
  const toggle = useApiMutation("post", [["rules"]], "Rule updated");
  const pin = useApiMutation("post", [["rules"]], "Pin updated");
  const update = useApiMutation("put", [["rules"]], "Rule updated");
  const [form, setForm] = useState({ name: "", day_of_week: -1, hour: 12, minute: 0, account_id: "", preferred_effect: "", caption_template_id: "", prefer_source_caption: true, pinned_video_id: "" });
  const [suggesting, setSuggesting] = useState(false);
  const list = (rules ?? []) as ScheduleRule[];
  // The zone rule hours are interpreted in — shown next to every time so
  // there is never any doubt which "12:00" a rule means.
  const { data: tz } = useTimezone();
  const tzNote = tz ? `${tz.label} time (${tz.utc_offset})` : "…";

  async function suggestBestTime() {
    if (!form.account_id || suggesting) return;
    setSuggesting(true);
    try {
      const { data } = await api.get(`/analytics/best-slots?account_id=${form.account_id}`);
      const top = (data.slots ?? [])[0];
      if (!top) {
        toast("error", "No posted history yet — post a few reels first");
        return;
      }
      setForm({ ...form, hour: top.hour_local, minute: 0 });
      toast("success", `Best slot: ${top.local} ${top.tz_label} (${top.avg_views} avg views)${data.personalized ? "" : " — global fallback"}`);
    } catch {
      toast("error", "Could not load suggestions");
    } finally {
      setSuggesting(false);
    }
  }

  function submit() {
    // An emptied number input parses to NaN (JSON null → 422), so clamp to
    // the valid range and fall back to sane defaults instead of sending NaN.
    const hour = Number.isFinite(form.hour) ? Math.min(23, Math.max(0, Math.round(form.hour))) : 12;
    const minute = Number.isFinite(form.minute) ? Math.min(59, Math.max(0, Math.round(form.minute))) : 0;
    create.mutate(
      {
        url: "/schedule",
        body: {
          name: form.name || `${dayLabel(form.day_of_week)} ${hour}:${String(minute).padStart(2, "0")}`,
          day_of_week: form.day_of_week, hour, minute,
          account_id: form.account_id ? Number(form.account_id) : null,
          preferred_effect: form.preferred_effect || null,
          caption_template_id: form.caption_template_id ? Number(form.caption_template_id) : null,
          prefer_source_caption: form.prefer_source_caption,
          pinned_video_id: form.pinned_video_id ? Number(form.pinned_video_id) : null,
        },
      },
      // Reset the form only after the rule is actually created — on error
      // the user's input stays so nothing is lost.
      { onSuccess: () => setForm({ name: "", day_of_week: -1, hour: 12, minute: 0, account_id: "", preferred_effect: "", caption_template_id: "", prefer_source_caption: true, pinned_video_id: "" }) },
    );
  }

  function ruleState(r: ScheduleRule) {
    // One-shot lifecycle at a glance: pinned rules retire after firing.
    if (r.pinned_video_id) {
      if (!r.is_active && r.pinned_video_status === "posted") return { chip: "done ✓", tone: "bg-sky-100 text-sky-700 dark:bg-sky-900/40 dark:text-sky-300" };
      if (!r.is_active && !r.pinned_video_label) return { chip: "pin gone", tone: "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300" };
      if (!r.is_active) return { chip: "paused (pinned)", tone: "bg-zinc-200 text-zinc-500" };
      return { chip: `📌 ${r.pinned_video_label ?? `#${r.pinned_video_id}`} · one-shot`, tone: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300" };
    }
    return r.is_active
      ? { chip: "active · queue", tone: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300" }
      : { chip: "paused", tone: "bg-zinc-200 text-zinc-500" };
  }

  return (
    <div className="space-y-4">
      <h1 className="text-xl font-extrabold tracking-tight">Posting schedule</h1>

      <Card>
        <CardTitle>Weekly calendar</CardTitle>
        <div className="overflow-x-auto">
        <div className="grid min-w-[520px] grid-cols-8 gap-1 text-center text-xs">
          <div />
          {["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"].map((d) => <div key={d} className="font-semibold text-zinc-500">{d}</div>)}
          {HOURS.filter((h) => list.some((r) => r.hour === h)).map((h) => (
            <Fragment key={`row-${h}`}>
              <div className="pr-1 text-right text-zinc-400">{h}:00</div>
              {[0, 1, 2, 3, 4, 5, 6].map((d) => {
                const hits = list.filter((r) => r.hour === h && (r.day_of_week === -1 || r.day_of_week === d));
                return (
                  <div key={`${h}-${d}`} className="min-h-6 rounded bg-zinc-100 px-1 dark:bg-zinc-800">
                    {hits.map((r) => (
                      <span key={r.id} className={`mr-0.5 inline-block h-2 w-2 rounded-full ${r.is_active ? "bg-emerald-500" : "bg-zinc-400"}`} title={r.name} />
                    ))}
                  </div>
                );
              })}
            </Fragment>
          ))}
        </div>
        </div>
        {list.length === 0 && !isLoading && <p className="mt-2 text-sm text-zinc-500">No rules yet — every active hour shows here once added.</p>}
      </Card>

      <Card>
        <CardTitle>New rule</CardTitle>
        <div className="grid gap-3 md:grid-cols-4">
          <Field label="Name"><input className="input" value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} placeholder="auto" /></Field>
          <Field label="Day">
            <select className="input" value={form.day_of_week} onChange={(e) => setForm({ ...form, day_of_week: Number(e.target.value) })}>
              <option value={-1}>Every day</option>
              {[0, 1, 2, 3, 4, 5, 6].map((d) => <option key={d} value={d}>{dayLabel(d)}</option>)}
            </select>
          </Field>
          <Field label={tz ? `Hour (${tz.label})` : "Hour"}><input className="input" type="number" min={0} max={23} value={form.hour} onChange={(e) => setForm({ ...form, hour: Number(e.target.value) })} /></Field>
          <Field label="Minute"><input className="input" type="number" min={0} max={59} value={form.minute} onChange={(e) => setForm({ ...form, minute: Number(e.target.value) })} /></Field>
          <Field label="Account">
            <select className="input max-w-full" value={form.account_id} onChange={(e) => setForm({ ...form, account_id: e.target.value })}>
              <option value="">Auto-select</option>
              {((accounts ?? []) as Account[]).map((a) => <option key={a.id} value={a.id}>@{a.username}</option>)}
            </select>
          </Field>
          <Field label="Golden hour">
            <button
              className="btn-ghost w-full !py-2 text-xs"
              disabled={!form.account_id || suggesting}
              title={form.account_id ? "Fill hour/minute with this account's best posting time" : "Pick an account first"}
              onClick={suggestBestTime}
            >
              {suggesting ? "Checking…" : "✨ Suggest best time"}
            </button>
          </Field>
          <Field label="Effect">
            <select className="input max-w-full" value={form.preferred_effect} disabled={!!form.pinned_video_id} title={form.pinned_video_id ? "Ignored while a video is pinned" : ""} onChange={(e) => setForm({ ...form, preferred_effect: e.target.value })}>
              <option value="">Any</option>
              {((effects ?? []) as Effect[]).map((e) => <option key={e.name} value={e.name}>{e.name}</option>)}
            </select>
          </Field>
          <Field label="Pinned video (one-shot)">
            <select className="input max-w-full" value={form.pinned_video_id} onChange={(e) => setForm({ ...form, pinned_video_id: e.target.value, preferred_effect: e.target.value ? "" : form.preferred_effect })}>
              <option value="">Auto (queue)</option>
              {readyVideos.map((v) => <option key={v.id} value={v.id}>{videoLabel(v)}</option>)}
            </select>
          </Field>
          <Field label="Caption template">
            <select className="input max-w-full" value={form.caption_template_id} onChange={(e) => setForm({ ...form, caption_template_id: e.target.value })}>
              <option value="">Random</option>
              {((captions ?? []) as Caption[]).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </Field>
          <label className="flex items-center gap-1.5 text-xs" title="Videos harvested from sources carry their original caption — post it verbatim instead of a template.">
            <input type="checkbox" checked={form.prefer_source_caption} onChange={(e) => setForm({ ...form, prefer_source_caption: e.target.checked })} />
            Prefer source caption
          </label>
          <div className="flex items-end"><button className="btn-primary w-full" onClick={submit} disabled={create.isPending}>{create.isPending ? "Adding…" : "Add rule"}</button></div>
        </div>
        <p className="mt-2 text-xs text-zinc-500">Rule times are {tzNote} — change <code>SCHEDULE_TZ</code> on the server to use another zone.</p>
      </Card>

      {isLoading ? <Spinner /> : isError ? <QueryFailed onRetry={() => refetch()} /> : list.length === 0 ? <EmptyState title="No schedule rules" /> : (
        <Card>
          {list.map((r) => {
            const st = ruleState(r);
            return (
            <div key={r.id} className="border-t border-zinc-100 py-2 text-sm first:border-0 dark:border-zinc-800">
              <div className="flex min-w-0 flex-wrap items-center gap-2">
                <strong title={r.name} className="min-w-0 flex-1 break-words">{r.name}</strong>
                <span className="shrink-0 text-zinc-500" title={tz ? `Fires at this wall-clock time in ${tz.tz} (${tz.utc_offset})` : ""}>{dayLabel(r.day_of_week)} · {r.hour}:{String(r.minute).padStart(2, "0")}{tz ? ` · ${tz.label}` : ""}</span>
                <span title={r.pinned_video_label ?? st.chip} className={`min-w-0 max-w-full truncate rounded-full px-2 py-0.5 text-xs font-semibold ${st.tone}`}>
                  {st.chip}
                </span>
                <span className="ml-auto flex shrink-0 flex-wrap gap-2">
                  <button className="btn-ghost !px-3 !py-1 text-xs" disabled={toggle.isPending} onClick={() => toggle.mutate({ url: `/schedule/${r.id}/toggle` })}>{toggle.isPending ? "Saving…" : "Toggle"}</button>
                  <button
                    className="btn-ghost !px-3 !py-1 text-xs"
                    disabled={update.isPending}
                    title="When ON, a harvested source caption posts verbatim instead of a template. PUT takes the full rule — every field is re-sent."
                    onClick={() => update.mutate({
                      url: `/schedule/${r.id}`,
                      body: {
                        name: r.name, day_of_week: r.day_of_week, hour: r.hour, minute: r.minute,
                        account_id: r.account_id, is_active: r.is_active, preferred_effect: r.preferred_effect,
                        caption_template_id: r.caption_template_id,
                        prefer_source_caption: !r.prefer_source_caption, pinned_video_id: r.pinned_video_id,
                      },
                    })}
                  >
                    {update.isPending ? "Saving…" : r.prefer_source_caption ? "Src caption ✓" : "Src caption off"}
                  </button>
                  <button className="btn-ghost !px-3 !py-1 text-xs text-red-500" disabled={remove.isPending} onClick={() => { if (confirm(`Delete rule "${r.name}"?`)) remove.mutate({ url: `/schedule/${r.id}` }); }}>{remove.isPending ? "Deleting…" : "Delete"}</button>
                </span>
              </div>
              <div className="mt-1 flex flex-wrap items-center gap-2 text-xs text-zinc-500">
                <label className="flex min-w-0 items-center">📌 Video:
                  <select
                    className="input ml-1 !w-auto min-w-0 max-w-full !py-1 text-xs"
                    value={r.pinned_video_id ?? ""}
                    disabled={pin.isPending}
                    onChange={(e) => {
                      const v = e.target.value;
                      if (v) pin.mutate({ url: `/schedule/${r.id}/pin`, body: { video_id: Number(v) } });
                      else pin.mutate({ url: `/schedule/${r.id}/unpin` });
                    }}
                  >
                    <option value="">Auto (queue)</option>
                    {readyVideos.map((v) => <option key={v.id} value={v.id}>{videoLabel(v)}</option>)}
                    {r.pinned_video_id && !readyVideos.some((v) => v.id === r.pinned_video_id) && (
                      <option value={r.pinned_video_id}>{r.pinned_video_label ?? `#${r.pinned_video_id}`} ({r.pinned_video_status ?? "gone"})</option>
                    )}
                  </select>
                </label>
                {r.pinned_video_id && r.is_active && <span>Pinned rules fire once, then retire. Effect filter is ignored while pinned.</span>}
              </div>
            </div>
            );
          })}
        </Card>
      )}
    </div>
  );
}
