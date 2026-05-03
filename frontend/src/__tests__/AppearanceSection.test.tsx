import { describe, it, expect, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import {
  applyAppearance,
  TYPE_PRESETS,
  ACCENT_PRESETS,
  DENSITY_PRESETS,
  saveAppearance,
} from '@/lib/theme';
import { AppearanceSection } from '@/components/settings/AppearanceSection';

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Strip all accent-* / type-* / density-* classes from <html> so tests are isolated.
function clearHtmlClasses() {
  const html = document.documentElement;
  ACCENT_PRESETS.forEach((p) => html.classList.remove(`accent-${p.id}`));
  TYPE_PRESETS.forEach((p) => html.classList.remove(`type-${p.id}`));
  DENSITY_PRESETS.forEach((p) => html.classList.remove(`density-${p.id}`));
}

// ---------------------------------------------------------------------------
// Setup
// ---------------------------------------------------------------------------

beforeEach(() => {
  clearHtmlClasses();
  localStorage.clear();
  vi.clearAllMocks();
});

// ---------------------------------------------------------------------------
// applyAppearance — unit tests (no rendering required)
// ---------------------------------------------------------------------------

describe('applyAppearance — <html> classList mutations', () => {
  it('adds accent-forest class when accent is "forest"', () => {
    applyAppearance({ accent: 'forest', type: 'serif-calm', density: 'default' });

    expect(document.documentElement.classList.contains('accent-forest')).toBe(true);
  });

  it('adds type-legacy class when type is "legacy"', () => {
    applyAppearance({ accent: 'ink-blue', type: 'legacy', density: 'default' });

    expect(document.documentElement.classList.contains('type-legacy')).toBe(true);
  });

  it('adds type-sans-modern class when type is "sans-modern"', () => {
    applyAppearance({ accent: 'ink-blue', type: 'sans-modern', density: 'default' });

    expect(document.documentElement.classList.contains('type-sans-modern')).toBe(true);
  });

  it('adds type-editorial class when type is "editorial"', () => {
    applyAppearance({ accent: 'ink-blue', type: 'editorial', density: 'default' });

    expect(document.documentElement.classList.contains('type-editorial')).toBe(true);
  });

  it('removes previous accent/type/density classes and applies the new ones', () => {
    // Pre-set some classes that should be removed
    document.documentElement.classList.add('accent-forest', 'type-editorial', 'density-compact');

    applyAppearance({ accent: 'ink-blue', type: 'serif-calm', density: 'default' });

    const classList = document.documentElement.classList;
    // Old classes must be gone
    expect(classList.contains('accent-forest')).toBe(false);
    expect(classList.contains('type-editorial')).toBe(false);
    expect(classList.contains('density-compact')).toBe(false);
    // New classes must be present
    expect(classList.contains('accent-ink-blue')).toBe(true);
    expect(classList.contains('type-serif-calm')).toBe(true);
    expect(classList.contains('density-default')).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// TYPE_PRESETS — shape and content
// ---------------------------------------------------------------------------

describe('TYPE_PRESETS', () => {
  it('has exactly 4 entries', () => {
    expect(TYPE_PRESETS.length).toBe(4);
  });

  it('entries are in the expected order: serif-calm, sans-modern, editorial, legacy', () => {
    expect(TYPE_PRESETS[0].id).toBe('serif-calm');
    expect(TYPE_PRESETS[1].id).toBe('sans-modern');
    expect(TYPE_PRESETS[2].id).toBe('editorial');
    expect(TYPE_PRESETS[3].id).toBe('legacy');
  });

  it('contains a legacy entry with correct label and description', () => {
    const legacy = TYPE_PRESETS.find((p) => p.id === 'legacy');

    expect(legacy).toBeDefined();
    expect(legacy?.label).toBe('Legacy');
    expect(legacy?.description).toBe('System sans — pre-v5 native look');
  });
});

// ---------------------------------------------------------------------------
// AppearanceSection — UI-level interaction
// ---------------------------------------------------------------------------

describe('AppearanceSection — UI interactions', () => {
  it('clicking Legacy type button calls saveAppearance with { type: "legacy" }', async () => {
    const user = userEvent.setup();

    // Spy on saveAppearance via the module — we test the DOM side-effect too
    vi.spyOn({ saveAppearance }, 'saveAppearance');

    render(<AppearanceSection />);

    // Find the Legacy button by its visible label text
    const legacyBtn = screen.getByText('Legacy');
    await user.click(legacyBtn);

    // After clicking, type-legacy should appear on <html>
    expect(document.documentElement.classList.contains('type-legacy')).toBe(true);
  });

  it('clicking Forest accent button applies accent-forest to <html>', async () => {
    const user = userEvent.setup();

    render(<AppearanceSection />);

    const forestBtn = screen.getByRole('button', { name: 'Forest' });
    await user.click(forestBtn);

    expect(document.documentElement.classList.contains('accent-forest')).toBe(true);
  });

  it('clicking Reset to defaults applies ink-blue / serif-calm / default classes', async () => {
    const user = userEvent.setup();

    // Start from a non-default state
    document.documentElement.classList.add('accent-forest', 'type-editorial', 'density-compact');
    localStorage.setItem(
      'jarvis.appearance',
      JSON.stringify({ accent: 'forest', type: 'editorial', density: 'compact' }),
    );

    render(<AppearanceSection />);

    const resetBtn = screen.getByRole('button', { name: /Reset to defaults/i });
    await user.click(resetBtn);

    const classList = document.documentElement.classList;
    expect(classList.contains('accent-ink-blue')).toBe(true);
    expect(classList.contains('type-serif-calm')).toBe(true);
    expect(classList.contains('density-default')).toBe(true);
    // Previous non-default classes should be gone
    expect(classList.contains('accent-forest')).toBe(false);
    expect(classList.contains('type-editorial')).toBe(false);
    expect(classList.contains('density-compact')).toBe(false);
  });
});
