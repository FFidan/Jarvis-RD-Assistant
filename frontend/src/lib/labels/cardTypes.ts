export type CardType = 'concept' | 'quote' | 'method' | 'comparison';

export const CARD_TYPE_LABELS: Record<string, string> = {
  concept: 'Concept',
  quote: 'Quote',
  method: 'Method',
  comparison: 'Comparison',
};

export function cardTypeLabel(type: string): string {
  return CARD_TYPE_LABELS[type] ?? type;
}
