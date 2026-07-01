export type FitStatus = 'fits' | 'partial' | 'unfit' | 'cloud' | 'unknown';

/** Plain-language label + colour for each fit status (single source of truth). */
const FIT_BADGE: Record<FitStatus, { label: string; colorClass: string }> = {
  fits: { label: 'Fits', colorClass: 'bg-green-100 text-green-800 dark:bg-green-900 dark:text-green-200' },
  partial: {
    label: 'Runs, but slower',
    colorClass: 'bg-yellow-100 text-yellow-800 dark:bg-yellow-900 dark:text-yellow-200',
  },
  unfit: { label: "Won't fit", colorClass: 'bg-red-100 text-red-800 dark:bg-red-900 dark:text-red-200' },
  cloud: { label: 'Cloud', colorClass: 'bg-muted text-muted-foreground' },
  unknown: { label: 'GPU not detected', colorClass: 'bg-muted text-muted-foreground' },
};

interface FitBadgeProps {
  status: FitStatus;
  /** Available VRAM — used only for the GB/GB detail tooltip, not the pill copy. */
  requiredVramGb?: number | null;
  availableVramGb?: number;
  largestFitting?: number;
}

export function FitBadge({ status, requiredVramGb, availableVramGb, largestFitting }: FitBadgeProps) {
  const { label, colorClass } = FIT_BADGE[status];
  // 'unfit' is actionable — append the largest context length that fits.
  const copy =
    status === 'unfit' && largestFitting !== undefined
      ? `${label} · try ${largestFitting.toLocaleString()} tokens`
      : label;
  // The raw GB / GB ratio is kept off the pill but available on hover.
  const detail =
    requiredVramGb != null && availableVramGb !== undefined
      ? `${requiredVramGb.toFixed(1)} GB / ${availableVramGb.toFixed(1)} GB`
      : undefined;

  return (
    <span
      className={`inline-flex items-center rounded-full px-2 py-0.5 text-xs font-medium ${colorClass}`}
      data-testid={`fit-badge-${status}`}
      title={detail}
    >
      {copy}
    </span>
  );
}
