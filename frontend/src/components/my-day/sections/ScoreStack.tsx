interface ScoreStackProps {
  score: number; // 0..1, displayed left of bar (e.g. "0.94")
  parts: { emb: number; llm: number; rec: number; graph: number };
  className?: string; // for size variants
}

export function ScoreStack({ score, parts, className = '' }: ScoreStackProps) {
  // Normalize parts so they sum to 1; if all zero, fall back to equal split
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
    <div className={`flex items-center gap-2 ${className}`}>
      <span className="font-mono tabular-nums text-[11px] w-9 text-soft">
        {score.toFixed(2)}
      </span>
      <div className="flex h-1.5 flex-1 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-800">
        <div className="bg-emerald-500" style={{ width: `${norm.emb * 100}%` }} />
        <div className="bg-sky-500" style={{ width: `${norm.llm * 100}%` }} />
        <div className="bg-violet-500" style={{ width: `${norm.rec * 100}%` }} />
        <div className="bg-amber-500" style={{ width: `${norm.graph * 100}%` }} />
      </div>
    </div>
  );
}
