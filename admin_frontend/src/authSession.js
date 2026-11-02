const TOKEN_KEY = 'stokit_admin_token';
const USER_KEY = 'stokit_admin_user';

const VALID_ROLES = new Set([
  'SUPER_ADMIN',
  'ADMIN',
  'SALESMAN',
  'WAREHOUSE_STAFF',
  'DELIVERY_BOY',
]);

export function validateLoginResponse(data) {
  if (!data || typeof data.token !== 'string' || !data.token.trim()) {
    throw new Error(data?.message || 'The login response did not include a valid token.');
  }
  if (!data.user || typeof data.user !== 'object' || Array.isArray(data.user)) {
    throw new Error('The login response did not include user information.');
  }
  if (typeof data.user.role !== 'string' || !VALID_ROLES.has(data.user.role)) {
    throw new Error('This account has no active admin role assigned. Contact a Super Admin.');
  }
  return { token: data.token, user: data.user };
}

export function clearAdminSession(storage = globalThis.sessionStorage) {
  try {
    storage?.removeItem(TOKEN_KEY);
    storage?.removeItem(USER_KEY);
  } catch {
    // Storage may be blocked by browser privacy settings; callers still fail closed.
  }
}

export function persistAdminSession(data, storage = globalThis.sessionStorage) {
  const session = validateLoginResponse(data);
  try {
    if (!storage) throw new Error('Session storage is unavailable.');
    storage.setItem(TOKEN_KEY, session.token);
    storage.setItem(USER_KEY, JSON.stringify(session.user));
    if (storage.getItem(TOKEN_KEY) !== session.token ||
        storage.getItem(USER_KEY) !== JSON.stringify(session.user)) {
      throw new Error('Session storage verification failed.');
    }
  } catch {
    clearAdminSession(storage);
    throw new Error('Could not save the login session in this browser. Enable session storage and try again.');
  }
  return session;
}

// Pure routing decision, evaluated on EVERY render (never cache it inside a route element).
// Returns the path to redirect to, or null when the visitor may stay on `pathname`.
export function guardRedirect(pathname, session) {
  // /forgot-password is a public page next to /login: reachable while signed out, pointless while signed in.
  const onLogin = pathname === '/login' || pathname.startsWith('/login/');
  const onForgotPassword = pathname === '/forgot-password' || pathname.startsWith('/forgot-password/');
  if (!session && !onLogin && !onForgotPassword) return '/login';
  // Authenticated users may intentionally open Forgot Password from their profile/security section.
  if (session && onLogin) return '/';
  return null;
}

// After a successful password change the server revokes older tokens and returns a fresh one for THIS browser.
// Only replace the stored token when the server actually supplied one; never sign the user out here.
export function replaceAdminToken(token, storage = globalThis.sessionStorage) {
  if (typeof token !== 'string' || !token.trim()) return false;
  try {
    storage?.setItem(TOKEN_KEY, token);
    return storage?.getItem(TOKEN_KEY) === token;
  } catch {
    return false;
  }
}
export function getAdminSession(storage = globalThis.sessionStorage) {
  try {
    const token = storage?.getItem(TOKEN_KEY);
    const rawUser = storage?.getItem(USER_KEY);
    if (!token || !rawUser) {
      clearAdminSession(storage);
      return null;
    }
    const user = JSON.parse(rawUser);
    return validateLoginResponse({ token, user });
  } catch {
    clearAdminSession(storage);
    return null;
  }
}

export function updateAdminSessionProfile(patch, storage = globalThis.sessionStorage) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch)) return false;
  try {
    const raw = storage?.getItem(USER_KEY);
    if (!raw) return false;
    const current = JSON.parse(raw);
    if (!current || typeof current !== 'object' || Array.isArray(current)) return false;
    const next = { ...current };
    for (const key of ['username', 'email', 'fullname']) {
      if (typeof patch[key] === 'string') next[key] = patch[key];
    }
    storage?.setItem(USER_KEY, JSON.stringify(next));
    return storage?.getItem(USER_KEY) === JSON.stringify(next);
  } catch {
    return false;
  }
}
