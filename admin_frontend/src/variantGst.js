// Pure GST helpers for product variants (no React, so they are unit-testable).
//   GST Price = Price x GST Percentage / 100, rounded half-up to two decimals.
// The arithmetic is done on decimal text with BigInt, so values such as 33.33 x 5% never drift through floating point.
// The server repeats the same formula and is the final authority; this only keeps the form in step with it.

const DECIMAL_TEXT = /^(?:\d+(?:\.\d*)?|\.\d+)$/;
let rowCounter = 0;

// { int, scale } for a plain non-negative decimal ("100", "5.5", ".5"); null for blank, negative, text, NaN,
// Infinity or exponent notation, so nothing invalid can reach the calculation.
function parseDecimal(value) {
  if (value === null || value === undefined || typeof value === 'boolean') return null;
  const text = typeof value === 'number' ? (Number.isFinite(value) ? String(value) : '') : String(value).trim();
  if (!DECIMAL_TEXT.test(text)) return null;
  const [whole, fraction = ''] = text.split('.');
  return { int: BigInt((whole || '0') + fraction), scale: fraction.length };
}

const withinPercentRange = (parsed) => parsed.int <= 100n * (10n ** BigInt(parsed.scale));
const formatCents = (cents) => {
  const digits = cents.toString().padStart(3, '0');
  return digits.slice(0, -2) + '.' + digits.slice(-2);
};

// "5.00" for price "100" and percentage "5". Anything blank, negative, non-numeric, non-finite or above 100
// gives "0.00" (never NaN or Infinity).
export function calcGstPrice(price, percentage) {
  const amount = parseDecimal(price);
  const rate = parseDecimal(percentage);
  if (!amount || !rate || !withinPercentRange(rate)) return '0.00';
  const numerator = amount.int * rate.int;
  const divisor = 10n ** BigInt(amount.scale + rate.scale);
  return formatCents((numerator * 2n + divisor) / (divisor * 2n)); // half-up, in cents
}

// '' when the percentage is blank (treated as 0) or valid; otherwise the message to show the admin.
export function gstPercentageError(value) {
  if (value === null || value === undefined || String(value).trim() === '') return '';
  const parsed = parseDecimal(value);
  if (!parsed) return 'GST percentage must be a number from 0 to 100.';
  if (!withinPercentRange(parsed)) return 'GST percentage cannot be more than 100.';
  return '';
}

// GST Applicable "No" always means 0.00; "Yes" uses the entered price and percentage.
export function gstPriceFor(variant) {
  return String(variant?.gst || 'N').toUpperCase() === 'Y' ? calcGstPrice(variant?.price, variant?.gst_percentage) : '0.00';
}

const GST_INPUTS = ['price', 'gst', 'gst_percentage'];

// One field edit on a variant row; the GST price is recalculated in the same step whenever an input of it changed.
export function updateVariantField(variant, key, value) {
  const next = { ...variant, [key]: value };
  if (key === 'gst_percentage') next.gst_percentage_overridden = true;
  if (key === 'gst') next.gst_overridden = true;
  return GST_INPUTS.includes(key) ? { ...next, gst_price: gstPriceFor(next) } : next;
}

// New variants inherit the current product default until the admin explicitly edits their own GST rate.
export function blankVariant(productGstPercentage = '') {
  rowCounter += 1;
  const percentage = productGstPercentage == null ? '' : String(productGstPercentage);
  const gst = Number(percentage) > 0 ? 'Y' : 'N';
  const row = { uid: 'variant-' + rowCounter, name: '', price: '', discount: '', quantity: '', gst, gst_percentage: percentage, gst_price: '0.00', gst_percentage_overridden: false, gst_overridden: false };
  return { ...row, gst_price: gstPriceFor(row) };
}

export function applyProductGstDefault(variant, productGstPercentage) {
  if (variant.gst_percentage_overridden) return variant;
  const percentage = String(productGstPercentage ?? '0');
  const next = { ...variant, gst_percentage: percentage };
  if (!variant.gst_overridden) next.gst = Number(percentage) > 0 ? 'Y' : 'N';
  return { ...next, gst_price: gstPriceFor(next) };
}

// Standalone Variants page, live form: the GST amount to show as soon as price, GST Applicable or percentage
// changes. null means "leave the amount as it is" (a legacy variant at 0% keeps the amount it already has).
// Otherwise it matches what resolveSavedVariantGst will save, so the field never disagrees with the saved value.
export function liveVariantGstPrice(values, originalPercentage) {
  if (String(values?.gst || 'N').toUpperCase() !== 'Y') return '0.00';
  const entered = parseDecimal(values?.gst_percentage);
  const original = parseDecimal(originalPercentage);
  const originalIsZero = !original || original.int === 0n;
  if (originalIsZero && (!entered || entered.int === 0n)) return null;
  return calcGstPrice(values?.price, values?.gst_percentage);
}

// Standalone Variants page (edit of a saved variant). Returns { error } or { gst_percentage?, gst_price }.
// A legacy variant (stored percentage 0) left at 0% keeps the GST amount it already has and is saved without a
// percentage, exactly as before; the moment a percentage is entered the amount follows the formula.
export function resolveSavedVariantGst(values, originalPercentage) {
  const problem = gstPercentageError(values?.gst_percentage);
  if (problem) return { error: problem };
  const gst = String(values?.gst || 'N').toUpperCase() === 'Y' ? 'Y' : 'N';
  const entered = parseDecimal(values?.gst_percentage);
  const percentage = entered ? Number(String(values.gst_percentage).trim()) : 0;
  const original = parseDecimal(originalPercentage);
  const originalIsZero = !original || original.int === 0n;
  const result = {};
  if (!(percentage === 0 && originalIsZero)) result.gst_percentage = percentage;
  if (gst !== 'Y') result.gst_price = 0;
  else if (percentage > 0 || !originalIsZero) result.gst_price = Number(calcGstPrice(values?.price, percentage));
  return result;
}
