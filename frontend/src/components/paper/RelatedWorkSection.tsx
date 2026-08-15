/**
 * RelatedWorkSection — the paper's citation neighbourhood.
 *
 * Renders References (what this paper cites) and Cited by (who cites it) from
 * the same citation store the Citation Graph page uses. Rows for papers known
 * only from bibliographies link to their detail page too — it carries the
 * metadata and the outbound link. The semantic-similarity list stays separate:
 * it relates papers that do NOT cite each other, which citations cannot do.
 */

import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { BookMarked, RefreshCw } from 'lucide-react';
import { Badge } from '@/components/ui/badge';
import { Button } from '@/components/ui/button';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getCitationGraph, fetchCitationsFromS2 } from '@/lib/api';
import { errorMessage } from '@/lib/errors';
import { toast } from 'sonner';

const PREVIEW_COUNT = 10;
const GRAPH_DEPTH = 1;

interface RelatedWorkSectionProps {
  paperId: number;
}

interface CitationRowData {
  id: number;
  title: string;
  citationCount: number;
  year: string | null;
  isStub: boolean;
  isInfluential: boolean;
}

interface StoredBibliographyEntry {
  rawText: string;
  title: string | null;
  authors: string[];
  year: number | null;
  venue: string | null;
}

function unresolvedBibliographyEntries(metadata: Record<string, unknown>): StoredBibliographyEntry[] {
  const bibliography = metadata.bibliography;
  if (!Array.isArray(bibliography)) return [];
  return bibliography.flatMap((value) => {
    if (!value || typeof value !== 'object' || Array.isArray(value)) return [];
    const entry = value as Record<string, unknown>;
    if (entry.resolved !== false || typeof entry.raw_text !== 'string') return [];
    return [{
      rawText: entry.raw_text,
      title: typeof entry.title === 'string' ? entry.title : null,
      authors: Array.isArray(entry.authors)
        ? entry.authors.filter((author): author is string => typeof author === 'string')
        : [],
      year: typeof entry.year === 'number' ? entry.year : null,
      venue: typeof entry.venue === 'string' ? entry.venue : null,
    }];
  });
}

function CitationList({
  label,
  rows,
  testId,
}: {
  label: string;
  rows: CitationRowData[];
  /** Scopes each list so a test can bind a row to the direction it belongs to. */
  testId: string;
}) {
  const [expanded, setExpanded] = useState(false);
  if (rows.length === 0) return null;
  const shown = expanded ? rows : rows.slice(0, PREVIEW_COUNT);

  return (
    <div className="space-y-2" data-testid={testId}>
      <h4 className="text-sm font-semibold">
        {label} <span className="font-normal text-muted-foreground">({rows.length})</span>
      </h4>
      <ul className="space-y-1.5">
        {shown.map((row) => (
          <li key={row.id} className="flex items-baseline gap-2 text-sm">
            <Link
              to={`/paper/${row.id}`}
              className="min-w-0 truncate text-left hover:underline"
              title={row.title}
            >
              {row.title}
            </Link>
            {row.year && <span className="shrink-0 text-xs text-muted-foreground">{row.year}</span>}
            {row.citationCount > 0 && (
              <span className="shrink-0 text-xs text-muted-foreground" title="Citation count">
                {row.citationCount.toLocaleString()} cit.
              </span>
            )}
            {row.isInfluential && (
              <Badge variant="outline" className="shrink-0 px-1.5 py-0 text-[10px]">
                influential
              </Badge>
            )}
            {row.isStub && (
              <span className="shrink-0 text-[10px] text-muted-foreground/70">
                not in your library
              </span>
            )}
          </li>
        ))}
      </ul>
      {rows.length > PREVIEW_COUNT && (
        <button
          type="button"
          onClick={() => setExpanded((v) => !v)}
          className="text-xs text-muted-foreground hover:text-foreground hover:underline"
        >
          {expanded ? 'Show fewer' : `Show all ${rows.length}`}
        </button>
      )}
    </div>
  );
}

