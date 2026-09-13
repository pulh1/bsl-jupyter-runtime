# Runtime-bound Jupyter BSL language services: acceptance evidence

Measured 2026-09-09 on Windows. This report distinguishes a real browser/kernel
and native language server with a test RuntimeSession backend from live 1C.
No live database opt-in was configured. Live reload and the <=5% p95 runtime
bridge budget are **UNVERIFIED**, not passed by the protocol measurements below.

## Environment and installation

- Python 3.13.14 isolated installed-wheel environment; JupyterLab 4.6.3,
  jupyterlab-lsp 5.3.0, jupyter-lsp 2.3.1, jupyter-server 2.21.0,
  ipykernel 6.31.0; native BSL language server 1.0.7; headless Edge.
- Fresh standalone `onec-interactive-jupyter` 0.1.0 wheel, rebuilt after the
  disconnected-position hover guard. Kernel/server import checks require
  `site-packages`, clear PYTHONPATH and use a temporary unrelated server cwd.
  Only explicit test-backend helper directories are added by startup code.
- Isolated `python -m jupyter labextension list` reports onec 0.1.0 and
  jupyterlab-lsp 5.3.0 enabled/OK; server extension listing reports onec and
  jupyter_lsp enabled/OK. Prepend the isolated Scripts directory to PATH so the
  Jupyter subcommand dispatcher does not select a global installation.
- Packaging tests install the prebuilt standalone wheel with no Node on PATH,
  no parsergen environment and no source-cwd shadowing. The base install imports
  core/Jupyter without a separate core/MCP distribution or optional LSP packages.
  Developer wheel builds still require Node. Do not install the separate core
  wheel alongside the standalone Jupyter wheel: both own `onec_runtime`.

## Acceptance matrix

| Item | Evidence and boundary |
|---|---|
| 1. Virtual-only before startup / null root | Real browser built-in and cross-cell local completion, native hover, mapped diagnostics; decoy EDT export inside notebook root is not indexed. Explicit `install_runtime` with `source_root=None` remains virtual-only. |
| 2. Automatic project binding | Real kernel comm/server/frontend startup and complete browser lifecycle passed for Designer, EDT parent and EDT src; real protocol passed the same layouts. Unavailable host root visibly falls back to virtual editing. |
| 3. Accepted source and reload | Real RuntimeSession/RuntimeAPI fixture publication A→B changes completion and immutable definition; confirmed failure preserves B; unknown promotion visibly invalidates matching. Exact accepted source differs from disk. Marked live test exists but is skipped without both opt-ins. |
| 4. Isolation / lifecycle | Browser: two notebooks sharing a kernel isolate locals, independent kernels sharing a root isolate accepted versions, rename/reconnect, late attachment, runtime close/replacement. Kernel-incarnation/restart/change and separate-root behavior additionally covered by kernel/context/gateway/frontend tests. |
| 5. Stale authority | `test_jupyter_lsp_gateway.py`, `test_jupyter_lsp_contexts.py`, frontend runtime fence/binding tests, real protocol stale hover response; final delayed browser completion/diagnostic/lease/restart gate passed, including transition-version diagnostics after acknowledged replacement. |
| 6. Privacy / readonly / paths | `test_jupyter_lsp_sources.py`, context/HTTP/WebSocket authorization tests; browser immutable accepted A editor stays A after B, readonly widget/model, endpoint writes/traversal denied, project fingerprints and notebook source-file set unchanged, no snapshot outputs/metadata. New real configured-root junction and linked-ancestor regressions exercise pre-resolve validation; symlink cases skip when Windows privilege is absent. |
| 7. Complete test/package gates | Fresh wheel build/install successful; frontend 28 tests and TypeScript check passed. Final offline run:3132 passed,16 skipped,24 deselected,1 Windows ZMQ warning in270.29s. Existing lease/stale-response browser and all3 runtime-browser layouts passed, each final run with0pageerrors. |
| 8. Performance | Real native-child cold/warm and process-tree measurements below. Executable live bridge-on/off harness provided, not run. Runtime publication→browser ready/new-completion latency is unmeasured. |

