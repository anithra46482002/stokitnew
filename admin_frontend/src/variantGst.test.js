import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { applyProductGstDefault, blankVariant, calcGstPrice, gstPercentageError, gstPriceFor, liveVariantGstPrice, resolveSavedVariantGst, updateVariantField } from './variantGst.js';

// ---------------------------------------------------------------- calcGstPrice
test('price 100 at 5% gives GST price 5.00', () => assert.equal(calcGstPrice('100', '5'), '5.00'));
test('price 200 at 5% gives GST price 10.00', () => assert.equal(calcGstPrice('200', '5'), '10.00'));
test('price 100 at 18% gives GST price 18.00', () => assert.equal(calcGstPrice('100', '18'), '18.00'));

test('numbers and text give the same answer', () => {
  assert.equal(calcGstPrice(100, 5), '5.00');
  assert.equal(calcGstPrice(' 100 ', ' 5 '), '5.00');
});

test('decimal prices round half-up to two decimals without floating point drift', () => {
  assert.equal(calcGstPrice('33.33', '5'), '1.67');   // 1.6665 -> 1.67
  assert.equal(calcGstPrice('10.10', '5'), '0.51');   // 0.505 -> 0.51 (floating point would give 0.50)
  assert.equal(calcGstPrice('1.15', '10'), '0.12');   // 0.115 -> 0.12
  assert.equal(calcGstPrice('99.99', '18'), '18.00'); // 17.9982
  assert.equal(calcGstPrice('0.01', '5'), '0.00');
  assert.equal(calcGstPrice('199.99', '12.5'), '25.00'); // 24.99875
  assert.equal(calcGstPrice('100', '2.5'), '2.50');
  assert.equal(calcGstPrice('.5', '50'), '0.25');
  assert.equal(calcGstPrice('100.', '5'), '5.00');
});

test('the result always has exactly two decimals', () => {
  for (const [price, pct] of [['1', '1'], ['100', '100'], ['7', '3'], ['1234567.89', '18'], ['0', '18']]) {
    assert.match(calcGstPrice(price, pct), /^\d+\.\d{2}$/, price + ' @ ' + pct);
  }
  assert.equal(calcGstPrice('100', '100'), '100.00');
  assert.equal(calcGstPrice('1234567.89', '18'), '222222.22'); // 222222.2202
});

test('zero price or zero percentage gives 0.00', () => {
  assert.equal(calcGstPrice('0', '18'), '0.00');
  assert.equal(calcGstPrice('100', '0'), '0.00');
});

test('empty, invalid, negative and over-100 percentages never produce NaN or a GST amount', () => {
  for (const bad of ['', '   ', null, undefined, 'abc', '5%', '1e1', '--5', '-5', '-0.01', '100.01', '101', '1000', NaN, Infinity, -Infinity, true, {}, []]) {
    assert.equal(calcGstPrice('100', bad), '0.00', 'percentage ' + String(bad));
  }
  assert.equal(calcGstPrice('100', '100'), '100.00', '100 is allowed');
  assert.equal(calcGstPrice('100', '100.00'), '100.00');
});

test('empty, invalid and negative prices give 0.00', () => {
  for (const bad of ['', null, undefined, 'abc', '-1', NaN, Infinity]) assert.equal(calcGstPrice(bad, '5'), '0.00', 'price ' + String(bad));
});

// ---------------------------------------------------------------- gstPercentageError
test('percentage validation: blank is fine (treated as 0), 0 to 100 is fine, everything else has a message', () => {
  for (const ok of ['', '  ', null, undefined, '0', '0.0', '5', '18', '12.5', '100', '100.00', 7]) assert.equal(gstPercentageError(ok), '', String(ok));
  for (const bad of ['abc', '-1', '-0.5', '1e2', '5%', NaN, Infinity]) assert.match(gstPercentageError(bad), /number from 0 to 100/, String(bad));
  for (const high of ['100.01', '101', '1000']) assert.match(gstPercentageError(high), /more than 100/, high);
});

// ---------------------------------------------------------------- GST Applicable
test('GST Applicable No forces GST price 0.00, whatever the price and percentage are', () => {
  assert.equal(gstPriceFor({ gst: 'N', price: '100', gst_percentage: '18' }), '0.00');
  assert.equal(gstPriceFor({ price: '100', gst_percentage: '18' }), '0.00', 'missing flag means No');
  assert.equal(gstPriceFor({ gst: 'n', price: '100', gst_percentage: '18' }), '0.00');
  assert.equal(gstPriceFor({ gst: 'Y', price: '100', gst_percentage: '18' }), '18.00');
  assert.equal(gstPriceFor({ gst: 'y', price: '100', gst_percentage: '18' }), '18.00');
  assert.equal(gstPriceFor(undefined), '0.00');
});

