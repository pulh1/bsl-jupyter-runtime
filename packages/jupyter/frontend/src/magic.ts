import { bsl } from '@1c-syntax/codemirror-lang-bsl';
import { Compartment, EditorState, Prec, type Extension } from '@codemirror/state';

const magicLine = /^[\t ]*%%bsl(?:[\t \r\n]|$)/;

/** Override the host language only while the first line is a BSL cell magic. */
export function bslMagic(source: string): Extension {
  const mode = new Compartment();
  const support = bsl();
  return [
    Prec.high(mode.of(magicLine.test(source) ? support : [])),
    EditorState.transactionExtender.of(transaction => {
      if (!transaction.docChanged) {
        return null;
      }
      const enabled = magicLine.test(transaction.newDoc.line(1).text);
      const previous = magicLine.test(transaction.startState.doc.line(1).text);
      return enabled === previous
        ? null
        : {effects: mode.reconfigure(enabled ? support : [])};
    })
  ];
}
