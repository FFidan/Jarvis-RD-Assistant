import { SectionHeader } from './SectionHeader';

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
