interface ScoreStackProps {
  score: number;
  parts: { emb: number; llm: number; rec: number; graph: number };
  className?: string;
  showBadges?: boolean;
}

export function ScoreStack({ score, parts, className = '', showBadges = true }: ScoreStackProps) {
  const sum = parts.emb + parts.llm + parts.rec + parts.graph;
  const norm =
    sum > 0
      ? {
          emb: parts.emb / sum,
          llm: parts.llm / sum,
          rec: parts.rec / sum,
          graph: parts.graph / sum,
        }
      : { emb: 0.25, llm: 0.25, rec: 0.25, graph: 0.25 };

  return (
    <div className={`flex items-center gap-3 ${className}`}>
      <span className="font-mono tabular-nums text-[11px] w-9 text-soft">{score.toFixed(2)}</span>
      <div className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-700/60 ring-1 ring-inset ring-transparent dark:ring-zinc-600/30">
        <div style={{ width: `${norm.emb * 100}%`, backgroundColor: 'var(--ink-blue)' }} />
        <div style={{ width: `${norm.llm * 100}%`, backgroundColor: 'color-mix(in srgb, var(--ink-blue) 75%, white)' }} />
        <div style={{ width: `${norm.rec * 100}%`, backgroundColor: 'color-mix(in srgb, var(--ink-blue) 50%, white)' }} />
        <div style={{ width: `${norm.graph * 100}%`, backgroundColor: 'color-mix(in srgb, var(--ink-blue) 30%, white)' }} />
      </div>
      {showBadges && (
        <span className="font-mono text-[10px] text-meta tracking-tight tabular-nums whitespace-nowrap">
          emb·llm·rec·g
        </span>
      )}
    </div>
  );
}
