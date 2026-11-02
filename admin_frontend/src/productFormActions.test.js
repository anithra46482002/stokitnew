import test from 'node:test';
import assert from 'node:assert/strict';
import { buildProductPayload, buildVariantPayload, describeProductDeleteError, describeSaveFailure, newSaveProgress, saveProductForm } from './productFormActions.js';
import { deleteRecord, pageAfterDelete } from './variantActions.js';

const httpError = (status, data) => Object.assign(new Error('Request failed with status code ' + status), { response: { status, data } });

const EDITING = { id: 3, name: 'pen', code: '1001', price: '10.00', mrp: '10.00', discount: '50.00', no_discount_banner: 'N', disable_variants: 'N', minimum_buying_count: '1', category_id: '1', subcategory_id: '1', visible: '0', highlight: false, stock: 'In Stock', gst_applicable: 'N', gst_percentage: '0.00', gst_price: '0.00', description: '', keywords: '', subtitle: '', brand: '', set_contains: '[]', discount_banners: ['uploads/a.webp'], image_url: 'http://x/y.png' };

// Fake axios-like client. `fail[key]` rejects the Nth call (1-based) of "METHOD url".
function fakeApi({ fail = {}, nextVariantId = 100 } = {}) {
  const calls = []; const counts = {};
  let variantId = nextVariantId;
  const run = (method, url, data) => {
    const key = method + ' ' + url; counts[key] = (counts[key] || 0) + 1; calls.push({ method, url, data });
    const failure = fail[key];
    if (failure && (failure.on === undefined || failure.on === counts[key])) return Promise.reject(failure.error);
    if (method === 'POST' && url === 'product-variants') return Promise.resolve({ status: 201, data: { id: variantId++ } });
    if (method === 'POST' && url === 'products') return Promise.resolve({ status: 201, data: { id: 77 } });
    return Promise.resolve({ status: 200, data: {} });
  };
  return {
    calls,
    request: (cfg) => run(cfg.method, cfg.url, cfg.data),
    patch: (url, data) => run('PATCH', url, data),
    post: (url, data) => run('POST', url, data),
    delete: (url, cfg) => run('DELETE', url, { params: cfg?.params, data: cfg?.data }),
  };
}
const uploads = []; const upload = async (file) => { uploads.push(file.name); return { image_path: 'uploads/' + file.name }; };

// ---------------------------------------------------------------- payload building
test('opening a product for edit and saving builds a typed PATCH body with the product id', () => {
  const { values, error } = buildProductPayload(EDITING);
  assert.equal(error, undefined);
  assert.equal(values.id, 3); assert.equal(values.price, 10); assert.equal(values.category_id, 1); assert.equal(values.subcategory_id, 1);
  assert.equal(values.minimum_buying_count, 1); assert.equal(values.minimum_buying_quantity, 1);
  assert.deepEqual(values.set_contains, []); assert.equal(values.code, '1001');
  assert.ok(!('image_url' in values) && !('discount_banners' in values) && !('color' in values), 'display-only/other-endpoint fields are not sent');
  assert.equal(values.gst_percentage, 0, 'product GST percentage is persisted as the default for new variants');
  assert.ok(!('gst_price' in values), 'product GST amount remains untouched; each variant owns its GST amount');
});

test('a legacy product with an empty code is saved without sending (and failing on) the empty code', () => {
  assert.ok(!('code' in buildProductPayload({ ...EDITING, code: '' }).values));
  assert.equal(buildProductPayload({ ...EDITING, id: undefined, code: '' }).values.code, '');
});

