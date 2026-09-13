export interface ProjectStatus {
  binding_id: string; epoch: number; mode: string; reason: string | null;
  analysis_state: string; analysis_reason: string | null; installation_id: string | null;
}

export function formatRuntimeStatus(status: ProjectStatus | null): string {
  if (!status) return 'BSL: virtual only · status unavailable';
  const mode = status.mode === 'project' ? 'project' : 'virtual only';
  const state = status.analysis_state === 'ready' ? 'ready · LS queries available' : status.analysis_state;
  let text = `BSL: ${mode} · ${state}`;
  const reason = status.analysis_reason ?? status.reason;
  if (reason === 'index-convergence-unconfirmed') {
    if (status.mode === 'project') text += ' · index convergence unconfirmed';
  } else if (reason) text += ` (${reason})`;
  return text;
}
