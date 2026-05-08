import { useState } from 'react';
import { errorMessage } from '@/lib/errors';
import { useQuery } from '@tanstack/react-query';
import { getCitationGraph } from '@/lib/api';
import { CytoscapeGraph } from '@/components/graph/CytoscapeGraph';
import { GraphControls, type LayoutType } from '@/components/graph/GraphControls';
import { GraphStats } from '@/components/graph/GraphStats';
import { CitationPaperSelector } from '@/components/citation/CitationPaperSelector';
import { FetchCitationsButton } from '@/components/citation/FetchCitationsButton';
import { EmptyState } from '@/components/EmptyState';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { GitFork } from 'lucide-react';
import type { PaperBrief } from '@/types';
import type { CytoscapeNode, CytoscapeEdge } from '@/components/graph/CytoscapeGraph';

export function CitationGraphPage() {
  const [selectedPapers, setSelectedPapers] = useState<PaperBrief[]>([]);
  const [depth, setDepth] = useState(1);
  const [layout, setLayout] = useState<LayoutType>('cose');

  const paperIds = selectedPapers.map((p) => p.id);

  const {
    data: graphData,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['citation-graph', paperIds, depth],
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

            <FetchCitationsButton />
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
          <CytoscapeGraph
            nodes={nodes}
            edges={edges}
            layout={layout}
            colorMap={citationColorMap}
            height={500}
          />
          <GraphStats stats={stats} />
        </>
      )}
    </div>
  );
}
