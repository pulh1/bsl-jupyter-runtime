type DiagnosticFeature = {handleDiagnostic: (...args: any[]) => any};
const guarded = new WeakSet<object>();

/** jupyterlab-lsp 5.3 keeps callbacks for disposed foreign documents. Only
 * terminal BSL documents are skipped; live/non-BSL behavior and errors remain
 * the original public feature's responsibility.
 */
export function guardDisposedBslDiagnostics(feature: DiagnosticFeature): void {
  if (guarded.has(feature)) return;
  const original = feature.handleDiagnostic;
  feature.handleDiagnostic = function (...args: any[]) {
    const document = args[1];
    if (document?.isDisposed === true && document.language === 'bsl') return;
    return original.apply(this, args);
  };
  guarded.add(feature);
}
