/**
 * SpacedRepetitionSection — thin wrapper around IngestionSection that scopes
 * the rendered groups to only the "Spaced Repetition" group (the `fsrs.*`
 * config keys).
 *
 * Used in Research → Spaced Repetition rail item. Without this wrapper the
 * bare <IngestionSection /> would render AI models + Spaced Repetition +
 * Preferences — a byte-identical duplicate of Models → LLM (Conflict-5).
 * The `filterGroups` prop (introduced in IngestionSection alongside this
 * wrapper) restricts output to the listed groups; the label string must match
 * GROUP_ORDER exactly.
 */
import { IngestionSection } from './IngestionSection';

export function SpacedRepetitionSection() {
  return <IngestionSection filterGroups={['Spaced Repetition']} />;
}
