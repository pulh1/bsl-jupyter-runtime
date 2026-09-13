import {ChildProcessWithoutNullStreams, spawn} from 'node:child_process';
import {mkdtemp, rm, writeFile} from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {Transform, TransformCallback} from 'node:stream';
import {
  CancellationTokenSource, createMessageConnection, Disposable, Message, MessageConnection,
  StreamMessageReader, StreamMessageWriter,
} from 'vscode-jsonrpc/node';
import {terminateOwnedProcessTree} from './processTree';

export const TRANSPORT_LIMITS = Object.freeze({
  maxPending: 32,
  maxMessageBytes: 8 * 1024 * 1024,
  maxHeaderBytes: 8 * 1024,
  maxInputBytesPerSecond: 32 * 1024 * 1024,
  maxMessagesPerSecond: 256,
  requestTimeoutMs: 60_000,
  writeTimeoutMs: 10_000,
  partialMessageTimeoutMs: 10_000,
});

const PRIVATE_CONFIGURATION = Object.freeze({
  sendErrors: 'never', traceLog: null, diagnostics: Object.freeze({computeTrigger: 'onType'}),
});

/** Validates framing before vscode-jsonrpc can allocate from an untrusted length. */
class BoundedInput extends Transform {
  private header = Buffer.alloc(0);
  private remaining = 0;
  private frameTimer?: NodeJS.Timeout;
  private windowStart = Date.now();
  private windowBytes = 0;
  private windowMessages = 0;

  override _transform(chunk: Buffer, _encoding: BufferEncoding, callback: TransformCallback): void {
    try {
      if (Date.now() - this.windowStart >= 1000) {
        this.windowStart = Date.now(); this.windowBytes = 0; this.windowMessages = 0;
      }
      this.windowBytes += chunk.length;
      if (this.windowBytes > TRANSPORT_LIMITS.maxInputBytesPerSecond) throw new Error('Language server input rate limit exceeded');
      let cursor = 0;
      while (cursor < chunk.length) {
        this.frameTimer ??= setTimeout(() => this.destroy(new Error('Language server partial message timed out')), TRANSPORT_LIMITS.partialMessageTimeoutMs);
        if (this.remaining > 0) {
          const size = Math.min(this.remaining, chunk.length - cursor);
          this.push(chunk.subarray(cursor, cursor + size));
          cursor += size; this.remaining -= size;
          if (this.remaining === 0) { clearTimeout(this.frameTimer); this.frameTimer = undefined; }
          continue;
        }
        // Header only; incremental scanning is bounded by 8 KiB, including split delimiters.
        const end = Math.min(chunk.length, cursor + TRANSPORT_LIMITS.maxHeaderBytes - this.header.length);
        const joined = Buffer.concat([this.header, chunk.subarray(cursor, end)]);
        const boundary = joined.indexOf('\r\n\r\n');
        if (boundary < 0) {
          this.header = joined; cursor = end;
          if (this.header.length >= TRANSPORT_LIMITS.maxHeaderBytes) throw new Error('Language server header limit exceeded');
          continue;
        }
        const headerLength = boundary + 4;
        const lines = joined.subarray(0, boundary).toString('ascii').split('\r\n');
        const lengths = lines.filter(line => /^content-length:/i.test(line));
        if (lengths.length !== 1 || !/^content-length:\s*[0-9]+\s*$/i.test(lengths[0])) throw new Error('Invalid language server message header');
        const length = Number(lengths[0].split(':')[1]);
        if (!Number.isSafeInteger(length) || length <= 0 || length > TRANSPORT_LIMITS.maxMessageBytes) throw new Error('Language server message size limit exceeded');
        if (++this.windowMessages > TRANSPORT_LIMITS.maxMessagesPerSecond) throw new Error('Language server message rate limit exceeded');
        cursor += headerLength - this.header.length;
        this.header = Buffer.alloc(0);
        this.remaining = length;
        this.push(joined.subarray(0, headerLength));
      }
      callback();
    } catch (error) { callback(error as Error); }
  }

  override _destroy(error: Error | null, callback: (error?: Error | null) => void): void {
    clearTimeout(this.frameTimer);
    callback(error);
  }
}

