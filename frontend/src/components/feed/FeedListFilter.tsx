import { useRef, useId } from 'react';
import { Search, X } from 'lucide-react';
import { cn } from '@/lib/utils';

/**
 * Scoped list-filter for the current faceted view.
 *
 * This is NOT intent-routing and is NOT the global ⌘K search.
 * It filters by title/author within the already-active facet selection.
 *
 * spec §3.4: "scoped list-filter only for the current faceted view
 * (title/author within active facets). It does NOT do intent-routing."
 */
interface FeedListFilterProps {
  value: string;
  onChange: (value: string) => void;
  /** Placeholder text. Defaults to 'Filter by title or author…' */
  placeholder?: string;
  className?: string;
}

export function FeedListFilter({
  value,
  onChange,
  placeholder = 'Filter by title or author…',
  className,
}: FeedListFilterProps) {
  const inputRef = useRef<HTMLInputElement>(null);
  const id = useId();

  function handleClear() {
    onChange('');
    inputRef.current?.focus();
  }

  return (
    <div className={cn('relative flex items-center', className)}>
      <label htmlFor={id} className="sr-only">
        {placeholder}
      </label>
      <Search
        size={15}
        className="pointer-events-none absolute left-3 text-muted-foreground/60"
        aria-hidden
      />
      <input
        id={id}
        ref={inputRef}
        type="search"
        value={value}
        onChange={(e) => onChange(e.target.value)}
        placeholder={placeholder}
        data-testid="feed-list-filter"
        className={cn(
          'h-8 w-full rounded-md border border-hair bg-muted/40 pl-9 pr-8 text-sm',
          'placeholder:text-muted-foreground/50',
          'focus:outline-none focus:ring-2 focus:ring-ring focus:ring-offset-1',
          'transition-colors',
          value && 'pr-8',
        )}
      />
      {value && (
        <button
          onClick={handleClear}
          type="button"
          aria-label="Clear filter"
          className="absolute right-2 rounded p-0.5 text-muted-foreground/60 hover:text-foreground focus:outline-none focus:ring-2 focus:ring-ring"
        >
          <X size={13} />
        </button>
      )}
    </div>
  );
}
