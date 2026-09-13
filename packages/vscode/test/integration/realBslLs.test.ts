import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import * as fs from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import {setTimeout as delay} from 'node:timers/promises';
import {pathToFileURL} from 'node:url';
import {test} from 'node:test';
import {NotebookSnapshot} from '../../src/notebookModel';
import {NotebookLspSession, createTransportFactory} from '../../src/notebookSession';
import {validateSourceRoot} from '../../src/projectRoot';

const binary = process.env.ONEC_BSL_LANGUAGE_SERVER;
const call = 'Результат = ProbeServer.ДисковыйВызов(1);';
const snapshot = NotebookSnapshot.fromCells([{uri: 'cell:probe', kind: 'code', languageId: 'python', text: `%%bsl\n${call}`}], 1);
const moduleText = (parameter: string, extra = '') => `Функция ДисковыйВызов(${parameter}) Экспорт\nВозврат ${parameter};\nКонецФункции\n${extra}`;

// Top-level node tests execute sequentially; only one native server is alive.
for (const layout of ['designer', 'edt-src', 'edt-parent'] as const) {
  test(`native 1.0.7 ${layout}: saved signatures, canonical create/delete, metadata rename and process loss converge`, {skip: !binary, timeout: 180_000}, async () => {
    const fixture = await fs.mkdtemp(path.join(os.tmpdir(), `bsl-native-${layout}-`));
    let session: NotebookLspSession | undefined;
    try {
      const {root, module} = await createFixture(fixture, layout);
      const expected = await fingerprint(fixture);
      const selected = await validateSourceRoot(root);
      session = await NotebookLspSession.open('file:///probe.ipynb', snapshot, selected.canonicalPath, createTransportFactory(binary));
      assert.equal(session.status, 'ready', session.reason);
      await eventually(session, 'textDocument/signatureHelp', call.length - 2, 'Первоначальный');
      await eventually(session, 'textDocument/completion', 'Результат = ProbeServer.'.length, 'ДисковыйВызов');
      assert.deepEqual(await fingerprint(fixture), expected);
      const pid = session.epoch.processId;
      const changed = moduleText('ПослеСохранения', 'Процедура НовыйЭкспорт() Экспорт\nКонецПроцедуры\n');
      await fs.writeFile(module, changed);
      expected[path.relative(fixture, module)] = digest(Buffer.from(changed));
      await session.filesChanged([{uri: pathToFileURL(module).href, type: 2}]);
      await eventually(session, 'textDocument/signatureHelp', call.length - 2, 'ПослеСохранения');
      await eventually(session, 'textDocument/completion', 'Результат = ProbeServer.'.length, 'НовыйЭкспорт');
      assert.equal(session.epoch.processId, pid, 'ordinary save must preserve its native child');
      // Removing/recreating the canonical module must converge before metadata rename.
      await fs.unlink(module); delete expected[path.relative(fixture, module)];
      await session.filesChanged([{uri: pathToFileURL(module).href, type: 3}]);
      await absent(session, 'НовыйЭкспорт');
      await fs.writeFile(module, changed); expected[path.relative(fixture, module)] = digest(Buffer.from(changed));
      await session.filesChanged([{uri: pathToFileURL(module).href, type: 1}]);
      await eventually(session, 'textDocument/completion', 'Результат = ProbeServer.'.length, 'НовыйЭкспорт');
      const source = layout === 'designer' ? fixture : path.join(fixture, 'src');
      const oldDirectory = path.join(source, 'CommonModules/ProbeServer');
      const newDirectory = path.join(source, 'CommonModules/RenamedServer');
      const oldMetadata = path.join(source, layout === 'designer' ? 'CommonModules/ProbeServer.xml' : 'CommonModules/ProbeServer/ProbeServer.mdo');
      const metadataText = (await fs.readFile(oldMetadata, 'utf8')).replaceAll('ProbeServer', 'RenamedServer');
      const newMetadata = path.join(source, layout === 'designer' ? 'CommonModules/RenamedServer.xml' : 'CommonModules/RenamedServer/RenamedServer.mdo');
      const config = path.join(source, layout === 'designer' ? 'Configuration.xml' : 'Configuration/Configuration.mdo');
      const configText = (await fs.readFile(config, 'utf8')).replaceAll('ProbeServer', 'RenamedServer');
      await fs.rename(oldDirectory, newDirectory);
      await fs.unlink(layout === 'designer' ? oldMetadata : path.join(newDirectory, 'ProbeServer.mdo'));
      await fs.writeFile(newMetadata, metadataText); await fs.writeFile(config, configText);
      const newModule = path.join(newDirectory, layout === 'designer' ? 'Ext/Module.bsl' : 'Module.bsl');
      delete expected[path.relative(fixture, module)]; delete expected[path.relative(fixture, oldMetadata)];
      expected[path.relative(fixture, newModule)] = digest(Buffer.from(changed));
      expected[path.relative(fixture, newMetadata)] = digest(Buffer.from(metadataText));
      expected[path.relative(fixture, config)] = digest(Buffer.from(configText));
      const renamedCall = call.replace('ProbeServer', 'RenamedServer');
      await session.update(NotebookSnapshot.fromCells([{uri: 'cell:probe', kind: 'code', languageId: 'python', text: `%%bsl\n${renamedCall}`}], 2));
      await session.filesChanged([{uri: pathToFileURL(oldMetadata).href, type: 3}, {uri: pathToFileURL(newMetadata).href, type: 1},
        {uri: pathToFileURL(module).href, type: 3}, {uri: pathToFileURL(newModule).href, type: 1}, {uri: pathToFileURL(config).href, type: 2}]);
      await eventually(session, 'textDocument/completion', 'Результат = RenamedServer.'.length, 'НовыйЭкспорт');
      await eventually(session, 'textDocument/signatureHelp', renamedCall.length - 2, 'ПослеСохранения');
      await session.diagnose();
      process.kill(session.epoch.processId);
      const lossDeadline = Date.now() + 5000;
      while ((session.status as string) !== 'unavailable' && Date.now() < lossDeadline) await delay(25);
      assert.equal(session.status, 'unavailable'); assert.ok(session.reason); assert.deepEqual(session.diagnostics, []);
      await session.close();
      assert.deepEqual(await fingerprint(fixture), expected);
    } finally {
      await session?.close();
      await fs.rm(fixture, {recursive: true, force: true});
    }
  });
}

