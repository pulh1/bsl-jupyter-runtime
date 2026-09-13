import assert from 'node:assert/strict';
import {existsSync} from 'node:fs';
import {mkdtemp, mkdir, writeFile, symlink, rm} from 'node:fs/promises';
import path from 'node:path';
import os from 'node:os';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';
import {NotebookSnapshot, Range} from '../../src/notebookModel';

function mapping(): any {
  assert.ok(existsSync(path.join(__dirname, '../../src/featureMapping.ts')), 'BSL feature mapping must be implemented');
  return require('../../src/featureMapping');
}
const range = (line: number, start = 0, end = start): Range => ({start: {line, character: start}, end: {line, character: end}});
const snapshot = NotebookSnapshot.fromCells([
  {uri: 'cell:a', kind: 'code', languageId: 'python', text: '%%bsl\nА😀Б'},
  {uri: 'cell:p', kind: 'code', languageId: 'python', text: 'print(1)'},
  {uri: 'cell:b', kind: 'code', languageId: 'python', text: '%%bsl\nМетод(Арг)\nДругой'},
], 7);

test('empty BSL body accepts completion at its first cursor without mapping the magic line', () => {
  const empty = NotebookSnapshot.fromCells([{uri: 'cell:empty', kind: 'code', languageId: 'python', text: '%%bsl\n'}], 1);
  assert.deepEqual(empty.toVirtual('cell:empty', {line: 1, character: 0}), {line: 0, character: 0});
  assert.equal(empty.toVirtual('cell:empty', {line: 0, character: 0}), undefined);
});

test('kernel context receiver detection preserves indexed and partial fields without matching strings or unrelated receivers', () => {
  const {isKernelContextReceiver} = mapping();
  for (const text of ['Контекст.', 'Контекст.Таблица[0].', 'Х = Контекст . Таблица [ 12 ] . По', 'Сообщить(контекст.Структура.По', 'Контекст.\nПоле.']) {
    assert.equal(isKernelContextReceiver(text), true, text);
  }
  for (const text of ['ОбщийМодуль.Метод(', 'Контекст.Метод().', 'НеКонтекст.', 'Объект.Контекст.', '// Контекст.', '"Контекст.', 'Новый Контекст.', 'Контекст[Х].']) {
    assert.equal(isKernelContextReceiver(text), false, text);
  }
});

test('kernel handoff recognizes a receiver on a new statement line after a procedure header', () => {
  const {isKernelContextReceiver} = mapping();
  assert.equal(isKernelContextReceiver('Процедура П()\nКонтекст.Таблица.'), true);
  assert.equal(isKernelContextReceiver('Вызов().Контекст.Таблица.'), false);
});

test('kernel handoff accepts RU and EN expression keywords but not constructors or receiver suffixes', () => {
  const {isKernelContextReceiver} = mapping();
  for (const prefix of [
    'Если ', 'ИначеЕсли ', 'Возврат ', 'Пока ', 'Для Каждого Строка Из ',
    'Если Значение И ', 'Если Значение ИЛИ ', 'Если НЕ ',
    'If ', 'ElsIf ', 'Return ', 'While ', 'For Each Row In ',
    'If Value And ', 'If Value Or ', 'If Not ',
  ]) {
    assert.equal(isKernelContextReceiver(`${prefix}Контекст.Таблица.`), true, prefix);
    assert.equal(isKernelContextReceiver(`${prefix}Контекст.Структура.По`), true, prefix);
  }
  for (const text of [
    'Новый Контекст.', 'New Контекст.', 'Вызов().Контекст.',
    'Объект.Контекст.', 'Объект.Если Контекст.', 'Value Контекст.',
    'Если "Контекст.', 'Return // Контекст.',
  ]) assert.equal(isKernelContextReceiver(text), false, text);
});

