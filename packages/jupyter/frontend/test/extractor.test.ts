import assert from 'node:assert/strict';
import { test } from 'node:test';
import { bslExtractor } from '../src/extractor.js';

test('extracts only the BSL body with its original cell range', () => {
  const source = '%%bsl\nСообщить("😀");\nОтвет = 42;';
  assert.equal(bslExtractor.hasForeignCode(source, 'code'), true);
  const [block] = bslExtractor.extractForeignCode(source);
  assert.equal(block.foreignCode, 'Сообщить("😀");\nОтвет = 42;');
  assert.deepEqual(block.range, {start: {line: 1, column: 0}, end: {line: 2, column: 11}});
  assert.equal(block.hostCode?.trim(), '');
  assert.equal(block.hostCode?.split('\n').length, 3);
});

test('keeps non-BSL and non-code cells out of the foreign document', () => {
  for (const source of ['%%bslx\n1', '"""\n%%bsl\n"""', 'print(1)', '%%bsl']) {
    assert.equal(bslExtractor.hasForeignCode(source, 'code'), false);
  }
  assert.equal(bslExtractor.hasForeignCode('%%bsl\n1', 'markdown'), false);
  assert.equal(bslExtractor.hasForeignCode('%%bsl\n1', 'raw'), false);
});

test('Windows lines and an empty body retain valid mapping', () => {
  const [block] = bslExtractor.extractForeignCode('%%bsl\r\n');
  assert.equal(block.foreignCode, '');
  assert.deepEqual(block.range, {start: {line: 1, column: 0}, end: {line: 1, column: 0}});
});
