interface GradientProgressBarProps {
  value: number;
  color: string;
  className?: string;
}

export function GradientProgressBar({ value, color, className = '' }: GradientProgressBarProps) {
  const clamped = Math.max(0, Math.min(100, value));
  return (
    <div className={`h-1 w-full rounded-full bg-zinc-100 dark:bg-zinc-700/60 ring-1 ring-inset ring-transparent dark:ring-zinc-600/30 overflow-hidden ${className}`}>
      <div
        className="h-full rounded-full"
        style={{
          width: `${clamped}%`,
          background: `linear-gradient(90deg, ${color} 0%, color-mix(in srgb, ${color} 60%, transparent) 100%)`,
        }}
      />
    </div>
  );
}
