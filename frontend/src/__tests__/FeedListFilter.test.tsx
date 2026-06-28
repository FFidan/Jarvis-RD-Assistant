/**
 * FeedListFilter component tests — Feed IA Redesign
 *
 * This is a scoped list-filter, NOT intent-routing and NOT global ⌘K search.
 * It filters title/author within the current faceted view.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { FeedListFilter } from '@/components/feed/FeedListFilter';

describe('FeedListFilter', () => {
  const onChange = vi.fn();

  beforeEach(() => {
    onChange.mockClear();
  });

  it('renders input with default placeholder', () => {
    render(<FeedListFilter value="" onChange={onChange} />);
    expect(screen.getByPlaceholderText('Filter by title or author…')).toBeInTheDocument();
  });

  it('renders with custom placeholder', () => {
    render(
      <FeedListFilter value="" onChange={onChange} placeholder="Filter inbox by title or author…" />,
    );
    expect(screen.getByPlaceholderText('Filter inbox by title or author…')).toBeInTheDocument();
  });

  it('has a visually hidden label for accessibility', () => {
    render(<FeedListFilter value="" onChange={onChange} />);
    // The input should have an accessible label
    const input = screen.getByRole('searchbox');
    expect(input).toBeInTheDocument();
  });

  it('calls onChange when input value changes', () => {
    render(<FeedListFilter value="" onChange={onChange} />);
    const input = screen.getByRole('searchbox');
    fireEvent.change(input, { target: { value: 'neural' } });
    expect(onChange).toHaveBeenCalledWith('neural');
  });

  it('shows clear button when value is non-empty', () => {
    render(<FeedListFilter value="neural" onChange={onChange} />);
    expect(screen.getByRole('button', { name: /clear filter/i })).toBeInTheDocument();
  });

  it('does not show clear button when value is empty', () => {
    render(<FeedListFilter value="" onChange={onChange} />);
    expect(screen.queryByRole('button', { name: /clear filter/i })).not.toBeInTheDocument();
  });

  it('calls onChange with empty string when clear button is clicked', () => {
    render(<FeedListFilter value="neural" onChange={onChange} />);
    fireEvent.click(screen.getByRole('button', { name: /clear filter/i }));
    expect(onChange).toHaveBeenCalledWith('');
  });

  it('has data-testid="feed-list-filter"', () => {
    render(<FeedListFilter value="" onChange={onChange} />);
    expect(screen.getByTestId('feed-list-filter')).toBeInTheDocument();
  });
});
