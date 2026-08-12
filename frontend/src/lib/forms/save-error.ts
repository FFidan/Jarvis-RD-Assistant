import { toast } from 'sonner';
import { errorMessage } from '@/lib/errors';

/**
 * The Settings tree's one save-failure reaction.
 *
 * A rejected save must never leave the previous value on screen in silence.
 * Errors that belong beside a specific field — where the control also has to
 * roll back — stay inline instead; see NumCtxSlider.
 */
export function onSaveError(fallback: string) {
  // errorMessage returns e.message for ANY Error, including an empty one, so
  // its own fallback never fires for a rejection that carries no text.
  return (error: unknown) => toast.error(errorMessage(error, fallback) || fallback);
}
