import { render, screen } from '@testing-library/react';
import { fireEvent } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { PaginationControls } from '@/components/feed/PaginationControls';
import type { PageSize } from '@/components/feed/PaginationControls';

const noop = vi.fn();

function renderPagination(overrides: {
  offset?: number;
  limit?: PageSize;
  total?: number;
  onChange?: (offset: number, limit: PageSize) => void;
} = {}) {
  const { offset = 0, limit = 30, total = 90, onChange = noop } = overrides;
  return render(
    <PaginationControls offset={offset} limit={limit} total={total} onChange={onChange} />,
  );
}

describe('PaginationControls', () => {
  it('renders "Page 1 of 3" for offset=0 limit=30 total=90', () => {
    renderPagination({ offset: 0, limit: 30, total: 90 });
    expect(screen.getByText(/Page 1 of 3/)).toBeInTheDocument();
  });

  it('disables Prev button on first page', () => {
    renderPagination({ offset: 0, limit: 30, total: 90 });
    expect(screen.getByLabelText('Previous page')).toBeDisabled();
  });

  it('disables Next button on last page', () => {
    renderPagination({ offset: 60, limit: 30, total: 90 });
    expect(screen.getByLabelText('Next page')).toBeDisabled();
  });

  it('calls onChange with offset+limit when Next is clicked', () => {
    const onChange = vi.fn();
    renderPagination({ offset: 0, limit: 30, total: 90, onChange });
    fireEvent.click(screen.getByLabelText('Next page'));
    expect(onChange).toHaveBeenCalledWith(30, 30);
  });

  it('calls onChange with offset-limit when Prev is clicked', () => {
    const onChange = vi.fn();
    renderPagination({ offset: 30, limit: 30, total: 90, onChange });
    fireEvent.click(screen.getByLabelText('Previous page'));
    expect(onChange).toHaveBeenCalledWith(0, 30);
  });

  it('resets offset to 0 when page size changes', () => {
    const onChange = vi.fn();
    renderPagination({ offset: 60, limit: 30, total: 90, onChange });
    // The Select trigger shows current value
    const trigger = screen.getByRole('combobox');
    fireEvent.click(trigger);
    // Select the "10" option
    const option = screen.getByRole('option', { name: '10' });
    fireEvent.click(option);
    expect(onChange).toHaveBeenCalledWith(0, 10);
  });

  it('shows total count in parentheses', () => {
    renderPagination({ offset: 0, limit: 30, total: 90 });
    expect(screen.getByText(/\(90 total\)/)).toBeInTheDocument();
  });

  it('renders page 2 of 3 for offset=30', () => {
    renderPagination({ offset: 30, limit: 30, total: 90 });
    expect(screen.getByText(/Page 2 of 3/)).toBeInTheDocument();
  });
});
