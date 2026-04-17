import { Input } from '@/components/ui/input';
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';

interface LibraryFiltersProps {
  filterText: string;
  onFilterTextChange: (v: string) => void;
  selectedStatuses: string[];
  onStatusChange: (v: string[]) => void;
  selectedSources: string[];
  onSourceChange: (v: string[]) => void;
  selectedTopics: string[];
  onTopicChange: (v: string[]) => void;
  topicOptions: string[];
  dateFrom: string;
  onDateFromChange: (v: string) => void;
  dateTo: string;
  onDateToChange: (v: string) => void;
  sortBy: string;
  onSortChange: (v: string) => void;
}

const STATUS_OPTIONS = ['new', 'reading', 'read', 'archived', 'starred'];
const SOURCE_OPTIONS = ['arxiv', 'semantic_scholar', 'local', 'openalex', 'pubmed'] as const;

/**
 * Multi-select implemented as a simple toggle list inside a Select.
 * Each click toggles the item in the selection array.
 */
function MultiSelect({
  label,
  options,
  selected,
  onChange,
}: {
  label: string;
  options: string[];
  selected: string[];
  onChange: (v: string[]) => void;
}) {
  const displayText = selected.length > 0 ? selected.join(', ') : label;

  function toggle(item: string) {
    if (selected.includes(item)) {
      onChange(selected.filter((s) => s !== item));
    } else {
      onChange([...selected, item]);
    }
  }

  return (
    <div className="relative">
      <Select
        value={selected.length > 0 ? selected[0] : undefined}
        onValueChange={(v) => toggle(v)}
      >
        <SelectTrigger className="w-full min-w-[120px]">
          <span className="truncate text-sm">{displayText}</span>
        </SelectTrigger>
        <SelectContent>
          {options.map((opt) => (
            <SelectItem key={opt} value={opt}>
              <span className="flex items-center gap-2">
                <span
                  className={`inline-block h-3 w-3 rounded-sm border ${
                    selected.includes(opt) ? 'border-primary bg-primary' : 'border-muted-foreground'
                  }`}
                />
                {opt}
              </span>
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}

export function LibraryFilters({
  filterText,
  onFilterTextChange,
  selectedStatuses,
  onStatusChange,
  selectedSources,
  onSourceChange,
  selectedTopics,
  onTopicChange,
  topicOptions,
  dateFrom,
  onDateFromChange,
  dateTo,
  onDateToChange,
  sortBy,
  onSortChange,
}: LibraryFiltersProps) {
  return (
    <div className="flex flex-col gap-2 lg:flex-row lg:items-center">
      <Input
        placeholder="Filter by title, abstract, or author..."
        value={filterText}
        onChange={(e) => onFilterTextChange(e.target.value)}
        className="lg:flex-1"
      />
      <div className="flex flex-wrap gap-2">
        <MultiSelect
          label="Status"
          options={STATUS_OPTIONS}
          selected={selectedStatuses}
          onChange={onStatusChange}
        />
        <MultiSelect
          label="Source"
          options={SOURCE_OPTIONS}
          selected={selectedSources}
          onChange={onSourceChange}
        />
        {topicOptions.length > 0 && (
          <MultiSelect
            label="Topic"
            options={topicOptions}
            selected={selectedTopics}
            onChange={onTopicChange}
          />
        )}
        <Input
          type="date"
          value={dateFrom}
          onChange={(e) => onDateFromChange(e.target.value)}
          className="w-[140px]"
          aria-label="Date from"
          placeholder="From"
        />
        <Input
          type="date"
          value={dateTo}
          onChange={(e) => onDateToChange(e.target.value)}
          className="w-[140px]"
          aria-label="Date to"
          placeholder="To"
        />
        <Select value={sortBy} onValueChange={onSortChange}>
          <SelectTrigger className="w-[150px]">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            <SelectItem value="discovered_at">Recent (newest first)</SelectItem>
            <SelectItem value="priority">Priority (highest first)</SelectItem>
            <SelectItem value="published_date">Published (newest first)</SelectItem>
            <SelectItem value="title">Title (A-Z)</SelectItem>
            <SelectItem value="citation_count">Most Cited (highest first)</SelectItem>
            <SelectItem value="recommendation">Recommended for you</SelectItem>
          </SelectContent>
        </Select>
      </div>
    </div>
  );
}
