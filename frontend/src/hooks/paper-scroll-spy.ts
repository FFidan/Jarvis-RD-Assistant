/**
 * paper-scroll-spy.ts
 *
 * Lightweight IntersectionObserver-based scroll-spy.
 * Returns the id of the section currently in the top portion of the viewport.
 */
import { useState, useEffect, RefObject } from 'react';

/**
 * Watch a set of section element IDs and report which one is currently "active"
 * (topmost visible section). Falls back to the first id if none are visible.
 *
 * @param ids   Ordered list of section IDs to observe (DOM must contain them).
 * @param root  Optional scroll container ref; defaults to the document viewport.
 */
export function usePaperScrollSpy(
  ids: string[],
  root?: RefObject<HTMLElement | null>,
): string | null {
  const [activeId, setActiveId] = useState<string | null>(ids[0] ?? null);

  useEffect(() => {
    if (ids.length === 0) return;

    // Track which sections are intersecting
    const visible = new Set<string>();

    const observer = new IntersectionObserver(
      (entries) => {
        entries.forEach((entry) => {
          if (entry.isIntersecting) {
            visible.add(entry.target.id);
          } else {
            visible.delete(entry.target.id);
          }
        });

        // Pick the first id (in document order) that is currently visible
        const next = ids.find((id) => visible.has(id));
        if (next) setActiveId(next);
      },
      {
        root: root?.current ?? null,
        // Top-biased: fire when element enters/exits the top 60% of the viewport
        rootMargin: '0px 0px -40% 0px',
        threshold: 0,
      },
    );

    // Observe all sections that currently exist in the DOM
    ids.forEach((id) => {
      const el = document.getElementById(id);
      if (el) observer.observe(el);
    });

    return () => observer.disconnect();
  }, [ids, root]);

  return activeId;
}
