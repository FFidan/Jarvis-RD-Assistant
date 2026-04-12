import type { ReactNode } from 'react';
import { Card, CardContent, CardHeader, CardTitle, CardDescription } from '@/components/ui/card';

interface SetupStepProps {
  stepNumber: number;
  totalSteps: number;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
}

/**
 * Shared layout wrapper for first-run setup wizard steps.
 * Renders a progress indicator (e.g. "Step 2 of 6") plus a titled card.
 */
export function SetupStep({
  stepNumber,
  totalSteps,
  title,
  description,
  children,
  footer,
}: SetupStepProps) {
  const percent = Math.round((stepNumber / totalSteps) * 100);
  return (
    <div className="mx-auto w-full max-w-2xl space-y-6 py-10 px-4">
      <div className="space-y-2">
        <div className="flex items-center justify-between text-xs text-muted-foreground">
          <span>
            Step {stepNumber} of {totalSteps}
          </span>
          <span>{percent}%</span>
        </div>
        <div
          className="h-1.5 w-full overflow-hidden rounded-full bg-muted"
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={totalSteps}
          aria-valuenow={stepNumber}
        >
          <div
            className="h-full bg-primary transition-all"
            style={{ width: `${percent}%` }}
          />
        </div>
      </div>

      <Card>
        <CardHeader>
          <CardTitle className="text-2xl">{title}</CardTitle>
          {description && <CardDescription>{description}</CardDescription>}
        </CardHeader>
        <CardContent className="space-y-4">{children}</CardContent>
      </Card>

      {footer && <div className="flex items-center justify-between">{footer}</div>}
    </div>
  );
}
