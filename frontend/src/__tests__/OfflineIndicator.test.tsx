/**
 * OfflineIndicator tests — Wave 3 P1d
 *
 * Coverage:
 *  - available-offline variant renders correct badge + accessible text
 *  - stale-cached variant renders "as of" timestamp text
 *  - online-only variant renders with WifiOff indicator
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import { OfflineIndicator } from '@/components/shared/OfflineIndicator';

// Freeze a known timestamp for testing
const KNOWN_TS = new Date('2026-05-15T10:00:00Z').getTime();

// Freeze Date.now to make formatCacheTime deterministic
beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(new Date('2026-05-15T10:30:00Z')); // 30 min after KNOWN_TS
});

afterEach(() => {
  vi.useRealTimers();
});

describe('OfflineIndicator', () => {
  describe('available-offline variant', () => {
    it('renders with data-testid', () => {
      render(<OfflineIndicator variant="available-offline" />);
      expect(screen.getByTestId('offline-indicator-available')).toBeTruthy();
    });

    it('includes "available offline" text', () => {
      render(<OfflineIndicator variant="available-offline" />);
      expect(screen.getByTestId('offline-indicator-available').textContent).toContain('available offline');
    });
  });

  describe('stale-cached variant', () => {
    it('renders with data-testid', () => {
      render(<OfflineIndicator variant="stale-cached" timestamp={KNOWN_TS} />);
      expect(screen.getByTestId('offline-indicator-stale')).toBeTruthy();
    });

    it('includes "stale-cached" text', () => {
      render(<OfflineIndicator variant="stale-cached" timestamp={KNOWN_TS} />);
      expect(screen.getByTestId('offline-indicator-stale').textContent).toContain('stale-cached');
    });

    it('shows "as of" with a time string', () => {
      render(<OfflineIndicator variant="stale-cached" timestamp={KNOWN_TS} />);
      const text = screen.getByTestId('offline-indicator-stale').textContent ?? '';
      expect(text).toContain('as of');
      // 30 minutes → "30m ago"
      expect(text).toContain('30m ago');
    });

    it('renders "as of unknown time" when timestamp is null', () => {
      render(<OfflineIndicator variant="stale-cached" timestamp={null} />);
      const text = screen.getByTestId('offline-indicator-stale').textContent ?? '';
      expect(text).toContain('unknown time');
    });

    it('sets title attribute for full timestamp', () => {
      render(<OfflineIndicator variant="stale-cached" timestamp={KNOWN_TS} />);
      const el = screen.getByTestId('offline-indicator-stale');
      expect(el.getAttribute('title')).toBeTruthy();
    });

    it('exposes role="status" for screen-reader live-region announcement', () => {
      render(<OfflineIndicator variant="stale-cached" timestamp={KNOWN_TS} />);
      const el = screen.getByTestId('offline-indicator-stale');
      expect(el.getAttribute('role')).toBe('status');
    });
  });

  describe('online-only variant', () => {
    it('renders with data-testid', () => {
      render(<OfflineIndicator variant="online-only" />);
      expect(screen.getByTestId('offline-indicator-online-only')).toBeTruthy();
    });

    it('includes "online-only" text', () => {
      render(<OfflineIndicator variant="online-only" />);
      expect(screen.getByTestId('offline-indicator-online-only').textContent).toContain('online-only');
    });

    it('prefixes label when provided', () => {
      render(<OfflineIndicator variant="online-only" label="Search" />);
      const text = screen.getByTestId('offline-indicator-online-only').textContent ?? '';
      expect(text).toContain('Search');
    });
  });
});
