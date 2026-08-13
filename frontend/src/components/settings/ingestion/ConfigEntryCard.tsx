/**
 * ConfigEntryCard — renders a single config entry as a Switch, custom element,
 * or text/number edit card depending on the entry shape and meta type.
 */
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Switch } from '@/components/ui/switch';
import { Card, CardContent } from '@/components/ui/card';
import { Pencil, Check, X } from 'lucide-react';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import type { ConfigEntry } from '@/types';
import { useId, type ReactNode } from 'react';

export interface ConfigEntryMeta {
  label: string;
  description: string;
  group: string;
  tooltip?: string;
  type?: 'boolean' | 'number' | 'string';
  min?: number;
  max?: number;
  step?: number;
}

export interface ConfigEntryCardProps {
  entry: ConfigEntry;
  meta: ConfigEntryMeta | undefined;
  /** Pre-rendered element for entries that need custom rendering (e.g. LLM model cards). */
  customElement?: ReactNode;
  editingKey: string | null;
  editValue: string;
  saveError: string | null;
  isMutPending: boolean;
  onMutate: (key: string, value: unknown) => void;
  onStartEdit: (entry: ConfigEntry) => void;
  onEditValueChange: (val: string) => void;
  onSaveEdit: () => void;
  onCancelEdit: () => void;
}

function formatConfigValue(value: unknown): string {
  return typeof value === 'string' ? value : JSON.stringify(value);
}

export function ConfigEntryCard({
  entry,
  meta,
  customElement,
  editingKey,
  editValue,
  saveError,
  isMutPending,
  onMutate,
  onStartEdit,
  onEditValueChange,
  onSaveEdit,
  onCancelEdit,
}: ConfigEntryCardProps) {
  const controlId = useId();

  if (customElement !== undefined) {
    return (
      <>
        {customElement}
        {saveError && (
          <p
            className="text-sm text-destructive mt-1"
            role="alert"
            data-testid={`config-save-error-${entry.key}`}
          >
            {saveError}
          </p>
        )}
      </>
    );
  }

  if (meta?.type === 'boolean') {
    return (
      <Card className="rounded-md border-hair shadow-none">
        <CardContent className="flex items-center justify-between p-4">
          <div>
            <Label htmlFor={controlId} className="text-sm font-medium">{meta.label}</Label>
            {meta.description && (
              <p className="text-xs text-muted-foreground">{meta.description}</p>
            )}
          </div>
          <Switch
            id={controlId}
            checked={entry.value === 'true' || entry.value === true}
            onCheckedChange={(checked) => onMutate(entry.key, String(checked))}
            disabled={isMutPending}
          />
        </CardContent>
      </Card>
    );
  }

  return (
    <Card className="rounded-md border-hair shadow-none">
      <CardContent className="flex items-center gap-4 p-4">
        {editingKey === entry.key ? (
          <div className="flex-1 space-y-1">
            <div className="flex items-center gap-2">
              <span className="shrink-0 text-sm font-medium">
                {meta?.label ?? entry.key}
              </span>
              <Input
                type={meta?.type === 'number' ? 'number' : 'text'}
                min={meta?.type === 'number' ? meta.min : undefined}
                max={meta?.type === 'number' ? meta.max : undefined}
                step={meta?.type === 'number' ? meta.step : undefined}
                value={editValue}
                onChange={(e) => onEditValueChange(e.target.value)}
                className="flex-1"
              />
              <Button
                size="icon"
                variant="ghost"
                onClick={onSaveEdit}
                disabled={isMutPending}
                aria-label="Save setting"
              >
                <Check className="h-4 w-4" />
              </Button>
              <Button size="icon" variant="ghost" onClick={onCancelEdit} aria-label="Cancel edit">
                <X className="h-4 w-4" />
              </Button>
            </div>
            {entry.key === 'fsrs.learning_steps' && (
              <p className="text-xs text-muted-foreground mt-1">
                Enter as [min, max], e.g. [1, 10] means review after 1 min then 10 min.
              </p>
            )}
            {saveError && (
              <p className="text-sm text-destructive mt-1">{saveError}</p>
            )}
          </div>
        ) : (
          <>
            <div className="flex-1 min-w-0">
              <div className="flex items-center gap-1 font-medium text-sm">
                {meta?.label ?? entry.key}
                {meta?.tooltip && <InfoTooltip content={meta.tooltip} />}
              </div>
              {meta?.description && (
                <p className="text-xs text-muted-foreground mt-0.5">
                  {meta.description}
                </p>
              )}
              <span className="text-sm text-muted-foreground mt-1 block">
                {formatConfigValue(entry.value)}
              </span>
            </div>
            <Button size="icon" variant="ghost" onClick={() => onStartEdit(entry)} aria-label="Edit setting">
              <Pencil className="h-4 w-4" />
            </Button>
          </>
        )}
      </CardContent>
    </Card>
  );
}
