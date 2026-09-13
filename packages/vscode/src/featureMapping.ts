import {access, realpath, stat} from 'node:fs/promises';
import {constants} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {isProjectFile} from './fileChanges';
import {NotebookSnapshot, Position, Range} from './notebookModel';

export type LspDiagnostic = {range: Range; message: string; severity?: number};
export type TextEdit = {range: Range; newText: string};
export type InsertReplaceEdit = {insert: Range; replace: Range; newText: string};
export type Markup = string | {kind: string; value: string};
export type LspCompletion = {
  label: string; labelDetails?: {detail?: string; description?: string}; kind?: number;
  detail?: string; documentation?: Markup; sortText?: string; filterText?: string;
  insertText?: string; insertTextFormat?: number; textEditText?: string;
  textEdit?: TextEdit | InsertReplaceEdit; additionalTextEdits?: TextEdit[];
  preselect?: boolean; commitCharacters?: string[]; tags?: number[];
  command?: {title: string; command: string; arguments?: unknown[]}; keepWhitespace?: boolean;
};

const EXPRESSION_KEYWORDS = new Set([
  'если', 'if', 'иначеесли', 'elsif', 'возврат', 'return', 'пока', 'while',
  'из', 'in', 'и', 'and', 'или', 'or', 'не', 'not',
]);

/** A syntax-only handoff. The existing kernel matcher validates live values. */
export function isKernelContextReceiver(prefix: string): boolean {
  const tokens: {kind: string; text: string; start: number; end: number}[] = [];
  const lexer = /\s+|\/\/[^\r\n]*|"(?:""|[^"])*(?:"|$)|'(?:[^'])*(?:'|$)|[\p{L}_][\p{L}\p{N}_]*|[0-9]+|./gu;
  for (const match of prefix.matchAll(lexer)) {
    const text = match[0];
    if (/^\s|^\/\//u.test(text)) continue;
    const kind = /^[\p{L}_]/u.test(text) ? 'id' : /^[0-9]/.test(text) ? 'number'
      : /^["']/.test(text) ? 'literal' : text;
    tokens.push({kind, text, start: match.index, end: match.index + text.length});
  }
  if (!tokens.length || tokens.at(-1)!.end !== prefix.length) return false;
  if (tokens.at(-1)?.kind === 'id') tokens.pop(); // Partially typed field.
  if (tokens.pop()?.kind !== '.') return false;
  if (tokens.at(-1)?.kind === ']') {
    tokens.pop();
    if (tokens.pop()?.kind !== 'number' || tokens.pop()?.kind !== '[') return false;
  }
  let root = tokens.pop();
  if (root?.kind !== 'id') return false;
  while (tokens.at(-1)?.kind === '.') {
    tokens.pop(); root = tokens.pop();
    if (root?.kind !== 'id') return false;
  }
  const previous = tokens.at(-1);
  const newStatementLine = previous && /[\r\n]/.test(prefix.slice(previous.end, root.start));
  // These keywords introduce an expression; an identifier/constructor does not.
  // Do not reinterpret a keyword-looking member of an earlier receiver as syntax.
  const afterKeyword = previous?.kind === 'id' && EXPRESSION_KEYWORDS.has(previous.text.toLowerCase()) && tokens.at(-2)?.kind !== '.';
  return root.text.toLowerCase() === 'контекст' && (!!newStatementLine || afterKeyword || !['id', 'number', 'literal', '.', ')', ']'].includes(previous?.kind ?? ''));
}

export function validRange(range: Range): boolean {
  return !!range && [range.start, range.end].every(position => !!position &&
    Number.isInteger(position.line) && Number.isInteger(position.character) && position.line >= 0 && position.character >= 0) &&
    compare(range.start, range.end) <= 0;
}

export function mapDiagnostic(snapshot: NotebookSnapshot, diagnostic: LspDiagnostic): {uri: string; range: Range; message: string; severity?: number} | undefined {
  if (!validRange(diagnostic.range)) return undefined;
  const mapped = snapshot.toCell(diagnostic.range);
  return mapped ? {...mapped, message: diagnostic.message, severity: diagnostic.severity} : undefined;
}

export function mapCompletion(snapshot: NotebookSnapshot, cellUri: string, position: Position, item: LspCompletion, defaultRange?: Range): LspCompletion | undefined {
  const map = (range: Range): Range | undefined => {
    if (!validRange(range)) return undefined;
    const mapped = snapshot.toCell(range);
    return mapped?.uri === cellUri ? mapped.range : undefined;
  };
  const primaryRange = (range: Range): Range | undefined => {
    const mapped = map(range);
    return mapped && mapped.start.line === mapped.end.line && compare(mapped.start, position) <= 0 && compare(position, mapped.end) <= 0 ? mapped : undefined;
  };
  let textEdit: LspCompletion['textEdit'];
  let primary = defaultRange;
  if (defaultRange && (!validRange(defaultRange) || !snapshot.toVirtual(cellUri, defaultRange.start) || !snapshot.toVirtual(cellUri, defaultRange.end))) return undefined;
  if (item.textEdit) {
    if (typeof item.textEdit.newText !== 'string') return undefined;
    if ('range' in item.textEdit) {
      primary = primaryRange(item.textEdit.range);
      if (!primary) return undefined;
      textEdit = {range: primary, newText: item.textEdit.newText};
    } else {
      const insert = primaryRange(item.textEdit.insert), replace = primaryRange(item.textEdit.replace);
      if (!insert || !replace || compare(insert.start, replace.start) !== 0 || compare(insert.end, replace.end) > 0) return undefined;
      primary = replace;
      textEdit = {insert, replace, newText: item.textEdit.newText};
    }
  }
  const occupied: Range[] = primary ? [primary] : [{start: position, end: position}];
  const additionalTextEdits: TextEdit[] = [];
  for (const edit of item.additionalTextEdits ?? []) {
    const range = map(edit.range);
    if (!range || typeof edit.newText !== 'string' || occupied.some(other => overlaps(other, range))) return undefined;
    occupied.push(range); additionalTextEdits.push({range, newText: edit.newText});
  }
  return {...item, textEdit, additionalTextEdits: item.additionalTextEdits ? additionalTextEdits : undefined};
}

/** Virtual-document definitions are mapped by the provider using its session URI. */
export async function mapDefinition(_snapshot: NotebookSnapshot, rootPath: string, target: {uri: string; range: Range}): Promise<{uri: string; range: Range} | undefined> {
  try {
    if (!validRange(target.range) || !isProjectFile(rootPath, target.uri)) return undefined;
    const candidate = fileURLToPath(target.uri);
    const [canonicalRoot, canonicalFile, info] = await Promise.all([realpath(rootPath), realpath(candidate), stat(candidate)]);
    const relative = path.relative(canonicalRoot, canonicalFile);
    if (!relative || relative === '..' || relative.startsWith(`..${path.sep}`) || path.isAbsolute(relative) || !info.isFile()) return undefined;
    await access(canonicalFile, constants.R_OK);
    // Check again after asynchronous filesystem work; never publish a linked path.
    return isProjectFile(canonicalRoot, target.uri) ? target : undefined;
  } catch { return undefined; }
}

function compare(a: Position, b: Position): number { return a.line - b.line || a.character - b.character; }
function overlaps(a: Range, b: Range): boolean {
  if (compare(a.start, a.end) === 0 && compare(b.start, b.end) === 0) return compare(a.start, b.start) === 0;
  return compare(a.start, b.end) < 0 && compare(b.start, a.end) < 0;
}
