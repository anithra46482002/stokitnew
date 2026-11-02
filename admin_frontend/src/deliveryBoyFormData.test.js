import test from 'node:test';
import assert from 'node:assert/strict';
import { buildDeliveryBoyFormData } from './deliveryBoyFormData.js';

test('Delivery Boy create payload uses FormData and includes profile, joining date and confirmation', () => {
  const image = new Blob(['profile-image-bytes'], { type: 'image/png' });
  Object.defineProperty(image, 'name', { value: 'driver.png' });
  const data = buildDeliveryBoyFormData({ kind: 'delivery', username: 'driver', email: 'driver@example.test', full_name: 'Driver', password: 'StrongPassword123', confirm_password: 'StrongPassword123', phone: '9876543210', address: 'Kochi', joining_date: '2026-10-01', status: 'ACTIVE', profile_image: image });
  assert.ok(data instanceof FormData);
  assert.equal(data.get('kind'), 'delivery');
  assert.equal(data.get('joining_date'), '2026-10-01');
  assert.equal(data.get('confirm_password'), 'StrongPassword123');
  assert.equal(data.get('status'), 'ACTIVE');
  assert.equal(data.get('profile_image').type, 'image/png');
  assert.equal(data.get('profile_image').name, 'driver.png');
  assert.equal(data.has('has_profile_image'), false);
  assert.equal(data.has('profile_image_version'), false);
});

test('Delivery Boy edit without replacement or removal preserves the saved image and blank password', () => {
  const data = buildDeliveryBoyFormData({ id: 12, kind: 'delivery', phone: '9876543210', password: '', confirm_password: '', joining_date: '', has_profile_image: true, profile_image_version: '2026-10-01T10:00:00Z' });
  assert.equal(data.get('id'), '12');
  assert.equal(data.get('password'), '');
  assert.equal(data.get('confirm_password'), '');
  assert.equal(data.has('profile_image'), false);
  assert.equal(data.has('remove_profile_image'), false);
  assert.equal(data.has('joining_date'), false, 'blank edit does not accidentally clear or invalidate an existing date');
});

test('Delivery Boy edit explicitly removes the saved image only when requested', () => {
  const data = buildDeliveryBoyFormData({ id: 12, kind: 'delivery', remove_profile_image: true, has_profile_image: true });
  assert.equal(data.get('remove_profile_image'), 'true');
  assert.equal(data.has('profile_image'), false);
});

test('Delivery Boy replacement image is sent without a removal flag', () => {
  const image = new Blob(['new-image'], { type: 'image/webp' });
  const data = buildDeliveryBoyFormData({ id: 12, kind: 'delivery', profile_image: image, remove_profile_image: false });
  assert.equal(data.get('profile_image').type, 'image/webp');
  assert.equal(data.has('remove_profile_image'), false);
});
