/**
 * ConnectivityBanner tests
 *
 * Coverage:
 *  - OfflineBanner: hidden when online, shown when offline
 *  - InstallAffordance: hidden when canInstall=false, shown when canInstall=true,
 *    dismissible, calls promptInstall on button click
 *  - onInstallAvailabilityChange subscription wires correctly
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, act } from '@testing-library/react';
import { ConnectivityBanner } from '@/components/layout/ConnectivityBanner';

// ---------------------------------------------------------------------------
// Mocks
// ---------------------------------------------------------------------------

let _online = true;
let _canInstall = false;
let _installListeners: Array<(v: boolean) => void> = [];

vi.mock('@/hooks/use-online-status', () => ({
  useOnlineStatus: () => ({ online: _online }),
}));

vi.mock('@/lib/pwa', () => ({
  canInstall: () => _canInstall,
  promptInstall: vi.fn().mockResolvedValue('accepted'),
  onInstallAvailabilityChange: vi.fn((cb: (v: boolean) => void) => {
    _installListeners.push(cb);
    return () => {
      _installListeners = _installListeners.filter((l) => l !== cb);
    };
  }),
}));

import { promptInstall } from '@/lib/pwa';
const mockPromptInstall = vi.mocked(promptInstall);

beforeEach(() => {
  _online = true;
  _canInstall = false;
  _installListeners = [];
  mockPromptInstall.mockResolvedValue('accepted');
  localStorage.clear();
});

afterEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
});

// ---------------------------------------------------------------------------
// OfflineBanner tests
// ---------------------------------------------------------------------------

describe('OfflineBanner (inside ConnectivityBanner)', () => {
  it('is not rendered when online', () => {
    _online = true;
    render(<ConnectivityBanner />);
    expect(screen.queryByTestId('connectivity-banner-offline')).toBeNull();
  });

  it('renders with offline copy when offline', () => {
    _online = false;
    render(<ConnectivityBanner />);
    const banner = screen.getByTestId('connectivity-banner-offline');
    expect(banner).toBeTruthy();
    // Check core copy is present (partial match)
    expect(banner.textContent).toContain("offline");
    expect(banner.textContent).toContain("saved data");
  });

  it('has role=status and aria-live=polite', () => {
    _online = false;
    render(<ConnectivityBanner />);
    const banner = screen.getByTestId('connectivity-banner-offline');
    expect(banner.getAttribute('role')).toBe('status');
    expect(banner.getAttribute('aria-live')).toBe('polite');
  });
});

// ---------------------------------------------------------------------------
// InstallAffordance tests
// ---------------------------------------------------------------------------

describe('InstallAffordance (inside ConnectivityBanner)', () => {
  it('is not rendered when canInstall returns false', () => {
    _canInstall = false;
    render(<ConnectivityBanner />);
    expect(screen.queryByTestId('install-affordance')).toBeNull();
  });

  it('renders when canInstall returns true', () => {
    _canInstall = true;
    render(<ConnectivityBanner />);
    expect(screen.getByTestId('install-affordance')).toBeTruthy();
  });

  it('calls promptInstall when Install button clicked', async () => {
    _canInstall = true;
    render(<ConnectivityBanner />);
    const btn = screen.getByTestId('install-affordance-button');
    await act(async () => { fireEvent.click(btn); });
    expect(mockPromptInstall).toHaveBeenCalledTimes(1);
  });

  it('is dismissed when X button clicked', () => {
    _canInstall = true;
    render(<ConnectivityBanner />);
    expect(screen.getByTestId('install-affordance')).toBeTruthy();
    fireEvent.click(screen.getByTestId('install-affordance-dismiss'));
    expect(screen.queryByTestId('install-affordance')).toBeNull();
  });

  it('hides when onInstallAvailabilityChange fires false', () => {
    _canInstall = true;
    render(<ConnectivityBanner />);
    expect(screen.getByTestId('install-affordance')).toBeTruthy();
    // Simulate browser reporting app already installed → availability → false
    act(() => {
      _installListeners.forEach((cb) => cb(false));
    });
    expect(screen.queryByTestId('install-affordance')).toBeNull();
  });

  it('appears when onInstallAvailabilityChange fires true (late prompt)', () => {
    _canInstall = false;
    render(<ConnectivityBanner />);
    expect(screen.queryByTestId('install-affordance')).toBeNull();
    act(() => {
      _installListeners.forEach((cb) => cb(true));
    });
    expect(screen.getByTestId('install-affordance')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// Install banner localStorage persistence
// ---------------------------------------------------------------------------

describe('InstallAffordance — localStorage persistence', () => {
  it('dismiss writes the localStorage key', () => {
    _canInstall = true;
    render(<ConnectivityBanner />);
    expect(screen.getByTestId('install-affordance')).toBeTruthy();
    fireEvent.click(screen.getByTestId('install-affordance-dismiss'));
    expect(localStorage.getItem('jarvis.install-banner-dismissed')).toBe('1');
  });

  it('banner stays hidden on remount when localStorage key is set', () => {
    localStorage.setItem('jarvis.install-banner-dismissed', '1');
    _canInstall = true;
    const { unmount } = render(<ConnectivityBanner />);
    expect(screen.queryByTestId('install-affordance')).toBeNull();
    unmount();
    render(<ConnectivityBanner />);
    expect(screen.queryByTestId('install-affordance')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// Combined: offline + install both active
// ---------------------------------------------------------------------------

describe('ConnectivityBanner combined', () => {
  it('shows both offline banner and install affordance simultaneously', () => {
    _online = false;
    _canInstall = true;
    render(<ConnectivityBanner />);
    expect(screen.getByTestId('connectivity-banner-offline')).toBeTruthy();
    expect(screen.getByTestId('install-affordance')).toBeTruthy();
  });

  it('shows nothing when online and not installable', () => {
    _online = true;
    _canInstall = false;
    const { container } = render(<ConnectivityBanner />);
    // Container should be nearly empty (just the React fragment wrapper)
    expect(container.children[0]?.children.length ?? 0).toBe(0);
  });
});