The actual-config no-follow fix rejects Windows junction/reparse roots and linked
ancestors before `resolve()` can erase their identity. Ordinary Designer, EDT
src/parent and null-root configs still reach RuntimeSession snapshots. This is a
runtime-neutral config check; it does not start 1C or inspect an existing database.

## Measurement boundaries

Final full offline command, using the documented parsergen baseline
`58f17f5af2be3ebf48c30aced8133f182128f32d` and candidate
`618dfa1463321088800ed220e8f4a7655172a99c` source directories through
`ONEC_PARSERGEN_BASELINE_SRC`, `ONEC_PARSERGEN_SRC` and
`ONEC_PARSERGEN_CANDIDATE_SRC`:

```powershell
$env:PYTHONPATH='src;packages/jupyter/src;packages/mcp/src;.'
python -m pytest -m 'not integration and not live_1c and not soak' -q
```

Final installed-wheel browser commands (PYTHONPATH removed):

```powershell
python tools/check_jupyter_lsp_browser.py --server artifacts/lsp-server/bin/bsl-language-server/bsl-language-server.exe --output artifacts/lsp-browser-task6-final
python tools/check_jupyter_lsp_runtime.py --server artifacts/lsp-server/bin/bsl-language-server/bsl-language-server.exe --output artifacts/lsp-runtime-task6-final --layout all
```

Both exited0. Each layout's source-free `runtime-summary.json` lists14 checks,
zero known host errors in the final run, live status UNVERIFIED and browser
publication-latency status UNMEASURED. The earlier two detached-observer errors
remain a disclosed host compatibility issue, not silently removed from evidence.
Post-reload harness readiness explicitly waits for the public app/restoration
promise and activates the renamed notebook before typing. No source snapshot,
credential or raw protocol frame is written to these summaries.

Real protocol command (installed wheel, PYTHONPATH removed):

```powershell
artifacts/lsp-env/Scripts/python.exe tools/check_jupyter_bsl_lsp.py --server artifacts/lsp-server/bin/bsl-language-server/bsl-language-server.exe --output artifacts/lsp-task6-protocol-final --samples 20 --warmup 3
```

All layouts passed rootless completion/pull diagnostics, separate overlays,
signature, immutable definition, warm update, stale fence and no source writes.
Each row has 3 warmup iterations and 20 measured iterations, nearest-rank p95.
Cold is one initialization/indexing plus two completion requests, not a p95.
These are small fixtures on a shared test host, not large-project sizing results.

| Layout | Cold ms | Warm completion p50 / p95 ms | Overlay barrier + completion p50 / p95 ms | Context payload bytes |
|---|---:|---:|---:|---:|
| Designer | 10773.64 | 9.56 / 11.13 | 20.51 / 23.18 | 893 |
| EDT parent | 10765.55 | 7.19 / 8.21 | 13.73 / 17.23 | 834 |
| EDT src | 11495.11 | 9.55 / 14.28 | 21.08 / 25.88 | 834 |

Actual warm child launcher PID was unchanged throughout each measured update:
40040→40040, 41220→41220 and 2524→2524 respectively. Corresponding process trees
were [40040,39304], [41220,35848] and [2524,40428], also checked unchanged.
Each layout had 3 language-server children (rootless and two project contexts),
each a 2-process launcher/JVM tree. Per-tree resident byte samples were:

- Designer: 386998272, 623378432, 619175936.
- EDT parent: 386998272, 623304704, 515534848.
- EDT src: 387932160, 616714240, 583577600.

Memory includes JVM descendants, not just the roughly 10 MB native launchers;
it is a point-in-time RSS sample, not peak/private memory. Gateway protocol
overlay timing excludes runtime execution, source-bridge scheduling, private
control polling and frontend status refresh. It therefore cannot establish
runtime publication→browser readiness or the <=5% runtime-overhead requirement.
A synchronous busy kernel cell can delay IOLoop comm delivery until it finishes.

The executable live command, only after explicit owned-fixture configuration:

```powershell
$env:ONEC_RUN_JUPYTER_LSP_INTEGRATION='1'
$env:ONEC_RUN_WORKER_UNIVERSE_INTEGRATION='1'
$env:ONEC_BSL_LANGUAGE_SERVER='C:\tools\bsl-language-server\bsl-language-server.exe'
python -m pytest tests/integration/test_jupyter_lsp_runtime_1c.py -m live_1c -q
```