test('invalid input is rejected before any request is made', () => {
  assert.match(buildProductPayload({ ...EDITING, price: 'abc' }).error, /valid price/i);
  assert.match(buildProductPayload({ ...EDITING, price: '-5' }).error, /valid price/i);
  assert.match(buildProductPayload({ ...EDITING, price: '' }).error, /price is required/i);
  assert.match(buildProductPayload({ ...EDITING, name: '   ' }).error, /name is required/i);
  assert.match(buildProductPayload({ ...EDITING, minimum_buying_count: '0' }).error, /positive whole number/i);
  assert.match(buildProductPayload({ ...EDITING, set_contains: '{oops' }).error, /valid JSON/i);
  for (const gst_percentage of ['abc', '-1', '100.01', 'NaN', 'Infinity']) assert.match(buildProductPayload({ ...EDITING, gst_percentage }).error, /GST percentage.*finite number between 0 and 100/i);
  assert.equal(buildProductPayload({ ...EDITING, gst_percentage: '5' }).values.gst_percentage, 5);
});

test('MRP defaults to price when the Add form leaves it blank', () => {
  assert.equal(buildProductPayload({ ...EDITING, id: undefined, mrp: '', price: '25' }).values.mrp, 25);
});

test('uploaded banners and images are not uploaded twice when a later step fails and is retried', async () => {
  uploads.length = 0;
  const api = fakeApi({ fail: { 'POST product-images': { on: 1, error: httpError(500, 'x') } } });
  const progress = newSaveProgress(); const values = buildProductPayload(EDITING).values;
  const args = { values, bannerFiles: [{ name: 'b1.png' }], oldBannerPaths: ['uploads/a.webp'], productImageFiles: [{ name: 'i1.png' }], progress };
  await assert.rejects(saveProductForm(api, upload, args));
  await saveProductForm(api, upload, args);
  assert.deepEqual(uploads, ['b1.png', 'i1.png']);
  assert.equal(api.calls.filter((c) => c.url === 'product-images').length, 2, 'one failed request and one successful retry');
  assert.deepEqual(api.calls.find((c) => c.method === 'PATCH' && c.data.discount_banners).data.discount_banners, ['uploads/a.webp', 'uploads/b1.png']);
});

test('save failure text: nothing stored vs. partly stored, for server and network errors', () => {
  assert.match(describeSaveFailure(httpError(403, { detail: 'Role may not write' }), newSaveProgress(), false), /Role may not write.*Nothing was saved\./);
  assert.match(describeSaveFailure(new Error('Network Error'), newSaveProgress(), false), /Network Error.*Nothing was saved/);
  assert.match(describeSaveFailure(httpError(500, { detail: 'boom' }), { productId: 3 }, false), /changes were saved but the rest was not/);
});

// ---------------------------------------------------------------- delete
test('product delete sends one DELETE to products with the selected id and resolves only on success', async () => {
  const api = fakeApi();
  await deleteRecord(api, 'products', null, 4);
  assert.deepEqual(api.calls, [{ method: 'DELETE', url: 'products', data: { params: { id: 4 }, data: { id: 4 } } }]);
  const refused = fakeApi({ fail: { 'DELETE products': { error: httpError(409, { detail: 'x' }) } } });
  await assert.rejects(deleteRecord(refused, 'products', null, 4));
});

test('product delete errors: protected, missing, forbidden, server and network failures', () => {
  const protectedProduct = describeProductDeleteError(httpError(409, { detail: 'This product has order history and cannot be permanently deleted. Set Visibility to Hidden to archive it.' }), 'bangless (product ID 4)');
  assert.equal(protectedProduct.archivable, true); assert.match(protectedProduct.message, /order history.*Nothing was changed/);
  const gone = describeProductDeleteError(httpError(404, { detail: 'Product not found' }), 'x'); assert.equal(gone.gone, true); assert.match(gone.message, /no longer exists/);
  assert.match(describeProductDeleteError(httpError(403, { detail: 'Role may not write' }), 'x').message, /permission/);
  assert.match(describeProductDeleteError(httpError(500, { detail: 'db' }), 'x').message, /server error.*Nothing was removed/);
  const network = describeProductDeleteError(new Error('Network Error'), 'x');
  assert.match(network.message, /not deleted.*Network Error.*Nothing was removed/); assert.equal(network.archivable, false);
  for (const outcome of [gone, network]) assert.equal(outcome.archivable, false);
});

