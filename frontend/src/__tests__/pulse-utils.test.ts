import { describe, it, expect } from 'vitest';
import { getConfigValue } from '@/components/settings/pulse/pulse-utils';
import type { ConfigEntry } from '@/types';

describe('getConfigValue', () => {
  const entries: ConfigEntry[] = [
    { key: 'foo', value: 42 } as ConfigEntry,
    { key: 'bar', value: 'hello' } as ConfigEntry,
  ];
  it('returns the value when key present (typed)', () => {
    expect(getConfigValue<number>(entries, 'foo', 0)).toBe(42);
    expect(getConfigValue<string>(entries, 'bar', '')).toBe('hello');
  });
  it('returns fallback when key absent', () => {
    expect(getConfigValue<number>(entries, 'missing', 99)).toBe(99);
  });
  it('returns fallback on empty list', () => {
    expect(getConfigValue<string>([], 'foo', 'default')).toBe('default');
  });
});
