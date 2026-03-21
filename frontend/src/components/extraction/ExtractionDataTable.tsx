import {
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableHeader,
  TableRow,
} from '@/components/ui/table';
import { Badge } from '@/components/ui/badge';
import { QuoteExpander } from '@/components/extraction/QuoteExpander';
import type { ExtractionTableRow, ExtractionField } from '@/types';

interface ExtractionDataTableProps {
  rows: ExtractionTableRow[];
  fields: ExtractionField[];
}

export function ExtractionDataTable({ rows, fields }: ExtractionDataTableProps) {
  if (rows.length === 0) {
    return null;
  }

  return (
    <Table>
      <TableHeader>
        <TableRow>
          <TableHead className="min-w-[200px]">Paper</TableHead>
          {fields.map((f) => (
            <TableHead key={f.name}>{f.label}</TableHead>
          ))}
          <TableHead>Quotes</TableHead>
        </TableRow>
      </TableHeader>
      <TableBody>
        {rows.map((row) => (
          <TableRow key={row.paper_id}>
            <TableCell className="font-medium">
              {row.paper_title.length > 60
                ? `${row.paper_title.slice(0, 60)}...`
                : row.paper_title}
            </TableCell>
            {fields.map((f) => {
              const field = row.extractions[f.name];
              const value = field?.value;
              const verified = field?.verified ?? false;
              return (
                <TableCell key={f.name}>
                  {value != null ? (
                    <div className="flex items-center gap-1">
                      <Badge variant={verified ? 'default' : 'outline'} className="text-xs">
                        {verified ? 'V' : '?'}
                      </Badge>
                      <span className="text-sm">{String(value)}</span>
                    </div>
                  ) : (
                    <span className="text-muted-foreground">--</span>
                  )}
                </TableCell>
              );
            })}
            <TableCell>
              {fields.map((f) => {
                const field = row.extractions[f.name];
                if (!field?.quote) return null;
                return (
                  <div key={f.name} className="mb-1">
                    <span className="text-xs font-medium">{f.label}: </span>
                    <QuoteExpander
                      quote={field.quote}
                      pageNumber={field.page_number}
                      verified={field.verified}
                    />
                  </div>
                );
              })}
            </TableCell>
          </TableRow>
        ))}
      </TableBody>
    </Table>
  );
}
