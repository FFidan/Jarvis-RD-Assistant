import { useState } from 'react';
import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { TypedConfirmDialog } from './TypedConfirmDialog';

const REQUIRED = 'RESTORE';

function StaticDialog({ onConfirm }: { onConfirm?: () => void }) {
  return (
    <TypedConfirmDialog
      requiredWord={REQUIRED}
      title="Confirm restore"
      description="This overwrites current data."
      confirmLabel="Restore"
      onConfirm={onConfirm ?? (() => {})}
      open
      onOpenChange={() => {}}
    />
  );
}

function ToggleableDialog({ onConfirm }: { onConfirm?: () => void }) {
  const [open, setOpen] = useState(true);
  return (
    <>
      <button onClick={() => setOpen(true)}>open</button>
      <TypedConfirmDialog
        requiredWord={REQUIRED}
        title="Confirm restore"
        description="This overwrites current data."
        confirmLabel="Restore"
        onConfirm={onConfirm ?? (() => {})}
        open={open}
        onOpenChange={setOpen}
      />
    </>
  );
}

const confirmButton = () => screen.getByRole('button', { name: /^restore$/i });
const typedInput = () => screen.getByLabelText(`Type ${REQUIRED} to confirm`);

describe('TypedConfirmDialog', () => {
  it('disables confirm before anything is typed', () => {
    render(<StaticDialog />);
    expect(confirmButton()).toBeDisabled();
  });

  it('keeps confirm disabled while the typed value does not match', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<StaticDialog />);

    await user.type(typedInput(), 'RESTOR');

    expect(confirmButton()).toBeDisabled();
  });

  it('enables confirm on an exact match and fires onConfirm when clicked', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    const onConfirm = vi.fn();
    render(<StaticDialog onConfirm={onConfirm} />);

    await user.type(typedInput(), REQUIRED);
    expect(confirmButton()).toBeEnabled();

    await user.click(confirmButton());
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });

  it('clears the input when closed and reopened', async () => {
    const user = userEvent.setup({ pointerEventsCheck: 0 });
    render(<ToggleableDialog />);

    await user.type(typedInput(), REQUIRED);
    expect(confirmButton()).toBeEnabled();

    await user.click(screen.getByRole('button', { name: /^cancel$/i }));
    await user.click(screen.getByRole('button', { name: /^open$/i }));

    expect(typedInput()).toHaveValue('');
    expect(confirmButton()).toBeDisabled();
  });
});
