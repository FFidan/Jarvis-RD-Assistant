/**
 * Shared cron-expression utility functions.
 *
 * Exported helpers are used by AutomationSection, PulseSection, and
 * SetupWizard so that the parsing logic lives in exactly one place.
 */

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];

/**
 * Matches a cron field that can represent more than one point in time: a
 * list (`,`), a range (`-`), a step (`/`), or a wildcard (`*`).
 */
const MULTI_VALUE_FIELD = /[,\-/*]/;

/**
 * Split and validate a 5-field cron expression.
 * Throws if the expression does not have exactly 5 whitespace-separated fields.
 * After the length check the fields are provably present, so the cast is safe.
 */
export function splitCron(expression: string): [string, string, string, string, string] {
  const parts = expression.trim().split(/\s+/);
  if (parts.length !== 5) {
    throw new Error(
      `Invalid cron expression: expected 5 fields, got ${parts.length}: "${expression}"`,
    );
  }
  return parts as [string, string, string, string, string];
}

/**
 * Whether a 5-field cron expression represents a single daily clock time
 * that a plain HH:MM picker can show and round-trip without losing runs.
 * False when the expression is malformed, or when either the minute or the
 * hour field carries a list, range, step or wildcard (multiple daily runs,
 * or an hourly/sub-hourly schedule).
 */
export function isTimeOnlyCron(cron: string): boolean {
  let parts: [string, string, string, string, string];
  try {
    parts = splitCron(cron);
  } catch {
    return false;
  }
  const [minute, hour] = parts;
  return !MULTI_VALUE_FIELD.test(minute) && !MULTI_VALUE_FIELD.test(hour);
}

/**
 * Convert an HH:MM time string to a cron expression.
 *
 * When `originalCron` is provided the day-of-week / day-of-month / month
 * fields are preserved (e.g. `0 8 * * 1` stays weekly on Monday), but only
 * when `originalCron` itself is a single clock time (see `isTimeOnlyCron`);
 * otherwise it is returned unchanged rather than collapsing a multi-run or
 * hourly schedule down to the new time.
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
  const timeParts = time.split(':');
  if (timeParts.length < 2 || !timeParts[0] || !timeParts[1]) {
    return originalCron ?? defaultCron;
  }
  const hour = parseInt(timeParts[0], 10);
  const minute = parseInt(timeParts[1], 10);
  if (isNaN(hour) || isNaN(minute)) {
    return originalCron ?? defaultCron;
  }
  if (originalCron) {
    if (!isTimeOnlyCron(originalCron)) {
      return originalCron;
    }
    const [, , dom, month, dow] = splitCron(originalCron);
    return `${minute} ${hour} ${dom} ${month} ${dow}`;
  }
  return `${minute} ${hour} * * *`;
}

/**
 * Extract the HH:MM time from a 5-field cron expression.
 * Returns '09:00' when the expression is malformed.
 */
export function cronToTime(cron: string): string {
  let parts: [string, string, string, string, string];
  try {
    parts = splitCron(cron);
  } catch {
    return '09:00';
  }
  const [minStr, hourStr] = parts;
  const minute = parseInt(minStr, 10);
  const hour = parseInt(hourStr, 10);
  if (isNaN(minute) || isNaN(hour)) return '09:00';
  return `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;
}

/**
 * Produce a human-readable description of a 5-field cron expression.
 * Examples: "Daily at 08:00", "Weekly on Mon at 08:00", "Weekdays at 08:00",
 * "Every hour", "Monthly on day 1 at 08:00".
 */
export function cronToHumanReadable(cron: string): string {
  let cronParts: [string, string, string, string, string];
  try {
    cronParts = splitCron(cron);
  } catch {
    return cron;
  }
  const [minStr, hourStr, domStr, monthStr, dowStr] = cronParts;
  const minute = parseInt(minStr, 10);
  if (isNaN(minute)) return cron;

  // A restricted month makes the schedule yearly whatever the other fields say,
  // and there is no short honest phrase for that, so show the expression.
  if (monthStr !== '*') return cron;

  if (hourStr === '*' && !MULTI_VALUE_FIELD.test(minStr)) {
    return 'Every hour';
  }

  const hour = parseInt(hourStr, 10);
  if (isNaN(hour)) return cron;
  const time = `${String(hour).padStart(2, '0')}:${String(minute).padStart(2, '0')}`;

  if (domStr !== '*') {
    // Cron ORs day-of-month with day-of-week, so `0 9 1 * 1` fires on the 1st
    // AND every Monday — more runs than a monthly phrase would admit to.
    if (dowStr !== '*') return cron;
    return `Monthly on day ${domStr} at ${time}`;
  }

  if (dowStr === '*') return `Daily at ${time}`;
  if (dowStr === '1-5') return `Weekdays at ${time}`;
  if (dowStr === '0,6' || dowStr === '6,0') return `Weekends at ${time}`;
  if (dowStr.includes('-') || dowStr.includes(',')) return `Custom days at ${time}`;
  const dow = parseInt(dowStr, 10);
  if (isNaN(dow)) return `Custom days at ${time}`;
  // Cron allows both 0 and 7 for Sunday. Anything else outside 0-6 is not a day
  // of the week, and naming one anyway would describe runs that never happen.
  const dayName = DAY_NAMES[dow === 7 ? 0 : dow];
  if (dayName === undefined) return cron;
  return `Weekly on ${dayName} at ${time}`;
}
