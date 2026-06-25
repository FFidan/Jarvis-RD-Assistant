export const SIGNAL_LABELS: Record<string, string> = {
  // Canonical Pulse weight keys (mirrors PULSE_WEIGHT_LABELS in pulse-constants)
  embedding: 'Semantic similarity',
  topic: 'Topic match',
  llm_relevance: 'Relevance score',
  llm_novelty: 'Novelty score',
  author_bonus: 'Tracked-author bonus',
  recency: 'Recency',
  citation_pagerank: 'Citation PageRank',
  citation_count: 'Citation count',
  citation_adamic_adar: 'Shared citation neighbourhood',
  classifier: 'Personal classifier',
  // Short aliases used in why-chip / score breakdown contexts
  emb: 'Semantic similarity',
  llm: 'Relevance score',
  rec: 'Recommendation',
  recommendation: 'Recommendation',
  graph: 'Citation graph',
  graph_boost: 'Citation graph',
  author_overlap: 'Author overlap',
  topic_match: 'Topic match',
  library_overlap: 'In your library',
};

export function signalLabel(key: string): string {
  return SIGNAL_LABELS[key] ?? key;
}
