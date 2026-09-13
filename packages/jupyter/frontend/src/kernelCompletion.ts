import type {ICompletionProvider} from '@jupyterlab/completer';
import {bslExtractor} from './extractor';

/** Preserve enhanced kernel rendering while allowing our BSL cell matcher. */
export function withBslKernelCompletion(provider: ICompletionProvider): ICompletionProvider {
  return {
    identifier: provider.identifier,
    rank: provider.rank,
    renderer: provider.renderer,
    async isApplicable(context) {
      const source = context.editor?.model.sharedModel;
      if (source && 'cell_type' in source && source.cell_type === 'code' &&
          bslExtractor.hasForeignCode(source.getSource(), 'code')) {
        // jupyterlab-lsp excludes foreign documents from its kernel provider.
        // Our kernel matcher explicitly understands %%bsl and needs full cells.
        return Boolean(context.session?.kernel);
      }
      return provider.isApplicable(context);
    },
    async fetch(request, context, trigger) {
      const reply = await provider.fetch(request, context, trigger);
      const source = context.editor?.model.sharedModel;
      if (!source || !('cell_type' in source) || source.cell_type !== 'code' ||
          !bslExtractor.hasForeignCode(source.getSource(), 'code')) return reply;
      // Only the owned kernel matcher emits property items for BSL context
      // fields. IPython's ordinary Python matches expose proxy internals.
      const items = reply.items.filter(item => item.type === 'property');
      return items.length === reply.items.length ? reply : {...reply, items};
    },
    resolve: provider.resolve?.bind(provider),
    modelFactory: provider.modelFactory?.bind(provider),
    shouldShowContinuousHint: provider.shouldShowContinuousHint?.bind(provider)
  };
}
