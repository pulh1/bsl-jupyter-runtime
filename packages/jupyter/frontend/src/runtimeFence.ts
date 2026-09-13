import type {ICompletionProvider, ICompletionContext} from '@jupyterlab/completer';

type DiagnosticResponse = {uri: string; version?: number; diagnostics: any[]};
export class RuntimeResponseFence {
  revision = 0;
  isDisposed = false;
  authorityReady = true;
  private diagnosticSerial = 0;
  private latest?: DiagnosticResponse;
  advance(authorityReady = true) {
    this.revision++; this.diagnosticSerial++; this.latest = undefined; this.authorityReady = authorityReady;
  }
  dispose() { this.advance(); this.isDisposed = true; }

  diagnostic(response: DiagnosticResponse, currentVersion: () => number,
    apply: (response: DiagnosticResponse) => void) {
    const serial = ++this.diagnosticSerial;
    // Upstream handles this signal synchronously, then schedules CodeMirror's
    // asynchronous linter. Repair its normal cache before that linter renders.
    queueMicrotask(() => {
      if (this.isDisposed || serial !== this.diagnosticSerial) return;
      const valid = this.authorityReady && Number.isInteger(response.version) && response.version === currentVersion();
      if (valid) this.latest = response;
      const latest = this.latest?.uri === response.uri && this.latest.version === currentVersion() ? this.latest : undefined;
      apply(latest ?? {...response, version: currentVersion(), diagnostics: []});
    });
  }
}

export function fenceCompletionProvider(provider: ICompletionProvider,
  current: (context: ICompletionContext) => RuntimeResponseFence | undefined): ICompletionProvider {
  const items = new WeakMap<object, {fence: RuntimeResponseFence; revision: number}>();
  const expired = (fence: RuntimeResponseFence, revision: number) =>
    fence.isDisposed || !fence.authorityReady || fence.revision !== revision;
  return {
    identifier: provider.identifier, rank: provider.rank, renderer: provider.renderer,
    isApplicable: context => provider.isApplicable(context),
    modelFactory: provider.modelFactory?.bind(provider),
    shouldShowContinuousHint: provider.shouldShowContinuousHint?.bind(provider),
    async fetch(request, context, trigger) {
      const fence = current(context); const revision = fence?.revision;
      const reply = await provider.fetch(request, context, trigger);
      if (!fence) return reply;
      if (expired(fence, revision!)) return {...reply, items: []};
      for (const item of reply.items) items.set(item, {fence, revision: revision!});
      return reply;
    },
    resolve: provider.resolve ? async (item, context, patch) => {
      const ticket = items.get(item);
      const active = current(context); const revision = active?.revision;
      if (active && expired(active, revision!)) throw new Error('BSL completion expired');
      if (ticket && expired(ticket.fence, ticket.revision)) throw new Error('BSL completion expired');
      const result = await provider.resolve!(item, context, patch);
      if (active && expired(active, revision!)) throw new Error('BSL completion expired');
      if (ticket && expired(ticket.fence, ticket.revision)) throw new Error('BSL completion expired');
      return result;
    } : undefined
  };
}
