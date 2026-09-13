import * as assert from 'node:assert/strict';
import test from 'node:test';

import {NotebookSnapshot} from '../../src/notebookModel';

test('maps BSL bodies in notebook order and preserves UTF-16 character offsets', () => {
  const model = NotebookSnapshot.fromCells([
    {uri: 'cell:a', kind: 'code', languageId: 'python', text: '%%bsl\nА😀Б'},
    {uri: 'cell:p', kind: 'code', languageId: 'python', text: 'print(1)'},
    {uri: 'cell:b', kind: 'code', languageId: 'python', text: '  %%bsl\r\nВызов()'},
  ], 7);

  assert.equal(model.version, 7);
  assert.equal(model.text, 'А😀Б\n\nВызов()');
  assert.deepEqual(model.toVirtual('cell:a', {line: 1, character: 3}), {line: 0, character: 3});
  assert.deepEqual(model.toVirtual('cell:b', {line: 1, character: 7}), {line: 2, character: 7});
});

test('does not map magic headers or cells without a Python BSL first line', () => {
  const model = NotebookSnapshot.fromCells([
    {uri: 'cell:header', kind: 'code', languageId: 'python', text: ' %%bsl\nПроцедура()'},
    {uri: 'cell:late', kind: 'code', languageId: 'python', text: '\n%%bsl\nИгнорировать()'},
    {uri: 'cell:markup', kind: 'markup', languageId: 'markdown', text: '%%bsl\nТекст'},
    {uri: 'cell:javascript', kind: 'code', languageId: 'javascript', text: '%%bsl\nИгнорировать()'},
  ], 2);

  assert.equal(model.text, 'Процедура()');
  assert.equal(model.toVirtual('cell:header', {line: 0, character: 0}), undefined);
  assert.equal(model.toVirtual('cell:late', {line: 1, character: 0}), undefined);
  assert.equal(model.toVirtual('cell:markup', {line: 1, character: 0}), undefined);
  assert.equal(model.toVirtual('cell:javascript', {line: 1, character: 0}), undefined);
});

test('keeps an empty BSL body unmapped while retaining body separators', () => {
  const model = NotebookSnapshot.fromCells([
    {uri: 'cell:first', kind: 'code', languageId: 'python', text: '%%bsl\nПервый()'},
    {uri: 'cell:empty', kind: 'code', languageId: 'python', text: '%%bsl'},
    {uri: 'cell:last', kind: 'code', languageId: 'python', text: '%%bsl\nПоследний()'},
  ], 3);

  assert.equal(model.text, 'Первый()\n\n\n\nПоследний()');
  assert.equal(model.toVirtual('cell:empty', {line: 0, character: 5}), undefined);
});

test('maps virtual ranges only when both endpoints stay in one BSL body', () => {
  const model = NotebookSnapshot.fromCells([
    {uri: 'cell:a', kind: 'code', languageId: 'python', text: '%%bsl\nА😀Б'},
    {uri: 'cell:b', kind: 'code', languageId: 'python', text: '%%bsl\nВызов()'},
  ], 7);

  assert.deepEqual(
    model.toCell({start: {line: 0, character: 1}, end: {line: 0, character: 3}}),
    {uri: 'cell:a', range: {start: {line: 1, character: 1}, end: {line: 1, character: 3}}},
  );
  assert.equal(model.toCell({start: {line: 0, character: 0}, end: {line: 2, character: 1}}), undefined);
  assert.equal(model.toCell({start: {line: 1, character: 0}, end: {line: 1, character: 1}}), undefined);
});