function privateEnvironment(directory: string): NodeJS.ProcessEnv {
  const env: NodeJS.ProcessEnv = {};
  // PATH resolves the explicitly selected command. No JVM, Node, proxy, credential,
  // project or user-option variables are inherited.
  for (const key of ['PATH', 'SystemRoot', 'WINDIR', 'COMSPEC', 'PATHEXT', 'LANG', 'LC_ALL']) {
    if (process.env[key] !== undefined) env[key] = process.env[key];
  }
  for (const key of ['HOME', 'USERPROFILE', 'APPDATA', 'LOCALAPPDATA', 'TEMP', 'TMP']) env[key] = directory;
  return env;
}

/** Bounds all writes, including JSON-RPC replies to server-initiated requests. */
class BoundedWriter extends StreamMessageWriter {
  private pending = 0;

  constructor(stream: NodeJS.WritableStream, private readonly failTransport: (error: Error) => void) {
    super(stream);
  }

  override async write(message: Message): Promise<void> {
    let snapshot: Message;
    try {
      const serialized = JSON.stringify(message);
      if (Buffer.byteLength(serialized, 'utf8') > TRANSPORT_LIMITS.maxMessageBytes) throw new Error();
      snapshot = JSON.parse(serialized) as Message;
    } catch {
      const error = new Error('Language server output message limit exceeded');
      this.failTransport(error);
      return;
    }
    if (this.pending >= TRANSPORT_LIMITS.maxPending) {
      const error = new Error('Language server output queue limit exceeded');
      this.failTransport(error);
      return;
    }
    this.pending++;
    let timer: NodeJS.Timeout | undefined;
    try {
      await Promise.race([
        super.write(snapshot),
        new Promise<never>((_, reject) => {
          timer = setTimeout(() => {
            const error = new Error('Language server write timed out');
            reject(error);
          }, TRANSPORT_LIMITS.writeTimeoutMs);
        }),
      ]);
    } catch {
      // vscode-jsonrpc 9's sendRequest async executor rethrows writer rejections
      // as an unhandled promise. Disposing the connection rejects its requests;
      // contain the write error here instead of rejecting that executor too.
      this.failTransport(new Error('Language server write failed or timed out'));
    } finally { clearTimeout(timer); this.pending--; }
  }
}

export class BslTransport {
  readonly pid: number;
  private readonly connection: MessageConnection;
  private readonly input = new BoundedInput();
  private readonly failures = new Set<(error: Error) => void>();
  private readonly operations = new Set<(error: Error) => void>();
  private failure?: Error;
  private closing?: Promise<void>;
  private closed = false;

  private constructor(private readonly child: ChildProcessWithoutNullStreams, private readonly directory: string) {
    this.pid = child.pid!;
    const reader = new StreamMessageReader(this.input);
    // BoundedInput owns the partial-frame deadline, including header-only input.
    // Disable jsonrpc's repeating informational timer, which survives reader disposal.
    reader.partialMessageTimeout = 0;
    this.connection = createMessageConnection(reader, new BoundedWriter(child.stdin, error => this.fail(error)));
    this.input.on('error', () => this.fail(new Error('Invalid or excessive language server input')));
    child.stdout.on('error', () => this.fail(new Error('Language server output failed')));
    child.stdin.on('error', () => this.fail(new Error('Language server input failed')));
    child.stderr.on('error', () => this.fail(new Error('Language server stderr failed')));
    // Drain without buffering, forwarding, or logging potentially sensitive content.
    child.stderr.resume();
    child.on('error', () => this.fail(new Error('Language server process failed')));
    child.on('exit', () => this.fail(new Error('Language server exited')));
    this.connection.onError(() => this.fail(new Error('Language server protocol failed')));
    this.connection.onClose(() => this.fail(new Error('Language server connection closed')));
    this.connection.onRequest('workspace/configuration', (params: unknown) => {
      const items = (params as {items?: unknown[]})?.items;
      if (!Array.isArray(items) || items.length > TRANSPORT_LIMITS.maxPending) throw new Error('Invalid configuration request');
      return items.map(() => PRIVATE_CONFIGURATION);
    });
    this.connection.onRequest('window/workDoneProgress/create', () => null);
    this.connection.onRequest('client/registerCapability', () => null);
    this.connection.listen();
    child.stdout.pipe(this.input);
  }

