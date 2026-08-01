import { useCallback, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { errorMessage } from '@/lib/errors';
import { formatDate } from '@/lib/utils';
import { useQuery } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getCitationGraph } from '@/lib/api';
import { CytoscapeGraph } from '@/components/graph/CytoscapeGraph';
import { GraphControls, type LayoutType } from '@/components/graph/GraphControls';
import { GraphStats } from '@/components/graph/GraphStats';
import { CitationPaperSelector } from '@/components/citation/CitationPaperSelector';
import { FetchCitationsButton } from '@/components/citation/FetchCitationsButton';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { GitFork, X } from 'lucide-react';
import type { PaperBrief } from '@/types';
import type { CytoscapeNode, CytoscapeEdge } from '@/components/graph/CytoscapeGraph';
import type { GraphNode } from '@/types/jobs';

/** The subset of GraphNode fields the page stashes on each CytoscapeNode. */
type CitationNodeMetadata = Pick<GraphNode, 'citation_count' | 'published_date' | 'is_stub'>;

export function CitationGraphPage() {
  const navigate = useNavigate();
  const [selectedPapers, setSelectedPapers] = useState<PaperBrief[]>([]);
  const [depth, setDepth] = useState(1);
  const [layout, setLayout] = useState<LayoutType>('cose');
  const [stubPanelNode, setStubPanelNode] = useState<CytoscapeNode | null>(null);

  const paperIds = selectedPapers.map((p) => p.id);

  const {
    data: graphData,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: QUERY_KEYS.citation.graph(paperIds, depth),
    queryFn: () => getCitationGraph(paperIds, depth),
    enabled: paperIds.length > 0,
  });

  const nodes: CytoscapeNode[] = (graphData?.nodes ?? []).map((n) => ({
    id: String(n.id),
    label: n.title || 'Untitled',
    type: n.is_stub ? 'stub' : 'paper',
    size: n.display_size ?? 20,
    metadata: {
      citation_count: n.citation_count,
      published_date: n.published_date,
      is_stub: n.is_stub,
    },
  }));

  const edges: CytoscapeEdge[] = (graphData?.edges ?? []).map((e) => ({
    source: String(e.source),
    target: String(e.target),
    label: e.is_influential ? 'influential' : '',
    directed: true,
  }));

  const citationColorMap: Record<string, string> = {
    paper: '#1f77b4',
    stub: '#cccccc',
  };

  // Cytoscape's tap handler delivers node ids as strings; `nodes` above already
  // converts each GraphNode.id (int) to that same string form, so this lookup is
  // the one place the number/string boundary is crossed.
  const handleNodeClick = useCallback(
    (nodeId: string) => {
      const node = nodes.find((n) => n.id === nodeId);
      if (!node) return;
      if (node.type === 'stub') {
        setStubPanelNode(node);
        return;
      }
      setStubPanelNode(null);
      navigate(`/paper/${node.id}`);
    },
    [nodes, navigate],
  );

  const stats = graphData
    ? [
        { label: 'Nodes', value: graphData.nodes.length },
        { label: 'Edges', value: graphData.edges.length },
        {
          label: 'Stub Papers',
          value: graphData.nodes.filter((n) => n.is_stub).length,
        },
        {
          label: 'Full Papers',
          value: graphData.nodes.filter((n) => !n.is_stub).length,
        },
      ]
    : [];

  return (
    <div className="space-y-6 p-6">
      <h1 className="text-[28px] leading-tight tracking-tight text-strong">Citation Graph</h1>
      <p className="text-muted-foreground text-sm">Citation network across your paper library</p>

      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-base">Paper Selection</CardTitle>
        </CardHeader>
        <CardContent className="space-y-4">
          <CitationPaperSelector
            selectedPapers={selectedPapers}
            onSelectionChange={setSelectedPapers}
          />

          <div className="flex flex-wrap items-end gap-4">
            <div>
              <label className="mb-1 flex items-center gap-1 text-sm font-medium">
                Depth: {depth}
                <InfoTooltip content="How many citation hops to follow. 1 = direct citations only. 2 = also includes papers cited by those papers. Higher values fetch more nodes but take longer." />
              </label>
              <input
                type="range"
                min={1}
                max={2}
                value={depth}
                onChange={(e) => setDepth(Number(e.target.value))}
                className="w-32"
              />
            </div>

            <FetchCitationsButton paperIds={paperIds} />
            <GraphControls layout={layout} onLayoutChange={setLayout} />
          </div>
        </CardContent>
      </Card>

      {paperIds.length === 0 && (
        <EmptyState
          icon={GitFork}
          title="No citations loaded"
          description="Select papers above and click 'Fetch Citations' to build the citation network."
        />
      )}

      {isLoading && (
        <Skeleton className="h-[500px] w-full rounded-md" />
      )}

      {isError && (
        <p className="text-sm text-destructive">
          Failed to load citation graph: {errorMessage(error)}
        </p>
      )}

      {graphData && nodes.length === 0 && paperIds.length > 0 && (
        <EmptyState
          icon={GitFork}
          title="No citation data yet"
          description="Click 'Fetch Citations' above to retrieve citation relationships for the selected papers."
        />
      )}

      {nodes.length > 0 && (
        <>
          {/* Keyboard-reachable mirror of the graph's mouse-only tap activation. */}
          <ul className="sr-only" aria-label="Citation graph nodes">
            {nodes.map((n) => (
              <li key={n.id}>
                <button type="button" onClick={() => handleNodeClick(n.id)}>
                  Open {n.label}
                </button>
              </li>
            ))}
          </ul>
          <CytoscapeGraph
            nodes={nodes}
            edges={edges}
            layout={layout}
            colorMap={citationColorMap}
            height={500}
            onNodeClick={handleNodeClick}
          />
          <GraphStats stats={stats} />
          {stubPanelNode && (
            <Card className="rounded-md border-hair shadow-none" data-testid="citation-stub-panel">
              <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0 pb-2">
                <CardTitle className="text-sm">{stubPanelNode.label}</CardTitle>
                <Button
                  variant="ghost"
                  size="icon"
                  className="h-6 w-6"
                  aria-label="Close"
                  onClick={() => setStubPanelNode(null)}
                >
                  <X className="h-4 w-4" />
                </Button>
              </CardHeader>
              <CardContent className="space-y-1">
                {(() => {
                  const metadata = stubPanelNode.metadata as CitationNodeMetadata;
                  return (
                    <>
                      <p className="text-xs text-muted-foreground">
                        {metadata.citation_count} citations
                      </p>
                      <p className="text-xs text-muted-foreground">
                        {formatDate(metadata.published_date)}
                      </p>
                    </>
                  );
                })()}
              </CardContent>
            </Card>
          )}
        </>
      )}
    </div>
  );
}
