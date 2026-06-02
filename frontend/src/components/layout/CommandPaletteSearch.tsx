import { Command } from 'cmdk';
import { Search } from 'lucide-react';
import { useNavigate } from 'react-router-dom';
import { Dialog, DialogContent, DialogDescription, DialogTitle } from '@/components/ui/dialog';
import { useCommandPalette } from '@/stores/command-palette-store';
import { useCommandPaletteController } from '@/hooks/useCommandPalette';
import type { SearchPreviewResult } from '@/types';

function resultKey(r: SearchPreviewResult) {
  return `${r.source_type}:${r.external_id}`;
}

function authorLine(authors: string[]) {
  if (authors.length === 0) return 'Unknown authors';
  if (authors.length <= 3) return authors.join(', ');
  return `${authors.slice(0, 3).join(', ')} +${authors.length - 3} more`;
}

export function CommandPaletteSearch() {
  // This component is always mounted in the TopBar on authed pages, so this
  // is where the app-wide ⌘K listener + debounced search live.
  useCommandPaletteController();

  const navigate = useNavigate();
  const isOpen = useCommandPalette((s) => s.isOpen);
  const query = useCommandPalette((s) => s.query);
  const results = useCommandPalette((s) => s.results);
  const loading = useCommandPalette((s) => s.loading);
  const errored = useCommandPalette((s) => s.errored);
  const open = useCommandPalette((s) => s.open);
  const close = useCommandPalette((s) => s.close);
  const setQuery = useCommandPalette((s) => s.setQuery);

  function handleSelect(result: SearchPreviewResult) {
    const paperId = result.library_match?.paper_id;
    close();
    if (paperId != null) {
      navigate(`/paper/${paperId}`);
    } else {
      // Not in the library yet — open the Discover/search surface so the user
      // can search external sources and add it (matches the in-app "Discover
      // papers" CTA; the search surface does not accept a prefilled query).
      navigate('/feed?surface=search');
    }
  }

  const trimmedQuery = query.trim();
  const hasQuery = trimmedQuery.length > 0;

  return (
    <>
      <button
        type="button"
        onClick={open}
        aria-label="Search papers, notes and cards — press Command K"
        className="relative h-9 w-full max-w-[440px] rounded-md border border-hair bg-card hover:bg-paper transition-colors flex items-center px-3 gap-2 text-left"
      >
        <Search className="h-3.5 w-3.5 text-faint shrink-0" />
        <span className="flex-1 text-[13px] text-faint select-none">
          Search papers, notes, cards…
        </span>
        <span className="flex items-center gap-0.5 shrink-0">
          <kbd className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-hair bg-paper text-meta">
            ⌘
          </kbd>
          <kbd className="font-mono text-[10px] px-1.5 py-0.5 rounded border border-hair bg-paper text-meta">
            K
          </kbd>
        </span>
      </button>

      <Dialog open={isOpen} onOpenChange={(next) => (next ? open() : close())}>
        <DialogContent className="max-w-xl p-0 gap-0 overflow-hidden">
          <DialogTitle className="sr-only">Search papers, notes and cards</DialogTitle>
          <DialogDescription className="sr-only">
            Search your library and discover new papers. Press Enter to open a paper or navigate to Discover.
          </DialogDescription>
          <Command
            shouldFilter={false}
            label="Search papers, notes and cards"
            className="flex flex-col"
          >
            <div className="flex items-center gap-2 border-b border-hair px-3">
              <Search className="h-4 w-4 text-faint shrink-0" />
              {/* Autofocus is the defining behaviour of a command palette:
                  ⌘K should land the cursor in the search box, not require a
                  second click. The dialog is modal and opened by explicit
                  user intent, so this does not steal focus unexpectedly. */}
              {/* eslint-disable-next-line jsx-a11y/no-autofocus */}
              <Command.Input autoFocus
                value={query}
                onValueChange={setQuery}
                placeholder="Search your papers by title or author…"
                className="flex-1 h-12 bg-transparent text-sm outline-none placeholder:text-faint"
              />
            </div>

            <Command.List className="max-h-[60vh] overflow-y-auto p-2">
              {!hasQuery && (
                <p className="px-3 py-6 text-center text-[13px] text-faint">
                  Start typing to search your papers.
                </p>
              )}

              {hasQuery && loading && (
                <p className="px-3 py-6 text-center text-[13px] text-faint">
                  Searching…
                </p>
              )}

              {hasQuery && !loading && errored && (
                <p className="px-3 py-6 text-center text-[13px] text-faint">
                  Couldn&apos;t search right now. Please try again in a moment.
                </p>
              )}

              {hasQuery && !loading && !errored && results.length === 0 && (
                <Command.Empty className="px-3 py-6 text-center text-[13px] text-faint">
                  No matches yet — keep typing.
                </Command.Empty>
              )}

              {hasQuery &&
                !loading &&
                !errored &&
                results.map((result) => {
                  const inLibrary = result.library_match?.paper_id != null;
                  return (
                    <Command.Item
                      key={resultKey(result)}
                      value={resultKey(result)}
                      onSelect={() => handleSelect(result)}
                      className="flex flex-col gap-0.5 rounded-md px-3 py-2 cursor-pointer data-[selected=true]:bg-paper"
                    >
                      <span className="text-[13px] font-medium text-ink line-clamp-1">
                        {result.title}
                      </span>
                      <span className="text-[11px] text-meta line-clamp-1">
                        {authorLine(result.authors)}
                        {!inLibrary && ' · Not in your library yet'}
                      </span>
                    </Command.Item>
                  );
                })}
            </Command.List>
          </Command>
        </DialogContent>
      </Dialog>
    </>
  );
}
