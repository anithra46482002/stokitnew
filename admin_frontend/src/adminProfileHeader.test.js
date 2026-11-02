import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8');
const css = readFileSync(new URL('./App.css', import.meta.url), 'utf8');
const profile = readFileSync(new URL('./ProfilePage.jsx', import.meta.url), 'utf8');
const session = readFileSync(new URL('./authSession.js', import.meta.url), 'utf8');

test('avatar, authenticated username, and role share one accessible link to /profile', () => {
  const header = app.match(/<NavLink\s+className="topbar-profile-link"[\s\S]*?<\/NavLink>/)?.[0];
  assert.ok(header, 'header profile link exists');
  assert.match(header, /to="\/profile"/);
  assert.match(header, /aria-label="Open admin profile"/);
  assert.ok(header.includes('event.key === " "') && header.includes('event.currentTarget.click()'), 'Space key activates the profile link');
  assert.match(header, /className="user-avatar"/);
  assert.match(header, /user\?\.username/);
  assert.match(header, /user\?\.role/);
  assert.match(header, /profileImageUrl \? <img className="topbar-profile-avatar-image" src=\{profileImageUrl\}/);
  assert.match(app, /api\.get\("profile"\)/);
  assert.match(app, /stokit-profile-image-updated/);
  assert.doesNotMatch(header, /superadmin|SUPER_ADMIN/);
});

test('logout remains a separate button and keeps its existing session-clear/login behavior', () => {
  const topbar = app.match(/<div className="topbar-user">[\s\S]*?<\/header>/)?.[0];
  assert.ok(topbar, 'topbar user section exists');
  assert.match(topbar, /<\/NavLink>\s*<button[\s\S]*?clearAdminSession\(\)[\s\S]*?window\.location\.href = "\/login"/);
  assert.match(topbar, />\s*Log out\s*<\/button>/);
  assert.doesNotMatch(topbar.match(/<NavLink[\s\S]*?<\/NavLink>/)?.[0] || '', /clearAdminSession|Log out/);
});

test('profile route uses the existing page inside the authenticated guarded application routes', () => {
  assert.match(app, /<Route path="\/profile" element=\{<ProfilePage \/>\} \/>/);
  assert.match(app, /function Guarded\(\{ children \}\)[\s\S]*?guardRedirect\(location\.pathname, getAdminSession\(\)\)/);
  assert.match(app, /path="\*"[\s\S]*?<Guarded>[\s\S]*?<Home \/>[\s\S]*?<\/Guarded>/);
  assert.match(profile, /api\.get\("profile"\)/);
  assert.match(session, /guardRedirect/);
});

test('profile link has visible hover/focus treatment and stays usable on mobile', () => {
  assert.match(css, /\.topbar-user \.topbar-profile-link:hover/);
  assert.match(css, /\.topbar-user \.topbar-profile-link:focus-visible/);
  assert.match(css, /@media \(max-width: 760px\)[\s\S]*?\.topbar-user \.topbar-profile-link \.user-avatar \{ display: grid/);
  assert.match(css, /\.topbar-user \.topbar-profile-link \.user-details \{ display: block/);
});
