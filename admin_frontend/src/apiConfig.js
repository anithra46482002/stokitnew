// Pure helpers for the API client (no import.meta / DOM) so they are unit-testable.
// When the admin app is served by Django on the same origin as the API (API base like "/admin-api/"), requests are
// same-origin, so the ngrok free-tier browser-warning bypass header can be sent without triggering a CORS preflight.
// For a cross-origin API base (e.g. http://127.0.0.1:8000/admin-api/ in development) no extra header is added.
export const NGROK_SKIP_WARNING_HEADER = 'ngrok-skip-browser-warning';

export function isSameOriginBase(apiBase) {
  return typeof apiBase === 'string' && apiBase.startsWith('/') && !apiBase.startsWith('//');
}

export function extraHeadersFor(apiBase) {
  return isSameOriginBase(apiBase) ? { [NGROK_SKIP_WARNING_HEADER]: '1' } : {};
}
