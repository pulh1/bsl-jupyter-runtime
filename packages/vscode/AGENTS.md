# VS Code extension

This is the TypeScript editor integration for local `%%bsl` notebook cells. `NotebookCoordinator` owns notebook session state; `SharedBslSession` reuses the installed 1C BSL extension's language server for static editor features. Microsoft Jupyter owns execution and kernel completion. Do not duplicate Python runtime execution or MAIN/CAPTURE state here.

Yield `e1cRuntimeКонтекст.*` static completion requests to the Jupyter kernel matcher; the VS Code syntax check must not claim live value knowledge. Keep source selection within the first workspace folder and temporary notebook BSL documents out of saved project sources.

Use `npm --prefix packages/vscode ci`, `npm --prefix packages/vscode run test:unit`, and `npm --prefix packages/vscode run compile` for changes here. Check `test/unit/featureMapping.test.ts` when changing kernel completion handoff. Do not commit `node_modules`, compiled output, or extension-host temporary workspaces.
