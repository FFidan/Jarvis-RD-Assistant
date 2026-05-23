/**
 * SourceConfigPanel — admin-only inline panel for source credentials and cooldown resets.
 * Extracted from PulseSection.tsx.
 */
import { useState } from 'react';
import { patchSourceConfig, clearSourceCooldown } from '@/lib/api';
import { Button } from '@/components/ui/button';
import { Label } from '@/components/ui/label';
import { Loader2 } from 'lucide-react';
import { toast } from 'sonner';

interface SourceConfigPanelProps {
  isAdmin: boolean;
  onArxivCooldownCleared: () => void;
}

export function SourceConfigPanel({ isAdmin, onArxivCooldownCleared }: SourceConfigPanelProps) {
  const [openAlexEmail, setOpenAlexEmail] = useState('');
  const [s2ApiKey, setS2ApiKey] = useState('');
  const [savingOpenAlex, setSavingOpenAlex] = useState(false);
  const [savingS2, setSavingS2] = useState(false);
  const [clearingArxiv, setClearingArxiv] = useState(false);

  if (!isAdmin) return null;

  const handleSaveOpenAlex = async () => {
    if (!openAlexEmail.trim()) return;
    setSavingOpenAlex(true);
    try {
      await patchSourceConfig('openalex', { email: openAlexEmail.trim() });
      toast.success('OpenAlex email saved.');
      setOpenAlexEmail('');
    } catch {
      toast.error('Failed to save OpenAlex email.');
    } finally {
      setSavingOpenAlex(false);
    }
  };

  const handleSaveS2 = async () => {
    if (!s2ApiKey.trim()) return;
    setSavingS2(true);
    try {
      await patchSourceConfig('semantic_scholar', { api_key: s2ApiKey.trim() });
      toast.success('Semantic Scholar API key saved.');
      setS2ApiKey('');
    } catch {
      toast.error('Failed to save Semantic Scholar API key.');
    } finally {
      setSavingS2(false);
    }
  };

  const handleClearArxiv = async () => {
    setClearingArxiv(true);
    try {
      await clearSourceCooldown('arxiv');
      toast.success('ArXiv cooldown cleared. It will retry on the next Pulse run.');
      onArxivCooldownCleared();
    } catch {
      toast.error('Failed to clear ArXiv cooldown.');
    } finally {
      setClearingArxiv(false);
    }
  };

  return (
    <div className="space-y-4 border-t pt-4">
      <div>
        <h4 className="text-sm font-medium">Source settings</h4>
        <p className="text-xs text-muted-foreground">
          Configure source credentials and reset cooldowns when a source gets temporarily blocked.
        </p>
      </div>

      {/* OpenAlex email */}
      <div className="space-y-1.5">
        <Label className="text-xs font-medium">OpenAlex contact email</Label>
        <p className="text-xs text-muted-foreground">
          OpenAlex asks for a contact email for reliable access — no account or signup required.
          Without it you may hit stricter rate limits.
        </p>
        <div className="flex gap-2">
          <input
            type="email"
            aria-label="OpenAlex contact email"
            placeholder="your@email.com"
            value={openAlexEmail}
            onChange={(e) => setOpenAlexEmail(e.target.value)}
            className="flex-1 rounded-md border bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <Button
            size="sm"
            variant="outline"
            onClick={() => void handleSaveOpenAlex()}
            disabled={savingOpenAlex || !openAlexEmail.trim()}
          >
            {savingOpenAlex ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Save'}
          </Button>
        </div>
      </div>

      {/* Semantic Scholar API key */}
      <div className="space-y-1.5">
        <Label className="text-xs font-medium">Semantic Scholar API key</Label>
        <p className="text-xs text-muted-foreground">
          An API key increases your rate limit with Semantic Scholar. Get one free at{' '}
          <span className="font-mono text-[11px]">semanticscholar.org/product/api</span>.
        </p>
        <div className="flex gap-2">
          <input
            type="password"
            aria-label="Semantic Scholar API key"
            placeholder="sk-..."
            value={s2ApiKey}
            onChange={(e) => setS2ApiKey(e.target.value)}
            className="flex-1 rounded-md border bg-background px-3 py-1.5 text-sm focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring"
          />
          <Button
            size="sm"
            variant="outline"
            onClick={() => void handleSaveS2()}
            disabled={savingS2 || !s2ApiKey.trim()}
          >
            {savingS2 ? <Loader2 className="h-3 w-3 animate-spin" /> : 'Save'}
          </Button>
        </div>
      </div>

      {/* ArXiv cooldown reset */}
      <div className="space-y-1.5">
        <Label className="text-xs font-medium">ArXiv rate-limit reset</Label>
        <p className="text-xs text-muted-foreground">
          If ArXiv is showing as rate-limited in diagnostics, use this to clear the cooldown and
          let Pulse retry immediately on the next run.
        </p>
        <Button
          size="sm"
          variant="outline"
          onClick={() => void handleClearArxiv()}
          disabled={clearingArxiv}
        >
          {clearingArxiv ? (
            <span className="flex items-center gap-2">
              <Loader2 className="h-3 w-3 animate-spin" />
              Clearing…
            </span>
          ) : (
            'Clear ArXiv cooldown'
          )}
        </Button>
      </div>
    </div>
  );
}
