import type { ReactNode } from 'react';

export interface MarkerLabelProps {
  children: ReactNode;
  /** When `as="label"`, the htmlFor target. Ignored otherwise. */
  htmlFor?: string;
  /** Render element. Defaults to `span`. */
  as?: 'span' | 'label' | 'h3';
  /** Optional extra className appended to the base small-caps style. */
  className?: string;
}

const BASE_CLASS = 'font-mono text-[11px] uppercase tracking-widest text-meta';

/**
 * Inline small-caps marker label for individual fields and micro-blocks.
 *
 * Use this in place of ad-hoc `<h3 class="font-mono text-[11px] uppercase
 * tracking-widest text-meta">...</h3>` elements (see AppearanceSection,
 * AutomationSection). For section-level markers that head a group of
 * sibling sub-blocks, use `MarkerCaption` instead — it renders the
 * tracked-uppercase eyebrow micro-label.
 *
 * See the typography contract: `docs/typography-contract.md`.
 */
export function MarkerLabel({
  children,
  htmlFor,
  as = 'span',
  className,
}: MarkerLabelProps) {
  const finalClass = className ? `${BASE_CLASS} ${className}` : BASE_CLASS;
  if (as === 'label') {
    return (
      <label className={finalClass} htmlFor={htmlFor}>
        {children}
      </label>
    );
  }
  if (as === 'h3') {
    return <h3 className={finalClass}>{children}</h3>;
  }
  return <span className={finalClass}>{children}</span>;
}
