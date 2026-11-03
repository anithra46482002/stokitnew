import test from 'node:test';
import assert from 'node:assert/strict';
import { existsSync, readFileSync, readdirSync } from 'node:fs';

const src = (name) => readFileSync(new URL(`./${name}`, import.meta.url), 'utf8');
const html = readFileSync(new URL('../index.html', import.meta.url), 'utf8');
const pages = readdirSync(new URL('./', import.meta.url)).filter((f) => f.endsWith('.jsx'));

// ---- favicon ---------------------------------------------------------------------------------------------------
test('favicon points at the official StokIt favicon PNG in public/, with a cache-busting version and the exact Stokit_Admin title', () => {
  const icon = html.match(/<link rel="icon"[^>]*href="([^"]+)"/);
  assert.ok(icon, 'an icon link exists');
  assert.match(icon[1], /^\/stokit-favicon\.png\?v=\d+$/);
  assert.ok(existsSync(new URL('../public/stokit-favicon.png', import.meta.url)), 'the referenced file exists in public/');
  assert.match(html, /<title>Stokit_Admin<\/title>/);
  assert.ok(!/favicon\.svg|vite\.svg/.test(html), 'no default Vite favicon reference');
  assert.ok(!existsSync(new URL('../public/favicon.svg', import.meta.url)), 'the default Vite favicon is not shipped');
});

// ---- empty state -----------------------------------------------------------------------------------------------
test('the shared empty state says exactly "No records"', () => {
  const component = src('EmptyState.jsx');
  assert.match(component, /const EMPTY_MESSAGE = 'No records';/);
  assert.match(component, /export function EmptyState\(/);
  assert.match(component, /export function EmptyRow\(/);
});

test('no page still renders the technical or per-page empty wording', () => {
  for (const file of pages) {
    const text = src(file);
    assert.ok(!/No records returned by this API/.test(text), `${file}: old API wording`);
    assert.ok(!/No (banners|categories|sub categories|products|offers|targets|assignments|notifications|allocations|orders) (yet|found|match|have)/.test(text), `${file}: per-page empty wording`);
  }
});

test('every empty-state render sits behind a loading and an error guard, so failures are never shown as "No records found"', () => {
  const guarded = {
    'BannersPage.jsx': '!loading && !error && rows.length === 0 && <EmptyState />',
    'CategoriesPage.jsx': '!loading && !error && rows.length === 0 && <EmptyRow',
    'SubCategoriesPage.jsx': '!loading && !error && rows.length === 0 && <EmptyRow',
    'ProductsPage.jsx': '!loading && !error && rows.length === 0 && <EmptyRow',
    'CustomersPage.jsx': '!loading && !error && rows.length === 0 && <EmptyState />',
    'NotificationsPage.jsx': '!loading && !error && rows.length === 0 && <EmptyState />',
    'OrdersPage.jsx': '!loading && !error && orders.length === 0 && <EmptyState />',
    'AdvancePaymentsPage.jsx': 'loaded && !error && rows.length === 0 && <EmptyState />',
    'ManageAllocationsPage.jsx': 'loaded && !busy && !error && rows.length === 0 && <EmptyState />',
    'TargetSchemesPage.jsx': '!loading && !error && visibleTargets.length === 0 && <EmptyRow',
  };
  for (const [file, needle] of Object.entries(guarded)) assert.ok(src(file).includes(needle), `${file} guards the empty state`);
  const app = src('App.jsx');
  assert.ok(app.includes('{data && rows.length === 0 && !totals && ('), 'generic list page only shows it after data loaded');
  assert.match(app, /<EmptyState \/>/);
  // the dashboard returns early for loading and for errors, so its two section empties are success-only
  const dashboard = src('DashboardPage.jsx');
  assert.ok(dashboard.indexOf('if (loading) return') < dashboard.indexOf('<EmptyRow') && dashboard.indexOf('if (error) return') < dashboard.indexOf('<EmptyRow'));
});

test('the loaded flag only turns true after a successful response', () => {
  assert.match(src('AdvancePaymentsPage.jsx'), /setRows\(response\.data\.results \|\| \[\]\); setLoaded\(true\);/);
  assert.match(src('ManageAllocationsPage.jsx'), /setRows\(a\.data\.results \|\| \[\]\); setLoaded\(true\);/);
});

// ---- cart discount ---------------------------------------------------------------------------------------------
test('Cart Discount no longer renders the Saved configuration box but keeps the editable form and the save call', () => {
  const app = src('App.jsx');
  const start = app.indexOf('function CartDiscountPage');
  const page = app.slice(start, app.indexOf('function Home()', start));
  assert.ok(!/Saved configuration/.test(page));
  assert.ok(!/setConfig|const \[config/.test(page), 'the display-only state is gone');
  assert.match(page, /api\.get\("cart-discount-settings"\)/);
  assert.match(page, /api\.put\("cart-discount-settings"/);
  assert.match(page, /Enable cart discount/);
  assert.match(page, /Minimum eligible subtotal/);
  assert.match(page, /Fixed cart discount amount/);
  assert.match(page, /Save Changes/);
  assert.match(page, /setForm\(\{ enabled: Boolean\(response\.data\.enabled\), minimum_subtotal: String\(response\.data\.minimum_subtotal \?\? ""\)/, 'saved values still load into the fields');
});
