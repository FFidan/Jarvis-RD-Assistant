/**
 * CitationMenu vitest (P3.2)
 *
 * Covers:
 *  1. Copy BibTeX calls copyPaperCitation then navigator.clipboard.writeText.
 *  2. Download .ris calls downloadPaperCitation.
 *  3. With >1 paper, the bulk helpers receive the full id array.
 *  4. The trigger is disabled when paperIds is empty.
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';

vi.mock('@/lib/api', () => ({
  copyPaperCitation: vi.fn(),
  downloadPaperCitation: vi.fn(),
  downloadPaperMarkdown: vi.fn(),
  copyBulkCitations: vi.fn(),
  downloadBulkCitations: vi.fn(),
}));

vi.mock('sonner', async () =>
  (await import('@/__tests__/fixtures/sonner-mock')).createSonnerMock());

import {
  copyPaperCitation,
  downloadPaperCitation,
  downloadPaperMarkdown,
  copyBulkCitations,
  downloadBulkCitations,
} from '@/lib/api';
import { CitationMenu } from '@/components/citation/CitationMenu';

const mockCopyPaper = vi.mocked(copyPaperCitation);
const mockDownloadPaper = vi.mocked(downloadPaperCitation);
const mockDownloadMarkdown = vi.mocked(downloadPaperMarkdown);
const mockCopyBulk = vi.mocked(copyBulkCitations);
const mockDownloadBulk = vi.mocked(downloadBulkCitations);

const writeText = vi.fn<(text: string) => Promise<void>>().mockResolvedValue(undefined);

// Radix DropdownMenu relies on pointer-capture APIs not present in jsdom.
beforeAll(() => {
  if (!window.HTMLElement.prototype.hasPointerCapture) {
    window.HTMLElement.prototype.hasPointerCapture = () => false;
  }
  if (!window.HTMLElement.prototype.setPointerCapture) {
    window.HTMLElement.prototype.setPointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.releasePointerCapture) {
    window.HTMLElement.prototype.releasePointerCapture = () => {};
  }
  if (!window.HTMLElement.prototype.scrollIntoView) {
    window.HTMLElement.prototype.scrollIntoView = () => {};
  }
});

beforeEach(() => {
  vi.clearAllMocks();
  writeText.mockResolvedValue(undefined);
  mockCopyPaper.mockResolvedValue('@article{x}');
  mockCopyBulk.mockResolvedValue('@article{x}\n@article{y}');
  mockDownloadPaper.mockResolvedValue(undefined);
  mockDownloadMarkdown.mockResolvedValue(undefined);
  mockDownloadBulk.mockResolvedValue(undefined);
});

async function openAndSelect(paperIds: number[], item: RegExp) {
  // userEvent.setup() installs its own navigator.clipboard; re-pin ours afterwards
  // so the component's clipboard.writeText hits the spy under test.
  const user = userEvent.setup({ pointerEventsCheck: 0 });
  Object.defineProperty(navigator, 'clipboard', {
    value: { writeText },
    configurable: true,
    writable: true,
  });
  render(<CitationMenu paperIds={paperIds} />);
  await user.click(screen.getByRole('button', { name: /cite/i }));
  await user.click(await screen.findByRole('menuitem', { name: item }));
}

describe('CitationMenu', () => {
  it('copies a single-paper citation via copyPaperCitation then clipboard', async () => {
    await openAndSelect([7], /copy bibtex/i);

    await waitFor(() => expect(mockCopyPaper).toHaveBeenCalledWith(7, 'bibtex'));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith('@article{x}'));
    expect(mockCopyBulk).not.toHaveBeenCalled();
  });

  it('downloads a single-paper citation via downloadPaperCitation', async () => {
    await openAndSelect([7], /download \.ris/i);

    await waitFor(() => expect(mockDownloadPaper).toHaveBeenCalledWith(7, 'ris'));
    expect(mockDownloadBulk).not.toHaveBeenCalled();
  });

  it('routes multiple papers through the bulk helpers with the full id array', async () => {
    await openAndSelect([1, 2, 3], /copy ris/i);

    await waitFor(() => expect(mockCopyBulk).toHaveBeenCalledWith([1, 2, 3], 'ris'));
    expect(mockCopyPaper).not.toHaveBeenCalled();
  });

  it('exports a single paper as Markdown via downloadPaperMarkdown', async () => {
    await openAndSelect([7], /export markdown/i);

    await waitFor(() => expect(mockDownloadMarkdown).toHaveBeenCalledWith(7));
  });

  it('hides the Markdown export when more than one paper is selected', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<CitationMenu paperIds={[1, 2]} />);
    await user.click(screen.getByRole('button', { name: /cite/i }));

    expect(await screen.findByRole('menuitem', { name: /download \.bib/i })).toBeInTheDocument();
    expect(screen.queryByRole('menuitem', { name: /export markdown/i })).toBeNull();
  });

  it('disables the trigger when paperIds is empty', () => {
    render(<CitationMenu paperIds={[]} />);
    expect(screen.getByRole('button', { name: /cite/i })).toBeDisabled();
  });
});
