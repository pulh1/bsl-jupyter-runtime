# Source-based BSL language services for JupyterLab

The optional `[lsp]` integration provides static completion, hover, diagnostics,
signature help and read-only definitions through jupyterlab-lsp and an external
BSL Language Server. It analyzes current saved project files and the notebook's
current BSL cells. Before project configuration it analyzes only those cells.
It never indexes the notebook directory or Jupyter working directory implicitly.

## Install

Install the matching core and Jupyter wheels from the `v0.1.18` release. The prebuilt wheels need no Node.js/npm; a developer build of the Jupyter frontend does.

```powershell
python -m pip install .\onec_interactive_runtime_core-0.1.18-py3-none-any.whl
python -m pip install '.\onec_interactive_jupyter-0.1.18-py3-none-any.whl[lsp]' "jupyterlab>=4.1,<5"
```

Installing the prebuilt wheels needs neither Node.js nor parsergen. The Jupyter
wheel requires the exact matching `onec-interactive-runtime-core` wheel; only the
core wheel owns `onec_runtime` and the bundled 1C extension. LSP dependencies
remain optional; core-only consumers do not acquire Jupyter or LSP dependencies.

Install the **same Jupyter/core wheel pair** in both server and kernel environments
if they differ. The kernel configuration protocol and frontend/server must match. Use
`--force-reinstall` when replacing version `0.1.0`, stop/restart the Jupyter server
and kernels, and reload the browser after upgrading.

