import assert from 'node:assert/strict';
import { test } from 'node:test';
import { python } from '@codemirror/lang-python';
import { language, syntaxTree } from '@codemirror/language';
import { Compartment, EditorState } from '@codemirror/state';
import { bslMagic } from '../src/magic.js';

function editor(doc: string) {
  return EditorState.create({doc, extensions: [python(), bslMagic(doc)]});
}

test('a saved magic cell uses BSL and preserves its source', () => {
  const doc = '%%bsl\nЕсли Истина Тогда\n Сообщить("Привет");\nКонецЕсли;';
  const state = editor(doc);
  assert.equal(state.facet(language)?.name, 'bsl');
  assert.equal(state.doc.toString(), doc);
  assert.match(syntaxTree(state).toString(), /If/);
});

test('typing and removing the magic switches back to the base language', () => {
  let state = editor('print(1)');
  assert.equal(state.facet(language)?.name, 'python');
  state = state.update({changes: {from: 0, insert: '%%bsl\n'}}).state;
  assert.equal(state.facet(language)?.name, 'bsl');
  state = state.update({changes: {from: 0, to: 6}}).state;
  assert.equal(state.facet(language)?.name, 'python');
  assert.equal(state.doc.toString(), 'print(1)');
});

test('magic arguments and Windows line endings are accepted', () => {
  assert.equal(editor('%%bsl --help\r\nСообщить(1);').facet(language)?.name, 'bsl');
  assert.equal(editor('%%bsl').facet(language)?.name, 'bsl');
});

test('Python strings, other magics and partial names stay Python', () => {
  for (const doc of ['%%bs', '%%bsl_extra', '%%bslx', '%%python\n1', '%bsl',
    'x = "%%bsl"', '"""\n%%bsl\n"""', '# comment\n%%bsl']) {
    assert.equal(editor(doc).facet(language)?.name, 'python', doc);
  }
});

test('base language reconfiguration does not disable a magic cell', () => {
  const base = new Compartment();
  const doc = '%%bsl\nСообщить(1);';
  let state = EditorState.create({doc, extensions: [base.of(python()), bslMagic(doc)]});
  state = state.update({effects: base.reconfigure(python())}).state;
  assert.equal(state.facet(language)?.name, 'bsl');
  state = state.update({changes: {from: 0, to: 6}}).state;
  assert.equal(state.facet(language)?.name, 'python');
});
