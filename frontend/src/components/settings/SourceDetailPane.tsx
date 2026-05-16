/**
 * SourceDetailPane — full-pane view for a single source in §II Sources.
 *
 * Shows the source-level enable/disable toggle and API-key management
 * for the selected source, then the full source reorder list below.
 */
import {
  DndContext,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  closestCenter,
} from '@dnd-kit/core';
import {
  SortableContext,
  verticalListSortingStrategy,
  sortableKeyboardCoordinates,
} from '@dnd-kit/sortable';
import { useQuery } from '@tanstack/react-query';
import { fetchSources } from '@/lib/api';
import { SourceSection, SOURCE_DESCRIPTIONS } from './SourceSection';
import { SourcesList } from './SourcesList';
import type { SourceConfig } from '@/types';

interface SourceDetailPaneProps {
  sourceType: string;
}

export function SourceDetailPane({ sourceType }: SourceDetailPaneProps) {
  const { data: sources = [], isLoading } = useQuery<SourceConfig[]>({
    queryKey: ['sources'],
    queryFn: fetchSources,
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  if (isLoading) {
    return <p className="text-sm text-muted-foreground">Loading sources…</p>;
  }

  const source = sources.find((s) => s.source_type === sourceType);

  if (!source) {
    return (
      <div className="space-y-4">
        <p className="text-sm text-muted-foreground">
          Source not available in this deployment.
        </p>
        <SourcesList />
      </div>
    );
  }

  const description = SOURCE_DESCRIPTIONS[source.source_type];
  const displayIdx = sources.findIndex((s) => s.source_type === sourceType) + 1;

  return (
    <div className="space-y-4">
      {description && (
        <p className="text-sm text-muted-foreground">{description}</p>
      )}

      {/* Wrap in the DnD providers required by SourceSection's useSortable hook */}
      <DndContext
        sensors={sensors}
        collisionDetection={closestCenter}
        onDragEnd={() => {/* no-op in single-item view */}}
      >
        <SortableContext items={[source.source_type]} strategy={verticalListSortingStrategy}>
          <SourceSection source={source} displayIdx={displayIdx} />
        </SortableContext>
      </DndContext>

      {/* Reorder strip — show the full ordered list below */}
      <div className="mt-6">
        <p className="text-xs font-mono uppercase tracking-widest text-muted-foreground mb-3">
          Source ordering
        </p>
        <SourcesList />
      </div>
    </div>
  );
}
