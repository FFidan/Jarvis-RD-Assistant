import { useEffect, useRef, useState } from 'react';
import { Quote } from 'lucide-react';
import { toast } from 'sonner';
import { Button, type ButtonProps } from '@/components/ui/button';
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from '@/components/ui/dropdown-menu';
import {
  copyBulkCitations,
  copyPaperCitation,
  downloadBulkCitations,
  downloadPaperCitation,
  type CitationFormat,
} from '@/lib/api';
import { errorMessage } from '@/lib/errors';

interface CitationMenuProps {
  paperIds: number[];
  size?: ButtonProps['size'];
  disabled?: boolean;
}

export function CitationMenu({ paperIds, size = 'sm', disabled }: CitationMenuProps) {
  const [copied, setCopied] = useState(false);
  const copyTimerRef = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(
    () => () => {
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
    },
    [],
  );

  const isEmpty = paperIds.length === 0;
  const isBulk = paperIds.length > 1;

  async function handleCopy(format: CitationFormat) {
    const [first] = paperIds;
    if (first === undefined) return;
    try {
      const text = isBulk
        ? await copyBulkCitations(paperIds, format)
        : await copyPaperCitation(first, format);
      await navigator.clipboard.writeText(text);
      setCopied(true);
      toast.success('Citation copied');
      if (copyTimerRef.current) clearTimeout(copyTimerRef.current);
      copyTimerRef.current = setTimeout(() => setCopied(false), 2000);
    } catch (err) {
      toast.error('Could not copy citation', { description: errorMessage(err) });
    }
  }

  async function handleDownload(format: CitationFormat) {
    const [first] = paperIds;
    if (first === undefined) return;
    try {
      if (isBulk) {
        await downloadBulkCitations(paperIds, format);
      } else {
        await downloadPaperCitation(first, format);
      }
    } catch (err) {
      toast.error('Could not export citation', { description: errorMessage(err) });
    }
  }

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <Button variant="outline" size={size} disabled={disabled || isEmpty} className="gap-1.5">
          <Quote className="h-3.5 w-3.5" />
          {copied ? 'Copied' : 'Cite'}
        </Button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end">
        <DropdownMenuItem onSelect={() => void handleCopy('bibtex')}>Copy BibTeX</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void handleCopy('ris')}>Copy RIS</DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => void handleDownload('bibtex')}>Download .bib</DropdownMenuItem>
        <DropdownMenuItem onSelect={() => void handleDownload('ris')}>Download .ris</DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
