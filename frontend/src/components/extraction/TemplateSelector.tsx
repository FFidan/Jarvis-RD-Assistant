import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from '@/components/ui/select';
import type { ExtractionTemplate } from '@/types';

interface TemplateSelectorProps {
  templates: ExtractionTemplate[];
  value: string;
  onChange: (templateId: string) => void;
}

const TEMPLATE_SELECTOR_ID = 'extraction-template-select';

export function TemplateSelector({ templates, value, onChange }: TemplateSelectorProps) {
  return (
    <div className="space-y-1">
      <label
        className="text-sm font-medium"
        htmlFor={TEMPLATE_SELECTOR_ID}
      >
        Extraction Template
      </label>
      <Select
        name="extraction-template"
        value={value}
        onValueChange={onChange}
      >
        <SelectTrigger
          id={TEMPLATE_SELECTOR_ID}
          className="w-72"
          aria-label="Extraction Template"
        >
          <SelectValue placeholder="Select a template..." />
        </SelectTrigger>
        <SelectContent>
          {templates.map((t) => (
            <SelectItem key={t.id} value={String(t.id)}>
              {t.name}
              {t.is_default ? ' (default)' : ''}
            </SelectItem>
          ))}
        </SelectContent>
      </Select>
    </div>
  );
}
