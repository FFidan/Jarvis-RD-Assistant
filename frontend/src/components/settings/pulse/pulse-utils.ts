import type { ConfigEntry } from '@/types';

/**
 * Look up a typed value in a flat ConfigEntry list, falling back when absent.
 * The `as T` cast is deliberate — entry.value is unknown at the type system but
 * each callsite knows the expected type from its config-key contract.
 */
export function getConfigValue<T>(entries: ConfigEntry[], key: string, fallback: T): T {
  const entry = entries.find((c) => c.key === key);
  return entry !== undefined ? (entry.value as T) : fallback;
}
