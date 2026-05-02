import {
  Tooltip,
  TooltipTrigger,
  TooltipContent,
  TooltipProvider,
} from '@/components/ui/tooltip';
import { SectionHeader } from './SectionHeader';

export function EndOfDaySection() {
  return (
    <section id="eod">
      <SectionHeader marker="End of day" />
      <TooltipProvider>
        <Tooltip>
          <TooltipTrigger asChild>
            <span tabIndex={0}>
              <button
                type="button"
                className="text-[12px] font-mono text-faint hover:text-zinc-900 dark:hover:text-zinc-100 cursor-not-allowed"
                disabled
              >
                + daily journal
              </button>
            </span>
          </TooltipTrigger>
          <TooltipContent>
            Coming in Phase 2 — daily reflection prompts persist to{' '}
            <code className="font-mono text-[11px]">journal_entries</code>.
          </TooltipContent>
        </Tooltip>
      </TooltipProvider>
    </section>
  );
}
