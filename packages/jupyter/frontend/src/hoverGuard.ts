/** Native jupyterlab-lsp 5.3 hover retains a rejected request when an
 * unconnected host document is hovered. Do not dispatch LSP mousemove there.
 * Button state deliberately does not bypass this guard: native hover also runs
 * during selection dragging. DOM propagation (and selection) remains intact.
 */
export function disconnectedHoverGuard(context: {
  notebook: boolean; buttons: number; uri: string | null; ready: boolean;
}): boolean {
  return context.notebook && context.uri !== null && !context.ready;
}
