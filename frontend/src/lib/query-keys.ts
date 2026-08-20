/**
 * Centralised TanStack Query key registry.
 *
 * Every key is a typed factory returning an `as const` tuple so call-sites get
 * exact literal types for cache matching and invalidation.  All inline
 * `queryKey: [...]` literals are migrated to these factories; do not add new
 * inline literals — use or extend this registry instead.
 */

export const QUERY_KEYS = {
  // ── Papers ────────────────────────────────────────────────────────────────
  papers: {
    feed: (
      surface: string,
      filter: string,
      scope: string,
      limit: number,
      offset: number,
      sourceTypes: string[] | null,
      topicId: number | null = null,
      untagged: boolean = false,
    ) =>
      (topicId == null
        ? (untagged
            ? (["papers-feed", surface, filter, scope, limit, offset, sourceTypes, "untagged"] as const)
            : (["papers-feed", surface, filter, scope, limit, offset, sourceTypes] as const))
        : (untagged
            ? ([
                "papers-feed",
                surface,
                filter,
                scope,
                limit,
                offset,
                sourceTypes,
                topicId,
                "untagged",
              ] as const)
            : ([
                "papers-feed",
                surface,
                filter,
                scope,
                limit,
                offset,
                sourceTypes,
                topicId,
              ] as const))),
    /** Bare prefix for `invalidateQueries` — matches all papers-feed cache entries. */
    feedAll: () => ["papers-feed"] as const,
    detail: (paperId: number) => ["paper-detail", paperId] as const,
  },

  // ── Setup / first-run ─────────────────────────────────────────────────────
  setup: {
    status: () => ["setup-status"] as const,
    firstRun: () => ["first-run-status"] as const,
  },

  // ── AI settings ───────────────────────────────────────────────────────────
  aiSettings: {
    settings: () => ["ai-settings"] as const,
  },

  // ── Admin ─────────────────────────────────────────────────────────────────
  admin: {
    auditLog: (actionPrefix: string) => ["admin", "audit-log", actionPrefix] as const,
    users: () => ["admin", "users"] as const,
    systemHealth: () => ["admin", "system-health"] as const,
    restoreStatus: () => ["admin", "restore-status"] as const,
    restorePoints: () => ["admin", "restore-points"] as const,
    backupRetention: () => ["admin", "backups", "retention"] as const,
    backupStatus: () => ["admin", "backups", "status"] as const,
    backupInbox: () => ["admin", "backups", "inbox"] as const,
  },

  // ── Citation graph ────────────────────────────────────────────────────────
  citation: {
    graph: (paperIds: number[], depth: number) =>
      ["citation-graph", paperIds, depth] as const,
  },

  // ── Knowledge graph ───────────────────────────────────────────────────────
  knowledge: {
    graph: (filterType: string | undefined, minPaperCount: number) =>
      ["knowledge-graph", filterType, minPaperCount] as const,
  },

  // ── Sources ───────────────────────────────────────────────────────────────
  sources: {
    list: () => ["sources"] as const,
  },

  // ── Projects ──────────────────────────────────────────────────────────────
  projects: {
    list: () => ["projects"] as const,
    activeOnly: () => ["projects", "active"] as const,
    detail: (id: number) => ["project", id] as const,
    papers: (projectId: number) => ["project-papers", projectId] as const,
    questions: (projectId: number) => ["project-questions", projectId] as const,
    milestones: (projectId: number) => ["milestones", projectId] as const,
    activity: (projectId: number) => ["project-activity", projectId] as const,
  },

  // ── Tasks ─────────────────────────────────────────────────────────────────
  tasks: {
    byProject: (projectId: number) => ["tasks", projectId] as const,
  },

  // ── Notes ─────────────────────────────────────────────────────────────────
  notes: {
    user: (paperId: number) => ["notes", paperId, "user"] as const,
    zotero: (paperId: number) => ["notes", paperId, "zotero"] as const,
  },

  // ── Highlights (in-PDF annotation reader) ─────────────────────────────────
  highlights: {
    list: (paperId: number) => ["highlights", paperId] as const,
  },

  // ── Contradictions ────────────────────────────────────────────────────────
  contradictions: {
    verified: (paperId: number) =>
      ["contradictions", paperId, "verified"] as const,
  },

  // ── Consensus ─────────────────────────────────────────────────────────────
  consensus: {
    all: () => ["consensus"] as const,
  },

  // ── Zotero ────────────────────────────────────────────────────────────────
  zotero: {
    linkage: (paperId: number) => ["zotero-linkage", paperId] as const,
  },

  // ── Feed ──────────────────────────────────────────────────────────────────
  feed: {
    counts: (
      scope?: "library" | "corpus",
      view?: string,
      source?: string | null,
      topicId?: number | null,
      untagged?: boolean,
    ) =>
      scope || view || source || topicId != null || untagged
        ? ([
            "feed-counts",
            scope ?? null,
            view ?? null,
            source ?? null,
            topicId ?? null,
            untagged ?? false,
          ] as const)
        : (["feed-counts"] as const),
    onboardingCheck: () => ["papers-feed", "onboarding-check"] as const,
    readingHero: () => ["feed", "reading", "hero"] as const,
  },

  // ── Dashboard ─────────────────────────────────────────────────────────────
  dashboard: {
    metrics: () => ["dashboard-metrics"] as const,
  },

  // ── Analytics ─────────────────────────────────────────────────────────────
  analytics: {
    summary: (days: number) => ["analytics", "summary", days] as const,
    activity: (days: number) => ["analytics", "activity", days] as const,
    retention: (days: number) => ["analytics", "retention", days] as const,
    reviews: (days: number) => ["analytics", "reviews", days] as const,
    llmCost: (days: number) => ["analytics", "llm-cost", days] as const,
    papersBySource: () => ["analytics", "papers-by-source"] as const,
    papersByStatus: () => ["analytics", "papers-by-status"] as const,
    missingFoundational: () => ["analytics", "missing-foundational"] as const,
  },

  // ── Logs ──────────────────────────────────────────────────────────────────
  logs: {
    correlation: (id: string) => ["logs", "correlation", id] as const,
    summaryAppOnly: () => ["logs", "summary", "app-only"] as const,
    recent: () => ["logs", "recent"] as const,
    sources: () => ["logs", "sources"] as const,
    events: (
      levelFilter: string,
      categoryFilter: string,
      sourceFilter: string,
      since: string,
      until: string,
      query: string,
    ) =>
      [
        "logs",
        "events",
        levelFilter,
        categoryFilter,
        sourceFilter,
        since,
        until,
        query,
      ] as const,
  },

  // ── Jobs ──────────────────────────────────────────────────────────────────
  jobs: {
    list: (statusFilter?: string) =>
      statusFilter ? (["jobs", statusFilter] as const) : (["jobs"] as const),
  },

  // ── My Day ────────────────────────────────────────────────────────────────
  myDay: {
    today: () => ["my-day"] as const,
    threads: () => ["my-day", "threads"] as const,
    yesterday: (tzOffsetMinutes: number) =>
      ["my-day", "yesterday", tzOffsetMinutes] as const,
    bundle: (tzOffsetMinutes: number) =>
      ["my-day-bundle", tzOffsetMinutes] as const,
  },

  // ── Intent ────────────────────────────────────────────────────────────────
  intent: {
    today: () => ["intent", "today"] as const,
  },

  // ── Pulse (settings / health) ─────────────────────────────────────────────
  pulseHealth: {
    sourceHealth: () => ["pulse", "source-health"] as const,
    sourceHistory: (days: number) => ["pulse", "source-history", days] as const,
    feedback: () => ["feedback-summary"] as const,
  },

  // ── Pulse (my-day / review) ───────────────────────────────────────────────
  pulse: {
    today: () => ["pulse-today"] as const,
    /** Per-day pulse stats — `days` is encoded so per-window caches don't collide. */
    stats: (days: number) => ["pulse-stats", days] as const,
    /** Bare prefix for `invalidateQueries` — matches all pulse-stats cache entries. */
    statsAll: () => ["pulse-stats"] as const,
    /** Pulse debug-only telemetry payload (admin diagnostics). */
    debug: () => ["pulse-debug"] as const,
    explain: (cardId: number) => ["pulse-explain", cardId] as const,
  },

  // ── Config ────────────────────────────────────────────────────────────────
  config: {
    all: () => ["config"] as const,
    systemModels: () => ["system-models"] as const,
    systemCapabilities: () => ["system-capabilities"] as const,
    providers: () => ["settings", "providers"] as const,
    providerAccount: (provider: string) => ["settings", "providers", provider, "account"] as const,
  },

  // ── Topics ────────────────────────────────────────────────────────────────
  topics: {
    list: () => ["topics"] as const,
    subscriptions: () => ["topic-subscriptions"] as const,
    rejected: () => ["rejected-topics"] as const,
  },

  // ── Authors ───────────────────────────────────────────────────────────────
  authors: {
    tracked: () => ["tracked-authors"] as const,
  },

  // ── Account ───────────────────────────────────────────────────────────────
  account: {
    self: () => ["account"] as const,
    smtp: () => ["smtp-config"] as const,
    nudges: () => ["nudges"] as const,
  },

  // ── Passkeys (WebAuthn sign-in + device management) ────────────────────────
  passkeys: {
    /** Server-side capability probe (origin allowlist) — drives the login button. */
    capability: () => ["passkeys", "capability"] as const,
    /** The current user's registered passkeys (Settings → Passkeys). */
    list: () => ["passkeys", "list"] as const,
    /** Admin: a single user's passkey count (per-row on the users page). */
    adminCount: (userId: number) => ["passkeys", "admin-count", userId] as const,
  },

  // ── Telegram pairing ──────────────────────────────────────────────────────
  pairing: {
    userTelegram: () => ["user-telegram-pairing"] as const,
    botTokenStatus: () => ["telegram-bot-token-status"] as const,
  },

  // ── Stack health ──────────────────────────────────────────────────────────
  stack: {
    /** Bare-key health probe: ["stack-health"]. The single cache entry for all
     * health surfaces (HealthDots, AdminSystemHealthPage, query-persister) so
     * they can never show contradictory snapshots. */
    health: () => ["stack-health"] as const,
  },

  // ── Papers brief (search) ─────────────────────────────────────────────────
  papersBrief: {
    list: (search?: string) =>
      search ? (["papers-brief", search] as const) : (["papers-brief"] as const),
    recentFeedback: (paperId: number) =>
      ["recent-feedback", paperId] as const,
  },

  // ── Action items ──────────────────────────────────────────────────────────
  actionItems: {
    unprocessed: () => ["action-items-unprocessed"] as const,
  },

  // ── Retention ─────────────────────────────────────────────────────────────
  retention: {
    stats: () => ["retention-stats"] as const,
  },

  // ── Review queue ──────────────────────────────────────────────────────────
  reviewQueue: {
    next: () => ["review-next"] as const,
  },

  // ── Digest ────────────────────────────────────────────────────────────────
  digest: {
    weekly: () => ["digest-weekly"] as const,
  },

  // ── Journal ───────────────────────────────────────────────────────────────
  journal: {
    entry: (date: string) => ["journalEntry", date] as const,
  },

  // ── Decks (spaced-repetition card decks) ─────────────────────────────────
  decks: {
    list: () => ["decks"] as const,
  },

  // ── Cards ─────────────────────────────────────────────────────────────────
  cards: {
    all: () => ["cards"] as const,
    byDeck: (deckId: number) => ["cards", deckId] as const,
    stats: () => ["card-stats"] as const,
  },

  // ── Extraction ────────────────────────────────────────────────────────────
  extraction: {
    templates: () => ["extraction-templates"] as const,
    table: (templateId: number | null, paperIds: number[]) =>
      ["extraction-table", templateId, paperIds] as const,
  },
} as const;
