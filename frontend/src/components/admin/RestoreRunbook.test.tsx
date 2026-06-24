/**
 * RestoreRunbook vitest
 *
 * Step 3 must restore the two real Qdrant collections (kg_entities,
 * paper_chunks) and must NOT reference the non-existent `papers` collection.
 */
import { describe, it, expect } from 'vitest';
import { render } from '@testing-library/react';
import { RestoreRunbook } from './RestoreRunbook';

describe('RestoreRunbook', () => {
  it('documents both real Qdrant collections and not the non-existent one', () => {
    const { container } = render(<RestoreRunbook />);
    const text = container.textContent ?? '';

    expect(text).toContain('/collections/kg_entities/snapshots/recover');
    expect(text).toContain('/collections/paper_chunks/snapshots/recover');
    expect(text).toContain('qdrant_kg_entities_YYYYMMDD_HHMMSS.snapshot');
    expect(text).toContain('qdrant_paper_chunks_YYYYMMDD_HHMMSS.snapshot');

    expect(text).not.toContain('/collections/papers/');
    expect(text).not.toContain('qdrant_papers_');
  });

  it('notes that encrypted snapshots must be decrypted first', () => {
    const { container } = render(<RestoreRunbook />);
    const text = container.textContent ?? '';
    expect(text).toContain('.snapshot.enc');
  });
});
