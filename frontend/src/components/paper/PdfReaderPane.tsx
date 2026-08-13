/**
 * PdfReaderPane — in-PDF reader with spatial highlight annotation.
 *
 * Renders a paper's PDF (via react-pdf-highlighter-extended), overlays the
 * persisted `paper_highlights`, and lets the user create / edit / delete
 * highlights (note + color). Geometry is bridged by `pdf-highlight-coords` so
 * the DB always holds normalized [0, 1] top-origin rects.
 *
 * The pdf.js worker is pinned to the bundled copy (the library otherwise
 * defaults to an unpkg CDN URL, which breaks offline / self-hosted installs).
 */
import { useEffect, useRef, useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  PdfLoader,
  PdfHighlighter,
  TextHighlight,
  MonitoredHighlightContainer,
  useHighlightContainerContext,
  usePdfHighlighterContext,
} from 'react-pdf-highlighter-extended';
import type {
  Highlight as LibHighlight,
  Tip,
} from 'react-pdf-highlighter-extended';
import workerUrl from 'pdfjs-dist/build/pdf.worker.min.mjs?url';
import { FileX, RefreshCw, Trash2 } from 'lucide-react';
import { toast } from 'sonner';

import { fetchPdfUrl } from '@/lib/api/papers';
import {
  listHighlights,
  createHighlight,
  updateHighlight,
  deleteHighlight,
} from '@/lib/api/highlights';
import { zoteroGetLinkage, zoteroPushHighlights } from '@/lib/api/zotero';
import type { Highlight, HighlightRect } from '@/types';
import { QUERY_KEYS } from '@/lib/query-keys';
import { useJobStore } from '@/stores/job-store';
import { errorMessage } from '@/lib/errors';
import { Skeleton } from '@/components/ui/skeleton';
import { Button } from '@/components/ui/button';
import { Textarea } from '@/components/ui/textarea';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { storedRectToScaledPosition, scaledPositionToStoredRect } from './pdf-highlight-coords';

// Preset highlight colors (inline swatches — no heavy color-picker dep, YAGNI).
const COLOR_PRESETS = [
  { name: 'Yellow', value: '#FBBF24' },
  { name: 'Green', value: '#34D399' },
  { name: 'Blue', value: '#60A5FA' },
  { name: 'Pink', value: '#F472B6' },
];
const DEFAULT_COLOR = '#FBBF24';

/** Library highlight extended with our note/color so renderers can read them. */
interface ReaderHighlight extends LibHighlight {
  note: string | null;
  color: string | null;
}

interface NewHighlightInput {
  page: number;
  rect: HighlightRect;
  quote: string | null;
  note: string | null;
  color: string | null;
}

// ── Color swatch picker ─────────────────────────────────────────────────────

function ColorSwatches({
  value,
  onChange,
}: {
  value: string;
  onChange: (color: string) => void;
}) {
  return (
    <div className="flex items-center gap-2" role="radiogroup" aria-label="Highlight color">
      {COLOR_PRESETS.map((preset) => (
        <button
          key={preset.value}
          type="button"
          role="radio"
          aria-checked={value === preset.value}
          aria-label={preset.name}
          onClick={() => onChange(preset.value)}
          className={`h-5 w-5 rounded-full ring-2 ring-offset-1 transition-transform hover:scale-110 ${
            value === preset.value ? 'ring-foreground' : 'ring-transparent'
          }`}
          style={{ backgroundColor: preset.value }}
        />
      ))}
    </div>
  );
}

// ── Selection tip (create flow) ─────────────────────────────────────────────
// Rendered by PdfHighlighter on a fresh text selection. We capture the selection
// once on mount (it is a snapshot, not the live DOM range) so later form input
// can't invalidate it.

