import { useState } from 'react';
import { errorMessage } from '@/lib/errors';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import { getKnowledgeGraph, batchExtractEntities } from '@/lib/api';
import { useAuthStore } from '@/stores/auth-store';
import { CytoscapeGraph } from '@/components/graph/CytoscapeGraph';
import { GraphControls, type LayoutType } from '@/components/graph/GraphControls';
import { GraphStats } from '@/components/graph/GraphStats';
import { EntityTypeFilter } from '@/components/knowledge/EntityTypeFilter';
import { KGQueryInput } from '@/components/knowledge/KGQueryInput';
import { EntityBreakdown } from '@/components/knowledge/EntityBreakdown';
import { Skeleton } from '@/components/ui/skeleton';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import { Network, Sparkles, Loader2, X } from 'lucide-react';
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
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const isAdmin = useAuthStore((s) => s.user?.role === 'admin');

  const queryClient = useQueryClient();
  const filterType = entityType === 'All' ? undefined : entityType;

  const {
    data: graphData,
    isLoading,
    isError,
    error,
  } = useQuery({
    queryKey: QUERY_KEYS.knowledge.graph(filterType, minPaperCount),
    queryFn: () => getKnowledgeGraph(filterType, minPaperCount),
  });

  const extractMut = useMutation({
    mutationFn: batchExtractEntities,
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['knowledge-graph'] }); // Note: bare prefix for invalidation — no registry factory for all knowledge-graph entries
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

  const entityNameById = new Map(entities.map((e) => [e.id, e.name] as const));
  const selectedEntity =
    selectedNodeId != null
      ? (entities.find((e) => String(e.id) === selectedNodeId) ?? null)
      : null;
  const selectedRelationships = selectedEntity
    ? relationships.filter(
        (r) =>
          r.source_entity_id === selectedEntity.id || r.target_entity_id === selectedEntity.id,
      )
    : [];

  const stats = [
    { label: 'Total Entities', value: entities.length },
    { label: 'Total Relationships', value: relationships.length },
    { label: 'Entity Types', value: Object.keys(entityTypeCounts).length },
  ];

  return (
    <div className="space-y-6 p-6">
      <h1 className="text-[28px] leading-tight tracking-tight text-strong">Knowledge Graph</h1>
      <p className="text-muted-foreground text-sm">
        Explore entities and relationships extracted from your papers
      </p>

      <Card className="rounded-md border-hair shadow-none">
        <CardHeader>
          <CardTitle className="text-base">Filters</CardTitle>
        </CardHeader>
        <CardContent>
          <div className="flex flex-wrap items-end gap-4">
            <EntityTypeFilter value={entityType} onChange={setEntityType} />

            <div>
              <label className="mb-1 flex items-center gap-1 text-sm font-medium">
                Min Paper Count: {minPaperCount}
                <InfoTooltip content="Show only entities that appear in at least this many papers. Increase to filter out rarely-mentioned terms and reduce noise." />
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

            <Button
              variant="outline"
              size="sm"
              onClick={() => {
                setEntityType('All');
                setMinPaperCount(1);
                setSelectedNodeId(null);
              }}
            >
              Reset filters
            </Button>
          </div>
        </CardContent>
      </Card>

      <KGQueryInput />

      {isLoading && <Skeleton className="h-[500px] w-full rounded-md" />}

      {isError && (
        <p className="text-sm text-destructive">
          Failed to load knowledge graph: {errorMessage(error)}
        </p>
      )}

      {!isLoading && !isError && entities.length === 0 && (
        <div className="flex flex-col items-center justify-center py-12 text-center">
          <Network className="mb-4 h-12 w-12 text-muted-foreground/50" />
          <h3 className="text-lg font-medium">No entities extracted yet</h3>
          <p className="mt-1 max-w-md text-sm text-muted-foreground">
            Extract entities from your processed papers to build the knowledge graph — use the
            Batch Extract Entities button on this page.
          </p>
          {extractMut.isSuccess && (
            <p className="mt-2 text-sm text-[var(--status-ok)]">
              Extracted entities from {extractMut.data.extracted} papers
            </p>
          )}
          {extractMut.isError && (
            <p className="mt-2 text-sm text-destructive">
              Extraction failed: {errorMessage(extractMut.error)}
            </p>
          )}
          <div className="mt-4 flex gap-2">
            <Button asChild variant="outline" size="sm">
              <Link to="/feed?surface=library">Open Papers</Link>
            </Button>
            {isAdmin && (
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
            )}
          </div>
        </div>
      )}

      {entities.length > 0 && (
        <>
          <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
            <CytoscapeGraph
              nodes={nodes}
              edges={edges}
              layout={layout}
              colorMap={TYPE_COLORS}
              height={500}
              onNodeClick={setSelectedNodeId}
            />
            <Card className="rounded-md border-hair shadow-none" data-testid="kg-node-detail">
              <CardHeader className="flex flex-row items-start justify-between gap-2 space-y-0 pb-2">
                <CardTitle className="text-sm">Node Details</CardTitle>
                {selectedEntity && (
                  <Button
                    variant="ghost"
                    size="icon"
                    className="h-6 w-6"
                    aria-label="Clear selection"
                    onClick={() => setSelectedNodeId(null)}
                  >
                    <X className="h-4 w-4" />
                  </Button>
                )}
              </CardHeader>
              <CardContent>
                {!selectedEntity ? (
                  <p className="text-sm text-muted-foreground">
                    Click a node in the graph to see its details and relationships.
                  </p>
                ) : (
                  <div className="space-y-3">
                    <div className="space-y-1">
                      <div className="flex items-center gap-2">
                        <span className="font-medium text-strong">{selectedEntity.name}</span>
                        <Badge variant="secondary" className="text-xs">
                          {selectedEntity.entity_type}
                        </Badge>
                      </div>
                      {selectedEntity.canonical_name &&
                        selectedEntity.canonical_name !== selectedEntity.name && (
                          <p className="text-xs text-muted-foreground">
                            Canonical: {selectedEntity.canonical_name}
                          </p>
                        )}
                      <p className="text-xs text-muted-foreground">
                        Appears in {selectedEntity.paper_count}{' '}
                        {selectedEntity.paper_count === 1 ? 'paper' : 'papers'}
                      </p>
                    </div>

                    {selectedEntity.description && (
                      <p className="text-sm text-muted-foreground">{selectedEntity.description}</p>
                    )}

                    <div className="space-y-1">
                      <p className="text-xs font-medium text-muted-foreground">
                        Relationships ({selectedRelationships.length})
                      </p>
                      {selectedRelationships.length === 0 ? (
                        <p className="text-xs text-muted-foreground">No relationships recorded.</p>
                      ) : (
                        <ul className="space-y-1">
                          {selectedRelationships.map((r) => {
                            const otherId =
                              r.source_entity_id === selectedEntity.id
                                ? r.target_entity_id
                                : r.source_entity_id;
                            const otherName = entityNameById.get(otherId) ?? `#${otherId}`;
                            return (
                              <li key={r.id} className="text-xs">
                                <span className="text-muted-foreground">
                                  {r.relationship_type.replace(/_/g, ' ')}
                                </span>{' '}
                                <span className="text-strong">{otherName}</span>
                              </li>
                            );
                          })}
                        </ul>
                      )}
                    </div>
                  </div>
                )}
              </CardContent>
            </Card>
          </div>
          <GraphStats stats={stats} />
          <EntityBreakdown counts={entityTypeCounts} />
        </>
      )}
    </div>
  );
}