test('completion replacement and additional edits stay in one cell with exact UTF-16 offsets', () => {
  const {mapCompletion} = mapping();
  assert.deepEqual(mapCompletion(snapshot, 'cell:a', {line: 1, character: 3}, {
    label: 'Замена', textEdit: {range: range(0, 1, 4), newText: 'Текст'},
  }).textEdit, {range: range(1, 1, 4), newText: 'Текст'});
  const item = {label: 'Вызов', textEdit: {insert: range(2, 0, 2), replace: range(2, 0, 5), newText: 'Вызов'}, additionalTextEdits: [{range: range(3, 0, 6), newText: 'После'}]};
  const result = mapCompletion(snapshot, 'cell:b', {line: 1, character: 2}, item);
  assert.deepEqual(result.textEdit, {insert: range(1, 0, 2), replace: range(1, 0, 5), newText: 'Вызов'});
  assert.deepEqual(result.additionalTextEdits, [{range: range(2, 0, 6), newText: 'После'}]);
});

test('completion rejects separator, cross-cell, invalid and overlapping edits as whole items', () => {
  const {mapCompletion} = mapping();
  const primary = {range: range(2, 0, 5), newText: 'Вызов'};
  for (const textEdit of [
    {range: {start: {line: 0, character: 0}, end: {line: 2, character: 1}}, newText: ''},
    {range: range(1), newText: ''}, {range: range(2, 0, 100), newText: ''},
    {range: range(2, 0.5, 3), newText: ''}, {range: range(2, 4, 5), newText: ''},
    {insert: range(2, 1, 2), replace: range(2, 0, 5), newText: ''},
  ]) assert.equal(mapCompletion(snapshot, 'cell:b', {line: 1, character: 2}, {label: 'bad', textEdit}), undefined);
  for (const extra of [range(0, 0, 1), range(1), range(2, 1, 2), range(3, 0, 100)]) {
    assert.equal(mapCompletion(snapshot, 'cell:b', {line: 1, character: 2}, {label: 'bad', textEdit: primary, additionalTextEdits: [{range: extra, newText: ''}]}), undefined);
  }
});

test('additional edits cannot overlap the implicit word replacement when the server omits textEdit', () => {
  const {mapCompletion} = mapping();
  assert.equal(mapCompletion(snapshot, 'cell:b', {line: 1, character: 3}, {
    label: 'Вызов', additionalTextEdits: [{range: range(2, 0, 1), newText: 'bad'}],
  }, range(1, 0, 5)), undefined);
});

test('diagnostics map only valid body ranges, discarding separators and malformed offsets', () => {
  const {mapDiagnostic} = mapping();
  assert.deepEqual(mapDiagnostic(snapshot, {range: range(0, 1, 3), message: 'Ошибка', severity: 2}), {uri: 'cell:a', range: range(1, 1, 3), message: 'Ошибка', severity: 2});
  for (const invalid of [range(1), range(-1), range(0, 0.1, 1), {start: {line: 0, character: 1}, end: {line: 2, character: 1}}]) {
    assert.equal(mapDiagnostic(snapshot, {range: invalid, message: 'bad'}), undefined);
  }
});

test('definitions require a readable real source under the canonical root and reject linked escapes', async () => {
  const {mapDefinition} = mapping();
  const fixture = await mkdtemp(path.join(os.tmpdir(), 'bsl-def-'));
  const root = path.join(fixture, 'project');
  await mkdir(root);
  const source = path.join(root, 'Module.bsl');
  await writeFile(source, 'Перем А;');
  await mkdir(path.join(fixture, 'outside'));
  await writeFile(path.join(fixture, 'outside', 'Escape.bsl'), 'bad');
  try {
    const target = {uri: pathToFileURL(source).href, range: range(0, 0, 1)};
    assert.deepEqual(await mapDefinition(snapshot, root, target), target);
    await symlink(path.join(fixture, 'outside'), path.join(root, 'linked'), process.platform === 'win32' ? 'junction' : 'dir');
    for (const file of [path.join(fixture, 'outside', 'Escape.bsl'), path.join(root, 'linked', 'Escape.bsl'), path.join(root, 'Missing.bsl'), root]) {
      assert.equal(await mapDefinition(snapshot, root, {uri: pathToFileURL(file).href, range: range(0)}), undefined);
    }
    assert.equal(await mapDefinition(snapshot, root, {uri: 'https://example.com/source.bsl', range: range(0)}), undefined);
    assert.equal(await mapDefinition(snapshot, root, {...target, range: range(-1)}), undefined);
  } finally { await rm(fixture, {recursive: true, force: true}); }
});
