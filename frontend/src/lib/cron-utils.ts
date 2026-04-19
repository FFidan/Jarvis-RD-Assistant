/**
 * Shared cron-expression utility functions.
 *
 * Exported helpers are used by AutomationSection, PulseSection, and
 * SetupWizard so that the parsing logic lives in exactly one place.
 */

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/**
 * Convert an HH:MM time string to a cron expression.
 *
 * When `originalCron` is provided the day-of-week / day-of-month / month
 * fields are preserved (e.g. `0 8 * * 1` stays weekly on Monday).
 * When only a time is given (SetupWizard) a daily `* * *` suffix is used.
 *
 * `defaultCron` is returned when `time` is invalid (NaN hours/minutes) and
 * `originalCron` is not supplied.  Defaults to `'0 9 * * *'` (09:00 daily).
 */
export function timeToCron(
  time: string,
  originalCron?: string | null,
  defaultCron = '0 9 * * *',
): string {
  const [hourStr, minuteStr] = time.split(':');
  const hour = parseInt(hourStr, 10);
  const minute = parseInt(minuteStr, 10);
  if (isNaN(hour) || isNaN(minute)) {
    return originalCron ?? defaultCron;
  }
  if (originalCron) {
    const parts = originalCron.split(/\s+/);
    return `${minute} ${hour} ${parts[2] ?? '*'} ${parts[3] ?? '*'} ${parts[4] ?? '*'}`;
  }
  return `${minute} ${hour} * * *`;
}

/**
 * Extract the HH:MM time from a 5-field cron expression.
 * Returns '09:00' when the expression is malformed.
 */
export function cronToTime(cron: string): string {
  const parts = cron.split(/\s+/);
  const minute = parseInt(parts[0], 10);
  const hour = parseInt(parts[1], 10);
  if (isNaN(minute) || isNaN(hour)) return '09:00';
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

/**
 * Produce a human-readable description of a 5-field cron expression.
 * Examples: "Daily at 08:00", "Weekly on Mon at 08:00", "Weekdays at 08:00".
 */
export function cronToHumanReadable(cron: string): string {
  const parts = cron.split(/\s+/);
  if (parts.length < 5) return cron;
  const [minStr, hourStr, , , dowStr] = parts;
  const minute = parseInt(minStr, 10);
  const hour = parseInt(hourStr, 10);
  if (isNaN(minute) || isNaN(hour)) return cron;
  const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
  if (dowStr === '*') return `Daily at ${time}`;
  if (dowStr === '1-5') return `Weekdays at ${time}`;
  if (dowStr === '0,6' || dowStr === '6,0') return `Weekends at ${time}`;
  if (dowStr.includes('-') || dowStr.includes(',')) return `Custom days at ${time}`;
  const dow = parseInt(dowStr, 10);
  if (isNaN(dow)) return `Custom days at ${time}`;
  const dayName = DAY_NAMES[dow] ?? dowStr;
  return `Weekly on ${dayName} at ${time}`;
}