function UnresolvedBibliographyList({ rows }: { rows: StoredBibliographyEntry[] }) {
  if (rows.length === 0) return null;
  return (
    <div className="space-y-2" data-testid="citation-unresolved-references">
      <h4 className="text-sm font-semibold">
        Unresolved bibliography entries{' '}
        <span className="font-normal text-muted-foreground">({rows.length})</span>
      </h4>
      <ul className="space-y-2">
        {rows.map((row, index) => (
          <li key={`${index}-${row.rawText}`} className="text-sm">
            <p>{row.rawText}</p>
            {row.title && (
              <p className="text-xs text-muted-foreground">
                {[row.title, row.authors.join(', '), row.year, row.venue]
                  .filter(Boolean)
                  .join(' · ')}
              </p>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function RelatedWorkSection({ paperId }: RelatedWorkSectionProps) {
  const queryClient = useQueryClient();
  const graphKey = QUERY_KEYS.citation.graph([paperId], GRAPH_DEPTH);
  const paperDetail = queryClient.getQueryData<{
    paper: { external_id: string; metadata: Record<string, unknown> };
  }>(QUERY_KEYS.papers.detail(paperId));
  const paperMetadata = paperDetail?.paper.metadata ?? {};
  const unresolvedReferences = unresolvedBibliographyEntries(paperMetadata);
  const citedByUnavailable = Boolean(
    paperDetail?.paper.external_id.startsWith('local:') && !paperMetadata.s2_id,
  );

  const { data: graph, isLoading } = useQuery({
    queryKey: graphKey,
    queryFn: () => getCitationGraph([paperId], GRAPH_DEPTH),
    staleTime: 5 * 60_000,
  });

  const fetchMut = useMutation({
    mutationFn: () => fetchCitationsFromS2(paperId),
    onSuccess: (res) => {
      toast.success(
        `Fetched ${res.references_added} references and ${res.citations_added} citing papers.`,
      );
      void queryClient.invalidateQueries({ queryKey: graphKey });
    },
    onError: (err: Error) =>
      toast.error('Citation lookup failed', { description: errorMessage(err) }),
  });

  const { references, citedBy } = useMemo(() => {
    if (!graph) return { references: [], citedBy: [] };
    const nodeById = new Map(graph.nodes.map((n) => [n.id, n]));
    const toRow = (nodeId: number, isInfluential: boolean): CitationRowData | null => {
      const node = nodeById.get(nodeId);
      if (!node) return null;
      return {
        id: node.id,
        title: node.title,
        citationCount: node.citation_count,
        year: node.published_date ? String(node.published_date).slice(0, 4) : null,
        isStub: node.is_stub,
        isInfluential,
      };
    };
    const references: CitationRowData[] = [];
    const citedBy: CitationRowData[] = [];
    for (const edge of graph.edges) {
      if (edge.source === paperId) {
        const row = toRow(edge.target, edge.is_influential ?? false);
        if (row) references.push(row);
      } else if (edge.target === paperId) {
        const row = toRow(edge.source, edge.is_influential ?? false);
        if (row) citedBy.push(row);
      }
    }
    const byWeight = (a: CitationRowData, b: CitationRowData) =>
      Number(b.isInfluential) - Number(a.isInfluential) || b.citationCount - a.citationCount;
    references.sort(byWeight);
    citedBy.sort(byWeight);
    return { references, citedBy };
  }, [graph, paperId]);

  const isEmpty = references.length === 0 && citedBy.length === 0
    && unresolvedReferences.length === 0 && !citedByUnavailable;

  return (
    <div className="space-y-5" data-testid="related-work">
      {isLoading ? (
        <p className="text-sm text-muted-foreground">Loading citations…</p>
      ) : isEmpty ? (
        <div className="flex flex-wrap items-center gap-3">
          <p className="text-sm text-muted-foreground">
            <BookMarked className="mr-1.5 inline h-4 w-4 align-text-bottom" aria-hidden="true" />
            No citation data yet for this paper.
          </p>
          <Button
            variant="outline"
            size="sm"
            onClick={() => fetchMut.mutate()}
            disabled={fetchMut.isPending}
          >
            <RefreshCw className="mr-1 h-3.5 w-3.5" />
            {fetchMut.isPending ? 'Fetching…' : 'Fetch citations'}
          </Button>
        </div>
      ) : (
        <>
          <CitationList label="References" rows={references} testId="citation-references" />
          <UnresolvedBibliographyList rows={unresolvedReferences} />
          {citedByUnavailable ? (
            <div className="space-y-1" data-testid="citation-cited-by-unavailable">
              <h4 className="text-sm font-semibold">Cited by</h4>
              <p className="text-sm text-muted-foreground">
                Citation-index data is unavailable because this document has not been identified
                in Semantic Scholar.
              </p>
            </div>
          ) : (
            <CitationList label="Cited by" rows={citedBy} testId="citation-cited-by" />
          )}
        </>
      )}
    </div>
  );
}
