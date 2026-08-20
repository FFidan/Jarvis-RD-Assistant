import { describe, expect, it } from 'vitest';
import { PAPER_STATE_LABELS, paperStateLabel } from './paperState';

describe('paperStateLabel', () => {
  it('uses plain lifecycle labels and preserves an unknown server value', () => {
    expect(PAPER_STATE_LABELS).toEqual({
      inbox: 'Inbox',
      to_read: 'Reading List',
      reading: 'Reading',
      done: 'Done',
      trash: 'Trash',
    });
    expect(paperStateLabel('to_read')).toBe('Reading List');
    expect(paperStateLabel('pending_review')).toBe('pending_review');
  });
});
