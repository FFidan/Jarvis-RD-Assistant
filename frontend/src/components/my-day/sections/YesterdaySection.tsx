import { SectionHeader } from './SectionHeader';

export function YesterdaySection() {
  return (
    <section id="yesterday">
      <SectionHeader marker="Yesterday" meta="— · — · —" />
      <p className="text-faint italic font-serif text-sm">
        Yesterday's recap will appear here once Phase 1b ships.
      </p>
    </section>
  );
}
