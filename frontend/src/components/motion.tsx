"use client";
import type { CSSProperties, ReactNode } from "react";
import { usePathname } from "next/navigation";
import { cn } from "@/lib/utils";

/** Re-triggers the fade-up entrance whenever the dashboard route changes. */
export function PageTransition({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  return (
    <div key={pathname} className="animate-fade-up">
      {children}
    </div>
  );
}

/**
 * Staggered entrance for lists/grids. Wrap each child in
 * <StaggerItem index={i}> — items enter one by one, 45ms apart.
 * Reduced-motion users see everything instantly (see globals.css).
 */
export function StaggerItem({
  index,
  children,
  className,
}: {
  index: number;
  children: ReactNode;
  className?: string;
}) {
  return (
    <div
      className={cn("stagger-item", className)}
      style={{ "--stagger-index": index } as CSSProperties}
    >
      {children}
    </div>
  );
}
