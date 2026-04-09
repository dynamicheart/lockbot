/**
 * Demo mode detection.
 *
 * Activates when:
 *  1. VITE_DEMO_MODE env var is 'true' at build time, OR
 *  2. ?demo query parameter is present in the URL at runtime.
 *
 * When demo mode is active, the mock API adapter replaces the real
 * axios instance so the frontend works without a backend.
 */
export const isDemoMode =
  import.meta.env.VITE_DEMO_MODE === 'true' ||
  new URLSearchParams(window.location.search).has('demo')
