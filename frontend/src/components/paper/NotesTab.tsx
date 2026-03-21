import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { fetchNotes, createNote, deleteNote } from '@/lib/api';
import type { Note } from '@/types';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Card, CardContent } from '@/components/ui/card';
import { EmptyState } from '@/components/EmptyState';
import { StickyNote, Trash2 } from 'lucide-react';
import { formatDate } from '@/lib/utils';

interface NotesTabProps {
  paperId: number;
}

export function NotesTab({ paperId }: NotesTabProps) {
  const queryClient = useQueryClient();
  const [noteText, setNoteText] = useState('');
  const [pageNumber, setPageNumber] = useState('');
  const [highlightText, setHighlightText] = useState('');

  const { data: notes = [], isLoading } = useQuery({
    queryKey: ['notes', paperId],
    queryFn: () => fetchNotes(paperId),
  });

  const createMut = useMutation({
    mutationFn: () =>
      createNote(paperId, {
        user_note: noteText.trim(),
        highlight_text: highlightText.trim() || null,
        page_number: pageNumber ? Number(pageNumber) : null,
      }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notes', paperId] });
      setNoteText('');
      setPageNumber('');
      setHighlightText('');
    },
  });

  const deleteMut = useMutation({
    mutationFn: (noteId: number) => deleteNote(noteId),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['notes', paperId] });
    },
  });

  return (
    <div className="space-y-6">
      {/* Create note form */}
      <section className="space-y-3">
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

      {/* Existing notes */}
      <section className="space-y-3">
        <h3 className="text-lg font-semibold">Existing notes</h3>
        {isLoading ? (
          <p className="text-sm text-muted-foreground">Loading notes...</p>
        ) : notes.length === 0 ? (
          <EmptyState
            icon={StickyNote}
            title="No notes yet"
            description="Add one above."
          />
        ) : (
          <div className="space-y-3">
            {notes.map((note: Note) => (
              <Card key={note.id}>
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
