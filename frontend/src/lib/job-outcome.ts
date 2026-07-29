export interface JobOutcomeCounts {
  failed: number;
  skipped: number;
  remaining: number;
  total: number;
}

type JobOutcomeField =
  | 'total'
  | 'remaining'
  | 'failed'
  | 'skipped'
  | 'errors'
  | 'blocked';

function nonNegativeInteger(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) && value >= 0
    ? value
    : null;
}

function outcomeField(result: unknown, field: JobOutcomeField): unknown {
  return result !== null && typeof result === 'object'
    ? Reflect.get(result, field)
    : undefined;
}

/** Normalize the two result schemas used by batch handlers.
 *
 * Newer handlers expose scalar counts; whole-library processing exposes
 * per-paper arrays. Prefer a valid scalar and fall back to the array length so
 * job rows and notifications cannot disagree about the same result.
 */
export function jobOutcomeCounts(result: unknown): JobOutcomeCounts {
  const errors = outcomeField(result, 'errors');
  const blocked = outcomeField(result, 'blocked');
  const failed = nonNegativeInteger(outcomeField(result, 'failed'))
    ?? (Array.isArray(errors) ? errors.length : 0);
  const skipped = nonNegativeInteger(outcomeField(result, 'skipped'))
    ?? (Array.isArray(blocked) ? blocked.length : 0);
  const remaining = nonNegativeInteger(outcomeField(result, 'remaining')) ?? 0;
  const total = nonNegativeInteger(outcomeField(result, 'total'))
    ?? failed + skipped + remaining;
  return { failed, skipped, remaining, total };
}
