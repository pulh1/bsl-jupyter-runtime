export type CellInput = {
  uri: string;
  kind: 'code' | 'markup';
  languageId: string;
  text: string;
};

export type Position = {
  line: number;
  character: number;
};

export type Range = {
  start: Position;
  end: Position;
};

type Body = {
  uri: string;
  sourceStartLine: number;
  virtualStartLine: number;
  lines: readonly string[];
};

const BSL_HEADER = /^[\t ]*%%bsl[\t ]*(?:\r?\n|$)/;

export class NotebookSnapshot {
  get hasBslCells(): boolean {return this.bodies.length > 0;}
  private constructor(
    readonly text: string,
    readonly version: number,
    private readonly bodies: readonly Body[],
  ) {}

  static fromCells(cells: readonly CellInput[], version: number): NotebookSnapshot {
    const bodies = cells.flatMap(cell => {
      if (cell.kind !== 'code' || cell.languageId !== 'python') return [];

      const header = BSL_HEADER.exec(cell.text);
      if (!header) return [];

      const body = cell.text.slice(header[0].length).replace(/\r\n/g, '\n');
      return [{uri: cell.uri, sourceStartLine: header[0].endsWith('\n') ? 1 : 0, body}];
    });

    let text = '';
    const mappedBodies: Body[] = [];
    for (const [index, body] of bodies.entries()) {
      if (index > 0) text += '\n\n';

      const virtualStartLine = countLines(text);
      // A newline after magic creates an editable empty body (Ctrl+Space at 1:0).
      const lines = body.body === '' && body.sourceStartLine === 0 ? [] : body.body.split('\n');
      mappedBodies.push({
        uri: body.uri,
        sourceStartLine: body.sourceStartLine,
        virtualStartLine,
        lines,
      });
      text += body.body;
    }

    return new NotebookSnapshot(text, version, mappedBodies);
  }

  toVirtual(cellUri: string, position: Position): Position | undefined {
    const body = this.bodies.find(candidate => candidate.uri === cellUri);
    if (!body) return undefined;

    const bodyLine = position.line - body.sourceStartLine;
    if (!isBodyPosition(body, bodyLine, position.character)) return undefined;

    return {line: body.virtualStartLine + bodyLine, character: position.character};
  }

  toCell(range: Range): {uri: string; range: Range} | undefined {
    if (comparePositions(range.start, range.end) > 0) return undefined;

    const startBody = this.bodyAt(range.start);
    const endBody = this.bodyAt(range.end);
    if (!startBody || startBody !== endBody) return undefined;

    return {
      uri: startBody.uri,
      range: {
        start: toSourcePosition(startBody, range.start),
        end: toSourcePosition(startBody, range.end),
      },
    };
  }

  private bodyAt(position: Position): Body | undefined {
    return this.bodies.find(body =>
      isBodyPosition(body, position.line - body.virtualStartLine, position.character),
    );
  }
}

function countLines(text: string): number {
  return text.split('\n').length - 1;
}

function isBodyPosition(body: Body, line: number, character: number): boolean {
  return line >= 0 && line < body.lines.length && character >= 0 && character <= body.lines[line].length;
}

function toSourcePosition(body: Body, position: Position): Position {
  return {
    line: body.sourceStartLine + position.line - body.virtualStartLine,
    character: position.character,
  };
}

function comparePositions(left: Position, right: Position): number {
  return left.line === right.line ? left.character - right.character : left.line - right.line;
}
