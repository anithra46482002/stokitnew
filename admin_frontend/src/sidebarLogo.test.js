import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync } from 'node:fs';

const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8');
const css = readFileSync(new URL('./App.css', import.meta.url), 'utf8');
const logoUrl = new URL('../public/stokit-logo.png', import.meta.url);

test('the sidebar logo is a real PNG in public/ so Vite and the Django-served build both expose it at the root', () => {
  assert.ok(existsSync(logoUrl), 'public/stokit-logo.png exists');
  const png = readFileSync(logoUrl);
  assert.deepEqual([...png.subarray(0, 8)], [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a], 'PNG signature');
  assert.ok(png.readUInt32BE(16) >= 104 && png.readUInt32BE(20) >= 104, 'at least 2x the 52px display size');
  assert.equal(png.readUInt32BE(16), png.readUInt32BE(20), 'square, so it is never stretched');
});

test('the brand row renders the logo image beside the Stokit / Admin Panel text', () => {
  assert.match(app, /className="brand-logo"/);
  assert.match(app, /src=\{`\$\{import\.meta\.env\.BASE_URL\}stokit-logo\.png`\}/);
  assert.match(app, /<h2>Stokit<\/h2>\s*<p>Admin Panel<\/p>/);
  assert.ok(!/className="brand-mark"/.test(app), 'the placeholder "S" tile is gone');
});

test('the logo is 52x52 with 16px rounded corners and cannot be stretched or squeezed', () => {
  const rule = css.match(/\.brand-logo\s*\{([^}]*)\}/);
  assert.ok(rule, '.brand-logo rule exists');
  assert.match(rule[1], /width:\s*52px/);
  assert.match(rule[1], /height:\s*52px/);
  assert.match(rule[1], /border-radius:\s*16px/);
  assert.match(rule[1], /object-fit:\s*contain/);
  assert.match(rule[1], /flex:\s*0 0 52px/);
});
