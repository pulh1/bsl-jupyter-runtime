import * as path from 'node:path';
import {downloadAndUnzipVSCode, runTests, runVSCodeCommand} from '@vscode/test-electron';

async function main(): Promise<void> {
  const vscodeExecutablePath = await downloadAndUnzipVSCode('1.136.1');
  for (const id of ['ms-toolsai.jupyter', 'ms-python.python',
                    'ms-python.vscode-pylance', '1c-syntax.language-1c-bsl']) {
    await runVSCodeCommand(['--install-extension', id], {version: '1.136.1'});
  }
  await runTests({
    vscodeExecutablePath,
    extensionDevelopmentPath: path.resolve(__dirname, '../..'),
    extensionTestsPath: path.resolve(__dirname, 'extensionHost/index.js'),
    launchArgs: [path.resolve(__dirname, '../../test/fixtures/workspace')],
  });
}

void main().catch(error => {console.error(error); process.exitCode = 1;});
