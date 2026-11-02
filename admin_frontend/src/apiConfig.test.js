import test from 'node:test';
import assert from 'node:assert/strict';
import { extraHeadersFor, isSameOriginBase } from './apiConfig.js';

test('same-origin API bases are detected', () => {
  assert.equal(isSameOriginBase('/admin-api/'), true);
  assert.equal(isSameOriginBase('//evil.example/admin-api/'), false);
  assert.equal(isSameOriginBase('http://127.0.0.1:8000/admin-api/'), false);
  assert.equal(isSameOriginBase('https://book-fiscally-reunion.ngrok-free.dev/admin-api/'), false);
  assert.equal(isSameOriginBase(undefined), false);
});

test('the ngrok warning bypass header is sent only for a same-origin API base', () => {
  assert.deepEqual(extraHeadersFor('/admin-api/'), { 'ngrok-skip-browser-warning': '1' });
  assert.deepEqual(extraHeadersFor('http://127.0.0.1:8000/admin-api/'), {});
});
