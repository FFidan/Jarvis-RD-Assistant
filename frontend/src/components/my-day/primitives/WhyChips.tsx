import { PULSE_WEIGHT_LABELS } from '@/components/settings/pulse/pulse-constants';
import { displayReasoning } from '@/components/pulse/reasoning-display';

// Keys not in the canonical map (WhyChips-only signals / short aliases)
const SIGNAL_LABELS_EXT: Record<string, string> = {
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

const SIGNAL_LABELS: Record<string, string> = { ...PULSE_WEIGHT_LABELS, ...SIGNAL_LABELS_EXT };

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
  const displayedReasoning = displayReasoning(reasoning);
  const ranked: WhySignal[] = Object.entries(signals)
    .filter(([, w]) => w > 0.1)
    .map(([k, w]) => ({ label: SIGNAL_LABELS[k] ?? k, weight: w }))
    .sort((a, b) => b.weight - a.weight)
    .slice(0, max);

  if (ranked.length === 0 && !displayedReasoning) return null;

  if (ranked.length === 0) {
    return <p className="font-serif italic text-[13.5px] text-soft mt-1">{displayedReasoning}</p>;
  }

  return (
    <div>
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
      {displayedReasoning && <p className="font-serif italic text-[13.5px] text-soft mt-1">{displayedReasoning}</p>}
    </div>
  );
}
