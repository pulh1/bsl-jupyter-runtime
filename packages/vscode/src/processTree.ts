import {ChildProcess, execFile} from 'node:child_process';
import path from 'node:path';

const EXIT_DEADLINE_MS = 5000;

function alive(child: ChildProcess): boolean {
  return child.pid !== undefined && child.exitCode === null && child.signalCode === null;
}

/** Only accepts the ChildProcess retained by its owner, never a discovered PID. */
export async function terminateOwnedProcessTree(child: ChildProcess): Promise<void> {
  const pid = child.pid;
  if (pid === undefined) return;
  if (!Number.isSafeInteger(pid) || pid <= 0) throw new Error('Invalid owned process ID');

  if (process.platform === 'win32') {
    // Never target an exited owner's PID: Windows may already have reused it.
    if (!alive(child)) return;
    const taskkill = path.join(process.env.SystemRoot ?? 'C:\\Windows', 'System32', 'taskkill.exe');
    await new Promise<void>((resolve, reject) => {
      execFile(taskkill, ['/PID', String(pid), '/T', '/F'], {
        windowsHide: true, timeout: EXIT_DEADLINE_MS, maxBuffer: 2048,
      }, error => {
        if (error && alive(child)) reject(new Error('Could not terminate owned language server tree'));
        else resolve();
      });
    });
  } else {
    // The owner was spawned detached, so its PID is also its private process-group ID.
    try { process.kill(-pid, 'SIGKILL'); }
    catch (error) { if ((error as NodeJS.ErrnoException).code !== 'ESRCH') throw new Error('Could not terminate owned language server group'); }
  }

  if (!alive(child)) return;
  await new Promise<void>((resolve, reject) => {
    const onExit = (): void => { clearTimeout(timer); resolve(); };
    const timer = setTimeout(() => {
      child.off('exit', onExit);
      reject(new Error('Owned language server did not exit before teardown deadline'));
    }, EXIT_DEADLINE_MS);
    child.once('exit', onExit);
  });
}
