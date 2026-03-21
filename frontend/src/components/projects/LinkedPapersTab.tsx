import { useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { FileText, Link2, Search, Unlink } from 'lucide-react';
import { fetchProjectPapers, linkPaper, unlinkPaper, searchLibrary } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Separator } from '@/components/ui/separator';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/EmptyState';

interface LinkedPapersTabProps {
  projectId: number;
}

export function LinkedPapersTab({ projectId }: LinkedPapersTabProps) {
  const queryClient = useQueryClient();
  const [searchQuery, setSearchQuery] = useState('');
  const [searchResults, setSearchResults] = useState<Array<{ id: number; title: string; published_date?: string | null }>>([]);
  const [searching, setSearching] = useState(false);

  const { data: papers = [], isLoading } = useQuery({
    queryKey: ['project-papers', projectId],
    queryFn: () => fetchProjectPapers(projectId),
  });

  const invalidate = () => {
    queryClient.invalidateQueries({ queryKey: ['project-papers', projectId] });
  };

  const linkMut = useMutation({
    mutationFn: (paperId: number) => linkPaper(projectId, paperId),
    onSuccess: () => {
      invalidate();
      setSearchResults([]);
      setSearchQuery('');
    },
  });

  const unlinkMut = useMutation({
    mutationFn: (paperId: number) => unlinkPaper(projectId, paperId),
    onSuccess: invalidate,
  });

  const handleSearch = async () => {
    if (!searchQuery.trim()) return;
    setSearching(true);
    try {
      const results = await searchLibrary(searchQuery.trim());
      // Filter out already-linked papers
      const linkedIds = new Set(papers.map((p) => p.id));
      setSearchResults(results.filter((r) => !linkedIds.has(r.id)));
    } catch {
      setSearchResults([]);
    } finally {
      setSearching(false);
    }
  };

  if (isLoading) {
    return (
      <div className="space-y-3">
        {[1, 2, 3].map((i) => <Skeleton key={i} className="h-12 w-full" />)}
      </div>
    );
  }

  return (
    <div className="space-y-4">
      <h3 className="text-sm font-medium text-muted-foreground">
        {papers.length} linked paper{papers.length !== 1 ? 's' : ''}
      </h3>

      {papers.length === 0 ? (
        <EmptyState
          title="No linked papers"
          description="Search your library below to link papers to this project."
          icon={FileText}
        />
      ) : (
        <div className="space-y-2">
          {papers.map((paper) => (
            <div
              key={paper.id}
              className="flex items-center gap-3 rounded-md border p-3"
            >
              <FileText className="h-4 w-4 text-muted-foreground shrink-0" />
              <div className="flex-1 min-w-0">
                <p className="text-sm font-medium truncate">{paper.title}</p>
                <p className="text-xs text-muted-foreground">
                  {paper.authors?.slice(0, 3).join(', ')}
                  {paper.published_date && ` - ${paper.published_date.slice(0, 10)}`}
                </p>
              </div>
              <Button
                variant="ghost"
                size="sm"
                onClick={() => unlinkMut.mutate(paper.id)}
                disabled={unlinkMut.isPending}
              >
                <Unlink className="mr-1 h-4 w-4" />
                Unlink
              </Button>
            </div>
          ))}
        </div>
      )}

      <Separator />

      <div>
        <h3 className="text-sm font-medium mb-2">Link a paper from your library</h3>
        <div className="flex gap-2">
          <div className="relative flex-1">
            <Search className="absolute left-2.5 top-2.5 h-4 w-4 text-muted-foreground" />
            <Input
              placeholder="Search papers..."
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && handleSearch()}
              className="pl-8"
            />
          </div>
          <Button onClick={handleSearch} disabled={searching || !searchQuery.trim()}>
            {searching ? 'Searching...' : 'Search'}
          </Button>
        </div>

        {searchResults.length > 0 && (
          <div className="mt-3 space-y-1">
            {searchResults.map((result) => (
              <div
                key={result.id}
                className="flex items-center gap-3 rounded-md border p-2"
              >
                <div className="flex-1 min-w-0">
                  <p className="text-sm truncate">{result.title}</p>
                </div>
                <Button
                  size="sm"
                  variant="outline"
                  onClick={() => linkMut.mutate(result.id)}
                  disabled={linkMut.isPending}
                >
                  <Link2 className="mr-1 h-4 w-4" />
                  Link
                </Button>
              </div>
            ))}
          </div>
        )}
      </div>
    </div>
  );
}
