/** Minimal line-art illustrations for empty states. Stroke-based, uses
 *  currentColor — pass a text color class (e.g. text-zinc-300). */

export function PostsArt({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 72 72" fill="none" stroke="currentColor" strokeWidth={2.5}
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="18" y="8" width="36" height="56" rx="9" />
      <path d="M30 29.5v13l11-6.5z" fill="currentColor" stroke="none" />
      <line x1="31" y1="57" x2="41" y2="57" />
    </svg>
  );
}

export function VideosArt({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 72 72" fill="none" stroke="currentColor" strokeWidth={2.5}
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="8" y="18" width="56" height="36" rx="6" />
      <line x1="18" y1="18" x2="18" y2="54" />
      <line x1="54" y1="18" x2="54" y2="54" />
      <path d="M18 26h0M18 34h0M18 42h0M54 26h0M54 34h0M54 42h0" strokeWidth={4} />
      <path d="M33 31v10l8-5z" fill="currentColor" stroke="none" />
    </svg>
  );
}

export function AccountsArt({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 72 72" fill="none" stroke="currentColor" strokeWidth={2.5}
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <circle cx="36" cy="26" r="10" />
      <path d="M18 56c2.5-9 9.5-13.5 18-13.5S51.5 47 54 56" />
      <circle cx="36" cy="26" r="3.5" fill="currentColor" stroke="none" opacity={0.45} />
    </svg>
  );
}

export function ScheduleArt({ className }: { className?: string }) {
  return (
    <svg viewBox="0 0 72 72" fill="none" stroke="currentColor" strokeWidth={2.5}
      strokeLinecap="round" strokeLinejoin="round" className={className} aria-hidden>
      <rect x="12" y="14" width="48" height="44" rx="6" />
      <line x1="12" y1="26" x2="60" y2="26" />
      <line x1="24" y1="8" x2="24" y2="18" />
      <line x1="48" y1="8" x2="48" y2="18" />
      <circle cx="36" cy="42" r="7" />
      <path d="M36 38.5V42l2.5 2.5" />
    </svg>
  );
}
