import { useState, useCallback, useMemo } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import { Card, CardContent, CardHeader, CardTitle } from '@/components/ui/card';
import { Button } from '@/components/ui/button';
import { Skeleton } from '@/components/ui/skeleton';
import { EmptyState } from '@/components/EmptyState';
import { TemplateSelector } from '@/components/extraction/TemplateSelector';
import { PaperSearchSelect } from '@/components/shared/PaperSearchSelect';
import { ExtractionDataTable } from '@/components/extraction/ExtractionDataTable';
import {
  fetchExtractionTemplates,
  fetchExtractionTable,
  batchExtract,
  downloadExtractionCsv,
} from '@/lib/api';
// ExtractionField type used via template.fields from ExtractionTemplate
import { Download, Loader2, TableProperties } from 'lucide-react';
import { InfoTooltip } from '@/components/ui/info-tooltip';

export function ExtractionTablePage() {
  const queryClient = useQueryClient();
  const [selectedTemplateValue, setSelectedTemplateValue] = useState<string | null>(null);
  const [selectedPaperIds, setSelectedPaperIds] = useState<number[]>([]);

  const templatesQuery = useQuery({
    queryKey: ['extraction-templates'],
    queryFn: fetchExtractionTemplates,
  });

  const defaultTemplateValue = useMemo(
    () =>
      templatesQuery.data?.find((t) => t.is_default)?.id?.toString() ??
      templatesQuery.data?.[0]?.id?.toString() ??
      '',
    [templatesQuery.data],
  );

  const effectiveTemplateValue = selectedTemplateValue ?? defaultTemplateValue;

  const selectedTemplateId = useMemo(
    () => (effectiveTemplateValue ? Number(effectiveTemplateValue) : null),
    [effectiveTemplateValue],
  );

  const selectedTemplate = useMemo(
    () => templatesQuery.data?.find((t) => t.id === selectedTemplateId) ?? null,
    [selectedTemplateId, templatesQuery.data],
  );

  const tableQuery = useQuery({
    queryKey: ['extraction-table', selectedTemplateId, selectedPaperIds],
    queryFn: () =>
      selectedTemplateId != null
        ? fetchExtractionTable(selectedTemplateId, selectedPaperIds)
        : Promise.resolve([]),
    enabled: selectedTemplateId != null && selectedPaperIds.length > 0,
  });

  const extractMutation = useMutation({
    mutationFn: () => {
      if (selectedTemplateId == null) throw new Error('No template selected');
      return batchExtract(selectedTemplateId, selectedPaperIds);
    },
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['extraction-table'] });
    },
  });

  const handleExport = useCallback(() => {
    if (selectedTemplateId == null) return;
    downloadExtractionCsv(selectedTemplateId);
  }, [selectedTemplateId]);

  return (
    <div className="space-y-6 p-6">
      <div className="flex items-center justify-between">
        <h1 className="text-2xl font-bold">Extraction Table</h1>
      </div>
      <p className="text-muted-foreground text-sm">Structured data extracted from papers using templates</p>

      <div className="rounded-md border bg-muted/30 px-4 py-3 text-sm text-muted-foreground">
        <strong>How to use:</strong> 1. Choose a template → 2. Search and select papers → 3. Click Extract Selected → 4. Compare results in the table below
      </div>

      {(templatesQuery.isError || tableQuery.isError) && (
        <div className="py-8 text-center">
          <p className="text-sm text-destructive">
            Failed to load data:{' '}
            {templatesQuery.isError
              ? (templatesQuery.error as Error).message
              : (tableQuery.error as Error).message}
          </p>
        </div>
      )}

      {/* Template + Paper Selection */}
      <div className="grid gap-6 md:grid-cols-2">
        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-1 text-lg">
              Extraction Template
              <InfoTooltip content="Templates define which facts to extract. Create or edit templates in Settings > Extraction Templates." />
            </CardTitle>
          </CardHeader>
          <CardContent className="space-y-4">
            {templatesQuery.isLoading ? (
              <Skeleton className="h-10 w-72" />
            ) : templatesQuery.data && templatesQuery.data.length > 0 ? (
              <TemplateSelector
                templates={templatesQuery.data}
                value={effectiveTemplateValue}
                onChange={setSelectedTemplateValue}
              />
            ) : (
              <EmptyState
                title="No templates"
                description="Create an extraction template in Settings first."
                icon={TableProperties}
              />
            )}

            {selectedTemplate && (
              <div className="text-sm text-muted-foreground">
                <span className="font-medium">Fields: </span>
                {selectedTemplate.fields.map((f) => f.label).join(', ')}
              </div>
            )}
          </CardContent>
        </Card>

        <Card>
          <CardHeader>
            <CardTitle className="flex items-center gap-1 text-lg">
              Paper Selection
              <InfoTooltip content="Search your library and add papers to compare. You can add up to 20 papers per extraction run." />
            </CardTitle>
          </CardHeader>
          <CardContent>
            <PaperSearchSelect
              values={selectedPaperIds}
              onChangeMulti={setSelectedPaperIds}
              placeholder="Search papers for extraction..."
            />
          </CardContent>
        </Card>
      </div>

      {/* Actions */}
      <div className="flex items-center gap-3">
        <div>
          <Button
            onClick={() => extractMutation.mutate()}
            disabled={
              !selectedTemplateId ||
              selectedPaperIds.length === 0 ||
              extractMutation.isPending
            }
          >
            {extractMutation.isPending ? (
              <>
                <Loader2 className="mr-2 h-4 w-4 animate-spin" />
                Extracting...
              </>
            ) : (
              'Extract Selected'
            )}
          </Button>
          <p className="text-xs text-muted-foreground mt-1">Sends selected papers to the LLM to fill in each template field. May take 30–60 seconds per paper.</p>
        </div>

        {tableQuery.data && tableQuery.data.length > 0 && (
          <Button variant="outline" onClick={handleExport}>
            <Download className="mr-2 h-4 w-4" />
            Export CSV
          </Button>
        )}

        {extractMutation.isSuccess && (
          <span className="text-sm text-green-600">
            Queued {extractMutation.data.total} papers (job {extractMutation.data.job_id.slice(0, 8)})
          </span>
        )}

        {extractMutation.isError && (
          <span className="text-sm text-destructive">
            Extraction failed: {(extractMutation.error as Error).message}
          </span>
        )}
      </div>

      {/* Results Table */}
      <Card>
        <CardHeader>
          <CardTitle className="text-lg">Comparison Table</CardTitle>
        </CardHeader>
        <CardContent>
          {tableQuery.isLoading ? (
            <div className="space-y-2">
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
              <Skeleton className="h-10 w-full" />
            </div>
          ) : tableQuery.data && tableQuery.data.length > 0 && selectedTemplate ? (
            <ExtractionDataTable
              rows={tableQuery.data}
              fields={selectedTemplate.fields}
            />
          ) : selectedPaperIds.length > 0 ? (
            <EmptyState
              title="No extractions match your filters"
              description="Try selecting different papers or run extraction to generate data."
              icon={TableProperties}
            />
          ) : (
            <EmptyState
              title="No papers selected"
              description="Pick papers above and click Extract Selected to fill this table."
              icon={TableProperties}
            />
          )}
        </CardContent>
      </Card>
    </div>
  );
}
