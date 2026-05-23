import { describe, it, expect } from 'vitest';
import { QUERY_KEYS } from '@/lib/query-keys';

/**
 * Safety-net tests for the centralized query key registry.
 * Asserts tuple byte-equivalence so renames can't silently drift from the
 * inline literals they replaced.
 */
describe('QUERY_KEYS registry', () => {
  describe('papers', () => {
    it('feedAll() returns the bare prefix tuple for cache invalidation', () => {
      expect(QUERY_KEYS.papers.feedAll()).toEqual(['papers-feed']);
    });

    it('feed(surface, ...) returns full parameterized tuple', () => {
      expect(QUERY_KEYS.papers.feed('inbox', 'all', 'library', 30, 0, null)).toEqual([
        'papers-feed',
        'inbox',
        'all',
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

    it('pulse.stats(days) encodes days', () => {
      expect(QUERY_KEYS.pulse.stats(7)).toEqual(['pulse-stats', 7]);
    });

    it('debug() returns bare key', () => {
      expect(QUERY_KEYS.pulse.debug()).toEqual(['pulse-debug']);
    });

    it('statsAll() returns bare prefix for invalidation', () => {
      expect(QUERY_KEYS.pulse.statsAll()).toEqual(['pulse-stats']);
    });

    it('explain(cardId) returns correct tuple', () => {
      expect(QUERY_KEYS.pulse.explain(99)).toEqual(['pulse-explain', 99]);
    });
  });

  describe('decks', () => {
    it('list() returns correct tuple', () => {
      expect(QUERY_KEYS.decks.list()).toEqual(['decks']);
    });
  });

  describe('cards', () => {
    it('all() returns bare key', () => {
      expect(QUERY_KEYS.cards.all()).toEqual(['cards']);
    });

    it('byDeck(deckId) returns parameterized key', () => {
      expect(QUERY_KEYS.cards.byDeck(5)).toEqual(['cards', 5]);
    });

    it('stats() returns correct tuple', () => {
      expect(QUERY_KEYS.cards.stats()).toEqual(['card-stats']);
    });
  });

  describe('tasks', () => {
    it('byProject(projectId) returns correct tuple', () => {
      expect(QUERY_KEYS.tasks.byProject(3)).toEqual(['tasks', 3]);
    });
  });

  describe('extraction', () => {
    it('templates() returns correct tuple', () => {
      expect(QUERY_KEYS.extraction.templates()).toEqual(['extraction-templates']);
    });

    it('table(templateId, paperIds) returns parameterized key', () => {
      expect(QUERY_KEYS.extraction.table(2, [10, 20])).toEqual(['extraction-table', 2, [10, 20]]);
    });
  });
});
