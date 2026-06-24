export const SOURCE_LABELS: Record<string, string> = {
  arxiv: 'arXiv',
  semantic_scholar: 'Semantic Scholar',
  openalex: 'OpenAlex',
  pubmed: 'PubMed',
  local: 'Uploaded PDF',
};

export function sourceLabel(type: string): string {
  return SOURCE_LABELS[type] ?? type;
}
