import {
  DndContext,
  PointerSensor,
  KeyboardSensor,
  useSensor,
  useSensors,
  closestCenter,
  type DragEndEvent,
} from '@dnd-kit/core';
import {
  SortableContext,
  verticalListSortingStrategy,
  sortableKeyboardCoordinates,
} from '@dnd-kit/sortable';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { fetchSources, reorderSources } from '@/lib/api';
import type { SourceConfig } from '@/types';
import { SourceSection } from './SourceSection';

export function SourcesList() {
  const qc = useQueryClient();
  const { data: sources = [], isLoading } = useQuery({
    queryKey: ['sources'],
    queryFn: fetchSources,
  });

  const sensors = useSensors(
    useSensor(PointerSensor, { activationConstraint: { distance: 8 } }),
    useSensor(KeyboardSensor, { coordinateGetter: sortableKeyboardCoordinates }),
  );

  const reorder = useMutation({
    mutationFn: reorderSources,
    onMutate: async (newTypes: string[]) => {
      await qc.cancelQueries({ queryKey: ['sources'] });
      const previous = qc.getQueryData<SourceConfig[]>(['sources']);
      if (previous) {
        const byType = new Map(previous.map((s) => [s.source_type, s]));
        const reordered = newTypes
          .map((t) => byType.get(t))
          .filter((s): s is SourceConfig => Boolean(s));
        qc.setQueryData(['sources'], reordered);
      }
      return { previous };
    },
    onError: (_err, _v, ctx) => {
      if (ctx?.previous) qc.setQueryData(['sources'], ctx.previous);
    },
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: ['sources'] });
    },
  });

  const handleDragEnd = (event: DragEndEvent) => {
    const { active, over } = event;
    if (!over || active.id === over.id) return;
    const oldIdx = sources.findIndex((s) => s.source_type === active.id);
    const newIdx = sources.findIndex((s) => s.source_type === over.id);
    if (oldIdx < 0 || newIdx < 0) return;
    const next = [...sources];
    const [moved] = next.splice(oldIdx, 1);
    next.splice(newIdx, 0, moved);
    reorder.mutate(next.map((s) => s.source_type));
  };

  if (isLoading) return <p className="text-sm text-muted-foreground">Loading sources…</p>;

  return (
    <DndContext
      sensors={sensors}
      collisionDetection={closestCenter}
      onDragEnd={handleDragEnd}
    >
      <SortableContext
        items={sources.map((s) => s.source_type)}
        strategy={verticalListSortingStrategy}
      >
        <div className="space-y-3">
          {sources.map((source, idx) => (
            <SourceSection key={source.source_type} source={source} displayIdx={idx + 1} />
          ))}
        </div>
      </SortableContext>
    </DndContext>
  );
}