test('deleting the only product on a later page steps back; rows get distinct client keys', () => {
  assert.equal(pageAfterDelete(3, 1), 2); assert.equal(pageAfterDelete(1, 1), 1);
  // Product forms no longer create or manage variants; variant actions are tested on /variants.
});

// ---------------------------------------------------------------- variant payloads (GST percentage)
test('Add Product variant payload carries gst_percentage and the calculated gst_price', () => {
  const { payload, error } = buildVariantPayload({ name: ' Box ', price: '100', discount: '', quantity: '10', gst: 'Y', gst_percentage: '5', gst_price: '999.00' }, 77);
  assert.equal(error, undefined);
  assert.deepEqual(payload, { product_id: 77, name: 'Box', price: 100, mrp: 100, quantity: 10, discount: '0', gst: 'Y', minimum_buying_count: 1, gst_percentage: 5, gst_price: 5 });
});

test('variant payload amounts: 200 at 5% is 10, 100 at 18% is 18, decimals round to two places', () => {
  const amount = (price, gst_percentage, gst = 'Y') => buildVariantPayload({ name: 'v', price, quantity: '1', gst, gst_percentage }, 1).payload;
  assert.equal(amount('200', '5').gst_price, 10);
  assert.equal(amount('300', '5').gst_price, 15.00, '300 at 5% GST is 15.00');
  assert.equal(amount('100', '18').gst_price, 18);
  assert.equal(amount('33.33', '5').gst_price, 1.67);
  assert.equal(amount('10.10', '5').gst_price, 0.51);
});

test('variant payload: GST No sends amount 0 (percentage kept), blank percentage sends 0', () => {
  const off = buildVariantPayload({ name: 'v', price: '100', quantity: '1', gst: 'N', gst_percentage: '18', gst_price: '18.00' }, 1).payload;
  assert.equal(off.gst_price, 0); assert.equal(off.gst, 'N'); assert.equal(off.gst_percentage, 18);
  const blank = buildVariantPayload({ name: 'v', price: '100', quantity: '1', gst: 'Y', gst_percentage: '', gst_price: '5.00' }, 1).payload;
  assert.equal(blank.gst_percentage, 0); assert.equal(blank.gst_price, 0, 'a typed amount is never trusted over the percentage');
  assert.equal(buildVariantPayload({ name: 'v', price: '100', quantity: '1' }, 1).payload.gst, 'N');
});

test('variant payload: invalid, negative and over-100 percentages are refused before any request', () => {
  for (const bad of ['abc', '-1', '100.01', '150']) {
    const { payload, error } = buildVariantPayload({ name: 'Box', price: '100', quantity: '1', gst: 'Y', gst_percentage: bad }, 1);
    assert.equal(payload, undefined, bad); assert.match(error, /^Variant Box: GST percentage/, bad);
  }
  assert.equal(buildVariantPayload({ name: 'Box', price: '100', quantity: '1', gst: 'Y', gst_percentage: '100' }, 1).payload.gst_price, 100);
  assert.match(buildVariantPayload({ name: 'Box', price: '-1', quantity: '1', gst: 'N' }, 1).error, /non-negative/);
});

// ---------------------------------------------------------------- product + variant creation
const newProduct = () => buildProductPayload({ ...EDITING, id: undefined }).values;
const rows = () => [
  { name: 'Small', price: '100', quantity: '5', gst: 'Y', gst_percentage: '5' },
  { name: 'Large', price: '200', quantity: '5', gst: 'Y', gst_percentage: '5' },
  { name: 'Free', price: '50', quantity: '5', gst: 'N', gst_percentage: '18' },
];

