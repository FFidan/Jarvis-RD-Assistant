/**
 * Centralized TanStack-Query key registry. Avoids inline string literals scattered
 * across hooks. New entries: add to this file and import.
 *
 * 114+ inline call-sites elsewhere are migrated lazily under rot-on-touch policy
 * (per audit DR2 §C5 / DRY-F2 acceptance criteria).
 */
export const QUERY_KEYS = {
  papers: {
    /** Parameterized feed list. Pass all feed dimensions so React Query
     *  re-fetches on any filter/pagination change. For cache-invalidation
     *  purposes (invalidateQueries) the prefix `['papers-feed']` is a
     *  partial match — callers can pass no args to hit all variants. */
    list: (
      surface?: string | null,
      filter?: string | null,
      scope?: string | null,
      limit?: number | null,
      offset?: number | null,
      sourceTypes?: string | null,
    ) => {
      if (surface !== undefined) {
        return ['papers-feed', surface, filter, scope, limit, offset, sourceTypes] as const;
      }
      return ['papers-feed'] as const;
    },
    detail: (id: number) => ['paper-detail', id] as const,
  },
  pulse: {
    today: () => ['pulse-today'] as const,
    stats: (days?: number) => {
      if (days !== undefined) {
        return ['pulse-stats', days] as const;
      }
      return ['pulse-stats'] as const;
    },
    debug: () => ['pulse-debug'] as const,
    explain: (cardId: number) => ['pulse-explain', cardId] as const,
  },
  decks: {
    list: () => ['decks'] as const,
    cards: (deckId?: number) => {
      if (deckId !== undefined) {
        return ['cards', deckId] as const;
      }
      return ['cards'] as const;
    },
    stats: () => ['card-stats'] as const,
  },
  tasks: {
    byProject: (projectId: number) => ['tasks', projectId] as const,
  },
  extractions: {
    templates: () => ['extraction-templates'] as const,
    table: (templateId?: number | null, paperIds?: number[]) => {
      if (templateId !== undefined && templateId !== null && paperIds !== undefined) {
        return ['extraction-table', templateId, paperIds] as const;
      }
      return ['extraction-table'] as const;
    },
  },
} as const;
