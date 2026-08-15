import { clsx, type ClassValue } from 'clsx';
import { twMerge } from 'tailwind-merge';

/** Merge Tailwind classes with clsx. Standard Shadcn/ui utility. */
export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs));
}

/** Format an ISO date string to a human-readable format. */
export function formatDate(isoString: string | null | undefined): string {
  if (!isoString) return 'N/A';
  const date = new Date(isoString);
  return date.toLocaleDateString('en-US', {
    year: 'numeric',
    month: 'short',
    day: 'numeric',
  });
}

/** Format an ISO datetime or Date with the browser's local date and time. */
export function formatDateTime(
  value: string | Date | null | undefined,
  fallback = 'N/A',
): string {
  if (!value) return fallback;
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? fallback : date.toLocaleString();
}

/** Format an author list for display, truncating if >3 authors. */
export function formatAuthors(authors: string[] | null | undefined): string {
  if (!authors || authors.length === 0) return 'Unknown';
  if (authors.length <= 3) return authors.join(', ');
  return `${authors.slice(0, 3).join(', ')} et al.`;
}