function SelectionTip({ onCreate }: { onCreate: (input: NewHighlightInput) => void }) {
  const { getCurrentSelection, setTip } = usePdfHighlighterContext();
  const selectionRef = useRef(getCurrentSelection());
  const [note, setNote] = useState('');
  const [color, setColor] = useState(DEFAULT_COLOR);

  const selection = selectionRef.current;
  if (!selection) return null;

  const handleSave = () => {
    const { page, rect } = scaledPositionToStoredRect(selection.position);
    onCreate({
      page,
      rect,
      quote: selection.content.text ?? null,
      note: note.trim() || null,
      color,
    });
    setTip(null);
  };

  return (
    <div className="w-64 space-y-2 rounded-md border border-border bg-popover p-3 text-popover-foreground shadow-md">
      <Textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Add a note (optional)"
        rows={2}
        aria-label="Highlight note"
      />
      <div className="flex items-center justify-between gap-2">
        <ColorSwatches value={color} onChange={setColor} />
        <div className="flex gap-1">
          <Button variant="ghost" size="sm" className="h-7" onClick={() => setTip(null)}>
            Cancel
          </Button>
          <Button size="sm" className="h-7" onClick={handleSave}>
            Save
          </Button>
        </div>
      </div>
    </div>
  );
}

// ── Note hover popup (read-only) ────────────────────────────────────────────

function NotePopup({ note }: { note: string }) {
  return (
    <div className="max-w-xs rounded-md border border-border bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md">
      {note}
    </div>
  );
}

// ── Persisted highlight renderer ────────────────────────────────────────────

function HighlightRenderer({ onEdit }: { onEdit: (id: number) => void }) {
  const { highlight, isScrolledTo } = useHighlightContainerContext<ReaderHighlight>();

  const component = (
    <TextHighlight
      isScrolledTo={isScrolledTo}
      highlight={highlight}
      onClick={() => onEdit(Number(highlight.id))}
      style={{ background: highlight.color ?? DEFAULT_COLOR, mixBlendMode: 'multiply' }}
    />
  );

  const highlightTip: Tip | undefined = highlight.note
    ? { position: highlight.position, content: <NotePopup note={highlight.note} /> }
    : undefined;

  return (
    <MonitoredHighlightContainer highlightTip={highlightTip}>
      {component}
    </MonitoredHighlightContainer>
  );
}

// ── Inline editor (edit / delete an existing highlight) ─────────────────────

function HighlightEditor({
  highlight,
  pending,
  onSave,
  onDelete,
  onCancel,
}: {
  highlight: Highlight;
  pending: boolean;
  onSave: (note: string | null, color: string | null) => void;
  onDelete: () => void;
  onCancel: () => void;
}) {
  const [note, setNote] = useState(highlight.note ?? '');
  const [color, setColor] = useState(highlight.color ?? DEFAULT_COLOR);
  const [confirmOpen, setConfirmOpen] = useState(false);

  return (
    <div
      className="space-y-3 rounded-md border border-hair bg-muted/30 p-3"
      data-testid="highlight-editor"
    >
      {highlight.quote && (
        <blockquote className="border-l-2 border-hair pl-3 text-sm italic text-muted-foreground">
          {highlight.quote}
        </blockquote>
      )}
      <Textarea
        value={note}
        onChange={(e) => setNote(e.target.value)}
        placeholder="Add a note (optional)"
        rows={3}
        aria-label="Edit highlight note"
      />
      <div className="flex flex-wrap items-center justify-between gap-3">
        <ColorSwatches value={color} onChange={setColor} />
        <div className="flex gap-2">
          <Button
            variant="ghost"
            size="sm"
            className="h-8 text-destructive hover:text-destructive"
            onClick={() => setConfirmOpen(true)}
            disabled={pending}
          >
            <Trash2 className="mr-1 h-3 w-3" /> Delete
          </Button>
          <Button variant="outline" size="sm" className="h-8" onClick={onCancel} disabled={pending}>
            Cancel
          </Button>
          <Button
            size="sm"
            className="h-8"
            onClick={() => onSave(note.trim() || null, color)}
            disabled={pending}
          >
            {pending ? 'Saving…' : 'Save'}
          </Button>
        </div>
      </div>
      <ConfirmDialog
        open={confirmOpen}
        title="Delete this highlight?"
        description="The highlight and its note will be permanently removed."
        confirmLabel="Delete"
        onConfirm={() => {
          setConfirmOpen(false);
          onDelete();
        }}
        onCancel={() => setConfirmOpen(false)}
      />
    </div>
  );
}

