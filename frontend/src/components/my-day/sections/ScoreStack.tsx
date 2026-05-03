interface ScoreStackProps {
  score: number;
  parts: { emb: number; llm: number; rec: number; graph: number };
  className?: string;
  showBadges?: boolean;
}

export function ScoreStack({ score, parts, className = '', showBadges = true }: ScoreStackProps) {
  const sum = parts.emb + parts.llm + parts.rec + parts.graph;
  const noSignal = sum === 0;
  const norm = noSignal
    ? null
    : {
        emb: parts.emb / sum,
        llm: parts.llm / sum,
        rec: parts.rec / sum,
        graph: parts.graph / sum,
      };

  return (
    <div className={`flex items-center gap-3 ${className}`}>
      <span className="font-mono tabular-nums text-[11px] w-9 text-soft">{score.toFixed(2)}</span>
      <div className="flex h-2 flex-1 overflow-hidden rounded-full bg-zinc-100 dark:bg-zinc-700/60 ring-1 ring-inset ring-transparent dark:ring-zinc-600/30">
        {noSignal ? (
          <div
            title="no signal"
            style={{ width: '100%', backgroundColor: 'var(--color-zinc-300, #d4d4d8)' }}
            className="bg-zinc-300 dark:bg-zinc-600"
          />
        ) : (
          <>
            <div title={`emb ${(norm!.emb * 100).toFixed(0)}%`}     style={{ width: `${norm!.emb * 100}%`,   backgroundColor: 'var(--ink-blue)' }} />
            <div title={`llm ${(norm!.llm * 100).toFixed(0)}%`}     style={{ width: `${norm!.llm * 100}%`,   backgroundColor: 'var(--score-llm, #14b8a6)' }} />
            <div title={`rec ${(norm!.rec * 100).toFixed(0)}%`}     style={{ width: `${norm!.rec * 100}%`,   backgroundColor: 'var(--score-rec, #a855f7)' }} />
            <div title={`graph ${(norm!.graph * 100).toFixed(0)}%`} style={{ width: `${norm!.graph * 100}%`, backgroundColor: 'var(--score-graph, #f59e0b)' }} />
          </>
        )}
      </div>
      {showBadges && (
        <span className="font-mono text-[10px] text-meta tracking-tight tabular-nums whitespace-nowrap">
          {noSignal ? 'no signal' : 'emb·llm·rec·g'}
        </span>
      )}
    </div>
  );
}
