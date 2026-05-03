const SIGNAL_LABELS: Record<string, string> = {
  embedding: 'Embedding match',
  emb: 'Embedding match',
  llm: 'LLM relevance',
  llm_relevance: 'LLM relevance',
  rec: 'Recommendation',
  recommendation: 'Recommendation',
  graph: 'Citation graph',
  graph_boost: 'Citation graph',
  author_overlap: 'Author overlap',
  topic_match: 'Topic match',
  library_overlap: 'In your library',
};

interface WhySignal {
  label: string;
  weight: number;
}

interface WhyChipsProps {
  signals: Record<string, number>;
  reasoning?: string | null;
  max?: number;
}

export function WhyChips({ signals, reasoning, max = 3 }: WhyChipsProps) {
  const ranked: WhySignal[] = Object.entries(signals)
    .filter(([, w]) => w > 0.1)
    .map(([k, w]) => ({ label: SIGNAL_LABELS[k] ?? k, weight: w }))
    .sort((a, b) => b.weight - a.weight)
    .slice(0, max);

  if (ranked.length === 0 && !reasoning) return null;

  return (
    <div className="flex items-start gap-2 flex-wrap">
      <span className="font-mono text-[10px] text-meta uppercase tracking-[0.18em] mt-0.5 shrink-0">WHY</span>
      <div className="flex flex-wrap gap-1.5">
        {ranked.map((s) => (
          <span
            key={s.label}
            className="inline-flex items-center rounded-full bg-[color-mix(in_srgb,var(--ink-blue)_12%,transparent)] border border-[color-mix(in_srgb,var(--ink-blue)_30%,transparent)] px-2.5 py-0.5 text-[11px] text-[var(--ink-blue)] font-medium"
          >
            {s.label} ({s.weight.toFixed(2)})
          </span>
        ))}
      </div>
    </div>
  );
}
