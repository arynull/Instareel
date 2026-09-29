"use client";
import { useCountUp } from "@/hooks/use-count-up";
import { cn } from "@/lib/utils";

/** Number that counts up to `value` with an ease-out motion.
 *  `format` turns the raw number into display text. */
export function AnimatedNumber({
  value,
  format = (n) => Math.round(n).toLocaleString("en-US"),
  duration,
  className,
}: {
  value: number;
  format?: (n: number) => string;
  duration?: number;
  className?: string;
}) {
  const v = useCountUp(value, duration);
  return <span className={cn("tnum font-display", className)}>{format(v)}</span>;
}