The inherited harness validates exact platform 8.3.27.2170 executables and creates
only its owned temporary infobase, never selecting an existing database. It tests
live A/B publication and a real parse-rejected reload, then alternates off/on
order across 23 pairs of same-shape reloads (3 warmup,20 measured per arm).
Source-free temporary `lsp-live-summary.json` records per-arm median/p95,
`p95_regression`, `budget:0.05`, `budget_status:PASS|FAIL`, separate convergence, payload bytes, child tree
count/RSS and PhaseRecorder phases. The p95 regression assertion is executable.
Its comm sink is explicitly in-process; actual kernel/browser transport is
covered separately. No live sample or passing budget is claimed here.

## Warnings and compatibility

`npm audit --omit=dev --json` reports **20 moderate,0 high,0 critical**, all
propagating from sanitize-html 2.12.1 pinned by @jupyterlab/apputils 4.7.3. The
tested JupyterLab host's own staging lock also pins 2.12.1. The onec extension's
third-party license inventory has no sanitize-html entry; it shares host
apputils. This is a host runtime exposure concern, not dismissed as build-only.

- [GHSA-vccv-cmxp-4j9h](https://github.com/advisories/GHSA-vccv-cmxp-4j9h): URI-bearing attribute handling, fixed in 2.17.5.
- [GHSA-g8qq-57p8-ggw5](https://github.com/advisories/GHSA-g8qq-57p8-ggw5): SVG SMIL URI-list handling, fixed in 2.17.7.
- [GHSA-jxwj-j7wr-gfrw](https://github.com/advisories/GHSA-jxwj-j7wr-gfrw): textarea/xmp parser differential, fixed in 2.17.6.

The installed host sanitizer allows textarea and video.poster while applying its
URI scheme gate to href/cite; SVG animate was not present in the inspected allow
list. These facts warrant caution but do not establish all exploit prerequisites:
no exploit was run. No forced audit upgrade, blanket npm override or clean-audit
claim was made; changing only this wheel's npm graph would not fix the host copy.
Builder output also reports legacy @jupyterlab/builder and labextension-build
deprecations. The offline suite emits the existing Windows ZMQ Proactor selector
thread warning, without a broad warning filter.

Native hover initially failed after moving through a disconnected magic header:
jupyterlab-lsp 5.3 retained a rejected hover promise. A public highest-precedence
mousemove guard now skips only disconnected notebook positions. Connected
languages retain native handling; real multiline drag and subsequent native
tooltip passed. Already-poisoned instances require browser reload. No private
upstream state repair or Python language-server dependency was added.

Late diagnostics for disposed BSL documents were separately reproduced at the
native callback: `isDisposed=true`, `language='bsl'`, `documentInfo=null` with a
valid response URI. A narrow public feature decorator skips only this terminal
BSL predicate; all other calls preserve the original receiver, arguments and
errors, including live asynchronous failures. No private listener/state changes
or broad exception catch was added.

JupyterLab 4.6.3 also schedules a notebook content-visibility observer callback
which may run after detachment has cleared the observer. The browser harness
records the exact known host message/stack category and count in source-free
artifacts; any unexpected error still fails. It does not patch host widget
internals or claim zero page errors for a run containing this category.

Windows shutdown counting initially found raw228→229 handles. A bounded native
inspection identified the extra object as the loader-retained canonical system
`kernel32.dll.mui`, not a pipe. Final test-only accounting derives the system
directory/UI locale and compares exact file identity, never a basename or count
allowance. Only regular disk-file handles receive identity queries; pipes,
devices and unqueryable identities remain counted. All non-exempt handles must
show zero growth over each shutdown cycle; thread and timeout limits remain.
Real pipe/event/file/same-basename-file leak regressions pass. Focused samples
reported raw227/excluded0/counted227 throughout; a separate test opens that exact
system resource and proves raw+1/excluded+1 without changing counted handles.
This is scoped ownership-aware test evidence, not a raw-total leak-free claim.

Large-project indexing, POSIX
process cleanup, Notebook 7 LSP and live 1C remain outside measured evidence.