Install the external [BSL Language Server 1.0.7 native distribution](https://github.com/1c-syntax/bsl-language-server/releases/tag/v1.0.7),
including its adjacent runtime/application directories. This LGPL-3.0-or-later
component is not bundled; its native distribution needs no global Java. Set the
executable on the **server host**, or put it on PATH:

```powershell
$env:ONEC_BSL_LANGUAGE_SERVER = 'C:\tools\bsl-language-server\bsl-language-server.exe'
jupyter lab
```

`jupyter labextension list` should show `@onec-interactive/jupyter-bsl` and
`@jupyter-lsp/jupyterlab-lsp`; `jupyter server extension list` should show
`onec_runtime_jupyter.lsp_server_extension` and `jupyter_lsp`. Missing language
server support produces unavailable/degraded status; runtime execution and
highlighting remain independent.

## Bind a project in the startup cell

Use the existing runtime startup workflow with an authorized dedicated infobase:

```python
from pathlib import Path
from onec_runtime.session import RuntimeSessionConfig
from onec_runtime_jupyter import InteractiveRuntimeSession

runtime = InteractiveRuntimeSession.start(
    RuntimeSessionConfig(
        runtime_config,             # existing RuntimeConfig for your dedicated infobase
        evidence_root,
        source_root=Path(r"C:\exports\configuration"),  # optional
    )
)
```

`RuntimeSessionConfig.source_root` is the sole project root. Omitting it or using
`None` keeps language services virtual-only. No environment or implicit cwd root
fallback exists. A direct `install_runtime()` call also establishes the bridge;
late notebook attachment discovers current state without rerunning startup.
`configure_capture_source()` configures a separate capture resolver and does not
change the catalog/LSP root.

Accepted layouts are Designer (`CommonModules/Name.xml` and
`CommonModules/Name/Ext/Module.bsl`) and EDT (`CommonModules/Name/Name.mdo` and
`Module.bsl`), with either EDT `src` or its parent as the configured root.
Ambiguous layouts, linked roots/ancestors and unsafe linked sources are rejected.
The configured path must exist on the kernel host and be readable on the
Jupyter/BSL LS host. A remote/unreadable server path yields visible virtual-only
fallback, without implicit path mapping.

Native 1C and an authorized infobase are needed to execute runtime code. They are
not needed for static rootless editing; the language server does not replace 1C.

## Editing and reload behavior

- `%%bsl` cells form a virtual document in notebook order. The magic is removed
  only for analysis; notebook text and execution inputs remain unchanged.
- Saved file edits update analysis without a Worker reload. In-memory Worker
  sources can differ from disk: loading, failing to load, or releasing a runtime
  generation does not change project analysis. Runtime generation retention,
  operation pins, rollback and debugger source maps retain their own semantics.
- Each notebook has its own child language server and notebook-local declarations.
  Shared kernels supply the same configured root; notebook-local declarations
  remain isolated even when several kernels use that root.
- Status distinguishes virtual only, indexing/updating, ready and unavailable.
  Ready means query availability; `index-convergence-unconfirmed` explicitly
  does not certify completion of asynchronous indexing. Execution errors and
  generation identities belong to the runtime UI.
- Closing the runtime retains the selected static project. Reinstallation
  replaces it, including `source_root=None`. Kernel restart/change and extension
  unload clear it. Late attachment and reconnect obtain current configuration.
- Jump to Definition opens a current read-only `.bsl`/`.os` file through
  `onec-bsl:`, including files outside Jupyter root. Each read is authorized
  against the current binding and reads current bytes. Reopen/refresh updates an
  existing viewer. Deleted or unreadable files show unavailable; any displayed
  old text is marked as not current. There are no historical source leases.

Only configuration and opaque installation identity travel over the authenticated
kernel comm. Project sources and Worker snapshots are not sent through it or
stored in notebook outputs/metadata. The gateway writes no project or virtual
files. It uses bounded filesystem observation and watched-file notifications;
canonical module/metadata creation, deletion or rename can recreate the LS for
reindexing. Ordinary same-path saves retain the child. Diagnostics do not require
editing `.bsl-language-server.json`.

Owner, notebook/kernel access, binding, extension and size checks apply to source
reads. Writes, traversal and linked escapes are denied. JupyterLab LSP 5.3's
failed-navigation fallback under the exact `.lsp_symlink/onec-bsl:` namespace is
always denied. A normal file-load error dialog and read-only unavailable fallback
may appear; this does not provide a filesystem alias or historical source copy.
The FileEditor adapter's reserved `file:///.../onec-bsl:...` document is excluded
from notebook replay, including refresh/reopen version resets. Opening a source
viewer never creates a notebook child or project binding.

Pending socket claims expire after 30 seconds and release their claim capacity.
Once a socket has successfully submitted an explicit binding claim, new document
associations on that socket also require an explicit claim. Already selected
valid bindings and live claims remain usable; expiry cannot select a stale or
different binding implicitly. Sockets that never successfully claim retain the
legacy automatic association behavior. Custom clients that mix explicit claims
and implicit association must claim each new document after their first accepted
claim. This uses bounded socket state without retaining expired-URI tombstones.

## Limits and evidence

The latest full Windows benchmark, R32 on the earlier product bytes, completed all functional/safety checks but
retains a formal **FAIL** against the unchanged 5% reload p95 limit: LSP off
1038.1013 ms, LSP on 1114.1602 ms (+7.3267%). The user accepted the residual
deviation for this handoff; it is not a passing numerical gate or statistical
proof that the difference is noise. The ON maximum of 2257.1445 ms remains in
the sample. This section reports verification evidence, not a formal review verdict.

The dedicated runtime heartbeat empty-response poll was reduced from 5.0 to
0.1 seconds, preserving the normal 15-second cadence, shared operation lock,
event ingestion and selected-target checks. This HTTPX timeout scalar is not a
total heartbeat deadline. A separate fixed 75-second real idle sample verified
four natural heartbeats and a subsequent load/canary; it does not prove arbitrary
lease duration or every near-timeout event-delivery case. In the full post-fix
run all 40 measured traces were observed with zero known-held heartbeat overlap
before profiling. The initial warmup remained censored, so global trace coverage
is INCOMPLETE. The remaining slowdown is not causally identified by that trace.

The historical R31 installed candidate was
`artifacts/task5-heartbeat-r31-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `1579405AFA1AFAEEC24E19662525984536F420D391B68A345FDDE94F763388C2`.
Its core identity and all nine source-based LSP module identities were verified
against that worktree. Native protocol and browser evidence was produced at R27
on the unchanged LSP modules/frontend and pinned server, not by rerunning the
browser against this subsequently rebuilt heartbeat-corrected wheel. The normal
build/install instructions above are unchanged; do not mistake an older local
`dist/jupyter` file for this identified candidate.

Post-R32 review corrected two benchmark-harness defects: an inventory failure
must not skip independent owned cleanup, and a late initial-population end must
not bypass or revive timeout admission. Windows gateway/nested tests now check
exact native termination receipts immediately after ordinary close. Product,
owner, frontend and installed-wheel bytes did not change in this correction.
The retained R27/R32 artifacts identify the older harness, not a strict proof of
the corrected harness. Their recorded initial populations were timely; no new
native, browser or live benchmark was run. The results document below records
both tool hashes and this historical-evidence boundary.

The subsequent whole-branch lifecycle fixes change five Jupyter LSP modules and
add an internal cleanup helper. Socket and extension unload attempt independent
cleanup stages even after failure or cancellation, observe background outcomes,
and retain failures for callers. Expired binding authority is revoked immediately;
kernel task cancellation and channel shutdown run on their owning event loop with
bounded, observed completion. Retired gateway locks and idle metadata are reclaimed
after active users and waiters drain. These fixes do not change the runtime core,
native process owners, frontend, workload or 5% performance threshold.

Focused source verification passed 143 tests in the base environment (29 skipped)
and 166 in the optional environment (6 skipped), including 19 lifecycle regression
cases. The first full gate exposed a child traversal-order regression, corrected while preserving the
existing test; fixed-seed gateway/lifecycle verification passed 80 optional tests
and 70 base tests (10 skipped). The whole-branch-wave installed candidate was
`artifacts/wholefix-final-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`, SHA256
`A641AB8F0F944DC8668C213760042034400BD4588776D254E0A723A59F42B99A`.
A fresh isolated import check confirmed equality of all 92 Python files across
source, wheel and site-packages, including the new helper. The final full offline
gate passed 3415 tests (56 skipped, 24 deselected), with one existing Proactor/ZMQ
selector warning. Native initial-index and protocol fixtures passed in Designer,
EDT parent and EDT src, including unchanged source trees; initial-index cleanup
left zero children/watchers. The full optional LSP suite passed 452 tests (7 skipped)
without warnings. Ordinary browser fixture checks and all 16 runtime-browser
fixture checks for each of Designer, EDT parent and EDT src passed with zero
host/page errors. These used a real kernel/browser/native LS with a test runtime;
live 1C and the runtime performance budget were not exercised. All 13 packaged
frontend entries matched the previously verified R31 wheel.
These are functional checks; the older R27/R32 evidence does not measure the
changed bytes' performance. R32's formal FAIL and practical acceptance
remain historical; no new performance run is claimed.

A subsequent focused correction prevents a pending kernel-client start from
reopening after close during its initial cleanup wait. Repeated close revokes
that pending start while preserving its cleanup completion; an explicitly
requested later start remains supported. The lifecycle check and reopen are
atomic with worker-thread close, and overlapping starts retain at most one
owned incarnation. Seven new regressions pass; focused lifecycle/kernel/context
verification passed 76 base tests (11 skipped) and 87 optional tests without
warnings. Its installed candidate is
`artifacts/startup-close-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `8F013E8E25C1444E3C1FE5B2B7840194C24E6A7570E3C6C817ED2F4B48290765`.
All 92 Python source/wheel/site-packages files match in an isolated import check;
the four runtime-core pins and all 13 packaged frontend entries are unchanged.
The fresh full offline gate passed 3422 tests (56 skipped, 24 deselected), with
the known Proactor/ZMQ fallback warning. The fresh optional suite passed 459 tests
(7 skipped) without warnings. The new Designer browser attempt failed at startup
with a `method-not-supported` dialog before exercising runtime scenarios.
Bounded diagnostics reproduced the same rejection of ordinary configuration
notifications in both the current and previous unchanged gateway, without using
the kernel client. The browser's exact settings payload was not retained.
At that stage this separate gateway defect remained open; the 8F013 browser gate
failed and did not establish runtime-browser acceptance. Earlier native
initial/protocol evidence was reused only for its nine unchanged modules and
matching tools/binary; it does not exercise this kernel client. The A641 wheel
and its all-layout browser outcomes above remain historical. New LSP performance
remains unmeasured; the historical R32 exception is unchanged.

The separately authorized gateway correction now consumes ordinary valid
`workspace/didChangeConfiguration` notifications without a response or dialog,
child creation/forwarding, or changes to project/private native-LS settings.
Private `onecProjectBinding` claims still use the unchanged authenticated
WebSocket checks; malformed messages, requests and unknown methods retain their
existing rejection. Eighteen focused boundary checks pass, including the real
HTTP/WebSocket ordinary-settings/private-claim sequence.
The new standalone installed candidate is
`artifacts/config-notification-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `E0AB4396926ED8B3466DE1B26195FF4B66664D059039257178D2442A2232F6D2`.
All 92 Python source/wheel/site-packages files match; all 13 frontend entries
match 8F013. The fresh full offline gate passed 3439 tests (57 skipped,
24 deselected), with the known Proactor/ZMQ fallback warning. Fresh native
initial-index and all 11 protocol checks passed in Designer, EDT parent and EDT
src with current nine-module/tool/binary provenance. Those runs overlapped
offline tests, so their timings are functional observations only. The fresh
full optional suite passed 477 tests (7 skipped) without warnings. The fresh
Designer runtime-browser gate passed all 16 checks with zero host/page errors;
126 configuration notifications produced no `window/showMessage`, with the
existing startup no-dialog assertion unchanged. This exercised real ipykernel,
browser and native LS with a test RuntimeAPI, not live 1C or its performance
budget. Other-layout browser outcomes remain historical. These verification
results are not a code-review verdict.
A prior focused optional run's existing handle-count
test failed at 245 versus baseline 244, then passed separately; the cause is
unresolved and that failure is retained. No DB/performance run was added.

This release targets JupyterLab 4 with jupyterlab-lsp 5.3. Notebook 7 LSP, classic
Notebook 6, VS Code, rename/refactoring and cross-cell formatting are not claimed.
Analysis follows cell order, not execution order, and does not infer live values.
BSL LS 1.0.7 signature help was verified inside a complete call; an unfinished
`Method(` can return no signature.

The extension guards mousemove only at notebook positions without a ready LSP
connection. This avoids jupyterlab-lsp 5.3 retaining a failed hover request after
the `%%bsl` header is hovered; connected languages keep the native tooltip and
mouse selection remains available. An already-poisoned upstream hover instance
requires a browser reload; installing the new wheel does not repair its state.
When the Gateway discards an in-flight hover after content/context/child changes
or cancellation, it returns the protocol's empty hover result. Stale content is
still fenced out; other methods and genuine language-service, timeout or access
failures remain errors. No private upstream promise state is modified.

Named constructor limits bound configuration payloads, scans, source reads,
queues and child lifetimes. Exhaustion is visible degradation and does not alter
runtime execution. Workspace observation retries a finite number of times; after
that, remove the final subscription and subscribe again, or restart the gateway.
A configuration heartbeat does not restart failed observation. There is no
automatic remote filesystem mapping or silent Windows polling fallback.

Windows process ownership uses a launch-time Job and a bounded daemon collector.
Normal close requires native termination signals reconciled with the Job's
lifetime process count, within the existing five-second forced-close deadline.
The collector retains at most 64 process handles and reclaims them during normal
child churn. Missing notifications, uncapturable short-lived children or native
API uncertainty make cleanup explicitly unconfirmed; accounting-zero alone is
not success. Routine completion-port waits are limited to 100 ms and do not
trigger process scans. The Windows-managed notification queue has no claimed fixed cap.

The [source-based acceptance results](research/2026-09-09-source-based-jupyter-lsp-results.md)
record current gates, actual measurements and unverified boundaries. The
[older runtime-bound results](research/2026-09-08-runtime-bound-jupyter-lsp-results.md)
are historical and do not verify this architecture. Fixture timings cannot
establish the real runtime budget. The tested JupyterLab host retains moderate
sanitize-html advisories and an intermittent ipykernel/pyzmq shutdown ENOTSOCK
trace; these are disclosed separately from wheel dependencies and test outcomes.

## Reproduce

Use an isolated environment with the built wheel `[lsp]`, pytest and Playwright.
The browser check uses Edge; for Linux use `--channel chromium` after installing
Playwright Chromium. Run from the feature checkout root; browser processes clear
PYTHONPATH and import the installed wheel. Only explicit test helpers are added
to the kernel import path.

```powershell
python tools/check_jupyter_bsl_lsp.py --server C:\tools\bsl-language-server\bsl-language-server.exe
python tools/check_jupyter_lsp_browser.py --server C:\tools\bsl-language-server\bsl-language-server.exe
python tools/check_jupyter_lsp_runtime.py --server C:\tools\bsl-language-server\bsl-language-server.exe --layout all
python tools/check_jupyter_lsp_zup.py --server C:\tools\bsl-language-server\bsl-language-server.exe --probe-fixtures
```

Browser checks use a test RuntimeSession backend with real ipykernel, server,
frontend transport and BSL LS. They execute explicit startup/reload fixtures,
not 1C. Evidence includes counts/timings only, never raw sources/frames or tokens.
The default Designer fixture is outside the temporary Jupyter root; protocol
checks also create temporary EDT src/parent fixtures.

The separately gated live tool accepts only the established exact benchmark DB,
platform, installed extension and frozen real module pair through the existing
read-only `_exact_live_inputs()` admission. Private connection values are supplied
only in process environment. It uses `ExtensionMode.MANUAL`, requires the expected
handshake, and never installs fixture/native configuration, repairs the extension,
writes the actual export or invokes business operations. A failed admission or
startup stops the run. Ordinary pytest keeps this opt-in disabled.

After the complete offline and browser gates, supply same-code all-layout native
initial-index and protocol proof artifacts, and run without concurrent suites or
indexing work:

```powershell
$env:ONEC_RUN_JUPYTER_LSP_INTEGRATION='1'
python tools/check_jupyter_lsp_zup.py --live --server C:\tools\bsl-language-server\bsl-language-server.exe --initial-index-proof artifacts\initial-index\evidence.json --protocol-proof artifacts\protocol\evidence.json --output artifacts\live-lsp
```

The fixed workload alternates full LSP off/on for 3 warmup pairs and 20 measured
pairs. Off has no owned LS child or workspace watcher. On establishes a fresh
indexed project and active observer before timing. Startup, initial index,
representative completion/definition, generation release and teardown are outside
the single public reload-call timer. Setup cost, RSS, payload metadata bytes,
source-free phases and p50/p95 are reported separately, with actual <=5% p95
budget PASS/FAIL and physical DB identity/owned-process cleanup checks.

The cold-initialization probe is pinned to the tested BSL LS 1.0.7 binary and exact
English/Russian progress resource pairs. It requires the created find-files token
to end, then a matched population begin/end on another created token, current
root/child/revision and actual language-service results. Reports at 100% happen
before processing completes and are not a barrier. This benchmark-only initial
observation does not change product readiness or certify watched-update convergence.
