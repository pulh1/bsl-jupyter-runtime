import assert from 'node:assert/strict';
import { test } from 'node:test';
import { disconnectedHoverGuard } from '../src/hoverGuard.js';

test('disconnected notebook positions cannot poison native hover, including during drag', () => {
  for (const buttons of [0, 1]) {
    assert.equal(disconnectedHoverGuard({notebook: true, buttons, uri: 'root', ready: false}), true);
  }
});

test('connected languages and non-notebook or unmapped positions keep native dispatch', () => {
  for (const uri of ['python', 'bsl']) {
    assert.equal(disconnectedHoverGuard({notebook: true, buttons: 0, uri, ready: true}), false);
  }
  assert.equal(disconnectedHoverGuard({notebook: false, buttons: 0, uri: 'root', ready: false}), false);
  assert.equal(disconnectedHoverGuard({notebook: true, buttons: 0, uri: null, ready: false}), false);
});
