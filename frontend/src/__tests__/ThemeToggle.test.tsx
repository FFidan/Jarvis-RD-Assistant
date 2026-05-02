import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { ThemeToggle } from '@/components/layout/ThemeToggle';
import { useThemeStore } from '@/stores/theme-store';

describe('ThemeToggle', () => {
  beforeEach(() => {
    // Reset to a known state before each test
    useThemeStore.setState({ theme: 'light' });
  });

  it('renders Sun icon and correct aria-label when theme is light', () => {
    useThemeStore.setState({ theme: 'light' });
    const { container } = render(<ThemeToggle />);

    const button = screen.getByRole('button', { name: 'Toggle theme: currently light' });
    expect(button).toBeInTheDocument();
    expect(container.querySelector('svg.lucide-sun')).not.toBeNull();
    expect(container.querySelector('svg.lucide-moon')).toBeNull();
    expect(container.querySelector('svg.lucide-monitor')).toBeNull();
  });

  it('renders Moon icon and correct aria-label when theme is dark', () => {
    useThemeStore.setState({ theme: 'dark' });
    const { container } = render(<ThemeToggle />);

    const button = screen.getByRole('button', { name: 'Toggle theme: currently dark' });
    expect(button).toBeInTheDocument();
    expect(container.querySelector('svg.lucide-moon')).not.toBeNull();
    expect(container.querySelector('svg.lucide-sun')).toBeNull();
    expect(container.querySelector('svg.lucide-monitor')).toBeNull();
  });

  it('renders Monitor icon and correct aria-label when theme is system', () => {
    useThemeStore.setState({ theme: 'system' });
    const { container } = render(<ThemeToggle />);

    const button = screen.getByRole('button', { name: 'Toggle theme: currently system' });
    expect(button).toBeInTheDocument();
    expect(container.querySelector('svg.lucide-monitor')).not.toBeNull();
    expect(container.querySelector('svg.lucide-sun')).toBeNull();
    expect(container.querySelector('svg.lucide-moon')).toBeNull();
  });

  it('click cycles theme from light to dark', async () => {
    useThemeStore.setState({ theme: 'light' });
    render(<ThemeToggle />);

    const button = screen.getByRole('button', { name: 'Toggle theme: currently light' });
    await userEvent.click(button);

    expect(useThemeStore.getState().theme).toBe('dark');
  });

  it('click cycles theme from dark to system', async () => {
    useThemeStore.setState({ theme: 'dark' });
    render(<ThemeToggle />);

    const button = screen.getByRole('button', { name: 'Toggle theme: currently dark' });
    await userEvent.click(button);

    expect(useThemeStore.getState().theme).toBe('system');
  });

  it('click cycles theme from system to light', async () => {
    useThemeStore.setState({ theme: 'system' });
    render(<ThemeToggle />);

    const button = screen.getByRole('button', { name: 'Toggle theme: currently system' });
    await userEvent.click(button);

    expect(useThemeStore.getState().theme).toBe('light');
  });
});
