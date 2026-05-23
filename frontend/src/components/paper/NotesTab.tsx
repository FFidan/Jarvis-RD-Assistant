import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { QUERY_KEYS } from '@/lib/query-keys';
import {
  fetchNotes,
  createNote,
  deleteNote,
  promoteZoteroNote,
  zoteroSyncAnnotations,
} from '@/lib/api';
import type { Note } from '@/types';
import { useJobStore } from '@/stores/job-store';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { CheckCircle, RefreshCw, ShieldCheck, StickyNote, Trash2, XCircle } from 'lucide-react';
import { formatDate } from '@/lib/utils';

interface NotesTabProps {
  paperId: number;
  /**
   * When true, disables the note creation form and delete/promote actions.
   * Offline note editing is an explicit NON-GOAL (Wave 3 offline contract).
   * Existing cached notes remain readable. Defaults to false.
   */
  readOnly?: boolean;
}

export function NotesTab({ paperId, readOnly = false }: NotesTabProps) {
  const queryClient = useQueryClient();
  const trackExternalJob = useJobStore((s) => s.trackExternalJob);
  const [noteText, setNoteText] = useState('');
  const [pageNumber, setPageNumber] = useState('');
  const [highlightText, setHighlightText] = useState('');

  const { data: notes = [], isLoading, isError } = useQuery({
    queryKey: QUERY_KEYS.notes.user(paperId),
    queryFn: () => fetchNotes(paperId, 'user'),
  });

  const {
    data: zoteroNotes = [],
    isLoading: zoteroLoading,
    isError: zoteroError,
  } = useQuery({
    queryKey: QUERY_KEYS.notes.zotero(paperId),
    queryFn: () => fetchNotes(paperId, 'zotero'),
  });

  const createMut = useMutation({
    mutationFn: () =>
      createNote(paperId, {
        user_note: noteText.trim(),
        highlight_text: highlightText.trim() || null,
        page_number: pageNumber ? Number(pageNumber) : null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.notes.user(paperId) });
      setNoteText('');
      setPageNumber('');
      setHighlightText('');
    },
  });

  const deleteMut = useMutation({
    mutationFn: (noteId: number) => deleteNote(noteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.notes.user(paperId) });
    },
  });

  const syncZoteroMut = useMutation({
    mutationFn: () => zoteroSyncAnnotations(paperId),
    onSuccess: (data) => {
      trackExternalJob({
        jobId: data.job_id,
        kind: 'zotero.sync_annotations',
        payload: { paper_id: paperId },
        status: 'queued',
      });
    },
  });

  const promoteZoteroMut = useMutation({
    mutationFn: (noteId: number) => promoteZoteroNote(noteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: QUERY_KEYS.notes.zotero(paperId) });
    },
  });

  const verificationLabel = (note: Note) => {
    if (note.verification_status === 'verified') {
      return {
        className: 'text-[var(--status-ok)]',
        icon: CheckCircle,
        text: note.verified_page_number
          ? `Verified evidence, page ${note.verified_page_number}`
          : 'Verified evidence',
      };
    }
    if (note.verification_status === 'failed') {
      return {
        className: 'text-destructive',
        icon: XCircle,
        text: 'Verification failed',
      };
    }
    return {
      className: 'text-muted-foreground',
      icon: ShieldCheck,
      text: 'Not promoted as evidence',
    };
  };

  return (
    <div className="space-y-6">
      <p className="text-sm text-muted-foreground">
        Page-anchored highlights and notes (separate from your Quick Rating in the sidebar).
      </p>
      {/* Create note form — hidden when read-only (offline NON-GOAL). */}
      {!readOnly && (
        <section className="space-y-3" data-testid="notes-create-form">
          <h3 className="text-lg font-semibold">Add a note</h3>
          <div className="space-y-2">
            <Label htmlFor="note-text">Note</Label>
            <Textarea
              id="note-text"
              value={noteText}
              onChange={(e) => setNoteText(e.target.value)}
              placeholder="Write your note..."
              rows={4}
            />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div className="space-y-1">
              <Label htmlFor="note-page">Page (optional)</Label>
              <Input
                id="note-page"
                type="number"
                min={1}
                value={pageNumber}
                onChange={(e) => setPageNumber(e.target.value)}
                placeholder="Page number"
              />
            </div>
            <div className="space-y-1">
              <Label htmlFor="note-highlight">Highlight text (optional)</Label>
              <Input
                id="note-highlight"
                value={highlightText}
                onChange={(e) => setHighlightText(e.target.value)}
                placeholder="Highlighted text"
              />
            </div>
          </div>
          <Button
            onClick={() => createMut.mutate()}
            disabled={!noteText.trim() || createMut.isPending}
          >
            {createMut.isPending ? 'Saving...' : 'Save note'}
          </Button>
          {createMut.isError && (
            <p className="text-sm text-destructive">
              {createMut.error instanceof Error ? createMut.error.message : 'Failed to save note'}
            </p>
          )}
        </section>
      )}

      {/* Existing notes */}
      <section className="space-y-3">
        <h3 className="text-lg font-semibold">Existing notes</h3>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading notes...</p>
        ) : isError ? (
          <p className="text-sm text-destructive">Failed to load notes.</p>
        ) : notes.length === 0 ? (
          <EmptyState
            icon={StickyNote}
            title="No notes yet"
            description="Add one above."
          />
        ) : (
          <div className="space-y-3">
            {notes.map((note: Note) => (
              <Card key={note.id} className="rounded-md border-hair shadow-none">
                <CardContent className="pt-4">
                  <p className="text-sm">{note.user_note}</p>
                  <div className="mt-2 flex items-center justify-between">
                    <p className="text-xs text-muted-foreground">
                      {[
                        note.page_number ? `Page ${note.page_number}` : null,
                        note.highlight_text ? `Highlight: "${note.highlight_text}"` : null,
                        formatDate(note.created_at),
                      ]
                        .filter(Boolean)
                        .join(' | ')}
                    </p>
                    {!readOnly && (
                      <Button
                        variant="ghost"
                        size="sm"
                        className="h-7 text-destructive hover:text-destructive"
                        onClick={() => deleteMut.mutate(note.id)}
                        disabled={deleteMut.isPending}
                      >
                        <Trash2 className="mr-1 h-3 w-3" />
                        Delete
                      </Button>
                    )}
                  </div>
                  {deleteMut.isError && deleteMut.variables === note.id && (
                    <p className="mt-1 text-xs text-destructive" role="alert">
                      {deleteMut.error instanceof Error
                        ? deleteMut.error.message
                        : 'Failed to delete note'}
                    </p>
                  )}
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </section>

      <section className="space-y-3">
        <div className="flex items-center justify-between gap-3">
          <h3 className="text-lg font-semibold">Zotero highlights</h3>
          <Button
            variant="outline"
            size="sm"
            onClick={() => syncZoteroMut.mutate()}
            disabled={syncZoteroMut.isPending || readOnly}
            title={readOnly ? 'Offline — sync unavailable' : undefined}
          >
            <RefreshCw className="mr-2 h-4 w-4" />
            {syncZoteroMut.isPending ? 'Syncing...' : 'Sync'}
          </Button>
        </div>
        {syncZoteroMut.isError && (
          <p className="text-sm text-destructive">
            {syncZoteroMut.error instanceof Error
              ? syncZoteroMut.error.message
              : 'Failed to sync Zotero highlights'}
          </p>
        )}
        {promoteZoteroMut.isError && (
          <p className="text-sm text-destructive">
            {promoteZoteroMut.error instanceof Error
              ? promoteZoteroMut.error.message
              : 'Failed to promote Zotero highlight'}
          </p>
        )}
        {zoteroLoading ? (
          <p className="text-sm text-muted-foreground">Loading highlights...</p>
        ) : zoteroError ? (
          <p className="text-sm text-destructive">Failed to load Zotero highlights.</p>
        ) : zoteroNotes.length === 0 ? (
          <EmptyState
            icon={StickyNote}
            title="No Zotero highlights"
            description="Sync annotations after highlighting this paper in Zotero."
          />
        ) : (
          <div className="space-y-3">
            {zoteroNotes.map((note: Note) => (
              <Card key={note.id} className="rounded-md border-hair shadow-none">
                <CardContent className="pt-4">
                  {note.highlight_text && (
                    <blockquote className="border-l-2 pl-3 text-sm text-muted-foreground">
                      {note.highlight_text}
                    </blockquote>
                  )}
                  <p className="mt-2 text-sm">{note.user_note}</p>
                  <div className="mt-2 flex flex-wrap items-center justify-between gap-2">
                    <div className="space-y-1">
                      <p className="text-xs text-muted-foreground">
                        {[
                          note.page_number ? `Page ${note.page_number}` : null,
                          formatDate(note.created_at),
                        ]
                          .filter(Boolean)
                          .join(' | ')}
                      </p>
                      {(() => {
                        const status = verificationLabel(note);
                        const StatusIcon = status.icon;
                        return (
                          <p className={`flex items-center gap-1 text-xs ${status.className}`}>
                            <StatusIcon className="h-3 w-3" />
                            {status.text}
                          </p>
                        );
                      })()}
                    </div>
                    {note.verification_status !== 'verified' && note.highlight_text && !readOnly && (
                      <Button
                        variant="outline"
                        size="sm"
                        onClick={() => promoteZoteroMut.mutate(note.id)}
                        disabled={promoteZoteroMut.isPending}
                      >
                        <ShieldCheck className="mr-2 h-4 w-4" />
                        Promote verified evidence
                      </Button>
                    )}
                  </div>
                </CardContent>
              </Card>
            ))}
          </div>
        )}
      </section>
    </div>
  );
}
