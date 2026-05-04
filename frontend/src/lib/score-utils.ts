const SIGNAL_MAP = {
  emb:   ['embedding', 'emb'],
  llm:   ['llm', 'llm_relevance', 'topic'],
  rec:   ['rec', 'recommendation', 'recency'],
  graph: ['graph', 'graph_boost', 'classifier', 'citation_pagerank'],
} as const;

export function toScoreParts(signals: Record<string, number>) {
  return Object.fromEntries(
    Object.entries(SIGNAL_MAP).map(([key, aliases]) => {
      const match = (aliases as readonly string[]).find(a => a in signals);
      return [key, match ? signals[match] : 0];
    })
  ) as { emb: number; llm: number; rec: number; graph: number };
}
