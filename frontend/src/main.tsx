// Polyfill for AbortSignal.any — must be imported before any React/app code.
import '@/lib/abort-signal-polyfill';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { AppProviders } from '@/providers/AppProviders';
import { App } from '@/App';
import { registerServiceWorker, requestPersistentStorage } from '@/lib/pwa';
import { useAuthStore } from '@/stores/auth-store';
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

// Ask the browser to protect the offline cache from storage-pressure
// eviction once an identity is established — covers a fresh login, a
// cookie-rehydrated tab, and a page load that resumes an already-persisted
// session. requestPersistentStorage() is itself idempotent (only the first
// call does anything), so checking the current state and subscribing to
// future changes can never double-fire the real request.
function requestPersistIfAuthed(): void {
  if (useAuthStore.getState().isAuthenticated) {
    requestPersistentStorage();
  }
}
requestPersistIfAuthed();
useAuthStore.subscribe(requestPersistIfAuthed);
