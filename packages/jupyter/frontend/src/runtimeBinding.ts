/** In-memory notebook identity and authenticated REST lifecycle. No notebook metadata. */
import type { ProjectStatus } from './runtimeStatus';

export type Endpoint = (method: string, path: string, body?: unknown, signal?: AbortSignal) => Promise<any>;
export type BindingTicket = Readonly<{epoch: number; kernelId: string | null}>;
export const STATUS_REFRESH_MS = 1500;
type Schedule = (callback: () => void) => () => void;
const defaultSchedule: Schedule = callback => { const id = setTimeout(callback, STATUS_REFRESH_MS); return () => clearTimeout(id); };
// Gateway emits these unavailable reasons only after successfully initializing
// the fallback virtual child. Transport/control failures are not acknowledgements.
const virtualChildReasons = new Set(['workspace-unsafe', 'workspace-safety-limit',
  'workspace-unavailable', 'workspace-replaced', 'workspace-notification-unavailable', 'workspace-stopped']);

export function findNotebookAdapter<T extends {widget: unknown}>(widget: unknown,
  tracker: {find: (predicate: (adapter: T) => boolean) => T | undefined}, legacy?: Map<string, T>): T | undefined {
  return tracker.find(adapter => adapter.widget === widget) ??
    (typeof legacy?.values === 'function' ? [...legacy.values()].find(adapter => adapter.widget === widget) : undefined);
}

export class NotebookRuntimeBinding {
  readonly documentUris = new Set<string>();
  status: ProjectStatus | null = null;
  kernelId: string | null = null;
  epoch = 0;
  isDisposed = false;
  private path = '';
  private key = '';
  private identities = new Map<string, string>();
  private stopRefresh?: () => void;
  private refreshAbort?: AbortController;
  private accepted: ProjectStatus | null = null;
  private awaitingAuthority = true;

  constructor(readonly widgetId: string, private endpoint: Endpoint,
    private fence: (authorityReady: boolean) => void = () => {}, private changed: () => void = () => {},
    private schedule: Schedule = defaultSchedule, private bound: (uri: string, binding: string) => void = () => {}) {}

  beginBinding(kernelId: string | null): BindingTicket {
    this.epoch++; this.kernelId = kernelId; this.status = null; this.accepted = null;
    this.stopRefresh?.(); this.refreshAbort?.abort();
    this.awaitingAuthority = true; this.fence(false); this.changed();
    for (const id of this.identities.values()) this.remove(id);
    this.identities.clear();
    return {epoch: this.epoch, kernelId};
  }

  accept(ticket: BindingTicket, response: ProjectStatus): boolean {
    if (this.isDisposed || ticket.epoch !== this.epoch || ticket.kernelId !== this.kernelId ||
        !response || !Number.isInteger(response.epoch) || typeof response.binding_id !== 'string') return false;
    if (this.accepted && response.binding_id === this.accepted.binding_id && response.epoch < this.accepted.epoch) return false;
    const authorityChanged = this.accepted && (response.binding_id !== this.accepted.binding_id ||
      response.installation_id !== this.accepted.installation_id || response.epoch > this.accepted.epoch);
    const ready = response.analysis_state === 'ready' || (response.analysis_state === 'unavailable' &&
      virtualChildReasons.has(response.analysis_reason ?? ''));
    if (authorityChanged || (!ready && !this.awaitingAuthority)) {
      this.awaitingAuthority = true;
      if (!ready) this.fence(false);
    }
    // REST creation/indexing is not proof that the gateway selected this context.
    // Only its current child acknowledgement makes a final sent-version fence safe.
    if (this.awaitingAuthority && ready) {
      this.fence(true); this.awaitingAuthority = false;
    }
    this.accepted = this.status = response; this.changed(); return true;
  }

  async update(path: string, kernelId: string | null, uris: string[], force = false): Promise<void> {
    if (this.isDisposed) return;
    const key = JSON.stringify([path, kernelId, [...uris].sort()]);
    if (!force && key === this.key) return;
    this.key = key; this.path = path;
    const ticket = this.beginBinding(kernelId);
    this.documentUris.clear(); uris.forEach(uri => this.documentUris.add(uri));
    await Promise.all(uris.map(async uri => {
      try {
        const response = await this.endpoint('POST', 'onec-bsl/contexts', {
          notebook_path: path, kernel_id: kernelId, document_uri: uri
        });
        if (!this.accept(ticket, response)) { if (response?.binding_id) this.remove(response.binding_id); return; }
        this.identities.set(uri, response.binding_id);
        this.bound(uri, response.binding_id);
      } catch {
        if (!this.isDisposed && ticket.epoch === this.epoch) this.unavailable();
      }
    }));
    if (!this.isDisposed && ticket.epoch === this.epoch) this.queueRefresh(ticket);
  }

  private queueRefresh(ticket: BindingTicket) {
    this.stopRefresh?.();
    this.stopRefresh = this.schedule(() => { void this.refresh(ticket); });
  }

  private async refresh(ticket: BindingTicket) {
    if (this.isDisposed || ticket.epoch !== this.epoch) return;
    this.refreshAbort = new AbortController();
    try {
      if (!this.identities.size && this.documentUris.size) {
        await this.update(this.path, this.kernelId, [...this.documentUris], true); return;
      }
      for (const [uri, id] of this.identities) {
        const reply = await this.endpoint('GET', `onec-bsl/contexts/${id}`, undefined, this.refreshAbort.signal);
        if (this.accept(ticket, reply)) this.bound(uri, id);
      }
    } catch (error) {
      if (!this.isDisposed && ticket.epoch === this.epoch && (error as {status?: number})?.status === 404) {
        await this.update(this.path, this.kernelId, [...this.documentUris], true); return;
      }
      if (!this.isDisposed && ticket.epoch === this.epoch) this.unavailable();
    }
    if (!this.isDisposed && ticket.epoch === this.epoch) this.queueRefresh(ticket);
  }

  private remove(id: string) { void this.endpoint('DELETE', `onec-bsl/contexts/${encodeURIComponent(id)}`).catch(() => {}); }

  private unavailable() {
    if (this.status) this.fence(false);
    this.awaitingAuthority = true;
    this.status = null; this.changed();
  }

  dispose() {
    if (this.isDisposed) return;
    this.beginBinding(null); this.isDisposed = true; this.documentUris.clear();
  }
}
