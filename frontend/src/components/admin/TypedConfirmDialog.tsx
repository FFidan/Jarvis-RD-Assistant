import { useEffect, useState, type ReactNode } from 'react';
import {
  AlertDialog,
  AlertDialogAction,
  AlertDialogCancel,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from '@/components/ui/alert-dialog';
import { Input } from '@/components/ui/input';

interface TypedConfirmDialogProps {
  requiredWord: string;
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  onConfirm: () => void;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}

/**
 * Destructive-action dialog whose confirm button stays disabled until the user
 * types {@link TypedConfirmDialogProps.requiredWord} verbatim. Generic on purpose
 * (the required word is a prop) so it is reusable beyond restore.
 */
export function TypedConfirmDialog({
  requiredWord,
  title,
  description,
  confirmLabel = 'Confirm',
  onConfirm,
  open,
  onOpenChange,
}: TypedConfirmDialogProps) {
  const [value, setValue] = useState('');

  // A reopened dialog must start empty so a prior match can't carry over.
  useEffect(() => {
    if (!open) setValue('');
  }, [open]);

  const confirmEnabled = value === requiredWord;

  return (
    <AlertDialog open={open} onOpenChange={onOpenChange}>
      <AlertDialogContent>
        <AlertDialogHeader>
          <AlertDialogTitle>{title}</AlertDialogTitle>
          <AlertDialogDescription>{description}</AlertDialogDescription>
        </AlertDialogHeader>
        <Input
          aria-label={`Type ${requiredWord} to confirm`}
          placeholder={requiredWord}
          autoComplete="off"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <AlertDialogFooter>
          <AlertDialogCancel>Cancel</AlertDialogCancel>
          <AlertDialogAction disabled={!confirmEnabled} onClick={onConfirm}>
            {confirmLabel}
          </AlertDialogAction>
        </AlertDialogFooter>
      </AlertDialogContent>
    </AlertDialog>
  );
}
