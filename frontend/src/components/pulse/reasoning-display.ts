export const LLM_SCORING_FAILED = 'LLM scoring failed';
const LLM_SCORING_UNAVAILABLE = 'AI scoring unavailable for this card';

// Mapped at render time so stored rows carrying the raw sentinel stay covered.
export function displayReasoning(reasoning: string | null | undefined): string | null {
  if (!reasoning) return null;
  if (reasoning === LLM_SCORING_FAILED) return LLM_SCORING_UNAVAILABLE;
  return reasoning;
}
