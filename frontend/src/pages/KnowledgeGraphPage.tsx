import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { getKnowledgeGraph, batchExtractEntities } from '@/lib/api';
import { CytoscapeGraph } from '@/components/graph/CytoscapeGraph';
import { GraphControls, type LayoutType } from '@/components/graph/GraphControls';
import { GraphStats } from '@/components/graph/GraphStats';
import { EntityTypeFilter } from '@/components/knowledge/EntityTypeFilter';
import { KGQueryInput } from '@/components/knowledge/KGQueryInput';
import { EntityBreakdown } from '@/components/knowledge/EntityBreakdown';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Network, Sparkles, Loader2 } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { Link } from 'react-router-dom';
import type { CytoscapeNode, CytoscapeEdge } from '@/components/graph/CytoscapeGraph';

const TYPE_COLORS: Record<string, string> = {
  method: '#1f77b4',
  dataset: '#2ca02c',
  metric: '#ff7f0e',
  concept: '#9467bd',
  institution: '#d62728',
  author: '#8c564b',
};

export function KnowledgeGraphPage() {
  const [entityType, setEntityType] = useState('All');
  const [minPaperCount, setMinPaperCount] = useState(1);
  const [layout, setLayout] = useState<LayoutType>('cose');

  const queryClient = useQueryClient();
  const filterType = entityType === 'All' ? undefined : entityType;

  const {
    data: graphData,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: ['knowledge-graph', filterType, minPaperCount],
    queryFn: () => getKnowledgeGraph(filterType, minPaperCount),
  });

  const extractMut = useMutation({
    mutationFn: batchExtractEntities,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-graph'] });
    },
  });

  const entities = graphData?.entities ?? [];
  const relationships = graphData?.relationships ?? [];
  const entityTypeCounts = graphData?.entity_type_counts ?? {};

  const nodes: CytoscapeNode[] = entities.map((e) => ({
    id: String(e.id),
    label: e.name,
    type: e.entity_type,
    size: e.display_size ?? 20,
    metadata: {
      description: e.description,
      paper_count: e.paper_count,
      canonical_name: e.canonical_name,
    },
  }));

  const edges: CytoscapeEdge[] = relationships.map((r) => ({
    source: String(r.source_entity_id),
    target: String(r.target_entity_id),
    label: r.relationship_type,
    directed: false,
  }));

  const stats = [
    { label: 'Total Entities', value: entities.length },
    { label: 'Total Relationships', value: relationships.length },
    { label: 'Entity Types', value: Object.keys(entityTypeCounts).length },
  ];

  return (
    <div className="space-y-6 p-6">
      <h1 className="text-2xl font-bold">Knowledge Graph</h1>
      <p className="text-muted-foreground text-sm">Explore entities and relationships extracted from your papers</p>

      <Card>
        <CardHeader>
          <CardTitle className="text-base">Filters</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-4">
            <EntityTypeFilter value={entityType} onChange={setEntityType} />

            <div>
              <label className="mb-1 block text-sm font-medium">
                Min Paper Count: {minPaperCount}
              </label>
              <input
                type="range"
                min={1}
                max={10}
                value={minPaperCount}
                onChange={(e) => setMinPaperCount(Number(e.target.value))}
                className="w-32"
              />
            </div>

            <GraphControls layout={layout} onLayoutChange={setLayout} />
          </div>
        </CardContent>
      </Card>

      <KGQueryInput />

      {isLoading && (
        <Skeleton className="h-[500px] w-full rounded-md" />
      )}

      {isError && (
        <p className="text-sm text-destructive">
          Failed to load knowledge graph: {(error as Error).message}
        </p>
      )}

      {!isLoading && !isError && entities.length === 0 && (
        <div className="flex flex-col items-center justify-center py-12 text-center">
          <Network className="mb-4 h-12 w-12 text-muted-foreground/50" />
          <h3 className="text-lg font-medium">No entities extracted yet</h3>
          <p className="mt-1 max-w-md text-sm text-muted-foreground">
            Extract entities from your processed papers to build the knowledge graph. Open a paper and click &apos;Extract Entities&apos;, or use batch extraction.
          </p>
          {extractMut.isSuccess && (
            <p className="mt-2 text-sm text-green-600">
              Extracted entities from {extractMut.data.extracted} papers
            </p>
          )}
          {extractMut.isError && (
            <p className="mt-2 text-sm text-destructive">
              Extraction failed: {(extractMut.error as Error).message}
            </p>
          )}
          <div className="mt-4 flex gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to="/feed">Go to Feed</Link>
            </Button>
            <Button
              variant="default"
              size="sm"
              disabled={extractMut.isPending}
              onClick={() => extractMut.mutate()}
            >
              {extractMut.isPending ? (
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
              ) : (
                <Sparkles className="mr-2 h-4 w-4" />
              )}
              Batch Extract Entities
            </Button>
          </div>
        </div>
      )}

      {entities.length > 0 && (
        <>
          <CytoscapeGraph
            nodes={nodes}
            edges={edges}
            layout={layout}
            colorMap={TYPE_COLORS}
            height={500}
          />
          <GraphStats stats={stats} />
          <EntityBreakdown counts={entityTypeCounts} />
        </>
      )}
    </div>
  );
}
