import { pathToFileURL } from 'node:url';
import path from 'node:path';
const core = path.join(process.env.TEMP || process.env.TMP, 'stokit-playwright', 'node_modules', 'playwright-core', 'index.mjs');
const { chromium } = await import(pathToFileURL(core).href);
const browser = await chromium.launch({ executablePath: 'C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe', headless: true });
const page = await browser.newPage();
const png = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p1sAAAAASUVORK5CYII=', 'base64');
const requests = [];
const webp = Buffer.from('UklGRjYAAABXRUJQVlA4ICoAAACwAQCdASoBAAEAAUAgJZACdLoABGaAAP7paR/4ILaj0w1L/8Buvi2XwAA=', 'base64');
let protectedImageAuth = false;
let protectedImageContentType = '';
const rows = [{ id: 1, username: 'existing_driver', email: 'existing@example.invalid', full_name: 'Existing Driver', phone: '9876543210', address: 'Kochi', joining_date: null, status: 'ACTIVE', has_profile_image: false, profile_image_version: '', created_at: '2026-10-01T10:00:00Z', updated_at: null }];
await page.addInitScript(() => { sessionStorage.setItem('stokit_admin_token', 'e2e-fake-token'); sessionStorage.setItem('stokit_admin_user', JSON.stringify({ id: 1, username: 'e2e', role: 'SUPER_ADMIN' })); });
await page.route('**/admin-api/**', async (route) => {
  const req = route.request(); const url = new URL(req.url()); const method = req.method();
  if (method === 'OPTIONS') return route.fulfill({ status: 200, headers: { 'access-control-allow-origin': '*', 'access-control-allow-headers': '*', 'access-control-allow-methods': '*' } });
  if (url.pathname.endsWith('/delivery-boy-image')) { protectedImageAuth = req.headers()['authorization'] === 'Bearer e2e-fake-token'; protectedImageContentType = 'image/webp'; return route.fulfill({ status: 200, contentType: 'image/webp', body: webp, headers: { 'cache-control': 'private, no-cache' } }); }
  if (url.pathname.endsWith('/staff')) {
    if (method === 'GET') { const search = (url.searchParams.get('search') || '').toLowerCase(); const filtered = rows.filter((row) => !search || [row.username, row.email, row.full_name].some((value) => String(value || '').toLowerCase().includes(search))); return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ count: filtered.length, page: Number(url.searchParams.get('page') || 1), page_size: 20, results: filtered }) }); }
    const body = req.postData() || ''; const contentType = req.headers()['content-type'] || '';
    requests.push({ method, body, contentType });
    if (!contentType.includes('multipart/form-data; boundary=')) return route.fulfill({ status: 415, contentType: 'application/json', body: JSON.stringify({ detail: 'Expected multipart/form-data' }) });
    if (method === 'POST') {
      const row = { id: 90001, username: 'ui_profile_test', email: 'ui_profile_test@example.invalid', full_name: 'UI Profile Test', phone: '9876543210', address: 'Kochi', joining_date: '2026-10-12', status: 'ACTIVE', has_profile_image: body.includes('name="profile_image"'), profile_image_version: '2026-10-30T10:00:00Z', created_at: '2026-10-30T10:00:00Z', updated_at: '2026-10-30T10:00:00Z' };
      rows.push(row); return route.fulfill({ status: 201, contentType: 'application/json', body: JSON.stringify({ ...row, kind: 'delivery' }) });
    }
    const row = rows.find((item) => item.id === 90001);
    if (body.includes('name="remove_profile_image"')) row.has_profile_image = false;
    if (body.includes('name="profile_image"')) row.has_profile_image = true;
    if (body.includes('name="joining_date"')) row.joining_date = '2026-10-12';
    if (body.includes('name="status"')) row.status = body.includes('INACTIVE') ? 'INACTIVE' : 'ACTIVE';
    row.profile_image_version = '2026-10-30T10:15:00Z'; row.updated_at = row.profile_image_version;
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ ...row, kind: 'delivery' }) });
  }
  return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ profile: {} }) });
});
const check = (label, condition) => { if (!condition) throw new Error('FAIL: ' + label); console.log('PASS: ' + label); };
try {
  await page.goto('http://127.0.0.1:5173/delivery');
  await page.getByRole('heading', { name: 'Delivery Boy' }).waitFor({ timeout: 15000 });
  await page.getByRole('table').waitFor({ timeout: 15000 });
  check('Delivery Boy list screen renders profile image column', await page.getByRole('columnheader', { name: 'Profile Image' }).count() === 1);
  await page.getByRole('button', { name: 'Add new' }).click();
  await page.getByRole('heading', { name: 'Create Delivery Boy' }).waitFor();
  check('Add form contains Joining Date and Profile Image', await page.getByLabel('Joining Date').count() === 1 && await page.locator('input[type=file][aria-label="Profile Image"]').count() === 1);
  const password = page.getByRole('textbox', { name: 'Password (10+ characters)' });
  const confirmation = page.getByRole('textbox', { name: 'Confirm Password' });
  check('Both password fields start hidden', await password.getAttribute('type') === 'password' && await confirmation.getAttribute('type') === 'password');
  await password.fill('ProfilePassword123'); await confirmation.fill('ProfilePassword123');
  await page.getByRole('button', { name: 'Show Password (10+ characters)' }).click();
  check('Password eye toggles only Password and retains value', await password.getAttribute('type') === 'text' && await confirmation.getAttribute('type') === 'password' && await password.inputValue() === 'ProfilePassword123');
  await page.getByRole('button', { name: 'Show Confirm Password' }).click();
  check('Confirm Password eye toggles independently and retains value', await confirmation.getAttribute('type') === 'text' && await confirmation.inputValue() === 'ProfilePassword123');
  await page.getByRole('button', { name: 'Hide Password (10+ characters)' }).click();
  await page.getByRole('button', { name: 'Hide Confirm Password' }).click();
  check('Both password fields can be hidden independently again', await password.getAttribute('type') === 'password' && await confirmation.getAttribute('type') === 'password');
  await page.getByLabel('Username').fill('ui_profile_test');
  await page.getByLabel('Email Address').fill('ui_profile_test@example.invalid');
  await page.getByLabel('Full Name').fill('UI Profile Test');
  await page.getByLabel('Phone Number').fill('9876543210');
  await page.getByLabel('Address', { exact: true }).fill('Kochi');
  await page.getByLabel('Joining Date').fill('2026-10-12');
  await page.locator('input[type=file][aria-label="Profile Image"]').setInputFiles({ name: 'driver.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('img', { name: 'Selected profile image preview' }).waitFor();
  await confirmation.fill('NotSameValue123');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await page.getByRole('dialog').getByRole('alert').waitFor();
  check('Password confirmation mismatch is blocked before any upload request', (await page.getByRole('dialog').getByRole('alert').innerText()).includes('Passwords do not match') && requests.length === 0);
  await confirmation.fill('ProfilePassword123');
  await page.getByRole('button', { name: 'Save changes' }).click();
  await page.getByRole('status').filter({ hasText: 'Saved successfully.' }).waitFor();
  check('Create request is multipart and includes image plus Joining Date', requests.some((r) => r.method === 'POST' && r.contentType.includes('multipart/form-data; boundary=') && r.body.includes('name="profile_image"') && r.body.includes('name="joining_date"') && r.body.includes('2026-10-12')));
  let row = page.locator('tbody tr').filter({ hasText: 'ui_profile_test' }); await row.waitFor();
  await page.locator('img.delivery-boy-image-thumb').waitFor();
  check('Saved image is rendered in the list through protected image endpoint', await page.locator('img.delivery-boy-image-thumb').count() >= 1);
  check('Protected WebP image response renders and request carries the admin bearer token', protectedImageAuth && protectedImageContentType === 'image/webp' && await page.locator('img.delivery-boy-image-thumb').evaluate((img) => img.naturalWidth === 1));
  await row.getByRole('button', { name: 'Edit' }).click();
  await page.getByRole('heading', { name: 'Edit Delivery Boy' }).waitFor();
  check('Edit form restores Joining Date', await page.getByLabel('Joining Date').inputValue() === '2026-10-12');
  await page.getByRole('img', { name: 'Current saved profile image' }).waitFor();
  check('Edit form displays the existing saved image', await page.getByRole('img', { name: 'Current saved profile image' }).count() === 1);
  await page.locator('input[type=file][aria-label="Profile Image"]').setInputFiles({ name: 'replacement.png', mimeType: 'image/png', buffer: png });
  await page.getByRole('img', { name: 'Selected profile image preview' }).waitFor();
  await page.getByRole('button', { name: 'Save changes' }).click();
  await page.getByRole('status').filter({ hasText: 'Saved successfully.' }).waitFor();
  check('Image replacement is sent as multipart PATCH', requests.some((r) => r.method === 'PATCH' && r.body.includes('name="profile_image"') && !r.body.includes('name="remove_profile_image"')));
  row = page.locator('tbody tr').filter({ hasText: 'ui_profile_test' });
  await row.getByRole('button', { name: 'Edit' }).click();
  await page.getByRole('button', { name: 'Remove saved image' }).click();
  check('Image removal requires an explicit action', await page.getByText('Saved image will be removed when you save.').count() === 1);
  await page.getByRole('button', { name: 'Save changes' }).click();
  await page.getByRole('status').filter({ hasText: 'Saved successfully.' }).waitFor();
  check('Image removal sends explicit multipart removal flag', requests.some((r) => r.method === 'PATCH' && r.body.includes('name="remove_profile_image"') && r.body.includes('true')));
  await page.getByLabel('Search records').fill('ui_profile_test');
  await row.waitFor();
  check('Delivery Boy search continues to work', await page.locator('tbody tr').count() === 1);
  console.log('UI RESULT: 16 checks passed. API responses were controlled stubs; no real DB image was written.');
} finally { await browser.close(); }
