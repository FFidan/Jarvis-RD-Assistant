import { describe, it, expect } from 'vitest';
import { QUERY_KEYS } from '@/lib/query-keys';

/**
 * Safety-net tests for the centralized query key registry.
 * Asserts tuple byte-equivalence so renames can't silently drift from the
 * inline literals they replaced.
 */
describe('QUERY_KEYS registry', () => {
  describe('papers', () => {
    it('list() returns the bare prefix tuple', () => {
      expect(QUERY_KEYS.papers.list()).toEqual(['papers-feed']);
    });

    it('list(surface, ...) returns full parameterized tuple', () => {
      expect(QUERY_KEYS.papers.list('inbox', null, 'library', 30, 0, null)).toEqual([
        'papers-feed',
        'inbox',
        null,
        'library',
        30,
        0,
        null,
      ]);
    });

    it('detail(id) returns correct tuple', () => {
      expect(QUERY_KEYS.papers.detail(42)).toEqual(['paper-detail', 42]);
    });
  });

  describe('pulse', () => {
    it('today() returns correct tuple', () => {
      expect(QUERY_KEYS.pulse.today()).toEqual(['pulse-today']);
    });

    it('stats() returns bare key', () => {
      expect(QUERY_KEYS.pulse.stats()).toEqual(['pulse-stats']);
    });

    it('stats(days) returns parameterized key', () => {
      expect(QUERY_KEYS.pulse.stats(7)).toEqual(['pulse-stats', 7]);
    });

    it('debug() returns correct tuple', () => {
      expect(QUERY_KEYS.pulse.debug()).toEqual(['pulse-debug']);
    });

    it('explain(cardId) returns correct tuple', () => {
      expect(QUERY_KEYS.pulse.explain(99)).toEqual(['pulse-explain', 99]);
    });
  });

  describe('decks', () => {
    it('list() returns correct tuple', () => {
      expect(QUERY_KEYS.decks.list()).toEqual(['decks']);
    });

    it('cards() returns bare key', () => {
      expect(QUERY_KEYS.decks.cards()).toEqual(['cards']);
    });

    it('cards(deckId) returns parameterized key', () => {
      expect(QUERY_KEYS.decks.cards(5)).toEqual(['cards', 5]);
    });

    it('stats() returns correct tuple', () => {
      expect(QUERY_KEYS.decks.stats()).toEqual(['card-stats']);
    });
  });

  describe('tasks', () => {
    it('byProject(projectId) returns correct tuple', () => {
      expect(QUERY_KEYS.tasks.byProject(3)).toEqual(['tasks', 3]);
    });
  });

  describe('extractions', () => {
    it('templates() returns correct tuple', () => {
      expect(QUERY_KEYS.extractions.templates()).toEqual(['extraction-templates']);
    });

    it('table() returns bare invalidation key', () => {
      expect(QUERY_KEYS.extractions.table()).toEqual(['extraction-table']);
    });

    it('table(templateId, paperIds) returns parameterized key', () => {
      expect(QUERY_KEYS.extractions.table(2, [10, 20])).toEqual(['extraction-table', 2, [10, 20]]);
    });
  });
});