  static async start(binaryPath: string, rootPath: string | undefined, extraArgs: readonly string[] = []): Promise<BslTransport> {
    // rootPath belongs in the adapter's initialize params; it must never become cwd.
    void rootPath;
    const directory = await mkdtemp(path.join(os.tmpdir(), 'onec-bsl-ls-'));
    let child: ChildProcessWithoutNullStreams | undefined;
    try {
      const configPath = path.join(directory, 'configuration.json');
      await writeFile(configPath, JSON.stringify(PRIVATE_CONFIGURATION), {mode: 0o600});
      child = spawn(binaryPath, [...extraArgs, `--configuration=${configPath}`], {
        shell: false, windowsHide: true, stdio: 'pipe', cwd: directory,
        env: privateEnvironment(directory), detached: process.platform !== 'win32',
      });
      const owned = child;
      await new Promise<void>((resolve, reject) => {
        owned.once('spawn', () => { owned.off('error', reject); resolve(); });
        owned.once('error', reject);
      });
      return new BslTransport(child, directory);
    } catch {
      if (child?.pid) await terminateOwnedProcessTree(child);
      await rm(directory, {recursive: true, force: true});
      throw new Error('Could not start BSL language server');
    }
  }

  request<T>(method: string, params: unknown, signal?: AbortSignal): Promise<T> {
    return this.perform<T>(method, params, signal, true);
  }

  notify(method: string, params: unknown): Promise<void> {
    return this.perform<void>(method, params, undefined, false);
  }

  onFailure(listener: (error: Error) => void): Disposable {
    this.failures.add(listener);
    if (this.failure) listener(this.failure);
    return {dispose: () => { this.failures.delete(listener); }};
  }

  close(): Promise<void> {
    if (this.closing) return this.closing;
    this.closed = true;
    for (const reject of this.operations) reject(new Error('Language server transport closed'));
    this.connection.dispose();
    this.child.stdout.unpipe(this.input);
    this.input.destroy();
    this.closing = (async () => {
      await terminateOwnedProcessTree(this.child);
      this.child.stdin.destroy(); this.child.stdout.destroy(); this.child.stderr.destroy();
      await rm(this.directory, {recursive: true, force: true});
      this.failures.clear();
    })();
    return this.closing;
  }

  private fail(error: Error): void {
    if (this.closed || this.failure) return;
    this.failure = error;
    for (const listener of this.failures) { try { listener(error); } catch { /* A consumer cannot prevent cleanup. */ } }
    void this.close().catch(() => { /* close() retains its rejection for the owner. */ });
  }

  private async perform<T>(method: string, params: unknown, signal: AbortSignal | undefined, request: boolean): Promise<T> {
    if (this.closed) throw new Error('Language server transport closed');
    if (signal?.aborted) throw new Error('Language server request cancelled');
    if (this.operations.size >= TRANSPORT_LIMITS.maxPending) throw new Error('Language server pending operation limit exceeded');
    let serialized: string;
    try { serialized = JSON.stringify({jsonrpc: '2.0', id: 0, method, params}); }
    catch { throw new Error('Language server parameters cannot be serialized'); }
    if (Buffer.byteLength(serialized, 'utf8') > TRANSPORT_LIMITS.maxMessageBytes) throw new Error('Language server outbound message too large');

    return new Promise<T>((resolve, reject) => {
      const cancellation = new CancellationTokenSource();
      const abort = (): void => { cancellation.cancel(); reject(new Error('Language server request cancelled')); };
      const finish = (error?: Error, value?: T): void => {
        clearTimeout(timer);
        signal?.removeEventListener('abort', abort);
        cancellation.dispose();
        this.operations.delete(onClose);
        if (error) reject(error); else resolve(value as T);
      };
      const onClose = (error: Error): void => finish(error);
      const timer = setTimeout(() => {
        const error = new Error(`Language server ${request ? 'request' : 'write'} timed out`);
        finish(error);
        this.fail(error);
      }, request ? TRANSPORT_LIMITS.requestTimeoutMs : TRANSPORT_LIMITS.writeTimeoutMs);
      this.operations.add(onClose);
      signal?.addEventListener('abort', abort, {once: true});
      // Keep cancelled wire requests counted until the server responds or its deadline
      // expires, since JSON-RPC cancellation alone does not release the pending request.
      const work = request
        ? this.connection.sendRequest<T>(method, params, cancellation.token)
        : this.connection.sendNotification(method, params) as Promise<T>;
      work.then(value => finish(undefined, value), () => finish(new Error('Language server request failed')));
    });
  }
}
