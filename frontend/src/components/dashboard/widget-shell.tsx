"use client";
import { useState, type ReactNode } from "react";
import { GripVertical } from "lucide-react";
import { cn } from "@/lib/utils";

/**
 * A reorderable dashboard section. Drag via the handle (or focus it and use
 * ↑/↓). The parent owns dragId/overId so several shells can coordinate.
 */
export function WidgetShell({
  id,
  title,
  dragId,
  overId,
  onDragStartId,
  onDragEndAll,
  onDragOverId,
  onDropOn,
  onMoveBy,
  children,
}: {
  id: string;
  title: string;
  dragId: string | null;
  overId: string | null;
  onDragStartId: (id: string) => void;
  onDragEndAll: () => void;
  onDragOverId: (id: string) => void;
  onDropOn: (targetId: string) => void;
  onMoveBy: (id: string, delta: -1 | 1) => void;
  children: ReactNode;
}) {
  // The section is only draggable while the handle is pressed, so text
  // selection and scrolling elsewhere keep working normally.
  const [armDrag, setArmDrag] = useState(false);
  const dragging = dragId === id;

  return (
    <section
      aria-label={title}
      draggable={armDrag}
      onDragStart={(e) => {
        e.dataTransfer.effectAllowed = "move";
        try {
          e.dataTransfer.setData("text/plain", id);
        } catch {
          /* some browsers restrict setData — dragId state is the source of truth */
        }
        onDragStartId(id);
      }}
      onDragEnd={onDragEndAll}
      onDragOver={(e) => {
        e.preventDefault();
        e.dataTransfer.dropEffect = "move";
        if (dragId && dragId !== id) onDragOverId(id);
      }}
      onDragLeave={() => {
        if (overId === id) onDragOverId("");
      }}
      onDrop={(e) => {
        e.preventDefault();
        onDropOn(id);
      }}
      className={cn(
        "group relative transition-opacity",
        dragging && "opacity-50",
        overId === id && !dragging && "widget-drop-target"
      )}
    >
      <span
        role="button"
        tabIndex={0}
        aria-label={`Reorder ${title} — drag, or press arrow keys to move`}
        title={`Drag to reorder ${title}`}
        className="absolute right-2 top-2 z-10 cursor-grab touch-none rounded-md border border-zinc-200 bg-white/90 p-1 text-zinc-400 opacity-0 shadow-sm backdrop-blur transition-opacity hover:text-zinc-600 focus:opacity-100 group-hover:opacity-100 active:cursor-grabbing dark:border-zinc-700 dark:bg-zinc-900/90 dark:hover:text-zinc-300"
        onMouseDown={() => setArmDrag(true)}
        onMouseUp={() => setArmDrag(false)}
        onKeyDown={(e) => {
          if (e.key === "ArrowUp") {
            e.preventDefault();
            onMoveBy(id, -1);
          } else if (e.key === "ArrowDown") {
            e.preventDefault();
            onMoveBy(id, 1);
          }
        }}
      >
        <GripVertical className="h-4 w-4" />
      </span>
      {children}
    </section>
  );
}
