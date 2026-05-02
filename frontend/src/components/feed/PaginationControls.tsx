import { ChevronLeft, ChevronRight } from 'lucide-react';
import { Button } from '@/components/ui/button';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

export const PAGE_SIZE_OPTIONS = [10, 20, 30, 50, 100] as const;
export type PageSize = (typeof PAGE_SIZE_OPTIONS)[number];

interface PaginationControlsProps {
  /** Current 0-based offset into the result set. */
  offset: number;
  /** Current page size (items per page). */
  limit: PageSize;
  /** Total number of items reported by the backend. */
  total: number;
  /** Called when the user navigates — receives the new offset and limit. */
  onChange: (offset: number, limit: PageSize) => void;
}

export function PaginationControls({ offset, limit, total, onChange }: PaginationControlsProps) {
  const currentPage = Math.floor(offset / limit) + 1;
  const totalPages = Math.max(1, Math.ceil(total / limit));
  const isFirstPage = offset === 0;
  const isLastPage = offset + limit >= total;

  const handlePrev = () => {
    if (!isFirstPage) onChange(Math.max(0, offset - limit), limit);
  };

  const handleNext = () => {
    if (!isLastPage) onChange(offset + limit, limit);
  };

  const handlePageSizeChange = (value: string) => {
    const newLimit = Number(value) as PageSize;
    // Changing page size always resets to the first page
    onChange(0, newLimit);
  };

  return (
    <div className="flex items-center gap-3 text-sm text-muted-foreground">
      <span>
        Page {currentPage} of {totalPages}
        {total > 0 && <span className="ml-1">({total} total)</span>}
      </span>

      <div className="flex items-center gap-1">
        <Button
          variant="outline"
          size="icon"
          className="h-7 w-7"
          onClick={handlePrev}
          disabled={isFirstPage}
          aria-label="Previous page"
        >
          <ChevronLeft className="h-4 w-4" />
        </Button>
        <Button
          variant="outline"
          size="icon"
          className="h-7 w-7"
          onClick={handleNext}
          disabled={isLastPage}
          aria-label="Next page"
        >
          <ChevronRight className="h-4 w-4" />
        </Button>
      </div>

      <div className="flex items-center gap-1.5">
        <span className="text-xs">Per page:</span>
        <Select value={String(limit)} onValueChange={handlePageSizeChange}>
          <SelectTrigger className="h-7 w-[70px] text-xs">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {PAGE_SIZE_OPTIONS.map((size) => (
              <SelectItem key={size} value={String(size)} className="text-xs">
                {size}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