// ---------------------------------------------------------------- Add Product rows
test('a new row starts with GST off, no default percentage and GST price 0.00', () => {
  const row = blankVariant();
  assert.equal(row.gst, 'N'); assert.equal(row.gst_percentage, ''); assert.equal(row.gst_price, '0.00');
  assert.notEqual(blankVariant().uid, blankVariant().uid, 'rows get distinct keys');
});

test('editing price, GST Applicable or percentage recalculates the GST price at once', () => {
  let row = { ...blankVariant(), name: 'A', price: '100', gst: 'Y', gst_percentage: '' };
  row = updateVariantField(row, 'gst_percentage', '5'); assert.equal(row.gst_price, '5.00');
  row = updateVariantField(row, 'price', '200'); assert.equal(row.gst_price, '10.00');
  row = updateVariantField(row, 'gst_percentage', '18'); assert.equal(row.gst_price, '36.00');
  row = updateVariantField(row, 'gst', 'N'); assert.equal(row.gst_price, '0.00');
  row = updateVariantField(row, 'gst', 'Y'); assert.equal(row.gst_price, '36.00');
  row = updateVariantField(row, 'gst_percentage', '101'); assert.equal(row.gst_price, '0.00', 'invalid percentage never leaves a stale amount');
  row = updateVariantField(row, 'gst_percentage', ''); assert.equal(row.gst_price, '0.00');
});

test('editing a field that is not a GST input leaves the GST price alone and does not mutate the row', () => {
  const row = { name: 'A', price: '100', gst: 'Y', gst_percentage: '5', gst_price: '5.00' };
  const next = updateVariantField(row, 'name', 'B');
  assert.equal(next.name, 'B'); assert.equal(next.gst_price, '5.00'); assert.equal(row.name, 'A');
  assert.deepEqual(updateVariantField(row, 'quantity', '3').gst_price, '5.00');
});

test('several variants calculate independently of each other', () => {
  let rows = [
    { ...blankVariant(), price: '100', gst: 'Y', gst_percentage: '5' },
    { ...blankVariant(), price: '200', gst: 'Y', gst_percentage: '5' },
    { ...blankVariant(), price: '100', gst: 'Y', gst_percentage: '18' },
    { ...blankVariant(), price: '100', gst: 'N', gst_percentage: '18' },
  ].map((row) => ({ ...row, gst_price: gstPriceFor(row) }));
  assert.deepEqual(rows.map((r) => r.gst_price), ['5.00', '10.00', '18.00', '0.00']);
  const edit = (uid, key, value) => { rows = rows.map((r) => (r.uid === uid ? updateVariantField(r, key, value) : r)); };
  edit(rows[0].uid, 'gst_percentage', '12');
  assert.deepEqual(rows.map((r) => r.gst_price), ['12.00', '10.00', '18.00', '0.00'], 'only the edited row changed');
  edit(rows[3].uid, 'gst', 'Y');
  assert.deepEqual(rows.map((r) => r.gst_price), ['12.00', '10.00', '18.00', '18.00']);
});

test('new variants inherit the product GST default and calculate independently', () => {
  const small = { ...blankVariant('5'), name: 'Small', price: '100' };
  const large = { ...blankVariant('5'), name: 'Large', price: '200' };
  assert.equal(small.gst, 'Y'); assert.equal(small.gst_percentage, '5');
  assert.equal(gstPriceFor(small), '5.00'); assert.equal(gstPriceFor(large), '10.00');
});

test('product default changes update only non-overridden unsaved variants', () => {
  const inherited = { ...blankVariant('5'), price: '100' };
  const customized = updateVariantField({ ...blankVariant('5'), price: '100' }, 'gst_percentage', '12');
  assert.equal(applyProductGstDefault(inherited, '18').gst_percentage, '18');
  assert.equal(applyProductGstDefault(inherited, '18').gst_price, '18.00');
  assert.equal(applyProductGstDefault(customized, '18').gst_percentage, '12');
  const gstOff = updateVariantField(inherited, 'gst', 'N');
  assert.equal(applyProductGstDefault(gstOff, '18').gst, 'N', 'explicit GST-off choice is preserved');
});