// ── Degraded panel ──────────────────────────────────────────────────────────

function DegradedPanel({ message }: { message: string }) {
  return (
    <div
      className="flex flex-col items-center justify-center gap-2 rounded-md border border-hair bg-muted/30 px-4 py-10 text-center text-sm text-muted-foreground"
      data-testid="pdf-reader-degraded"
    >
      <FileX className="h-8 w-8 text-muted-foreground/60" />
      <p className="font-medium text-foreground">The PDF could not be loaded</p>
      <p className="max-w-md">{message}</p>
    </div>
  );
}

// ── Main pane ────────────────────────────────────────────────────────────────

interface PdfReaderPaneProps {
  paperId: number;
}

export function PdfReaderPane({ paperId }: PdfReaderPaneProps) {
  const queryClient = useQueryClient();
  const [pdfUrl, setPdfUrl] = useState<string | null>(null);
  const [pdfError, setPdfError] = useState<string | null>(null);
  const [editingId, setEditingId] = useState<number | null>(null);

  // Fetch the PDF as an authenticated blob URL; revoke on unmount (leak guard).
  useEffect(() => {
    let cancelled = false;
    let objectUrl: string | null = null;

    setPdfUrl(null);
    setPdfError(null);

    fetchPdfUrl(paperId)
      .then((url) => {
        if (cancelled) {
          URL.revokeObjectURL(url);
          return;
        }
        objectUrl = url;
        setPdfUrl(url);
      })
      .catch((err) => {
        if (!cancelled) setPdfError(errorMessage(err, 'PDF unavailable'));
      });

    return () => {
      cancelled = true;
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [paperId]);

  const highlightsQuery = useQuery({
    queryKey: QUERY_KEYS.highlights.list(paperId),
    queryFn: () => listHighlights(paperId),
  });
  const highlights = highlightsQuery.data ?? [];

  const invalidate = () =>
    queryClient.invalidateQueries({ queryKey: QUERY_KEYS.highlights.list(paperId) });

  const createMut = useMutation({
    mutationFn: (input: NewHighlightInput) =>
      createHighlight(paperId, {
        page: input.page,
        rect: input.rect,
        note: input.note,
        color: input.color,
        quote: input.quote,
      }),
    onSuccess: () => {
      invalidate();
      toast.success('Highlight added');
    },
    onError: (err) =>
      toast.error('Failed to add highlight', { description: errorMessage(err) }),
  });

  const updateMut = useMutation({
    mutationFn: (vars: { id: number; note: string | null; color: string | null }) =>
      updateHighlight(vars.id, { note: vars.note, color: vars.color }),
    onSuccess: () => {
      invalidate();
      setEditingId(null);
      toast.success('Highlight updated');
    },
    onError: (err) =>
      toast.error('Failed to update highlight', { description: errorMessage(err) }),
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteHighlight(id),
    onSuccess: () => {
      invalidate();
      setEditingId(null);
      toast.success('Highlight deleted');
    },
    onError: (err) =>
      toast.error('Failed to delete highlight', { description: errorMessage(err) }),
  });

  // Export-to-Zotero affordance: gated on the paper being linked to a Zotero
  // item (mirrors the backend `not_linked` short-circuit), enqueues a per-paper
  // batch job that pushes unsynced highlights as openable Zotero annotations.
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const exportRunning = useJobStore((s) =>
    s.isRunning('zotero.push_highlights', { paper_id: paperId }),
  );

  const linkageQuery = useQuery({
    queryKey: QUERY_KEYS.zotero.linkage(paperId),
    queryFn: () => zoteroGetLinkage(paperId),
  });
  const isLinked = !!linkageQuery.data?.zotero_item_key;

  const pushHighlightsMut = useMutation({
    mutationFn: () => zoteroPushHighlights(paperId),
    onSuccess: (data) =>
      trackExternalJob({
        jobId: data.job_id,
        kind: 'zotero.push_highlights',
        payload: { paper_id: paperId },
        status: 'queued',
      }),
    onError: (err) =>
      toast.error('Failed to export highlights to Zotero', { description: errorMessage(err) }),
  });

  const exporting = pushHighlightsMut.isPending || exportRunning;

  // Project stored highlights into the library's model. The coordinate basis is
  // arbitrary (the library re-normalizes), so we use a unit basis.
  const libHighlights: ReaderHighlight[] = highlights.map((h) => ({
    id: String(h.id),
    type: 'text',
    content: { text: h.quote ?? undefined },
    position: storedRectToScaledPosition(h.rect, h.page, 1, 1),
    note: h.note,
    color: h.color,
  }));

  const editingHighlight =
    editingId != null ? highlights.find((h) => h.id === editingId) ?? null : null;

  if (pdfError) {
    return <DegradedPanel message={pdfError} />;
  }

  if (!pdfUrl) {
    return <Skeleton className="h-[70vh] w-full" data-testid="pdf-reader-loading" />;
  }

  return (
    <div className="space-y-4">
      {highlightsQuery.isError && (
        <p className="text-sm text-destructive" role="alert">
          Couldn’t load saved highlights: {errorMessage(highlightsQuery.error)}
        </p>
      )}

      <div className="flex flex-wrap items-center justify-between gap-2">
        <p className="text-xs text-muted-foreground">
          Select text in the PDF to add a highlight. Click a highlight to edit or delete it.
        </p>
        <Button
          variant="outline"
          size="sm"
          className="h-8 shrink-0"
          disabled={!isLinked || exporting}
          onClick={() => pushHighlightsMut.mutate()}
          title={
            isLinked
              ? undefined
              : 'Send this paper to Zotero first to export its highlights.'
          }
        >
          <RefreshCw className={`mr-2 h-4 w-4 ${exporting ? 'animate-spin' : ''}`} />
          {exporting ? 'Exporting…' : 'Sync highlights to Zotero'}
        </Button>
      </div>

      {editingHighlight && (
        <HighlightEditor
          key={editingHighlight.id}
          highlight={editingHighlight}
          pending={updateMut.isPending || deleteMut.isPending}
          onSave={(note, color) =>
            updateMut.mutate({ id: editingHighlight.id, note, color })
          }
          onDelete={() => deleteMut.mutate(editingHighlight.id)}
          onCancel={() => setEditingId(null)}
        />
      )}

      <div
        className="relative h-[70vh] overflow-hidden rounded-md border border-hair"
        data-testid="pdf-reader-surface"
      >
        <PdfLoader
          document={pdfUrl}
          workerSrc={workerUrl}
          beforeLoad={() => <Skeleton className="h-[70vh] w-full" />}
          errorMessage={(err) => <DegradedPanel message={err.message} />}
          onError={() => {}}
        >
          {(pdfDocument) => (
            <PdfHighlighter
              pdfDocument={pdfDocument}
              highlights={libHighlights}
              enableAreaSelection={() => false}
              selectionTip={<SelectionTip onCreate={(input) => createMut.mutate(input)} />}
              utilsRef={() => {}}
            >
              <HighlightRenderer onEdit={setEditingId} />
            </PdfHighlighter>
          )}
        </PdfLoader>
      </div>
    </div>
  );
}
