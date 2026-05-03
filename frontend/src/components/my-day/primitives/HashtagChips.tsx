interface HashtagChipsProps {
  tags?: string[] | null;
}

export function HashtagChips({ tags }: HashtagChipsProps) {
  if (!tags?.length) return null;
  return (
    <div className="flex gap-2 flex-wrap">
      {tags.slice(0, 5).map((t) => (
        <span key={t} className="font-mono text-[10px] text-meta">
          #{t.replace(/^#/, '')}
        </span>
      ))}
    </div>
  );
}
