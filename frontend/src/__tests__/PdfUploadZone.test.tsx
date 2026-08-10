import { StrictMode } from 'react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor, within } from '@testing-library/react';
import { PdfUploadZone } from '@/components/feed/PdfUploadZone';

const uploadPdf = vi.fn();
const processPdf = vi.fn();

vi.mock('@/lib/api', () => ({
  uploadPdf: (...a: unknown[]) => uploadPdf(...a),
  processPdf: (...a: unknown[]) => processPdf(...a),
}));

// useJobStore is consumed as a selector hook: useJobStore((s) => s.trackExternalJob)
vi.mock('@/stores/job-store', () => ({
  useJobStore: (selector: (s: { trackExternalJob: ReturnType<typeof vi.fn> }) => unknown) =>
    selector({ trackExternalJob: vi.fn() }),
}));

vi.mock('@tanstack/react-query', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@tanstack/react-query')>();
  return { ...actual, useQueryClient: () => ({ invalidateQueries: vi.fn() }) };
});

describe('PdfUploadZone — pure setFiles updater', () => {
  beforeEach(() => {
    uploadPdf.mockReset();
    processPdf.mockReset();
  });

  it('fires uploadPdf exactly once per file even under StrictMode double-invoke', async () => {
    uploadPdf.mockResolvedValue({ id: 'p1' });
    processPdf.mockResolvedValue({ job_id: 'j1' });

    const { container } = render(
      <StrictMode>
        <PdfUploadZone />
      </StrictMode>,
    );
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toHaveAttribute('name', 'pdf-files');
    const file = new File(['pdf'], 'paper.pdf', { type: 'application/pdf' });

    fireEvent.change(input, { target: { files: [file] } });

    await waitFor(() => {
      expect(screen.getByText('Indexing in background')).toBeTruthy();
    });
    expect(screen.queryByText('Done')).not.toBeInTheDocument();
    expect(uploadPdf).toHaveBeenCalledTimes(1);
  });
});

describe('PdfUploadZone — stable uid keys (FEE-3)', () => {
  beforeEach(() => {
    uploadPdf.mockReset();
    processPdf.mockReset();
  });

  it('renders two identically-named files as distinct rows and retries only the targeted row', async () => {
    uploadPdf.mockRejectedValue(new Error('upload fail'));

    const { container } = render(<PdfUploadZone />);
    const input = container.querySelector('input[type="file"]') as HTMLInputElement;
    expect(input).toBeTruthy();

    const file1 = new File(['identical'], 'paper.pdf', { type: 'application/pdf' });
    const file2 = new File(['identical'], 'paper.pdf', { type: 'application/pdf' });
    expect(file1.name).toBe(file2.name);
    expect(file1.size).toBe(file2.size);

    fireEvent.change(input, { target: { files: [file1, file2] } });

    const rows = await screen.findAllByRole('listitem');
    expect(rows).toHaveLength(2);

    await waitFor(() => {
      expect(screen.getAllByRole('button', { name: /retry/i })).toHaveLength(2);
    });
    expect(uploadPdf).toHaveBeenCalledTimes(2);

    uploadPdf.mockReset();
    uploadPdf.mockRejectedValue(new Error('retry fail'));
    const firstRetry = within(rows[0]!).getByRole('button', { name: /retry/i });
    fireEvent.click(firstRetry);

    await waitFor(() => {
      expect(uploadPdf).toHaveBeenCalledTimes(1);
    });
  });
});
