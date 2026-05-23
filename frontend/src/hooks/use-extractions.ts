import { useQuery } from '@tanstack/react-query';
import { fetchExtractionTemplates, fetchExtractionTable } from '@/lib/api';
import { QUERY_KEYS } from '@/lib/query-keys';

/**
 * Fetch all extraction templates.
 * Wraps the `['extraction-templates']` query key from the central registry.
 */
export function useExtractionTemplates() {
  return useQuery({
    queryKey: QUERY_KEYS.extraction.templates(),
    queryFn: fetchExtractionTemplates,
  });
}

/**
 * Fetch the extraction results table for a given template and paper selection.
 * Wraps the `['extraction-table', templateId, paperIds]` query key from the central registry.
 */
export function useExtractionTable(templateId: number | null, paperIds: number[]) {
  return useQuery({
    queryKey: QUERY_KEYS.extraction.table(templateId, paperIds),
    queryFn: () =>
      templateId != null
        ? fetchExtractionTable(templateId, paperIds)
        : Promise.resolve([]),
    enabled: templateId != null && paperIds.length > 0,
  });
}
