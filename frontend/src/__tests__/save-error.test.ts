import { describe, expect, it, vi } from 'vitest';
import { toast } from 'sonner';
import { onSaveError } from '@/lib/forms/save-error';

vi.mock('sonner', () => ({ toast: { error: vi.fn() } }));

describe('onSaveError', () => {
  it('shows the server message when there is one', () => {
    onSaveError('Could not save')(new Error('Key rejected by provider'));
    expect(toast.error).toHaveBeenCalledWith('Key rejected by provider');
  });

  it('falls back to the caller message for a non-Error rejection', () => {
    onSaveError('Could not save the author')(null);
    expect(toast.error).toHaveBeenCalledWith('Could not save the author');
  });

  it('falls back to the caller message when the error carries no text', () => {
    onSaveError('Could not save the topic')(new Error(''));
    expect(toast.error).toHaveBeenCalledWith('Could not save the topic');
  });
});
