import { useState } from 'react';
import type { Chunk } from '@/types';
import { EmptyState } from '@/components/EmptyState';
import { FileStack, ChevronDown, ChevronRight } from 'lucide-react';

interface ChunksTabProps {
  chunks: Chunk[];
}

function ChunkItem({ chunk }: { chunk: Chunk }) {
  const [open, setOpen] = useState(false);

  return (
    <div className="rounded-md border">
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-medium hover:bg-muted/50"
        onClick={() => setOpen(!open)}
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0" />
        )}
        Chunk {chunk.chunk_index} (Page {chunk.page_number ?? '?'})
      </button>
      {open && (
        <div className="border-t px-4 py-3">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">
            {chunk.content || 'Empty chunk.'}
          </p>
        </div>
      )}
    </div>
  );
}

export function ChunksTab({ chunks }: ChunksTabProps) {
  if (chunks.length === 0) {
    return (
      <EmptyState
        icon={FileStack}
        title="No chunks available"
        description="Process the PDF first to extract text chunks."
      />
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{chunks.length} chunks extracted</p>
      <div className="space-y-2">
        {chunks.map((chunk) => (
          <ChunkItem key={chunk.id} chunk={chunk} />
        ))}
      </div>
    </div>
  );
}
