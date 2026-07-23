import type { ReactElement } from 'react';
import { describe, it, expect, vi, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { RouteErrorBoundary } from './RouteErrorBoundary';

function Thrower(): never {
  throw new Error('boom');
}

describe('RouteErrorBoundary', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('renders the fallback UI and logs the error with its component stack', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {});

    render(
      <RouteErrorBoundary>
        <Thrower />
      </RouteErrorBoundary>,
    );

    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    const call = spy.mock.calls.find((args) => args[0] === 'RouteErrorBoundary caught:');
    expect(call).toBeDefined();
    expect(call?.[1]).toBeInstanceOf(Error);
    expect(call?.[1]).toMatchObject({ message: 'boom' });
    expect(call?.[2]).toMatchObject({ componentStack: expect.any(String) });
  });

  it('resets on retry and renders children again', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {});
    let shouldThrow = true;
    function MaybeThrow(): ReactElement {
      if (shouldThrow) throw new Error('boom');
      return <div>recovered</div>;
    }

    render(
      <RouteErrorBoundary>
        <MaybeThrow />
      </RouteErrorBoundary>,
    );

    expect(screen.getByText('Something went wrong')).toBeInTheDocument();
    shouldThrow = false;
    fireEvent.click(screen.getByText('Try again'));
    expect(screen.getByText('recovered')).toBeInTheDocument();
  });
});
