// Polyfill for AbortSignal.any — must be imported before any React/app code.
import '@/lib/abort-signal-polyfill';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppProviders } from '@/providers/AppProviders';
import { App } from '@/App';
import { registerServiceWorker } from '@/lib/pwa';
import '@/index.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <AppProviders>
      <App />
    </AppProviders>
  </StrictMode>,
);

// PWA foundation: register the offline service worker + capture
// the install affordance. No-ops in dev / unsupported / insecure contexts.
registerServiceWorker();
