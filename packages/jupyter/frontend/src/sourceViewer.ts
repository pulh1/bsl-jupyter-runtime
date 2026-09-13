import {Drive, type Contents} from '@jupyterlab/services';

export type SourceViewerState = 'pending' | 'available' | 'unavailable';
export const SOURCE_FALLBACK_PREFIX = '.lsp_symlink/onec-bsl:';

/** Identify viewers only; all source authorization remains on the server. */
export function sourceViewerPath(path: string): {canonical: string; fallback: boolean} | null {
  if (path.length > 8192) return null;
  const fallback = path.startsWith(SOURCE_FALLBACK_PREFIX);
  const candidate = fallback ? path.slice('.lsp_symlink/'.length) : path;
  if (!candidate.startsWith('onec-bsl:')) return null;
  let decoded: string;
  try { decoded = decodeURIComponent(candidate.slice('onec-bsl:'.length)); } catch { return null; }
  const [binding, token, ...parts] = decoded.split('/');
  if (!/^[a-f0-9]{32}$/.test(binding ?? '') || !/^[a-f0-9]{64}$/.test(token ?? '') || !parts.length ||
      parts.some(part => !part || ['.', '..'].includes(part) || /[. ]$/.test(part) || /[\\:%?#\x00-\x1f]/.test(part)) ||
      !/\.(bsl|os)$/i.test(parts.at(-1)!)) return null;
  return {canonical:'onec-bsl:' + decoded, fallback};
}

/** Observe errors through the public Drive API without changing their outcome. */
export class SourceDrive extends Drive {
  constructor(options: Drive.IOptions, private failed: (canonical: string) => void) { super(options); }
  async get(path: string, options?: Contents.IFetchOptions): Promise<Contents.IModel> {
    try { return await super.get(path, options); } catch (error) {
      const source = sourceViewerPath('onec-bsl:' + path);
      if (source) this.failed(source.canonical);
      throw error;
    }
  }
}

/** Refresh only through the public document context. Never retains a server lease. */
export class SourceViewer {
  isDisposed = false;
  private again = false;
  private revision = 0;
  private pending?: Promise<void>;
  constructor(private context: {ready: Promise<void>; revert(): Promise<void>},
    private changed: (state: SourceViewerState) => void) {}

  refresh(): Promise<void> {
    if (this.isDisposed) return Promise.resolve();
    this.revision++;
    this.changed('pending');
    if (this.pending) { this.again = true; return this.pending; }
    this.pending = this.read().finally(() => { this.pending = undefined; });
    return this.pending;
  }

  private async read(): Promise<void> {
    do {
      this.again = false;
      const revision = this.revision;
      try {
        await this.context.ready;
        if (this.isDisposed) return;
        await this.context.revert();
        if (!this.isDisposed && !this.again && revision === this.revision) this.changed('available');
      } catch {
        if (!this.isDisposed && !this.again) this.changed('unavailable');
      }
    } while (!this.isDisposed && this.again);
  }

  unavailable() {
    this.revision++;
    if (!this.isDisposed) this.changed('unavailable');
  }

  dispose() { this.isDisposed = true; }
}