test('creating a product then its variants sends the product first, then one variant request per row with its own GST', async () => {
  const api = fakeApi(); const progress = newSaveProgress();
  const result = await saveProductForm(api, upload, { values: newProduct(), variants: rows(), progress });
  assert.equal(result.productId, 77);
  assert.deepEqual(api.calls.map((c) => c.method + ' ' + c.url), ['POST products', 'POST product-variants', 'POST product-variants', 'POST product-variants']);
  const sent = api.calls.filter((c) => c.url === 'product-variants').map((c) => c.data);
  assert.ok(sent.every((v) => v.product_id === 77));
  assert.deepEqual(sent.map((v) => [v.name, v.gst_percentage, v.gst_price]), [['Small', 5, 5], ['Large', 5, 10], ['Free', 18, 0]]);
  assert.equal(api.calls[0].data.gst_percentage, 0, 'product-level GST percentage is sent with the product');
  assert.ok(!('gst_price' in api.calls[0].data), 'product GST amount is not overwritten by the form');
});

test('a variant rejected after the product was stored is retried without a second product or duplicate variants', async () => {
  const api = fakeApi({ fail: { 'POST product-variants': { on: 2, error: httpError(500, { detail: 'boom' }) } } });
  const progress = newSaveProgress(); const args = { values: newProduct(), variants: rows(), progress };
  await assert.rejects(saveProductForm(api, upload, args));
  assert.equal(progress.productId, 77); assert.equal(progress.variantsDone, 1);
  assert.match(describeSaveFailure(httpError(500, { detail: 'boom' }), progress, true), /product was created but the rest was not.*nothing will be duplicated/i);
  await saveProductForm(api, upload, args);
  const variantPosts = api.calls.filter((c) => c.method === 'POST' && c.url === 'product-variants');
  assert.equal(variantPosts.length, 4, 'Small once, Large twice (one rejected + one retried), Free once');
  assert.deepEqual(variantPosts.filter((_, i) => i !== 1).map((c) => c.data.name), ['Small', 'Large', 'Free'], 'each variant stored exactly once');
  const productRequests = api.calls.filter((c) => c.url === 'products');
  assert.equal(productRequests.filter((c) => c.method === 'POST').length, 1, 'the product is created once');
  assert.equal(productRequests[1].method, 'PATCH'); assert.equal(productRequests[1].data.id, 77, 'the retry updates the stored product');
  assert.equal(progress.variantsDone, 3);
});

test('an invalid variant percentage stops the save after the product, before that variant is requested, and a corrected retry finishes', async () => {
  const api = fakeApi(); const progress = newSaveProgress();
  const list = rows(); list[1] = { ...list[1], gst_percentage: '101' };
  const args = { values: newProduct(), variants: list, progress };
  await assert.rejects(saveProductForm(api, upload, args), /Variant Large: GST percentage cannot be more than 100/);
  assert.equal(api.calls.filter((c) => c.url === 'product-variants').length, 1, 'only the valid first variant was sent');
  list[1] = { ...list[1], gst_percentage: '5' };
  await saveProductForm(api, upload, args);
  assert.deepEqual(api.calls.filter((c) => c.method === 'POST' && c.url === 'product-variants').map((c) => c.data.name), ['Small', 'Large', 'Free']);
});

test('saving a product with no variants makes no variant request (existing flow unchanged)', async () => {
  const api = fakeApi();
  await saveProductForm(api, upload, { values: buildProductPayload(EDITING).values, progress: newSaveProgress() });
  assert.deepEqual(api.calls.map((c) => c.method + ' ' + c.url), ['PATCH products']);
});


