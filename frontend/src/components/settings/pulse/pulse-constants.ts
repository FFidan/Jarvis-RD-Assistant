/**
 * Shared constants for the Pulse settings panel.
 * Extracted from PulseSection.tsx (bloat-reduction).
 */

export type PulseWeightKey =
  | 'embedding'
  | 'topic'
  | 'llm_relevance'
  | 'llm_novelty'
  | 'author_bonus'
  | 'recency'
  | 'citation_pagerank'
  | 'citation_count'
  | 'citation_adamic_adar'
  | 'classifier';

export const DEFAULT_PULSE_WEIGHTS: Record<PulseWeightKey, number> = {
  embedding: 0.2,
  topic: 0.2,
  llm_relevance: 0.3,
  llm_novelty: 0.1,
  author_bonus: 0.15,
  recency: 0.05,
  citation_pagerank: 0,
  citation_count: 0,
  citation_adamic_adar: 0,
  classifier: 0,
};

/** Core signals that are always available (no extra dependencies). */
export const CORE_SIGNAL_KEYS: PulseWeightKey[] = [
  'embedding',
  'topic',
  'llm_relevance',
  'llm_novelty',
  'author_bonus',
  'recency',
];

/** Optional signals that require extra data or backend dependencies. */
export const OPTIONAL_SIGNAL_KEYS: PulseWeightKey[] = [
  'citation_pagerank',
  'citation_count',
  'citation_adamic_adar',
  'classifier',
];

export const PULSE_WEIGHT_KEYS: PulseWeightKey[] = [
  'embedding',
  'topic',
  'llm_relevance',
  'llm_novelty',
  'author_bonus',
  'recency',
  'citation_pagerank',
  'citation_count',
  'citation_adamic_adar',
  'classifier',
];

/** Canonical plain-language label for each signal key — single source of truth for signal labels. */
export const PULSE_WEIGHT_LABELS: Record<PulseWeightKey, string> = {
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
};

export const PULSE_WEIGHT_TOOLTIPS: Record<PulseWeightKey, string> = {
  embedding:
    "Semantic similarity between this paper and papers you've previously starred or rated. High weight = surface papers similar to what you already read.",
  topic:
    "Match between the paper's content and your configured research Topics. High weight = stay close to your declared research interests.",
  llm_relevance:
    'How relevant this paper is to your research focus, scored by a language model. Slower but more accurate than keyword matching. High weight = quality over speed.',
  llm_novelty:
    "How novel or surprising this paper is given your reading history, scored by a language model. High weight = prioritise papers you're unlikely to have already seen.",
  author_bonus:
    'Additive bonus for papers co-authored by anyone in your tracked Authors list. High weight = always surface papers by your followed researchers.',
  recency:
    'Prefer papers published more recently. High weight = always surface the newest work, even if it scores lower on relevance.',
  citation_pagerank:
    'Boosts papers that are highly influential in the citation network near your interests. Needs citation data — fetch citations for some papers first.',
  citation_count:
    'Boosts papers with more citations from source metadata. Needs citation data — fetch citations for some papers first.',
  citation_adamic_adar:
    'Boosts candidates that share specific citation neighbours with papers you liked, without computing the full graph. Needs citation data — fetch citations for some papers first.',
  classifier:
    'Probability from a personal classifier trained on your Pulse ratings. Gets better as you rate more papers — best after about 30 ratings.',
};

/**
 * Gate tooltip shown only when the required capability is missing.
 * Maps signal key → { capability, message }.
 */
// These packages ship in every standard image, so telling a researcher to
// install them on the server was wrong on both audience and facts. The gate
// only ever fires on a custom server build; say that.
const CITATION_GATE_MESSAGE =
  'Citation-based signals are not available on this server build. Standard installations include them by default.';

export const CONDITIONAL_SIGNAL_GATES: Partial<
  Record<PulseWeightKey, { capability: 'networkx' | 'scikit_learn'; message: string }>
> = {
  citation_pagerank: { capability: 'networkx', message: CITATION_GATE_MESSAGE },
  citation_count: { capability: 'networkx', message: CITATION_GATE_MESSAGE },
  citation_adamic_adar: { capability: 'networkx', message: CITATION_GATE_MESSAGE },
  classifier: {
    capability: 'scikit_learn',
    message:
      'The personal classifier is not available on this server build. Standard installations include it by default.',
  },
};

export const CRON_TOOLTIP =
  'The time of day when Pulse discovery runs automatically. Papers are scored and ranked so your deck is ready when you start your day.';

/**
 * Presets for signal weights.
 * All presets use only core signals (always available) so they always sum correctly
 * without needing optional dependencies. Values sum to 1.0.
 */
export const WEIGHT_PRESETS: {
  label: string;
  description: string;
  weights: Partial<Record<PulseWeightKey, number>>;
}[] = [
  {
    label: 'Balanced',
    description: 'Equal emphasis on relevance, novelty, and semantic similarity.',
    weights: { ...DEFAULT_PULSE_WEIGHTS },
  },
  {
    label: 'Semantic-first',
    description: 'Surface papers closely matching your existing reading, minimise LLM cost.',
    weights: {
      embedding: 0.4,
      topic: 0.35,
      llm_relevance: 0.1,
      llm_novelty: 0.05,
      author_bonus: 0.05,
      recency: 0.05,
      citation_pagerank: 0,
      citation_count: 0,
      citation_adamic_adar: 0,
      classifier: 0,
    },
  },
  {
    label: 'Freshness-first',
    description: 'Always surface the newest papers, regardless of similarity.',
    weights: {
      embedding: 0.15,
      topic: 0.1,
      llm_relevance: 0.25,
      llm_novelty: 0.05,
      author_bonus: 0.05,
      recency: 0.4,
      citation_pagerank: 0,
      citation_count: 0,
      citation_adamic_adar: 0,
      classifier: 0,
    },
  },
];
