# 1C BSL Notebooks

BSL highlighting and static completion, signatures, hover, and
definitions for Python cells whose first line is `%%bsl`, in local Jupyter
notebooks. Microsoft Jupyter keeps ownership of execution and kernel completion.
The extension never changes cell text, language, metadata, or execution inputs.

This VSIX is a preview. Unit and compilation checks run in this repository;
Visual TextMate, Pylance, live kernel coexistence, and POSIX acceptance need
additional validation.

## Install

Use VS Code 1.136 or newer with Microsoft Jupyter, Python/Pylance, and
`1c-syntax.language-1c-bsl` installed separately. Install the VSIX
with **Extensions: Install from VSIX**, or:

```powershell
code --install-extension .\bsl-notebook-0.1.3.vsix
```

The installed BSL extension starts and manages its language server. This
extension reuses that server through VS Code's BSL providers and does not
start another process. Its former `onecBsl.languageServerPath` setting is no
longer used.

`onecBsl.serverStartup` controls when notebook support activates the installed
BSL extension. The default, `onDemand`, leaves the shared server alone while
opening a notebook and starts it on the first BSL completion, hover, signature,
or definition request. That first request can wait for server startup and
indexing. `onNotebookOpen` restores eager startup when a notebook containing
`%%bsl` opens. Opening a regular `.bsl` file can still start the standard BSL
extension independently of this setting. Change the mode in VS Code Settings
under **1C BSL Notebooks: Server Startup**.

## Select saved sources

Open the project folder in VS Code so it contains the saved sources; opening
the folder one level above `src` or a Designer export works. In a multi-root
workspace, put this folder first. If that folder contains several configurations,
the shared server may combine their symbols. Then open a local `.ipynb` and run **1C BSL:
Выбрать исходники проекта** or click its BSL status item. Select a Designer
export, EDT `src`, or the parent of an EDT project inside that first workspace
folder. **Сменить исходники проекта** changes the selection;
**Сбросить исходники проекта** clears the explicit selection; the BSL server
continues to use the first workspace folder.

The status shows the selected directory and shared-server workspace. With no
selection, the standard BSL server still uses the first VS Code workspace
folder. The selection identifies the source boundary for notebook navigation;
it cannot redirect the shared server to a directory outside that workspace.
Indexing can continue after the status becomes `ready`.

Selection lives only in memory for each open notebook and clears on close,
rename, extension-host restart, or VS Code restart. It is never written to the
notebook, workspace settings, or extension storage. This is separate from the
runtime's existing `RuntimeSessionConfig.source_root`; the extension cannot
read or verify that runtime value.

## Scope and limits

- Each active notebook has a temporary `.bsl` file in the OS temporary
  directory containing its current `%%bsl` bodies in order. Provider requests
  read that file without opening an editor or creating an unsaved document.
  The file is removed when the session expires or the notebook closes. All
  requests use the one language server managed by `1c-syntax.language-1c-bsl`.
  The standard extension indexes saved project files in the first workspace
  folder.
- Only local `file:` notebooks and readable, unlinked local directories are
  supported. Symlinks, junctions, virtual filesystems, live infobase metadata,
  and automatic source transfer are outside this version's scope.
- Static completion yields `Контекст.*` requests to the existing kernel matcher.
  Actual combined Jupyter/Pylance/kernel behavior still needs manual acceptance.
  Ordinary `.bsl` and `.os` editors remain with the BSL extension.
- The bridge resolves at most eight leading completion items per request; later
  items remain insertable but may lack documentation loaded on demand by the
  standard extension.
- Notebook diagnostics are disabled in shared-server mode. VS Code exposes the
  standard extension's diagnostics without a document version, so forwarding
  them could put errors from an old cell layout onto a newer one. Diagnostics
  for ordinary `.bsl` files remain with the standard BSL extension.
- No NotebookController, hidden cell execution, Python gateway, JupyterLab
  dependency, or source copying is introduced. The temporary `.bsl` file does
  contain notebook code while its BSL session is active; a forced VS Code or
  system shutdown may leave it in the OS temporary directory.

## Build and check

```powershell
npm --prefix packages/vscode ci
npm --prefix packages/vscode run test:unit
npm --prefix packages/vscode run test:host
npm --prefix packages/vscode run compile
npm --prefix packages/vscode run package
```

The extension-host suite includes the installed BSL extension and checks
platform and saved-module completion in a workspace opened above the source
directory. `test:integration` still exercises the old private-transport code,
which is no longer used by the extension runtime.
