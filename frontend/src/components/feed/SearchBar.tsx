import { useState } from 'react';
import { Input } from '@/components/ui/input';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import { Search, Loader2 } from 'lucide-react';

interface SearchBarProps {
  onSearch: (query: string, source: string, maxResults: number) => void;
  isLoading: boolean;
}

export function SearchBar({ onSearch, isLoading }: SearchBarProps) {
  const [query, setQuery] = useState('');
  const [source, setSource] = useState('arxiv');
  const [maxResults, setMaxResults] = useState(10);

  function handleSubmit() {
    const trimmed = query.trim();
    if (!trimmed) return;
    onSearch(trimmed, source, maxResults);
  }

  return (
    <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
      <Input
        placeholder="Search arXiv or Semantic Scholar..."
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === 'Enter') handleSubmit();
        }}
        className="flex-1"
      />
      <div className="flex gap-2">
        <Select value={source} onValueChange={setSource}>
          <SelectTrigger className="w-[160px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="arxiv">arXiv</SelectItem>
            <SelectItem value="semantic_scholar">Semantic Scholar</SelectItem>
            <SelectItem value="both">Both</SelectItem>
          </SelectContent>
        </Select>
        <Input
          type="number"
          min={1}
          max={50}
          value={maxResults}
          onChange={(e) => setMaxResults(Number(e.target.value))}
          className="w-[80px]"
          aria-label="Max results"
        />
        <Button onClick={handleSubmit} disabled={isLoading || !query.trim()}>
          {isLoading ? (
            <Loader2 className="mr-2 h-4 w-4 animate-spin" />
          ) : (
            <Search className="mr-2 h-4 w-4" />
          )}
          Search
        </Button>
      </div>
    </div>
  );
}
