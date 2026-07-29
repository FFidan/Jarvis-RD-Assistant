/**
 * AboutSection.test.tsx — Settings footer showing FE/BE version + the
 * "update available" hint when they differ (Task 6.4).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { AboutSection } from '@/components/settings/AboutSection';
import type { StackHealthSummary } from '@/lib/api';

vi.mock('@/lib/api', async (importOriginal) => {
  const orig = await importOriginal<typeof import('@/lib/api')>();
  return {
    ...orig,
    fetchStackHealth: vi.fn(),
  };
});

import { fetchStackHealth } from '@/lib/api';
import { createTestQueryClient, renderWithProviders } from '@/__tests__/test-utils';
const mockFetchStackHealth = vi.mocked(fetchStackHealth);

function makeHealth(version: string): StackHealthSummary {
  return {
    overall: 'ok',
    degradedCount: 0,
    downCount: 0,
    services: [],
    version,
  };
}

function renderAboutSection() {
  const queryClient = createTestQueryClient();
  return renderWithProviders(
    <AboutSection />,
    { queryClient },
  );
}

describe('AboutSection', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders the FE build version and "Unknown" for the server while the health query is pending', () => {
    mockFetchStackHealth.mockReturnValue(new Promise(() => {})); // never resolves
    renderAboutSection();

    expect(screen.getByTestId('about-fe-version')).toHaveTextContent(`v${__APP_VERSION__}`);
    expect(screen.getByTestId('about-be-version')).toHaveTextContent('Unknown');
  });

  it('renders the BE server version once the health query resolves', async () => {
    mockFetchStackHealth.mockResolvedValue(makeHealth('9.9.9'));
    renderAboutSection();

    await waitFor(() => {
      expect(screen.getByTestId('about-be-version')).toHaveTextContent('v9.9.9');
    });
  });

  it('shows the update-available hint when FE and BE versions differ', async () => {
    mockFetchStackHealth.mockResolvedValue(makeHealth('9.9.9'));
    renderAboutSection();

    await waitFor(() => {
      expect(screen.getByTestId('about-update-hint')).toHaveTextContent(
        'An update is available — reload to finish updating.',
      );
    });
  });

  it('does NOT show the update hint when FE and BE versions match', async () => {
    mockFetchStackHealth.mockResolvedValue(makeHealth(__APP_VERSION__));
    renderAboutSection();

    await waitFor(() => {
      expect(screen.getByTestId('about-be-version')).toHaveTextContent(`v${__APP_VERSION__}`);
    });
    expect(screen.queryByTestId('about-update-hint')).not.toBeInTheDocument();
  });

  it('does NOT show the update hint while the server version is unknown', () => {
    mockFetchStackHealth.mockReturnValue(new Promise(() => {}));
    renderAboutSection();

    expect(screen.queryByTestId('about-update-hint')).not.toBeInTheDocument();
  });

  it('renders "Unknown" and no update hint when the server reports version "unknown"', async () => {
    // An env-less deployment resolves app_version() to the literal "unknown";
    // that is "can't determine", not a differing version — no false update hint.
    mockFetchStackHealth.mockResolvedValue(makeHealth('unknown'));
    renderAboutSection();

    await waitFor(() =>
      expect(screen.getByTestId('about-be-version')).toHaveTextContent('Unknown'),
    );
    expect(screen.getByTestId('about-be-version')).not.toHaveTextContent('vunknown');
    expect(screen.queryByTestId('about-update-hint')).not.toBeInTheDocument();
  });
});
