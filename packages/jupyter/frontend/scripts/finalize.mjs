import { copyFile, readFile, writeFile } from 'node:fs/promises';

// The Jupyter builder uses native path separators on Windows. The manifest
// contains a URL path and must also work when this wheel is installed on Unix.
const manifestUrl = new URL('../../src/onec_runtime_jupyter/labextension/package.json', import.meta.url);
const manifest = JSON.parse(await readFile(manifestUrl, 'utf8'));
manifest.jupyterlab._build.load = manifest.jupyterlab._build.load.replaceAll('\\', '/');
await writeFile(manifestUrl, JSON.stringify(manifest, null, 2) + '\n');
await copyFile(
  new URL('../LICENSE', import.meta.url),
  new URL('../../src/onec_runtime_jupyter/labextension/LICENSE', import.meta.url)
);
await copyFile(
  new URL('../COPYRIGHT', import.meta.url),
  new URL('../../src/onec_runtime_jupyter/labextension/COPYRIGHT', import.meta.url)
);
