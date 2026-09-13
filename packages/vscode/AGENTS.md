# VS Code extension

This is the TypeScript editor integration. Keep notebook and editor lifecycle state in this package and share the BSL server session through the existing coordinator. Do not duplicate runtime execution logic from Python.

Use `npm --prefix packages/vscode ci`, `npm --prefix packages/vscode run test:unit`, and `npm --prefix packages/vscode run compile` for changes here. Do not commit `node_modules`, compiled output, or extension-host temporary workspaces.
