// TODO: Re-enable in MyDayPage once the daily-rollup job ships (spec: docs/plans/daily-rollup-job-spec.md).
// The component is intentionally excluded from MyDayPage until the backend produces rollup data.
import { MarkerCaption as SectionHeader } from '@/components/typography/MarkerCaption';

export function YesterdaySection() {
  return (
    <section className="space-y-3">
      <SectionHeader marker="Yesterday" meta="placeholder" />
      <p className="text-sm text-soft italic">
        Yesterday's carryover summary lands here once the daily-rollup job ships.
      </p>
    </section>
  );
}
