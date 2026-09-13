import * as fs from 'node:fs/promises';
import {constants as fsConstants, type Dir} from 'node:fs';
import * as path from 'node:path';

export type SourceRootLayout = 'designer' | 'edt-src' | 'edt-parent';

export type SourceRoot = {
  canonicalPath: string;
  layout: SourceRootLayout;
};

const MAX_SCANNED_ENTRIES = 100_000;
const MAX_SCAN_DURATION_MS = 10_000;

export async function validateSourceRoot(sourcePath: string): Promise<SourceRoot> {
  if (!sourcePath.trim() || !path.isAbsolute(sourcePath)) {
    throw new Error('Source root must be an absolute directory path.');
  }

  const requestedPath = path.normalize(sourcePath);
  await assertNoLinkedAncestor(requestedPath);
  const root = await readableDirectory(requestedPath);
  if (!root) {
    throw new Error('Source root is not a readable directory.');
  }

  const canonicalPath = await fs.realpath(requestedPath);
  const scan = new ScanBudget();

  if (await isDesignerLayout(canonicalPath, scan)) {
    return {canonicalPath, layout: 'designer'};
  }
  if (await isEdtSourceLayout(canonicalPath, scan)) {
    return {canonicalPath, layout: 'edt-src'};
  }
  if (await isEdtSourceLayout(path.join(canonicalPath, 'src'), scan)) {
    return {canonicalPath, layout: 'edt-parent'};
  }

  throw new Error('Source root does not match a supported Designer or EDT layout.');
}

export class OpenNotebookRoots {
  private readonly roots = new Map<string, string>();

  get(notebookUri: string): string | undefined {
    return this.roots.get(notebookUri);
  }

  set(notebookUri: string, canonicalPath: string): void {
    this.roots.set(notebookUri, canonicalPath);
  }

  clear(notebookUri: string): void {
    this.roots.delete(notebookUri);
  }

  clearAll(): void {
    for (const notebookUri of this.roots.keys()) {
      this.clear(notebookUri);
    }
  }
}

async function assertNoLinkedAncestor(candidate: string): Promise<void> {
  let current = candidate;
  while (true) {
    let details: Awaited<ReturnType<typeof fs.lstat>>;
    try {
      details = await fs.lstat(current);
    } catch (error) {
      if ((error as NodeJS.ErrnoException).code === 'ENOENT') {
        throw new Error('Source root does not exist.');
      }
      throw new Error('Source root cannot be accessed.');
    }
    if (details.isSymbolicLink()) {
      throw new Error('Source root cannot contain a symbolic link or junction.');
    }

    const parent = path.dirname(current);
    if (parent === current) return;
    current = parent;
  }
}

async function isDesignerLayout(root: string, scan: ScanBudget): Promise<boolean> {
  if (!(await readableFile(root, ['Configuration.xml']))) return false;

  return findReadableModule(root, scan, async (moduleRoot, name) => {
    return (await readableFile(root, ['CommonModules', `${name}.xml`])) &&
      readableFile(root, ['CommonModules', moduleRoot, 'Ext', 'Module.bsl']);
  });
}

async function isEdtSourceLayout(root: string, scan: ScanBudget): Promise<boolean> {
  if (!(await readableFile(root, ['Configuration', 'Configuration.mdo']))) return false;

  return findReadableModule(root, scan, async (moduleRoot, name) => {
    return (await readableFile(root, ['CommonModules', moduleRoot, `${name}.mdo`])) &&
      readableFile(root, ['CommonModules', moduleRoot, 'Module.bsl']);
  });
}

async function findReadableModule(
  root: string,
  scan: ScanBudget,
  matchesModule: (moduleRoot: string, name: string) => Promise<boolean>,
): Promise<boolean> {
  const commonModules = path.join(root, 'CommonModules');
  if (!await readableDirectory(commonModules)) return false;

  let directory: Dir;
  try {
    directory = await fs.opendir(commonModules);
  } catch {
    return false;
  }

  try {
    for await (const entry of directory) {
      scan.consume();
      if (!entry.isDirectory() || entry.isSymbolicLink()) continue;
      if (await matchesModule(entry.name, entry.name)) return true;
    }
  } finally {
    await directory.close().catch(() => undefined);
  }
  return false;
}

async function readableDirectory(directoryPath: string): Promise<boolean> {
  try {
    const details = await fs.lstat(directoryPath);
    if (details.isSymbolicLink() || !details.isDirectory()) return false;
    await fs.access(directoryPath, fsConstants.R_OK);
    return true;
  } catch {
    return false;
  }
}

async function readableFile(root: string, segments: readonly string[]): Promise<boolean> {
  let current = root;
  for (const segment of segments) {
    current = path.join(current, segment);
    try {
      const details = await fs.lstat(current);
      if (details.isSymbolicLink()) return false;
    } catch {
      return false;
    }
  }

  try {
    const details = await fs.lstat(current);
    if (!details.isFile()) return false;
    await fs.access(current, fsConstants.R_OK);
    return true;
  } catch {
    return false;
  }
}

class ScanBudget {
  private readonly startedAt = Date.now();
  private entries = 0;

  consume(): void {
    if (Date.now() - this.startedAt > MAX_SCAN_DURATION_MS) {
      throw new Error('Source root scan exceeded 10 seconds.');
    }
    this.entries += 1;
    if (this.entries > MAX_SCANNED_ENTRIES) {
      throw new Error('Source root scan exceeded 100000 entries.');
    }
  }
}
