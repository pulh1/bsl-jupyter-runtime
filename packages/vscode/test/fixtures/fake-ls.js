const {spawn} = require('node:child_process');
const fs = require('node:fs');
const {createMessageConnection, StreamMessageReader, StreamMessageWriter} = require('vscode-jsonrpc/node');

if (process.argv.includes('--grandchild')) {
  setInterval(() => {}, 1000);
} else {
  const rpc = createMessageConnection(new StreamMessageReader(process.stdin), new StreamMessageWriter(process.stdout));
  let opened;
  rpc.onRequest('initialize', () => ({capabilities: {hoverProvider: true}}));
  rpc.onNotification('textDocument/didOpen', params => { opened = params; });
  rpc.onRequest('test/opened', () => opened);
  rpc.onRequest('test/configuration', () => rpc.sendRequest('workspace/configuration', {items: [{section: 'bsl'}, {section: 'anything'}]}));
  rpc.onRequest('test/serverRequest', ({method, params}) => rpc.sendRequest(method, params));
  rpc.onRequest('test/environment', () => ({
    cwd: process.cwd(), env: process.env,
    configuration: JSON.parse(fs.readFileSync(process.argv.find(arg => arg.startsWith('--configuration=')).slice(16), 'utf8')),
  }));
  rpc.onRequest('test/grandchild', () => new Promise((resolve, reject) => {
    const child = spawn(process.execPath, [__filename, '--grandchild'], {stdio: 'ignore', windowsHide: true});
    child.once('spawn', () => resolve(child.pid));
    child.once('error', reject);
  }));
  rpc.onRequest('test/hang', () => new Promise(() => {}));
  rpc.onRequest('test/pauseInput', () => { process.stdin.pause(); return true; });
  rpc.onNotification('test/flood', () => {
    const message = JSON.stringify({jsonrpc: '2.0', method: 'test/noop', params: {}});
    process.stdout.write((`Content-Length: ${Buffer.byteLength(message)}\r\n\r\n${message}`).repeat(1000));
  });
  rpc.onRequest('test/exit', () => { process.exit(7); });
  rpc.onNotification('test/oversize', () => process.stdout.write('Content-Length: 999999999\r\n\r\n'));
  rpc.onNotification('test/badHeader', () => process.stdout.write('Content-Length: nope\r\n\r\n'));
  rpc.onNotification('test/stderr', () => process.stderr.write(Buffer.alloc(1024 * 1024, 120)));
  rpc.onNotification('test/secretError', () => process.stdout.write('Content-Length: 15\r\n\r\nsecret-password'));
  rpc.listen();
  setInterval(() => {}, 1000);
}
