import type { IForeignCodeExtractor } from '@jupyterlab/lsp';

const header = /^[\t ]*%%bsl[\t ]*\r?\n/;

export const bslExtractor: IForeignCodeExtractor = {
  language: 'bsl', fileExtension: 'bsl', cellType: ['code'], standalone: false,
  hasForeignCode: (code, cellType) => cellType === 'code' && header.test(code),
  extractForeignCode: code => {
    const match = code.match(header);
    if (!match) {
      return [{foreignCode: null, hostCode: code, range: null, virtualShift: null}];
    }
    const body = code.slice(match[0].length);
    const lines = body.split('\n');
    return [{
      foreignCode: body,
      hostCode: code.replace(/[^\r\n]/g, ' '),
      range: {
        start: {line: 1, column: 0},
        end: {line: lines.length, column: lines[lines.length - 1].length}
      },
      virtualShift: null
    }];
  }
};
