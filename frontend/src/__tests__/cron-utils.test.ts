import { describe, expect, it } from 'vitest';
import { cronToHumanReadable, isTimeOnlyCron, timeToCron } from '@/lib/cron-utils';

describe('isTimeOnlyCron', () => {
  it('rejects anything a single clock time cannot represent', () => {
    // BOTH fields must be checked. Guarding only the hour still loses the
    // ':30' run of '0,30 8 * * *'.
    for (const cron of ['0 8,20 * * *', '0 9-17 * * *', '0 */2 * * *', '0,30 8 * * *']) {
      expect(isTimeOnlyCron(cron)).toBe(false);
    }
  });
  it('accepts a plain daily time', () => {
    expect(isTimeOnlyCron('30 7 * * *')).toBe(true);
  });
});

describe('cronToHumanReadable', () => {
  it('does not call a monthly schedule daily', () => {
    expect(cronToHumanReadable('0 9 1 * *')).not.toMatch(/^Daily/);
  });
  it('names the Sunday alias', () => {
    expect(cronToHumanReadable('0 9 * * 7')).toBe('Weekly on Sun at 09:00');
  });
  it('describes an hourly schedule', () => {
    expect(cronToHumanReadable('0 * * * *')).toBe('Every hour');
  });
  it('does not call a yearly schedule monthly', () => {
    expect(cronToHumanReadable('0 9 1 3 *')).not.toMatch(/Monthly/);
  });
});

describe('timeToCron', () => {
  it('never throws on a malformed original', () => {
    expect(() => timeToCron('09:00', 'not a cron')).not.toThrow();
  });
  it('does not silently convert an hourly schedule to a daily one', () => {
    expect(timeToCron('09:00', '0 * * * *')).toBe('0 * * * *');
  });
  it('refuses to collapse a multi-run schedule', () => {
    expect(timeToCron('09:00', '0 8,20 * * *')).toBe('0 8,20 * * *');
    expect(timeToCron('09:00', '0,30 8 * * *')).toBe('0,30 8 * * *');
  });
});