test('editing variants PATCHes saved IDs and POSTs only rows without database IDs', async () => {
  const api = fakeApi(); const progress = newSaveProgress();
  const existing = { uid: 'saved-12', id: 12, product_id: 3, name: 'Small', price: '300', mrp: '350', discount: '5', quantity: '4', gst: 'Y', gst_percentage: '5', minimum_buying_count: '2' };
  const added = { uid: 'new-row', name: 'Large', price: '400', quantity: '6', gst: 'Y', gst_percentage: '5', minimum_buying_count: '1' };
  await saveProductForm(api, upload, { values: buildProductPayload(EDITING).values, variants: [existing, added], originalVariants: [existing], progress });
  assert.deepEqual(api.calls.filter((c) => c.url === 'product-variants').map((c) => [c.method, c.data.id, c.data.name]), [
    ['PATCH', 12, 'Small'], ['POST', undefined, 'Large'],
  ]);
  assert.equal(added.id, 100, 'confirmed POST ID is stored on the row immediately');
});

test('a successful new variant POST is not repeated when a later variant fails and save is retried', async () => {
  const api = fakeApi({ fail: { 'POST product-variants': { on: 2, error: httpError(500, { detail: 'boom' }) } } });
  const progress = newSaveProgress();
  const first = { uid: 'new-a', name: 'A', price: '100', quantity: '1', gst: 'Y', gst_percentage: '5' };
  const second = { uid: 'new-b', name: 'B', price: '200', quantity: '1', gst: 'Y', gst_percentage: '5' };
  const args = { values: buildProductPayload(EDITING).values, variants: [first, second], progress };
  await assert.rejects(saveProductForm(api, upload, args));
  assert.equal(first.id, 100);
  await saveProductForm(api, upload, args);
  assert.equal(api.calls.filter((c) => c.method === 'POST' && c.url === 'product-variants').length, 3, 'A once, B rejected once and retried once');
  assert.equal(api.calls.filter((c) => c.method === 'PATCH' && c.url === 'product-variants').length, 1, 'the confirmed new ID is patched on retry, not POSTed again');
});

test('removed original variants are deleted once and a refused delete reports the variant', async () => {
  const original = [{ id: 21, name: 'Old', uid: 'saved-21' }];
  const api = fakeApi(); const progress = newSaveProgress();
  await saveProductForm(api, upload, { values: buildProductPayload(EDITING).values, variants: [], originalVariants: original, progress });
  assert.equal(api.calls.filter((c) => c.method === 'DELETE' && c.url === 'product-variants').length, 1);
  await saveProductForm(api, upload, { values: buildProductPayload(EDITING).values, variants: [], originalVariants: original, progress });
  assert.equal(api.calls.filter((c) => c.method === 'DELETE' && c.url === 'product-variants').length, 1, 'confirmed delete is not repeated on retry');

  const blocked = fakeApi({ fail: { 'DELETE product-variants': { error: httpError(409, { detail: 'Variant has stock history' }) } } });
  await assert.rejects(saveProductForm(blocked, upload, { values: buildProductPayload(EDITING).values, variants: [], originalVariants: original, progress: newSaveProgress() }), /Could not delete variant.*Old.*stock history/i);
});


test('a newly created variant removed after a partial failure is deleted by its confirmed ID', async () => {
  const api = fakeApi({ fail: { 'POST product-variants': { on: 2, error: httpError(500, { detail: 'boom' }) } } });
  const progress = newSaveProgress();
  const first = { uid: 'created-then-removed', name: 'Temporary', price: '100', quantity: '1', gst: 'N', gst_percentage: '0' };
  const second = { uid: 'still-needed', name: 'Keep', price: '200', quantity: '1', gst: 'N', gst_percentage: '0' };
  const args = { values: newProduct(), variants: [first, second], progress };
  await assert.rejects(saveProductForm(api, upload, args));
  assert.equal(first.id, 100);
  args.variants = [second];
  await saveProductForm(api, upload, args);
  assert.equal(api.calls.filter((c) => c.method === 'DELETE' && c.url === 'product-variants' && c.data.data.id === 100).length, 1);
  assert.equal(api.calls.filter((c) => c.method === 'POST' && c.url === 'product-variants' && c.data.name === 'Temporary').length, 1);
});
