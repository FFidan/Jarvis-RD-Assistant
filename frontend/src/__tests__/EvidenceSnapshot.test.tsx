import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { EvidenceSnapshot } from '@/components/shared/EvidenceSnapshot';

// Mock fetchSnapshot from api module
vi.mock('@/lib/api', () => ({
  fetchSnapshot: vi.fn(),
}));

import { fetchSnapshot } from '@/lib/api';
const mockFetchSnapshot = fetchSnapshot as ReturnType<typeof vi.fn>;

// Minimal URL.createObjectURL / revokeObjectURL stubs
const FAKE_OBJECT_URL = 'blob:http://localhost/fake-snapshot-uuid';
const createObjectURLSpy = vi.fn(() => FAKE_OBJECT_URL);
const revokeObjectURLSpy = vi.fn();

beforeEach(() => {
  vi.clearAllMocks();
  globalThis.URL.createObjectURL = createObjectURLSpy;
  globalThis.URL.revokeObjectURL = revokeObjectURLSpy;
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe('EvidenceSnapshot', () => {
  it('renders an <img> with the blob URL when fetch succeeds (200)', async () => {
    // fetchSnapshot returns the object URL string (fetch → blob → createObjectURL done inside)
    mockFetchSnapshot.mockResolvedValueOnce(FAKE_OBJECT_URL);

    render(
      <EvidenceSnapshot paperId={42} page={3} altText="Page 3 snapshot" />,
    );

    // Initially shows loading skeleton (no img yet)
    expect(screen.queryByRole('img', { name: 'Page 3 snapshot' })).not.toBeInTheDocument();

    await waitFor(() => {
      const img = screen.getByRole('img', { name: 'Page 3 snapshot' });
      expect(img).toBeInTheDocument();
      expect(img).toHaveAttribute('src', FAKE_OBJECT_URL);
    });

    expect(mockFetchSnapshot).toHaveBeenCalledWith(42, 3);
  });

  it('renders a fallback placeholder when fetch fails (404)', async () => {
    mockFetchSnapshot.mockRejectedValueOnce(new Error('404'));

    render(
      <EvidenceSnapshot paperId={99} page={1} altText="Page 1 snapshot" />,
    );

    await waitFor(() => {
      // Fallback renders a div with role="img" and aria-label="Snapshot unavailable"
      const fallback = screen.getByRole('img', { name: 'Snapshot unavailable' });
      expect(fallback).toBeInTheDocument();
    });

    // Should NOT render a real <img> element
    expect(screen.queryByRole('img', { name: 'Page 1 snapshot' })).not.toBeInTheDocument();
  });

  it('revokes the object URL on unmount', async () => {
    mockFetchSnapshot.mockResolvedValueOnce(FAKE_OBJECT_URL);

    const { unmount } = render(
      <EvidenceSnapshot paperId={7} page={2} />,
    );

    await waitFor(() => {
      expect(screen.getByRole('img')).toBeInTheDocument();
    });

    unmount();

    // The component stores the object URL in a ref and revokes on cleanup.
    // Since fetchSnapshot already returns the URL string in our mock,
    // the component's effect stores it in objectUrlRef and revokes on unmount.
    // However, our mock returns the URL directly — the component calls
    // URL.revokeObjectURL on the ref value which equals FAKE_OBJECT_URL.
    expect(revokeObjectURLSpy).toHaveBeenCalledWith(FAKE_OBJECT_URL);
  });
});
