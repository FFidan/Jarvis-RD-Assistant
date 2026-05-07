import { useState } from 'react';
import { useQuery, useMutation, useQueryClient } from '@tanstack/react-query';
import {
  fetchExtractionTemplates,
  createExtractionTemplate,
  updateExtractionTemplate,
  deleteExtractionTemplate,
} from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Input } from '@/components/ui/input';
import { Label } from '@/components/ui/label';
import { Textarea } from '@/components/ui/textarea';
import { Badge } from '@/components/ui/badge';
import { Card, CardContent } from '@/components/ui/card';
import { ConfirmDialog } from '@/components/ConfirmDialog';
import { EmptyState } from '@/components/EmptyState';
import { useConfirm } from '@/hooks/use-confirm';
import { Trash2, Plus, Pencil, TableProperties } from 'lucide-react';
import { InfoTooltip } from '@/components/ui/info-tooltip';
import type { ExtractionTemplate } from '@/types';

export function ExtractionTemplateSection() {
  const queryClient = useQueryClient();
  const { isOpen, confirm, handleConfirm, handleCancel } = useConfirm();
  const [showAdd, setShowAdd] = useState(false);
  const [addForm, setAddForm] = useState({ name: '', description: '', fields: '' });
  const [deleteTarget, setDeleteTarget] = useState<number | null>(null);
  const [editTemplate, setEditTemplate] = useState<ExtractionTemplate | null>(null);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [editFields, setEditFields] = useState('');

  const { data: templates = [], isLoading } = useQuery({
    queryKey: ['extraction-templates'],
    queryFn: fetchExtractionTemplates,
  });

  const createMut = useMutation({
    mutationFn: (data: Partial<ExtractionTemplate>) => createExtractionTemplate(data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['extraction-templates'] });
      setShowAdd(false);
      setAddForm({ name: '', description: '', fields: '' });
    },
  });

  const editMut = useMutation({
    mutationFn: ({ id, data }: { id: number; data: Partial<ExtractionTemplate> }) =>
      updateExtractionTemplate(id, data),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['extraction-templates'] });
      setEditTemplate(null);
    },
  });

  const deleteMut = useMutation({
    mutationFn: (id: number) => deleteExtractionTemplate(id),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey: ['extraction-templates'] });
    },
  });

  const startEdit = (tmpl: ExtractionTemplate) => {
    setEditTemplate(tmpl);
    setEditName(tmpl.name);
    setEditDescription(tmpl.description ?? '');
    setEditFields(
      tmpl.fields
        .map((f) => `${f.name}|${f.label}|${f.description}|${f.type}`)
        .join('\n'),
    );
  };

  const handleEditSave = () => {
    if (!editTemplate || !editName.trim() || !editFields.trim()) return;
    const parsedFields = editFields
      .trim()
      .split('\n')
      .map((line) => {
        const parts = line.split('|').map((p) => p.trim());
        if (parts.length < 3) return null;
        return {
          name: parts[0],
          label: parts[1],
          description: parts[2],
          type: parts[3] || 'text',
        };
      })
      .filter(Boolean);

    if (parsedFields.length === 0) return;

    editMut.mutate({
      id: editTemplate.id,
      data: {
        name: editName.trim(),
        description: editDescription.trim() || undefined,
        fields: parsedFields,
      } as Partial<ExtractionTemplate>,
    });
  };

  const handleDelete = async (id: number) => {
    setDeleteTarget(id);
    const confirmed = await confirm();
    if (confirmed) {
      deleteMut.mutate(id);
    }
    setDeleteTarget(null);
  };

  const handleAdd = () => {
    if (!addForm.name.trim() || !addForm.fields.trim()) return;
    const parsedFields = addForm.fields
      .trim()
      .split('\n')
      .map((line) => {
        const parts = line.split('|').map((p) => p.trim());
        if (parts.length < 3) return null;
        return {
          name: parts[0],
          label: parts[1],
          description: parts[2],
          type: parts[3] || 'text',
        };
      })
      .filter(Boolean);

    if (parsedFields.length === 0) return;

    createMut.mutate({
      name: addForm.name.trim(),
      description: addForm.description.trim() || undefined,
      fields: parsedFields,
    } as Partial<ExtractionTemplate>);
  };

  if (isLoading) {
    return <div className="py-8 text-center text-muted-foreground">Loading templates...</div>;
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground mb-4">
        Extraction templates define the structured fields JARVIS pulls from papers. Each field extracts a specific fact (e.g. sample size, main finding). Once you create a template, use it in the Extraction Table to compare papers side-by-side.
      </p>
      {templates.length === 0 && !showAdd ? (
        <EmptyState
          title="No extraction templates"
          description="Define structured fields to extract from papers."
          icon={TableProperties}
        />
      ) : (
        <div className="space-y-2">
          {templates.map((tmpl) => (
            <Card key={tmpl.id} className="rounded-md border-hair shadow-none">
              <CardContent className="flex items-center gap-4 p-4">
                <div className="flex-1 min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="font-medium">{tmpl.name}</span>
                    {tmpl.is_default && <Badge>Default</Badge>}
                    <Badge variant="secondary">{tmpl.fields.length} fields</Badge>
                  </div>
                  {tmpl.description && (
                    <p className="mt-1 text-sm text-muted-foreground">{tmpl.description}</p>
                  )}
                  <p className="mt-1 text-xs text-muted-foreground">
                    {tmpl.fields.map((f) => f.label || f.name).join(', ')}
                  </p>
                </div>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`Edit template ${tmpl.name}`}
                  onClick={() => startEdit(tmpl)}
                >
                  <Pencil className="h-4 w-4" />
                </Button>
                <Button
                  size="icon"
                  variant="ghost"
                  aria-label={`Delete template ${tmpl.name}`}
                  onClick={() => handleDelete(tmpl.id)}
                >
                  <Trash2 className="h-4 w-4" />
                </Button>
              </CardContent>
            </Card>
          ))}
        </div>
      )}

      {showAdd ? (
        <Card className="rounded-md border-hair shadow-none">
          <CardContent className="space-y-3 p-4">
            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <Label htmlFor="tmpl-name">Template Name</Label>
                <Input
                  id="tmpl-name"
                  value={addForm.name}
                  onChange={(e) => setAddForm({ ...addForm, name: e.target.value })}
                  placeholder="e.g. ML Paper Analysis"
                />
              </div>
              <div>
                <Label htmlFor="tmpl-desc">Description (optional)</Label>
                <Input
                  id="tmpl-desc"
                  value={addForm.description}
                  onChange={(e) => setAddForm({ ...addForm, description: e.target.value })}
                  placeholder="Extract key ML metrics"
                />
              </div>
            </div>
            <div>
              <Label htmlFor="tmpl-fields" className="flex items-center gap-1">
                Fields
                <InfoTooltip content="Each line defines one column in the Extraction Table. Use text for prose answers, number for quantities, boolean for yes/no, list for multiple items." />
              </Label>
              <div className="text-xs text-muted-foreground space-y-1 mb-1">
                <p>One field per line, format: <code className="bg-muted px-1 rounded">field_name | Display Label | Description | type</code></p>
                <p>Types: <code className="bg-muted px-1 rounded">text</code> · <code className="bg-muted px-1 rounded">number</code> · <code className="bg-muted px-1 rounded">boolean</code> · <code className="bg-muted px-1 rounded">list</code></p>
                <p>Example: <code className="bg-muted px-1 rounded">sample_size | Sample Size | Number of participants | number</code></p>
              </div>
              <Textarea
                id="tmpl-fields"
                value={addForm.fields}
                onChange={(e) => setAddForm({ ...addForm, fields: e.target.value })}
                placeholder={
                  'methodology|Methodology|Research methodology used|text\nresults|Results|Key experimental results|text'
                }
                rows={5}
              />
            </div>
            <div className="flex gap-2">
              <Button onClick={handleAdd} disabled={createMut.isPending}>
                Create Template
              </Button>
              <Button variant="outline" onClick={() => setShowAdd(false)}>
                Cancel
              </Button>
            </div>
          </CardContent>
        </Card>
      ) : (
        <Button variant="outline" onClick={() => setShowAdd(true)}>
          <Plus className="mr-2 h-4 w-4" />
          Add Template
        </Button>
      )}

      {editTemplate && (
        <Card className="rounded-md border-hair shadow-none">
          <CardContent className="space-y-3 p-4">
            <h4 className="text-sm font-semibold">Edit Template</h4>
            <div className="grid gap-2 sm:grid-cols-2">
              <div>
                <Label htmlFor="edit-tmpl-name">Template Name</Label>
                <Input
                  id="edit-tmpl-name"
                  value={editName}
                  onChange={(e) => setEditName(e.target.value)}
                  placeholder="e.g. ML Paper Analysis"
                />
              </div>
              <div>
                <Label htmlFor="edit-tmpl-desc">Description (optional)</Label>
                <Input
                  id="edit-tmpl-desc"
                  value={editDescription}
                  onChange={(e) => setEditDescription(e.target.value)}
                  placeholder="Extract key ML metrics"
                />
              </div>
            </div>
            <div>
              <Label htmlFor="edit-tmpl-fields" className="flex items-center gap-1">
                Fields
                <InfoTooltip content="Each line defines one column in the Extraction Table. Use text for prose answers, number for quantities, boolean for yes/no, list for multiple items." />
              </Label>
              <div className="text-xs text-muted-foreground space-y-1 mb-1">
                <p>One field per line, format: <code className="bg-muted px-1 rounded">field_name | Display Label | Description | type</code></p>
                <p>Types: <code className="bg-muted px-1 rounded">text</code> · <code className="bg-muted px-1 rounded">number</code> · <code className="bg-muted px-1 rounded">boolean</code> · <code className="bg-muted px-1 rounded">list</code></p>
                <p>Example: <code className="bg-muted px-1 rounded">sample_size | Sample Size | Number of participants | number</code></p>
              </div>
              <Textarea
                id="edit-tmpl-fields"
                value={editFields}
                onChange={(e) => setEditFields(e.target.value)}
                placeholder={
                  'methodology|Methodology|Research methodology used|text\nresults|Results|Key experimental results|text'
                }
                rows={5}
              />
            </div>
            <div className="flex gap-2">
              <Button onClick={handleEditSave} disabled={editMut.isPending}>
                {editMut.isPending ? 'Saving...' : 'Save Changes'}
              </Button>
              <Button variant="outline" onClick={() => setEditTemplate(null)}>
                Cancel
              </Button>
            </div>
          </CardContent>
        </Card>
      )}

      <ConfirmDialog
        open={isOpen && deleteTarget !== null}
        title="Delete Template"
        description="This will permanently remove this extraction template. Are you sure?"
        confirmLabel="Delete"
        onConfirm={handleConfirm}
        onCancel={handleCancel}
      />
    </div>
  );
}
