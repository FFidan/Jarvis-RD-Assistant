import type { DiscoveryResult } from '@/types';
import { Button } from '@/components/ui/button';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Badge } from '@/components/ui/badge';
import { Separator } from '@/components/ui/separator';
import { formatAuthors } from '@/lib/utils';
import { ExternalLink, X, Sparkles } from 'lucide-react';

interface DiscoveryResultsProps {
  results: DiscoveryResult[];
  onClear: () => void;
}

export function DiscoveryResults({ results, onClear }: DiscoveryResultsProps) {
  if (results.length === 0) return null;

  return (
    <Card className="border-primary/20">
      <CardHeader className="pb-3">
        <div className="flex items-center justify-between">
          <CardTitle className="flex items-center gap-2 text-base">
            <Sparkles className="h-4 w-4" />
            Discovered Papers ({results.length} results)
          </CardTitle>
          <Button variant="ghost" size="sm" onClick={onClear}>
            <X className="mr-1 h-3 w-3" />
            Clear
          </Button>
        </div>
      </CardHeader>
      <CardContent className="space-y-3">
        {results.map((dr, i) => (
          <div key={i}>
            <div className="flex items-start justify-between gap-4">
              <div className="min-w-0 flex-1">
                <p className="font-medium leading-tight">{dr.title || 'Untitled'}</p>
                <p className="text-sm text-muted-foreground">
                  {formatAuthors(dr.authors)}
                </p>
                {dr.matching_snippet && (
                  <p className="mt-1 line-clamp-2 text-sm text-muted-foreground">
                    {dr.matching_snippet}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 flex-col items-end gap-1">
                <Badge variant="secondary">
                  {(dr.similarity_score * 100).toFixed(0)}% similar
                </Badge>
                {dr.url && dr.url.startsWith('http') && (
                  <a
                    href={dr.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="inline-flex items-center gap-1 text-xs text-primary hover:underline"
                  >
                    Open <ExternalLink className="h-3 w-3" />
                  </a>
                )}
              </div>
            </div>
            {i < results.length - 1 && <Separator className="mt-3" />}
          </div>
        ))}
      </CardContent>
    </Card>
  );
}