async function absent(session: NotebookLspSession, text: string): Promise<void> {
  const deadline = Date.now() + 20_000;
  let last: unknown;
  do {
    last = await session.query('textDocument/completion', 'cell:probe', {line: 1, character: 'Результат = ProbeServer.'.length});
    if (last != null && !JSON.stringify(last).includes(text)) return;
    await delay(250);
  } while (Date.now() < deadline);
  assert.fail(`deleted module completion retained ${text}; status=${session.status}; last=${JSON.stringify(last)}`);
}

async function eventually(session: NotebookLspSession, method: string, character: number, text: string): Promise<void> {
  const deadline = Date.now() + 45_000;
  let last: unknown;
  do {
    last = await session.query(method, 'cell:probe', {line: 1, character});
    if (JSON.stringify(last)?.includes(text)) return;
    await delay(250);
  } while (Date.now() < deadline);
  assert.fail(`${method} did not contain ${text}; status=${session.status}; last=${JSON.stringify(last)}`);
}

async function createFixture(fixture: string, layout: string): Promise<{root: string; module: string}> {
  const source = layout === 'designer' ? fixture : path.join(fixture, 'src');
  const module = path.join(source, 'CommonModules/ProbeServer', layout === 'designer' ? 'Ext/Module.bsl' : 'Module.bsl');
  const config = layout === 'designer' ? 'Configuration.xml' : 'Configuration/Configuration.mdo';
  const metadata = layout === 'designer' ? 'CommonModules/ProbeServer.xml' : 'CommonModules/ProbeServer/ProbeServer.mdo';
  const content = layout === 'designer' ? [
    '<?xml version="1.0" encoding="UTF-8"?><MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.20"><Configuration uuid="7b22aaef-49a8-4f1b-9c1b-515394df8d01"><Properties><Name>OnecLspProbe</Name><ScriptVariant>Russian</ScriptVariant></Properties><ChildObjects><CommonModule>ProbeServer</CommonModule></ChildObjects></Configuration></MetaDataObject>',
    '<?xml version="1.0" encoding="UTF-8"?><MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses" version="2.20"><CommonModule uuid="7b22aaef-49a8-4f1b-9c1b-515394df8d03"><Properties><Name>ProbeServer</Name><Global>false</Global><Server>true</Server><ServerCall>true</ServerCall></Properties></CommonModule></MetaDataObject>',
  ] : [
    '<?xml version="1.0" encoding="UTF-8"?><mdclass:Configuration xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" uuid="06ca1e23-8338-4cf8-8391-511f62e6d0db"><name>OnecLspProbe</name><scriptVariant>Russian</scriptVariant><commonModules>CommonModule.ProbeServer</commonModules></mdclass:Configuration>',
    '<?xml version="1.0" encoding="UTF-8"?><mdclass:CommonModule xmlns:mdclass="http://g5.1c.ru/v8/dt/metadata/mdclass" uuid="d178221d-9ed0-47dc-adc8-43a680cf1ae8"><name>ProbeServer</name><server>true</server><serverCall>true</serverCall></mdclass:CommonModule>',
  ];
  for (const [relative, text] of [[config, content[0]], [metadata, content[1]], [path.relative(source, module), moduleText('Первоначальный')]]) {
    const destination = path.join(source, relative);
    await fs.mkdir(path.dirname(destination), {recursive: true});
    await fs.writeFile(destination, text);
  }
  return {root: layout === 'edt-parent' ? fixture : source, module};
}

function digest(bytes: Buffer): string { return createHash('sha256').update(bytes).digest('hex'); }
async function fingerprint(root: string): Promise<Record<string, string>> {
  const found: Record<string, string> = {};
  async function scan(directory: string): Promise<void> {
    for (const entry of await fs.readdir(directory, {withFileTypes: true})) {
      const file = path.join(directory, entry.name);
      if (entry.isDirectory()) await scan(file);
      else found[path.relative(root, file)] = digest(await fs.readFile(file));
    }
  }
  await scan(root);
  return found;
}
