import { useCallback, useRef, useState } from 'react';
import { useQueryClient } from '@tanstack/react-query';
import { Upload } from 'lucide-react';
import { Button } from '@/components/ui/button';
import { uploadPdf, processPdf } from '@/lib/api';

type FileStatus = 'idle' | 'uploading' | 'processing' | 'done' | 'error';

interface FileEntry {
  file: File;
  status: FileStatus;
  error?: string;
}

interface PdfUploadZoneProps {
  onComplete?: () => void;
}

export function PdfUploadZone({ onComplete }: PdfUploadZoneProps) {
  const [files, setFiles] = useState<FileEntry[]>([]);
  const [dragging, setDragging] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);
  const queryClient = useQueryClient();

  const updateFile = (index: number, patch: Partial<FileEntry>) =>
    setFiles(prev => prev.map((f, i) => (i === index ? { ...f, ...patch } : f)));

  const retryFile = (index: number) => {
    const entry = files[index];
    if (!entry) return;
    setFiles(prev => prev.map((f, i) => (i === index ? { ...f, status: 'idle', error: undefined } : f)));
    void (async () => {
      try {
        setFiles(s => s.map((f, si) => (si === index ? { ...f, status: 'uploading' as FileStatus } : f)));
        const paper = await uploadPdf(entry.file, entry.file.name.replace(/\.pdf$/i, ''));
        setFiles(s => s.map((f, si) => (si === index ? { ...f, status: 'processing' as FileStatus } : f)));
        await processPdf(paper.id);
        setFiles(s => s.map((f, si) => (si === index ? { ...f, status: 'done' as FileStatus } : f)));
        queryClient.invalidateQueries({ queryKey: ['feed'] });
        onComplete?.();
      } catch (err) {
        setFiles(s =>
          s.map((f, si) =>
            si === index
              ? { ...f, status: 'error' as FileStatus, error: err instanceof Error ? err.message : 'Upload failed' }
              : f,
          ),
        );
      }
    })();
  };

  const processFiles = useCallback(async (newFiles: File[]) => {
    const entries: FileEntry[] = newFiles.map(f => ({ file: f, status: 'idle' as FileStatus }));
    setFiles(prev => {
      const startIndex = prev.length;
      void (async () => {
        for (let i = 0; i < entries.length; i++) {
          const idx = startIndex + i;
          const file = entries[i].file;
          try {
            setFiles(s => s.map((f, si) => (si === idx ? { ...f, status: 'uploading' as FileStatus } : f)));
            const paper = await uploadPdf(file, file.name.replace(/\.pdf$/i, ''));
            setFiles(s => s.map((f, si) => (si === idx ? { ...f, status: 'processing' as FileStatus } : f)));
            await processPdf(paper.id);
            setFiles(s => s.map((f, si) => (si === idx ? { ...f, status: 'done' as FileStatus } : f)));
          } catch (err) {
            setFiles(s =>
              s.map((f, si) =>
                si === idx
                  ? { ...f, status: 'error' as FileStatus, error: err instanceof Error ? err.message : 'Upload failed' }
                  : f,
              ),
            );
          }
        }
        queryClient.invalidateQueries({ queryKey: ['feed'] });
        onComplete?.();
      })();
      return [...prev, ...entries];
    });
  }, [queryClient, onComplete]);

  const handleDrop = (e: React.DragEvent) => {
    e.preventDefault();
    setDragging(false);
    const dropped = Array.from(e.dataTransfer.files).filter(f => f.type === 'application/pdf');
    if (dropped.length) void processFiles(dropped);
  };

  const handleInput = (e: React.ChangeEvent<HTMLInputElement>) => {
    const selected = Array.from(e.target.files ?? []);
    if (selected.length) void processFiles(selected);
    e.target.value = '';
  };

  const statusLabel: Record<FileStatus, string> = {
    idle: 'Queued',
    uploading: 'Uploading\u2026',
    processing: 'Indexing\u2026',
    done: 'Done',
    error: 'Error',
  };

  const statusColor: Record<FileStatus, string> = {
    idle: 'text-muted-foreground',
    uploading: 'text-blue-500',
    processing: 'text-amber-500',
    done: 'text-green-600',
    error: 'text-destructive',
  };

  return (
    <div className="space-y-3">
      <div
        onDrop={handleDrop}
        onDragOver={e => { e.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onClick={() => inputRef.current?.click()}
        className={`cursor-pointer rounded-lg border-2 border-dashed px-6 py-8 text-center transition-colors ${
          dragging ? 'border-primary bg-primary/5' : 'border-muted-foreground/25 hover:border-primary/50'
        }`}
      >
        <Upload className="mx-auto mb-2 h-8 w-8 text-muted-foreground" />
        <p className="text-sm font-medium">Drop PDF files here or click to browse</p>
        <p className="mt-1 text-xs text-muted-foreground">Multiple files supported &middot; Max 100 MB each</p>
        <input ref={inputRef} type="file" accept=".pdf" multiple className="hidden" onChange={handleInput} />
      </div>

      {files.length > 0 && (
        <ul className="space-y-1">
          {files.map((entry, i) => (
            <li key={i} className="flex items-center justify-between rounded-md bg-muted/30 px-3 py-2 text-xs">
              <span className="truncate max-w-[60%] font-medium">{entry.file.name}</span>
              <span className="flex items-center gap-2">
                <span className={statusColor[entry.status]}>
                  {entry.status === 'error' ? (entry.error ?? 'Error') : statusLabel[entry.status]}
                </span>
                {entry.status === 'error' && (
                  <Button variant="ghost" size="sm" className="h-5 px-2 text-xs" onClick={() => retryFile(i)}>
                    Retry
                  </Button>
                )}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
