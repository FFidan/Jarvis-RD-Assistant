/**
 * Open-redirect / XSS guard for server-supplied `action_link.href` values.
 *
 * Only same-app navigation is allowed: an href must begin with a single `/`
 * (not `//`, which is protocol-relative and points off-site) and must not
 * contain a backslash (browsers normalise `\` → `/`, defeating naive checks).
 * Everything else — `javascript:`, `data:`, absolute `http(s):`, any other
 * scheme, protocol-relative, or non-string — is rejected so callers render the
 * label as inert text instead of a live link.
 *
 * Mirrors the inline guard in stores/job-store.ts.
 */
export function isSafeRelativeHref(href: string): boolean {
  if (typeof href !== 'string' || href.length === 0) return false;
  if (href.includes('\\')) return false;
  return href.startsWith('/') && !href.startsWith('//');
}
