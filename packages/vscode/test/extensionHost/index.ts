import * as smoke from './smoke.test';
import * as projectRoot from './projectRoot.test';
import * as projectWatch from './projectWatch.test';
import * as editorFeatures from './editorFeatures.test';
import * as lifecycle from './lifecycle.test';
import * as sharedServer from './sharedServer.test';

export async function run(): Promise<void> {
  await smoke.run();
  await sharedServer.run();
  await projectRoot.run();
  await projectWatch.run();
  await editorFeatures.run();
  await lifecycle.run();
}