// ---------------------------------------------------------------- standalone Variants page
test('live form: the amount follows price and percentage as they change', () => {
  const values = { gst: 'Y', price: '100', gst_percentage: '5' };
  assert.equal(liveVariantGstPrice(values, '5.00'), '5.00');
  assert.equal(liveVariantGstPrice({ ...values, price: '200' }, '5.00'), '10.00');
  assert.equal(liveVariantGstPrice({ ...values, gst_percentage: '18' }, '5.00'), '18.00');
  assert.equal(liveVariantGstPrice({ ...values, gst_percentage: '' }, '5.00'), '0.00', 'clearing a saved percentage does not leave the old amount showing');
  assert.equal(liveVariantGstPrice({ ...values, gst_percentage: '150' }, '5.00'), '0.00');
});

test('live form: GST Applicable No always shows 0.00, even for a legacy variant', () => {
  assert.equal(liveVariantGstPrice({ gst: 'N', price: '100', gst_percentage: '18' }, '18.00'), '0.00');
  assert.equal(liveVariantGstPrice({ gst: 'N', price: '100', gst_percentage: '' }, '0.00'), '0.00');
});

test('live form: a legacy variant at 0% keeps its existing amount until a percentage is entered', () => {
  for (const original of ['0', '0.00', 0, '', null, undefined]) {
    assert.equal(liveVariantGstPrice({ gst: 'Y', price: '100', gst_percentage: '0.00', gst_price: '7.00' }, original), null, 'price edit keeps the legacy amount: ' + String(original));
  }
  assert.equal(liveVariantGstPrice({ gst: 'Y', price: '100', gst_percentage: '', gst_price: '7.00' }, '0.00'), null);
  assert.equal(liveVariantGstPrice({ gst: 'Y', price: '100', gst_percentage: '18' }, '0.00'), '18.00', 'entering a percentage switches to the formula');
});

test('saving: a saved percentage is sent with the calculated amount', () => {
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: '5.00' }, '5.00'), { gst_percentage: 5, gst_price: 5 });
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 200, gst_percentage: '18' }, '5.00'), { gst_percentage: 18, gst_price: 36 });
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 33.33, gst_percentage: '5' }, '0.00'), { gst_percentage: 5, gst_price: 1.67 });
});

test('saving: GST Applicable No sends amount 0 and still carries the percentage', () => {
  assert.deepEqual(resolveSavedVariantGst({ gst: 'N', price: 100, gst_percentage: '18' }, '18.00'), { gst_percentage: 18, gst_price: 0 });
});

test('saving: clearing or zeroing a saved percentage zeroes the amount (nothing stale is kept)', () => {
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: '' }, '18.00'), { gst_percentage: 0, gst_price: 0 });
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: '0' }, '18.00'), { gst_percentage: 0, gst_price: 0 });
});

test('saving: a legacy variant at 0% is saved exactly as before, without a percentage or an amount override', () => {
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: '0.00', gst_price: 7 }, '0.00'), {});
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: '' }, undefined), {});
  assert.deepEqual(resolveSavedVariantGst({ gst: 'N', price: 100, gst_percentage: '0.00' }, '0.00'), { gst_price: 0 });
});

test('saving: invalid percentages are refused with a message and nothing to send', () => {
  for (const bad of ['abc', '-1', '100.01', '101']) {
    const result = resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: bad }, '5.00');
    assert.ok(result.error, bad); assert.ok(!('gst_price' in result) && !('gst_percentage' in result), bad);
  }
  assert.deepEqual(resolveSavedVariantGst({ gst: 'Y', price: 100, gst_percentage: '100' }, '5.00'), { gst_percentage: 100, gst_price: 100 });
});

test('the standalone Variants page is wired to these helpers and shows the percentage field', () => {
  const app = readFileSync(new URL('./App.jsx', import.meta.url), 'utf8');
  assert.match(app, /\["gst_percentage", "GST percentage \(%\)", "number"\]/);
  assert.match(app, /liveVariantGstPrice\(next, variantOriginalPctRef\.current\)/);
  assert.match(app, /resolveSavedVariantGst\(values, variantOriginalPctRef\.current\)/);
  assert.match(app, /variantOriginalPctRef\.current = row\?\.gst_percentage \?\? "0"/);
});
