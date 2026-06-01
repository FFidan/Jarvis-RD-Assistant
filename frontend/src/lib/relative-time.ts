/**
 * Shared time-formatting utilities used across multiple components.
 */

/** Format an ISO datetime string as a short HH:MM (24-hour) time. */
export function formatTime(iso: string): string {
  try {
    const d = new Date(iso);
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch {
    return iso;
  }
}

/** Format an ISO datetime string as "Mon DD, HH:MM". */
export function formatTimestamp(iso: string): string {
  try {
    return new Date(iso).toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}
