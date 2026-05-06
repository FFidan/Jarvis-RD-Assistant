type Props = { entryNum: number };

export function MyDayFooter({ entryNum }: Props) {
  return (
    <footer className="mt-12 pt-6 border-t border-hair text-meta font-mono text-[10px] tracking-[0.18em] uppercase flex flex-wrap gap-x-6 gap-y-1">
      <span>end of entry {entryNum}</span>
      <span>j k jump</span>
      <span>⌘. command</span>
      <span>⇧↩ seal day</span>
    </footer>
  );
}
