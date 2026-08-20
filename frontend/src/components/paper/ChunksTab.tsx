import { useState } from 'react';
import type { Chunk } from '@/types';
import { EmptyState } from '@/components/EmptyState';
import { FileStack, ChevronDown, ChevronRight } from 'lucide-react';

interface ChunksTabProps {
  chunks: Chunk[];
}

export function passageAnchorId(chunkId: number): string {
  return `source-passage-${chunkId}`;
}

function ChunkItem({ chunk, total }: { chunk: Chunk; total: number }) {
  const [open, setOpen] = useState(false);

  return (
    <div
      id={passageAnchorId(chunk.id)}
      data-chunk-index={chunk.chunk_index}
      className="scroll-mt-4 rounded-md border"
    >
      <button
        type="button"
        className="flex w-full items-center gap-2 px-4 py-3 text-left text-sm font-medium hover:bg-muted/50"
        onClick={() => setOpen(!open)}
        aria-expanded={open}
      >
        {open ? (
          <ChevronDown className="h-4 w-4 shrink-0" />
        ) : (
          <ChevronRight className="h-4 w-4 shrink-0" />
        )}
        {/* chunk_index is 0-based in storage; readers count from one. */}
        Passage {chunk.chunk_index + 1} of {total} (Page {chunk.page_number ?? '?'})
      </button>
      {open && (
        <div className="border-t px-4 py-3">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">
            {chunk.content || 'Empty passage'}
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
        title="No passages extracted yet"
        description="Analyze the paper first to extract text passages."
      />
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">{chunks.length} passages from the PDF</p>
      <div className="space-y-2">
        {chunks.map((chunk) => (
          <ChunkItem key={chunk.id} chunk={chunk} total={chunks.length} />
        ))}
      </div>
    </div>
  );
}
