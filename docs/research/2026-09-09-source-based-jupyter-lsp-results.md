# Source-based Jupyter BSL language services — acceptance results

Updated: 2026-09-10. The latest R32 full run, on product bytes preceding the
whole-branch lifecycle fixes below, completed functional/safety checks
but retains **formal budget FAIL: +7.3267% against the unchanged 5% p95 limit**.
The user accepted this residual for finalization; no threshold, sample or FAIL
was changed, and no repeated-run uncertainty estimate proves statistical noise
or equivalence. This section reports verification evidence, not a formal review
verdict. Historical
generation-overlay results do not verify the source-based contract.

## Ordinary configuration notification correction — current candidate

After the failed 8F013 Designer gate below, the user separately authorized a
bounded gateway correction. Ordinary valid `workspace/didChangeConfiguration`
notifications are consumed without a response/dialog, child creation/forwarding,
configuration storage or project/private native-LS settings mutation. Valid
`params.settings` is LSPAny (including null, scalar and list), not just an object.
The message must have no `id` key and raw object params containing settings.
Reserved `onecProjectBinding` settings are not swallowed: their authenticated
WebSocket interception, exact shape/authorization and selection remain unchanged.
Malformed ordinary messages, private claims reaching Gateway, requests and
unknown methods retain existing errors; this is not a generic JSON-RPC rewrite.
The startup client and lifecycle tests are byte unchanged from the focused
correction described below.

TDD observed seven valid-setting Gateway cases fail on the actual
`window/showMessage(method-not-supported)` output, and an actual HTTP/WebSocket
case fail at an initialize-response barrier on the same output. After the
six-line product correction, **18 targeted checks passed**: seven ordinary
settings, ten rejection boundaries and one real socket sequence with ordinary
settings before/after an authorized private claim and document open. It uses
response barriers, not sleeps, and malformed private claims still close 1008.
Focused base gateway/WebSocket checks passed **83 tests, 19 skipped** (6.90s).

The first focused optional run had **1 failed, 95 passed, 6 skipped** (16.14s):
the existing isolated-process handle test
`test_owned_gateway_releases_handles_across_repeated_launch_and_failed_assignment`
failed at `test_jupyter_lsp_websocket.py:367`, after awaited owned close, confirmed
child exit, deletion and GC, measuring **245 handles against baseline 244**.
Handle identities and iteration number were not recorded; the cause remains
unknown. That test launches a stdin-reading Python child, not Gateway.handle.
An exact-case repeat passed (2.24s), and a focused optional repeat passed
**96 tests, 6 skipped** (15.95s). Neither PASS explains or erases the first
failure; no native-owner change or weakened handle assertion followed.

The controller built and installed only the new standalone wheel:
`artifacts/config-notification-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `E0AB4396926ED8B3466DE1B26195FF4B66664D059039257178D2442A2232F6D2`.
An isolated `python -I -B` check matched all **92 Python files** across source,
wheel and imported site-packages; all **13 packaged frontend entries** match
8F013. Four runtime-core pins remain unchanged. No frontend rebuild or separate
core wheel is claimed.

Fresh full offline verification passed **3439 tests, 57 skipped, 24 deselected**
in **263.48s**, exit 0, with one known Proactor/ZMQ selector fallback warning
and explicit source PYTHONPATH/`PYTHONHASHSEED=2`.
Fresh installed native evidence now includes the changed Gateway:

- `artifacts/config-notification-initial-index/evidence.json`, SHA256
  `23D4C73439B96811110FCB44A440F8530D70837D6053196DC2150FB5B4C13BAA`:
  Designer, EDT parent and EDT src initial population/completion/definition PASS,
  each ending with zero remaining children/watchers.
- `artifacts/config-notification-protocol/evidence.json`, SHA256
  `D4D8EFE05C62F2B37E2166240B6294748CDF32713D91CEF7D279C5D10CA24205`:
  all three layouts and all 11 checks PASS, including saved signatures,
  delete/create/rename, current definitions, stable PID, viewer exclusion and
  unchanged source trees.

The controller matched all nine current module pins, each harness and native
binary for both artifacts. Native runs overlapped the full offline suite;
timings are functional observations, not isolated performance measurements.
The independent fresh full optional suite passed **477 tests, 7 skipped** in
**71.43s**, exit 0, without warnings, including the unchanged strict isolated
handle-count test. The earlier 245-versus-244 failure remains unexplained, not
relabeled as fixed.

The fresh installed-wheel Designer runtime-browser scenario exited 0 with
**all 16 checks PASS**, zero known host/page errors and `browser-errors=[]`.
The existing startup no-dialog assertion is unchanged. Saved LSP inventory
contains **126 configuration notifications and no window/showMessage**, while
completion, hover, definitions, diagnostics and private binding continued
through lifecycle stages. Checks include None/reinstall, unavailable root,
restart/kernel change, shared/different-root isolation, rename/reconnect, saved
disk independent of accepted/failed/unknown Worker state, viewer deletion/restore,
exact fixture restoration and no source outputs or binding metadata.
This uses real ipykernel/browser/native LS with a **test RuntimeAPI**, not live
1C or a runtime budget measurement. Artifacts under
`artifacts/config-notification-browser-runtime/designer/`:

- `runtime-summary.json`, SHA256
  `0CCFEB9F82EB31B0C56A03215779DC5A44ADC51B998908931BC83A2C36E2CD41`.
- `lsp-summary.json`, SHA256
  `844E1B4AE00CC02E859379FD40618EE9F37D74B7925A08C1E9A217493405472B`.
- `browser-errors.json`, SHA256
  `4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945`.

All planned current functional gates are complete. These verification results
are not a code-review verdict. The prior 8F013 browser failure and older all-layout
browser results remain historical, not reruns on E0AB. No live DB or performance
run occurred. New LSP performance remains **UNMEASURED**, and R32's formal
FAIL/user exception is unchanged.

## Historical startup/close candidate and failed browser gate

The capped whole-branch scoped review found all four original findings addressed,
but reproduced a new startup/close gap in the A641 candidate: registry shutdown
during the client's initial close-completion await removed the binding, yet the
pending start subsequently reopened channels and listener/monitor tasks. An
explicitly authorized focused follow-up changes only `lsp_kernel_client.py` in
product. Every close now advances the existing lifecycle generation even when
reusing the same cleanup Future. Startup captures that authority before waiting
and checks/reopens/registers its tasks atomically under the existing lock, with
no lock held across an await. Explicit fresh later starts still work.

Seven new regressions exercise registry close, unbind, invalidation, worker close
during the actual early drain, overlapping starts, repeated close during old-task
draining with a fresh later start, and a controlled worker interleaving at reopen.
All seven failed against the exact prior client module and pass after the fix.
Final focused lifecycle/kernel/context gates passed **76 tests, 11 skipped** in
base (9.17 seconds) and **87 tests** in the optional environment (9.02 seconds),
without warnings, with explicit source PYTHONPATH and `PYTHONHASHSEED=2`.

A test-boundary issue was separately diagnosed: an inexhaustible immediate fake
shell reply starved asyncio after close. The fixture now delivers one response
per request through a real asyncio Queue. This fixture correction is neither a
product shutdown diagnosis nor an explanation of historical ENOTSOCK.

The controller built and installed the new standalone candidate at
`artifacts/startup-close-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `8F013E8E25C1444E3C1FE5B2B7840194C24E6A7570E3C6C817ED2F4B48290765`.
The isolated `python -I -B` check passed equality of all 92 Python source/wheel/
imported site-packages files. The four runtime-core pins are unchanged. An
explicit inventory compared all 13 package/shared-data frontend entries equal
between A641 and the new wheel; source frontend/tools/core diffs are empty.

Earlier `wholefix-final-initial-index` and `wholefix-final-protocol` evidence is
reused after matching all nine recorded module hashes, each harness hash and
the native binary hash against current files. This is exact unchanged-component
evidence, not a fresh native run or coverage of the changed kernel client, which
is absent from those nine modules. Initial comparison adjustments normalized
hexadecimal case and included shared-data frontend entries; final explicit
comparisons passed without product changes.

The fresh full offline gate passed **3422 tests, 56 skipped, 24 deselected** in
233.88 seconds, exit 0, with one known Proactor/ZMQ selector fallback warning
(`PYTHONHASHSEED=2`). The fresh optional LSP suite passed **459 tests, 7 skipped**
in 67.77 seconds, exit 0, with no warnings. The new Designer browser attempt
exited 1 at its startup-dialog check (`browser.py:309`), before
`exercise_browser_runtime`, with `Message from onec-bsl / method-not-supported`.
`artifacts/startup-close-browser-runtime/designer/` contains `lsp-summary.json`
and `browser-errors.json`, but no runtime summary; browser-errors is an empty
list. The saved inventory contains three configuration notifications and one
window/showMessage, alongside otherwise handled startup methods.

Bounded read-only asyncio-debug probes of the current Gateway and the exact
BASE `8f6e5fd` Gateway both reproduced the same window/showMessage rejection for
ordinary `workspace/didChangeConfiguration` with `settings={}`, without using
a child, KernelProjectClient, native LS or DB. The WebSocket consumes private
binding claims but forwards ordinary configuration notifications; the gateway
rejects the latter as unsupported. Gateway bytes are unchanged by this client
fix. The exact original browser settings payload was not retained, so the probe
does not claim to recover it or explain why earlier runs lacked the dialog.

The configuration-notification defect was separate from the focused client
correction and required separate scope authorization. At that point no retry,
assertion weakening, gateway edit, commit or final acceptance followed this
failed gate. The later authorized correction has its own evidence above.
These artifacts do not establish a passing runtime-browser scenario. Prior A641
all-layout and ordinary-browser runs below remain historical
evidence on earlier client bytes. Core, heartbeat,
frontend, native owners, workload and 5% threshold are unchanged. New LSP
performance remains **UNMEASURED**; there is no new live DB/performance run and
R32's +7.3267% formal FAIL/user exception remains historical.

## Whole-branch lifecycle corrections and evidence boundary

The consolidated fix wave addresses all four findings in the whole-branch review:

- Socket and extension unload attempt each independent cleanup stage despite
  detach, owner, control, socket-close or other stage failure. A shared internal
  helper observes background outcomes, retains the first failure with bounded
  secondary failure notes, and finishes cleanup before propagating cancellation.
  Unload explicitly starts the idempotent socket cleanup callback even when
  transport close fails. Native owner deadlines and deferred ownership are unchanged.
- Registry pruning revokes binding authority synchronously. Kernel client task
  cancellation, comm-close and channel shutdown execute on their owning asyncio
  loop; callers await bounded completion outside registry locks. Restart drains
  the prior incarnation, including an in-flight connection, before replacing it.
  A real ControlServer worker test with asyncio debug enabled exercises this path
  using actual tasks and controlled channel boundaries that record thread identity.
- Expired preopen claims release capacity during admission and reconciliation.
  After the first successfully admitted explicit claim, a socket requires claims
  for new document associations, preserving existing valid selections and live
  claims. Failed first claims do not switch mode. Never-claim sockets keep legacy
  fallback. Mixed custom clients must explicitly claim subsequent new documents;
  no unbounded URI tombstones or new wire fields are introduced.
- Gateway lock usage includes holders and waiters. Retirement reclaims locks and
  idle metadata after users drain, including failed child creation. Reappearance
  uses the existing lock until that drain completes; cancellation cannot split
  one binding across two locks. Coverage includes 100 sequential retired bindings
  and a real waiter/retirement/reappearance race.

Focused verification on the amended source: optional environment **166 passed,
6 skipped** in 26.96 seconds; base environment **143 passed, 29 skipped** in
19.56 seconds. Both commands covered lifecycle, kernel, context, gateway,
WebSocket and control tests; neither emitted pytest warnings. The new lifecycle
module contains 19 cases, including bounded task timeout and cancellation
regressions. Root owns the fresh full gates, new wheel identity and separately
admitted native/browser functional verification; these focused results do not
claim those outcomes.

The controller built and installed a provisional lifecycle-fix candidate at
`artifacts/wholefix-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`, SHA256
`D4DE192609BCFC1D466412E609959D721CC57D5431F302B01DDE916FFC41BCFB`.
An isolated working-directory `python -I -B` import check compared all 92 Python
files across source, wheel and installed site-packages and passed, including
`lsp_cleanup.py`. All seven code/test freeze hashes independently matched.
An initial controller temporary-directory cleanup hit a Windows cwd issue;
the corrected isolated identity check passed. This was not a product failure.
The first controller full gate reported **1 failed, 3414 passed, 56 skipped,
24 deselected**, with one known Proactor warning (261.50 seconds). The failure
was the unchanged global-diagnostics fencing test: adding orphan-lock retirement
through a set union accidentally changed child traversal from insertion order
to hash order. With `PYTHONHASHSEED=2`, the exact test failed deterministically;
preserving insertion order while appending orphan locks made it pass without
editing the test. Seed-2 gateway/lifecycle covering checks then passed **80 tests**
in the optional environment (10.29 seconds) and **70 passed, 10 skipped** in base
(5.06 seconds), with no warnings. This is a scoped correction within the same
fix wave, not a retry-to-green attribution.

The gateway changed after that provisional wheel's identity check, so the
`D4DE...` wheel is historical and does not certify final bytes. The controller
then rebuilt and installed
`artifacts/wholefix-final-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `A641AB8F0F944DC8668C213760042034400BD4588776D254E0A723A59F42B99A`.
A fresh isolated `python -I -B` check again passed equality of all 92 Python
files across source, wheel and site-packages. The final full offline gate with
fixed reproducer condition `PYTHONHASHSEED=2` passed: **3415 passed, 56 skipped,
24 deselected** in 282.88 seconds, exit 0, with one existing Proactor/ZMQ selector
fallback warning. The final optional LSP suite passed **452 tests, 7 skipped** in
69.34 seconds, exit 0, without warnings, using the same fixed seed and explicit
source PYTHONPATH. The unchanged frontend passed 32 tests and typecheck; all 13
packaged frontend entries match the previously verified R31 wheel, and the final
source frontend diff is empty.

On the final wheel, the native initial-index fixture check passed in Designer,
EDT parent and EDT src, including initial population, completion and definition;
each fixture ended with zero children and zero watchers. Evidence:
`artifacts/wholefix-final-initial-index/evidence.json`, SHA256
`4FDEF4BBD992FC42DB37266D8A1F38896C355D5434B9673239D92E29230E8F33`.
Its current gateway/context pins match the final source. This used synthetic
fixtures and an in-process ProjectBridge, without a browser, kernel or live DB.
The check overlapped offline tests, so it is functional evidence, not an isolated
performance measurement.

The final native protocol check also passed all three layouts. Its 11 checks
include source-viewer exclusion from replay and no source writes; the overall
`source_tree_unchanged=true` result covers all three checked layouts, and all nine
recorded module pins matched.
Evidence: `artifacts/wholefix-final-protocol/evidence.json`, SHA256
`5E5E657E7161E259CF35E98E786F64D0EFCB9FE3256279BC87D986EFAC3BBCB5`.
The controller read the complete JSON. Like the initial-index fixture, this ran
alongside offline tests and provides functional evidence without a standalone
runtime performance inference.

The ordinary browser fixture check passed against the final installed `A641...`
wheel, including frontend behavior, runtime close, None installation,
reinstallation, reconnect and restart. Under
`artifacts/wholefix-final-browser-normal/`, `lsp-summary.json` has SHA256
`E9AE7924EA75A5F6F1E404B2C3ADDACC2052B7E694499E9F5F30700BEBDCD704`.
The diagnostic window passed with transient maximum 0, panelChanged false and
queued 0 (`diagnostic-window.json`, SHA256
`46BC2982255D63A68706981AE9611171A26720B5798647B248D7E88E8968B3B6`).
`browser-errors.json` is an empty list, with zero host/page errors (SHA256
`4F53CDA18C2BAA0C0354BB5F9A3ECBE5ED12AB4D8E11BA873C2F11161202B945`).
These are fixture outcomes, not a live database/runtime performance check or a
retrospective explanation of the historical intermittent errors below.

The final all-layout runtime-browser fixture check passed all 16 checks in each
of Designer, EDT parent and EDT src. The backend was
`test-runtime-real-kernel-browser-native-lsp`, using the final A641 wheel. Each
summary reported zero known host/page errors and an empty errors list; exact
restoration, source-output and binding-metadata assertions also passed in stdout.
The three files at
`artifacts/wholefix-final-browser-runtime/{designer,edt-parent,edt-src}/runtime-summary.json`
have the same SHA256
`0CCFEB9F82EB31B0C56A03215779DC5A44ADC51B998908931BC83A2C36E2CD41`.
The controller read all three summaries and error records. Their boundaries remain
explicit: `live_1c=UNVERIFIED`, `runtime_budget=SEPARATE_LIVE_GATE`. No new live
database or runtime performance run was performed. These final installed-wheel
checks complete functional verification of the amended bytes without extending
the historical R32 performance acceptance to a new measurement.

Five product modules changed (`lsp_websocket`, `lsp_server_extension`,
`lsp_contexts`, `lsp_kernel_client`, `lsp_gateway`) and `lsp_cleanup` was added.
The runtime core, native process owners, frontend, dependencies and acceptance
tools are unchanged by this wave. R27/R32 and the R31 wheel identities below
remain historical evidence for their earlier bytes. In particular, R32's
**formal budget FAIL: +7.3267% against 5%** and the user's practical acceptance
do not constitute a new performance measurement of the amended wheel. There
was no performance retry, retuning or new real-database run in this fix wave.

The independently reproduced wrong-thread prune defect does not establish the
cause or repair of historical ENOTSOCK output. Historical generic hover `-32001`,
the once-observed post-None hover failure, platform limitations, host advisories,
warnings and indexing-convergence limits remain unresolved as recorded below.

## Historical R32 result and user-accepted exception

One separately admitted post-fix run completed the original 3 warmup plus 20
measured counterbalanced pairs: 46 arms, 20 measured loads per mode. Startup,
initial indexing, handle release and teardown remained outside the unchanged
single-public-load timer. OFF had no LS/watcher; ON created a fresh indexed LS
and active watcher before each load. No arrival was selected, heartbeat forced,
sample dropped, timeout retuned during measurement or retry performed.

| Public load timing | LSP off | LSP on |
| --- | ---: | ---: |
| p50 | 945.1699 ms | 1017.2391 ms |
| p95 | 1038.1013 ms | 1114.1602 ms |
| Maximum | 1041.8858 ms | 2257.1445 ms |

The original nearest-rank calculation uses sorted[ceil(n*fraction)-1], so p95
is the 19th ascending sample of 20. Its exact increase is 7.3267319867446945%.
The allowed ON p95 is 1090.006365 ms: the result exceeds it by about 24.154 ms,
or 2.3267 percentage points beyond the limit. The user stated: "Два процента
отклонения выше нормы это в рамках шума". This is recorded as practical acceptance
of this specific residual, not a statistical finding, general waiver or PASS.

Session41927 exited1; final scoped native/runtime/Java/test inventory was0 at
2026-09-10T06:57:56.1644555Z before artifact inspection. Two compatible MANUAL
handshakes, existing canary, complete source-tree oracle, stable physical DB
identity, zero remaining owned target processes and all46 child/watcher cleanup
checks passed. Exact target/workload admission and independent final checks were
unchanged. No source/configuration/business writes, automatic repair or alternate
target were used. The measurement ran without concurrent controller test/build/
scan work; this does not claim a universally quiet operating system.

All40 measured traces are OBSERVED/uncensored. Their independently recomputed
known-held heartbeat overlap with worker-entry-to-profile-entry is0. Only initial
warmup ordinal0 is UNKNOWN/censored: global trace INCOMPLETE is preserved, not
used to explain or hide the numeric FAIL. There are67 returned heartbeats,
134 command records and368 boundary events, no fault/overflow/raised/unfinished
heartbeat, and confirmed heartbeat join/hook restoration. Trace caps remain
2048 heartbeats,8192 commands,1024 events and46 arms.

The ON maximum (ordinal41/pair20) is2257.1445ms outer versus2256.7358ms inner
end_to_end, with no recorded heartbeat overlap even inside its profile interval.
Catalog validation is205.6753ms. Subtracting the four non-end_to_end recorded
totals leaves2050.0503ms arithmetic inner residual. That residual is not a measured operation
or identified cause. Across measured loads, inner p95 is1037.6721ms OFF versus
1113.8198ms ON; catalog p95 is285.0359ms versus313.5852ms. These correlated phase
values cannot establish causality, and independent phase percentiles are not
additive. The original outer timer alone determines the performance gate.

Authoritative completed artifacts and SHA256:

- `artifacts/task5-heartbeat-benchmark-r32/evidence.json`:
  `F6C35ECADDAE0C27D4F67E42829E8100DD515EAF1DECD0136CC429A2D15417CE`.
- `artifacts/task5-heartbeat-benchmark-r32/timing-trace.json`:
  `D33259754C588308259F1D6649DE15D9F4B214B1CC25B8EB2B6FAAB06C100C09`.
- `artifacts/task5-heartbeat-benchmark-r32/wrapper-provenance.json`:
  `F6BF5F482377078DC78C2611B952AA2C9009A834E501E21A5D864FB73B6527A4`.

Strict public/trace/provenance validation, exact all46 arm/duration/inner-time
associations, all40 overlap unions, original metrics and hash/status links passed
independent worker/controller checks. The wrapper binds the unchanged original
tool/R30 adapter to reviewed post-fix core identities; it does not replace their
timer or artifacts.

## Heartbeat mechanism, correction and bounded idle evidence

R28's artificial noDB hold and R29's deliberately aligned single real no-LSP load
demonstrated only their scoped serialization mechanism. Later R30 observed the
unchanged full natural-arrival workload: four measured ON pre-profile brackets
overlapped known-held heartbeat bodies by4262.9498,2641.5082,445.5047 and4259.0818ms.
Those overlaps explain each bracket to within0.1ms in that run, not retrospectively
the earlier R27 tails. R30 original outer p95 remained FAIL (+408.98076695%);
inner p95 was also about9.064% higher. Evidence:
`artifacts/task5-causal-timing-r30-baseline/evidence.json`, SHA256
`D17E50F7CCD34F03A9B68C8F893BAF3D5C62A297930041A9E1D556858738FEA5`, and
`timing-trace.json`, SHA256
`FBA8DECB53F61CEDBF936A0CCD246B3BC7952E557C021879BAC063991E22782D`.

The user-approved R31 correction changes only the dedicated heartbeat `_poll`
argument from5.0 to0.1 seconds. Normal15-second cadence, shared operation lock,
lease identity, event ingestion, selected-target checks, active/default poll
timeouts and capture/runtime semantics remain. This HTTPX scalar affects timeout
dimensions, not a total100ms heartbeat deadline. Actual RuntimeSession/RdbgSession/
Transport regressions cover blocked-load timing, real parsed stop/evaluation/local
data consumption, transient recovery, missing targets and serialized attachment/
breakpoint replay. Final scoped suite passed282 tests with1 existing Windows
symlink-privilege skip; root's subsequent full offline gate passed3393 with46
skips and24 deselections. The fresh finalization full offline gate passed3393
tests,46 skipped,24 deselected in239.89s, with the existing Windows Proactor/ZMQ
selector-fallback warning. Fresh optional `test_jupyter_lsp*.py` passed420 tests,
7 skipped in65.62s, no warnings. Frontend passed32/32 tests in1530.3474ms, no
skips/failures, and TypeScript `tsc --noEmit` exited0. Skips are not passes. No
test was duplicated by the documentation worker, and no new native/browser run
is claimed.

A separately admitted R32 no-LSP idle sample observed a fixed75.0087105-second
window with4 returned/verified natural heartbeats, then one public pair load and
IDLE canary. All safety checks passed, join/restoration confirmed and final scoped
process count0. Idle status PASSED while zero-arm global trace INCOMPLETE remains.
Artifact `artifacts/task5-heartbeat-idle-r32/evidence.json`, SHA256
`24F6A2A315AF7E08206B827859F73B6F03D488D13E4F5D437C087F15F3A4ADCD`.
This does not prove arbitrary lease duration or event delivery at every timeout
edge. Fixed `no-http-with-error` metadata identifies no particular exception type.

## Current installed candidate and evidence reuse

Latest installed standalone wheel:
`artifacts/task5-heartbeat-r31-wheel/onec_interactive_jupyter-0.1.0-py3-none-any.whl`,
SHA256 `1579405AFA1AFAEEC24E19662525984536F420D391B68A345FDDE94F763388C2`.
Root built/reinstalled it with `--reinstall --no-deps`; fresh isolated imports
verified reviewed core4 == worktree == frozen and all nine LSP modules == worktree.
The source-based modules/frontend and pinned BSL LS binary stayed unchanged from
the accepted R27 native/protocol/browser gates. Their reuse is same-component
identity evidence, not a browser rerun against this newly rebuilt wheel.

Current-owner initial proof `artifacts/task5-initial-index-r27-deadline/evidence.json`
has SHA256 `831701AB2CEDC3DA991A1CC9B66236D45B4501FA6589DF98548E7D9304AB77D5`;
protocol proof `artifacts/task5-protocol-r27-deadline/evidence.json` has SHA256
`CB93777D9DA279C7677923C6C1D441802BA67F0434D89E4DDAA8A53BFEAEE013`.
Both strict validators passed before R32 admission. R27 ordinary browser and
all-layout runtime-browser artifacts are `artifacts/task5-browser-normal-r27-deadline`
and `artifacts/task5-browser-runtime-r27-deadline`: all16 runtime checks per layout
passed for Designer/EDT-parent/EDT-src, zero recorded host/page errors. These use
a test runtime backend with real kernel/browser/native LS, not real1C. Real1C
evidence is the separately scoped exact-MANUAL idle/full benchmark above.

### Post-R32 harness correction and historical-proof boundary

Formal review found two benchmark-only defects: process-inventory failure could
skip bridge/gateway cleanup, and an initial-population end recorded after the
deadline could bypass admission timeout. Cleanup now attempts each independent
stage and every known-process check, retains bounded source-free failure evidence,
and finishes in-flight gateway cleanup before propagating caller cancellation.
Timeout now uses recorded completion elapsed time and latches failure; completion
at or after the deadline is rejected, while timely completion remains valid when
checked later. The existing Windows gateway/nested ownership tests now assert
exact native receipts immediately after ordinary close, before additional waits;
the inner-child assertion runs before its response is emitted. No product owner,
runtime, frontend, installed wheel, workload, threshold or original timer changed.

The preserved R27 initial proof and R32 artifact identify historical tool SHA256
`6FFE0F8E9CE39C77C405AEF22BEA562B963316F009C33D5FD7797151AC67B51E`.
The corrected `tools/check_jupyter_lsp_zup.py` is SHA256
`724759DA226FE6A02316068CAF4B1D30A8A2CD61DCDC36B054C202AC315865CF`.
Earlier strict-validation statements describe their historical admission checks:
the old artifacts are not strict same-harness proof for today's altered tool.
Artifacts, frozen wrappers and hash checks were not rewritten, and no new native
LS, browser, database or performance run was performed for this correction.
Same-component R27/R32 evidence on unchanged runtime/LSP bytes remains historical
evidence. The R27 three-layout recorded initial populations were approximately
3.981–4.028 seconds; all 23 R32 enabled-arm populations were present and ranged
from 31.551–45.073 seconds. Each was below the original 600-second deadline; the
newly found admission defect does not establish a timeout in those runs.

Focused corrected-harness/ownership checks passed 179 tests with 18 expected skips
in the base environment and 191 with 6 expected skips in the optional environment,
with no pytest warnings. The optional skips were one disabled real-LS test and
five POSIX-only cases. These local checks are not new live acceptance evidence.
Fresh full offline verification on the corrected code passed 3406 tests with
46 skips, 24 deselections and one existing Proactor/ZMQ selector-fallback warning
in 257.80s. The full optional LSP suite passed 433 tests with 7 skips in 65.77s,
without warnings. Unchanged frontend bytes retain the earlier same-day 32/32
tests and successful typecheck; no new frontend or live run is claimed.
R32's original numerical FAIL and the user's specific practical exception above
remain unchanged; historical host issues below remain unresolved.

## Historical R27 third full live result: measured budget FAIL

The exact MANUAL route completed3warmup+20measured counterbalanced pairs:
46arms total and20measured public load calls per mode. Startup17.722s and two
compatible handshakes passed. Every arm closed with zero children/watchers;
the existing harmless canary passed, the full source tree was unchanged,
physical database identity was stable, and no owned target process remained.
Independent final scoped native/test inventory was0 at17:48:55.9039622UTC.

| Public load timing | LSP off | LSP on |
| --- | ---: | ---: |
| p50 | 914.7785ms | 991.6598ms |
| p95 | 1026.4994ms | 5168.5167ms |

The frozen nearest-rank percentile uses sorted[ceil(n*fraction)-1] (10th/19th
observations for p50/p95 at n20). The p95 increase is **403.508984%**, above5%:
on_p95/off_p95-1=4.035089840354944. No outliers were excluded or timers changed.
Setup/indexing, generation release and teardown remained outside load timing.
The run exited1 because its statistical budget failed, not because safety or
cleanup failed. There is no automatic retry or passing-budget claim.

Evidence `artifacts/task5-live-source-lsp-r27-third/evidence.json`, SHA256
`B25398704E6BF7F796275D14F755D9067319283DCDA60AD6EB6E849DEED97890`,
passes source-free validation and binds the corrected installed owner below.
The largest outer timing gaps lie outside the internal end_to_end profiler;
that interval excludes the session operation-lock acquisition as well as
executor submission/return resumption. At that stage its cause was not established;
later scoped observations do not retrospectively attribute this R27 run.

A separately approved two-case noDB diagnostic reproduced the candidate lock-wait
mechanism with the actual installed public method and heartbeat loop, fake
dependencies and bounded event gates. Worker-entry-to-profile-start was0.0072ms
without heartbeat and51.5260ms while the fake heartbeat was held; submission
and return-resumption delays remained sub-millisecond. The50ms negative check
confirmed no profile start while held. One load per case, six/eight fixed events,
cleanup confirmed and no nativeLS/runtime start/private source/database access.
The three inspected installed core modules match the worktree. Evidence
`artifacts/task5-timing-boundary-r28/evidence.json`, SHA256
`9B17B23E1DF2EB1EFEC48358F581EB2C4FC3CA1E2B7EB7573DC7C104694F6EBF`.
Artificial scheduling and instrumentation limit this to a mechanism demonstration;
before-to_thread is not exact queue insertion and the heartbeat timestamps cover
only a known-held subset. It does **not** attribute the historical outliers,
change the public-call budget boundary, or supersede the measured FAIL.

## Historical selected real-heartbeat diagnostic (Ruling 29)

A separately admitted, single no-LSP MANUAL diagnostic submitted one unchanged
public module-pair load on the first observed naturally scheduled heartbeat.
The corrected one-shot completed with **OVERLAP_OBSERVED**. It did not trigger,
hold, retime or repeat heartbeat, change the public-call timer, or run another
full paired benchmark. Installed core hashes matched the then-current worktree.

| Selected-run boundary | Duration |
| --- | ---: |
| Before-to_thread → worker entry | 0.3698ms |
| Worker entry → profile entry | 5024.8572ms |
| Real heartbeat body, a known-held subset | 5026.2427ms |
| Heartbeat/pre-profile intersection | 5024.8285ms |
| Actual internal end_to_end | 3923.1507ms |
| Public-call outer interval | 8948.596800ms |
| Worker return → coroutine resumed | 0.1974ms |

Profiling entered after heartbeat exit. Thus actual shared-lock serialization
was observed for this selected real no-LSP load. Entry timestamps bracket the
operations, not exact native mutex acquisition or executor insertion. The one
selected cold load and timing hooks cannot estimate p95 or establish why the
historical LSP-on tails occurred. Those R27 tails remain **UNATTRIBUTED** and that
run remains **FAIL (+403.508984%)**; R29 applied no outlier removal,
budget waiver or timer reinterpretation. Today's R32 exception is separate.

The diagnostic recorded8events/1returned heartbeat interval, no overflow,
observer fault or censoring. Two compatible MANUAL handshakes, the existing
IDLE canary after load/release, full source unchanged, stable physical database
identity, zero remaining owned target processes and explicit heartbeat join all
passed. Final external scoped native/test inventory was0 at18:38:03.5379629UTC.
Source-free evidence `artifacts/task5-real-heartbeat-r29-second/evidence.json`,
SHA256 `49241CD6DA7879211ED1B9C444B391C5719EB89FC999BD82E2E49678304A7122`.

The first separately admitted R29 attempt failed on an ignored-launcher import
before exact private target admission or RuntimeSession.start: ROOT/tools was
missing for the reused SourceWriteOracle helper's support import. Its FAILED
artifact is preserved, with null—not passing—final source/identity/owned/join
states: `artifacts/task5-real-heartbeat-r29/evidence.json`, SHA256
`3326F6BA3524A84A9211A9A95C74481182247228AE84501F5B16EBBE0F1E2513`.
Only the ignored checkout bootstrap was corrected, after a fresh-process import
regression failed; the corrected noDB suite passed42tests and the controller
separately readmitted the successful one-shot. No runtime/product behavior was
changed by R29. Runtime/RDBG work required a separate scope decision; later
R30/R31/R32 were independently approved and are documented above.

## Retained Windows ownership verification (Ruling 27)

The unchanged deadline-corrected owner had **231 focused tests passing,7 platform
skips**, including three new regressions for late accounting, completed join
and root-wait returns. The corrected wheel was rebuilt/reinstalled, SHA256
`8427E19933E0CD20C5A1B9FA23AC125D070852865A4CE4A6F89C739B426B0618`;
all nine installed module hashes match the worktree from a fresh temporary cwd.
All-layout ordinary installed initial-index/completion/definition/close fixtures
pass strict current-owner validation with cleanup0/0. A separately re-admitted
corrected-byte uninstrumented source-only run also passed: initial population
30.247s, completion53.19ms/definition6.10ms, both processes natively certified,
source unchanged, no failure entries and cleanup0/0. No runtime or DB access;
final scoped inventory0 at17:07:17.1888818UTC. Frontend32tests and typecheck pass;
full base gate3387passed,46skipped,24deselected with one known Proactor/ZMQ
warning in279.92s. Optional suite420passed,7skipped in68.16s. All-layout native
protocol passes strict nine-module validation; ordinary browser passes all
assertions with zero host/page errors. All-layout runtime-browser passes all
16checks for each Designer/EDT-parent/EDT-src layout, zero host/page errors each.
Final installed/source identity and all strict same-byte proofs revalidate;
scoped native/test inventory0 at17:25:09.3235376UTC. These corrected-byte
prerequisites preceded the separately admitted R27 third live run. Subsequent
R30/R32 full runs required their own admissions; today's result is at the top.
The next three paragraphs retain pre-correction R27 evidence and old wheel hashes.

The launch-owned Job now uses a bounded lifetime collector rather than treating
Job accounting-zero as the termination certificate. Exact native process signals
and aggregate lifetime accounting must agree; missing/unobservable admissions
fail closed. One daemon collector/port per Job,64 retained process handles,
64 packets per batch and512 close batches share the existing five-second forced
termination deadline with the root wait. Routine port waits are at most100ms
without periodic process scans. Natural owner-death Job cleanup is preserved.
The kernel-owned completion queue has no claimed fixed capacity; unobservable
short-lived children can conservatively make close unconfirmed.

Pre-correction owner regressions:49 passed; clean combined owner/child/WebSocket/harness
gate228 passed,7 platform skips in41.71s. Tests include real eight-nested-tree
ownership,70-child cumulative churn, immediate native signal assertions,
cancellation, startup/cleanup faults, bounded work and deferred ownership after
pathological native hangs. An intermediate patch-placement regression caused a
failed50/178/7 run and a lingering test worker; exact attempt-owned identities
were reclaimed before the corrected clean rerun. It is not acceptance evidence.

The pre-deadline-correction rebuilt/reinstalled standalone wheel SHA256 was
`CE6C96BDC3A0A08EA37BE5D95FD5E84FDC1F1145A2BA1AE459EB74719DD15475`.
Fresh temporary-directory imports prove all nine product module hashes match
installed bytes, now including the process owner, Windows helper and POSIX
guardian. Old six-module proofs no longer admit this owner. That installed
Designer/EDT-parent/EDT-src initial-index/completion/definition/ordinary-close
fixtures pass, including unchanged source checks and cleanup0/0.

One separately admitted **uninstrumented source-only** run on the established
read-only real export also passed: initial population30.31s, representative
completion45.78ms/definition7.18ms, native owner certificate confirmed for both
processes, source unchanged and zero remaining owned processes. No runtime or
database was started/accessed, no Worker loads/canary ran, and no diagnostic API
proxy or retained diagnostic handles were used. Session exited0; final scoped
native/test inventory was0 at16:37:32.9468301UTC. This is not a runtime benchmark.
The pre-correction broader gates subsequently passed:3384 full offline tests
(46skips,24deselected,1known warning),417 optional tests(7skips),32frontend tests
and typecheck, all-layout native protocol and ordinary/all-layout runtime-browser
acceptance (zero recorded host/page errors). At that pre-correction stage a third
real-DB run was not admitted and the runtime budget was **UNMEASURED**; later
deadline-corrected and real-run outcomes are separately recorded above.

## Contract and environment

The LS analyzes saved Designer/EDT project sources and the current virtual
notebook text. Runtime Worker generations, operation pins and debugger source
maps remain execution concerns. The sole configured root is
`RuntimeSessionConfig.source_root`; runtime close retains it, reinstall replaces
it (including None), and kernel restart/change clears it. No project source
snapshots, generation overlays, historical definition leases or load hooks remain.

Environment: Windows, Python 3.13.14, JupyterLab 4.6.3, jupyterlab-lsp 5.3.0,
jupyter-lsp 2.3.1, ipykernel 6.31.0 and Edge through Playwright. BSL LS 1.0.7 is an
external LGPL-3.0-or-later native distribution, SHA256
`143324F254383BC61B3281FB93AD72381818CA6A58BE3B3DC2F6FE5B87308EB4`.
No global Java, Node or parsergen is required to install the prebuilt standalone
wheel. Developer builds use the existing frontend toolchain.

## Initial-index measurement boundary

Product `ready` is query availability and retains
`index-convergence-unconfirmed`. The benchmark separately opts into bounded
work-done progress metadata and validates the actual initialize response. It
tracks at most eight tokens, bounds strings to 160 characters and retains only
lifecycle state, counters and phase times.

For the pinned binary, initial file discovery must end before a matched
population begin/end on one created token, using exact English or Russian
resource pairs. The population end follows completion of submitted file work;
percentage reports occur before processing. This distinction comes from
[ServerContext.populateContext](https://github.com/1c-syntax/bsl-language-server/blob/v1.0.7/src/main/java/com/github/_1c_syntax/bsl/languageserver/context/ServerContext.java)
and [WorkDoneProgressHelper](https://github.com/1c-syntax/bsl-language-server/blob/v1.0.7/src/main/java/com/github/_1c_syntax/bsl/languageserver/client/WorkDoneProgressHelper.java).
The exact localized pairs are in
[English resources](https://github.com/1c-syntax/bsl-language-server/blob/v1.0.7/src/main/resources/com/github/_1c_syntax/bsl/languageserver/context/ServerContext_en.properties)
and [Russian resources](https://github.com/1c-syntax/bsl-language-server/blob/v1.0.7/src/main/resources/com/github/_1c_syntax/bsl/languageserver/context/ServerContext_ru.properties).

Initial admission also requires unchanged normalized/physical root, current
child/revision, no degradation/reindex and actual representative completion and
definition. Unknown version/locale, malformed or foreign lifecycle, replacement,
missing end or timeout cannot produce a budget PASS. This cold observation does
not certify later watched-file update convergence.

Historical pre-R27 copied-fixture native probe (each fresh child, one watcher):

| Layout | Population end from child setup | Completion | Definition | Child tree RSS |
| --- | ---: | ---: | ---: | ---: |
| Designer | 5538.0 ms | 1973.2 ms | 7.6 ms | 617418752 bytes |
| EDT parent | 5782.7 ms | 1639.3 ms | 6.7 ms | 590471168 bytes |
| EDT src | 5366.9 ms | 1858.4 ms | 6.9 ms | 618598400 bytes |

All three observed actual 1.0.7/Russian initialization, three created tokens,
successful matched population end, unchanged complete fixture trees and zero
remaining owned child/watcher resources. Metadata progress preceded initialize
completion, which is permitted but cannot itself admit measurement. These are
single functional setup observations made alongside offline/browser work, not
isolated latency distributions. Tool and six product-module hashes bind this
proof to the implementation used. Artifact:
`artifacts/task5-initial-index-hoverfix/evidence.json`. Earlier `-first` and
`-current` artifacts predate compatibility corrections and are historical only.

## Historical pre-R27 verification results and retained test contracts

The offline full-shape lifecycle test runs 3 warmup plus 20 measured off/on pairs
with actual RuntimeAPI/test transport/packer loads, 23 owned controlled-protocol
children and actual native workspace observers. All 46 returned handles are
released outside the timer; confirmed runtime inventory remains one manifest and
two artifacts. LSP-off has no child or watcher. The test input tree remains
unchanged. These synthetic protocol results establish lifecycle behavior, not
runtime performance.

The source-write oracle compares the complete observed tree against its initial
fixture plus explicitly planned byte payloads, creates/deletes and renames. It
never resets expectations from observed post-edit output. Injected extra files,
extra directories and unexpected content changes are rejected. Saved/restored
fixture bytes preserve CRLF exactly.

Fresh standalone wheel was built/reinstalled and imported from site-packages in
a temporary cwd with PYTHONPATH cleared. A separate base-wheel installation test
passes without optional Jupyter LSP/parsergen dependencies or a core distribution.
That stage's wheel SHA256 (not the current candidate):
`E6EB995267958E1F0099A847689DEE6DABD18152CD9C0F0B49692B40756E1AC3`.
Gateway SHA256:
`be07622d1be5c5386a9f5429086dd61e7208dfeb564faa7bf19b9ca30efea5ff`.
The historical full offline gate after Rulings 24/25 had3329 passed,46 skipped,
24 deselected in 276.14 s (one known Windows Proactor/ZMQ warning). Optional browser-tool tests
are skipped in the base environment and run separately in the LSP environment.
The skip increase from 32 to 46 comprises 14 added optional tool tests: three
fixture-copy topology cases, one bounded hover summary and ten diagnostic-window
cases. Those skips are not passes; the optional-environment gate executes them.
The pre-Rulings-24/25 optional-environment `test_jupyter_lsp*.py` suite passed 316 tests,
with 7 skipped, in 53.28 s; it includes actual HTTP/WebSocket and kernel boundaries.
That stage's frontend had32 tests passing in342.80ms; TypeScript typecheck passed.
The then-focused acceptance/lifecycle tools passed122 tests in7.16s.
Individual full browser lifecycles passed Designer, EDT parent
and EDT src on the same installed product, each with zero page errors. These do
not erase the separately recorded intermittent hover and ordinary-panel failures;
at that stage integrated acceptance remained in progress.

Browser artifacts: `task5-browser-hoverfix-second/designer` and `/edt-parent`,
plus `task5-browser-edt-src-preserved/edt-src`. The latter uses the corrected
basename-preserving fixture copy. Each includes saved-file/memory independence,
current viewer refresh/deletion/reopen, shared-kernel locals, same/different-root
kernels, rename/reconnect, runtime close, unavailable root and kernel replacement.

Two narrow compatibility boundaries were added after actual native-browser
failures. FileEditor `file:///.../onec-bsl:<binding>/<token>/...` aliases are
excluded before notebook text/version replay; they cannot acquire source or
project authority. Expected discarded hover requests return the normal empty
hover result only for the exact Gateway content-modified/-32801 fence or handled
open-Gateway cancellation. Other methods, genuine errors, timeouts, code-only
matches and source/context/auth failures remain errors. No frontend private
promise/provider mutation or generic error suppression is used. Negative tests
preserve all stale-result fences and request cleanup.

The historical pre-R27 native protocol passed all three layouts, each with3 warmup and
20 measured saves, unchanged full input trees, exact planned fixture restoration,
stable child PID for same-path saves, and create/delete/valid metadata rename:

| Layout | Detection p95 | Changed signature p95 | Warm completion p95 |
| --- | ---: | ---: | ---: |
| Designer | 78.38 ms | 82.63 ms | 4.45 ms |
| EDT parent | 75.34 ms | 81.00 ms | 6.34 ms |
| EDT src | 105.77 ms | 126.84 ms | 8.22 ms |

Artifact: `artifacts/task5-protocol-hoverfix/evidence.json`. This functional
fixture run overlapped offline tests/browser setup; these timings are not an
isolated performance budget. It includes the exact observed reserved viewer
alias lifecycle against actual Gateway/child inventories and continued real
notebook completion. The browser provides complementary URI/context/UI evidence,
not internal map inspection. Any subsequent
gateway/workspace/bridge/child change invalidates this same-code proof and
requires a fresh installed-wheel run before live admission.

## Historical first and second live attempts: FAIL / UNMEASURED

**Historical FAIL / UNMEASURED.** The second, separately approved exact-target attempt used
the final Rulings24/25 harness and its then-current strict initial-index proof. Startup
took 18364.03 ms with both compatible MANUAL handshakes. Only the first LSP-off
warmup arm completed (3993.82 ms public load). The first enabled arm failed at
`arm-close` with `lsp-owned-process-survived`; the bounded ledger contains no
earlier prepare/check/load/release failure and no secondary failure. Its immediate
diagnostic reports `role=descendant`, `identity=matched`, `execution=running`,
`handle_close=closed`. Here `running` means the exact-identity native handle was
not signaled at a zero-time wait; it does not distinguish continued user-code
execution from termination still in progress. This is not merely psutil retaining
a signaled terminated object. It does not establish why cleanup returned before
the descendant was signaled or how long that state lasted.

The second attempt's complete source oracle and physical DB identity checks
passed, with zero remaining matching runtime target processes. No measured pairs
completed and the final canary was not reached. A later read-only inventory at
15:29:08.842 UTC found no scoped LS/Java/native 1C/harness processes and neither
live launcher nor worker; that later zero does not negate the immediate nonsignaled
descendant. Source-free evidence validation passed. Artifact:
`artifacts/task5-live-source-lsp-r25-second/evidence.json`.
The run started after a fresh zero-competing-jobs inventory at 15:26:15.381 UTC;
no concurrent suite, build, index or Git operation was performed. It stopped at
this failure without an automatic retry or product/timeout/liveness-rule change.

The first exact-target attempt passed frozen admission and
MANUAL startup with both compatible handshakes; startup took 29002.55 ms. Its
only completed arm was LSP-off in warmup pair 0: 4191.71 ms for the public load,
zero children/watchers and verified zero child/watcher cleanup. No measured pair
was completed, so there is no p50/p95 comparison or runtime budget result.

The first enabled arm stopped at the immediate `lsp-owned-process-survived`
cleanup assertion. A `finally` cleanup exception can replace an earlier setup or
load exception; the artifact does not establish that initial indexing or the
first enabled load succeeded, or identify any preceding failure. No first-on
setup checkpoint exists. The final semantic canary was not reached. No automatic retry,
target repair, configuration installation or benchmark-rule change followed.

Failure evidence confirms unchanged complete source contents/path membership,
stable physical database identity and zero remaining matching target processes.
A subsequent read-only inventory at 14:43:06 UTC found zero BSL LS/Java processes,
zero native 1C/debug-server processes and no live launcher/worker. This later
absence does not erase the failed immediate cleanup assertion or prove a race
was its cause. Source-free evidence validation passed. Artifact:
`artifacts/task5-live-source-lsp-first/evidence.json`.

Before this attempt, an unrelated active pytest worker blocked nonconcurrency.
The user explicitly authorized its exact cleanup, and a fresh inventory found
no competing jobs before live entry. The user then requested a checkpoint push;
the conservative git window was 14:26:36–14:27:29 UTC. An observation after that
window still found no startup checkpoint. The first evidence file was created
at 14:40:01.874 UTC, so no startup or reload timer overlapped those git operations.
The attempt remained one sample set; checkpoint `9cdf909` is not completed live
acceptance.

Global admission and source verification are distinct from per-arm setup/index
and reload timings. The tool fingerprints the complete source tree once before
startup and checks it after the run (also on failure), not on each enabled arm.
The initial process-to-checkpoint wall interval was about 15 min 16 s, with
observed ongoing reads; it includes multiple admission steps and is not an
isolated fingerprint duration. Each enabled arm separately creates a fresh LS
index and a bounded metadata-only workspace observer. The tool does not report
an individually instrumented global fingerprint duration.

### Unchanged live method (also used by subsequent complete runs)

The tool is disabled unless explicitly opted in. It must admit the
exact established benchmark DB/platform/extension/source workload through the
frozen read-only `_exact_live_inputs()` route, use MANUAL extension mode, and
verify compatible handshakes. It installs no fixture configuration, repairs no
extension, changes no real export files and invokes only existing harmless
canaries alongside in-memory Worker loads.

The required comparison uses the same real module pair for 3 warmup and 20
measured counterbalanced pairs. Each enabled arm starts a fresh LS, observes its
initial index, proves completion/definition and retains an active watcher before
timing. Each disabled arm has zero owned LS/watcher resources. Startup, setup,
handle release and teardown are outside the one-public-load-call timer. P50/p95,
the <=5% p95 PASS/FAIL, metadata payload size, RSS and source-free phase metrics
are separate from fixture file-observation timing. Physical DB identity and
owned process cleanup must be checked before accepting a result.

## Limits carried into final review

Earlier descriptions of an isolated control or no competing jobs refer only to
Task 5 jobs, not proven quiet-machine conditions: the unrelated old pytest worker
already existed during those checks. This is not evidence for the cause of any
historical failure and does not invalidate functional assertions. Fixture timing
observations are not isolated performance measurements; the later live attempt
had a separately verified no-competing-job preflight.

Windows coarse notifications can report an unlocated change and require a
controlled reindex. A freshly written tiny offline fixture triggered this during
setup; admission correctly rejected that child. The stable read-only controlled
fixture and all three real copied native probes passed; no claim is made that
the earlier notification behavior was repaired or that all external edits are
atomic. Canonical metadata/module membership changes can incur cold reindexing.

The native EDT comparison requires retaining the `src` directory name. Identical
EDT files passed under original `src`, failed representative completion when
flattened into `project`, and passed under another container's `src`, after actual
initial population in each case. Browser fixtures therefore preserve the selected
root basename/topology while remaining independent owned copies; no sibling
directories are copied. Arbitrarily renamed flat EDT roots are not verified.

Observation retries are finite. Once exhausted, remove the last subscription
and subscribe again or restart the gateway. Configuration heartbeats do not
restart observation. Remote filesystem mapping and silent Windows polling
fallback are not provided.

JupyterLab LSP's exact `.lsp_symlink/onec-bsl:` fallback is denied. Missing current
definitions may show the host's normal file-load dialog and an unavailable
read-only fallback; no writable source alias or old-byte authority is granted.

Known host issues remain: moderate sanitize-html audit advisories, existing
traitlets deprecation warnings, occasional detached-notebook observer races,
and intermittent pyzmq ENOTSOCK shutdown output. An asyncio-debug creation trace
located the latter's pending IOPub poll in KernelProjectClient._listen; no fix
of that historical error, blanket suppression or definitive shutdown-order
cause is claimed. The independently reproduced control-worker affinity defect
and expired pending-claim capacity defect are addressed in the whole-branch
corrections above. An earlier native-hover generic -32001 response was not origin-resolved;
the nullable-hover change does not catch it or establish that it was repaired.
The separately observed `workspace/didChangeConfiguration` method-not-supported
notification was not changed by either compatibility correction.
It is addressed by the later ordinary-configuration correction described above.
Post-correction native hover also failed once after None installation; an
origin-observed diagnostic subsequently showed null followed by a new nonempty
hover and visible tooltip, without reproducing that failure. Its cause remains
unclassified; a diagnostic pass is not evidence that the failure was repaired.

The ordinary browser's original all-panel-rows assertion has a proven false
positive: a controlled real current-version diagnostic delivered after its
baseline changed the panel from zero to three current rows while the old
diagnostic remained withheld and transient stale lint markers stayed zero.
This establishes an oracle defect, not the cause of the earlier uncontrolled
failure. The corrected current-anchored, isolated observation passed the ordinary
uninstrumented browser on the unchanged wheel. Actual current version 25 and its
rendered fixture rows establish the baseline; same socket/document and stable
version are required. During the 1200 ms stale-only window, all further real
diagnostics, including empty ones, enter a bounded 64-message/2 MiB queue. All
remaining packets drain exactly once in queue order in finally; intentional
anchor/stale replay reordering is not globally original delivery order. Artifact
`artifacts/task5-browser-normal-anchored-identity/diagnostic-window.json` records
75 frames, zero transient stale lint, unchanged panel rows and an empty final
queue. All other ordinary browser assertions passed with zero page errors.
Offline tool/lifecycle tests passed 76 tests in 6.99 s, including current/stale
and socket/document identity, version changes, empty diagnostics, queue bounds,
exactly-once anchor delivery and final draining. The rare hover and historical
uncontrolled failures remain disclosed, not relabeled as fixed.
Linux/macOS, Notebook 7, classic Notebook, remote roots, refactoring and
live-value analysis are not verified by these Windows/JupyterLab checks.

### Historical failure evidence follow-up (Ruling 24)

The benchmark harness now preserves the first failure and independent cleanup
failures as at most six ordered, fixed-allowlist phase/type/code entries, with
explicit overflow. It exports no exception text or dynamic class names. Cleanup
failures cannot produce a passing budget; unknown final process/identity state
remains unknown. Cancellation finishes the same in-flight startup, load, canary,
release or close operation before propagating; it does not repeat the operation.
CLI cancellation produces source-free failure evidence and exit code 130.
Focused offline checks passed 91 tests in 7.02 s, including 15 new regressions.
These changes cannot recover the original failed live attempt's masked error.

Two owned-fixture cleanup diagnostics were recorded at
`artifacts/task5-cleanup-fixture-diagnostic-attempt2/evidence.json`. The installed
LspArm Designer prepare and ordinary close completed; immediately afterward its
root was native-signaled/terminated and its descendant absent. In the separate
real OwnedProcess control, intentionally retained query/synchronize handles
still referred to terminated root and child objects, but psutil reported both
PIDs absent. Both diagnostic handles closed successfully, and all four owned
identities were absent on the later postcheck. No same-identity psutil survivor
was reproduced. Object retention is observable but does not establish the cause
of the historical live failure; the harness liveness assertion is unchanged.

The first diagnostic invocation failed in the collector before publishing native
states. It is retained as COLLECTOR_FAIL/no usable outcome in
`artifacts/task5-cleanup-fixture-diagnostic-attempt1/evidence.json`, not as a
passing diagnostic or a recovered native result. The corrected second invocation
publishes each case independently, including unknown states. Neither used the
live target. At that stage the real runtime budget was FAIL/UNMEASURED with no
measured pairs; these fixture diagnostics did not include a real-DB retry.

Ruling 25 adds only a failure-path diagnostic for the exact existing
`lsp-owned-process-survived` condition. One freshly opened, noninheritable Windows
query/synchronize handle checks the already-selected process identity, performs
a zero-time wait, and closes in finally. Four fixed enum fields describe role,
identity, execution state and diagnostic-handle closure; every field is
revalidated when aggregating failure evidence. No PID, creation timestamp, exit
code, path or API error is exported. Identity mismatch and unavailable queries
remain unknown. Every diagnostic outcome still raises the original cleanup
failure; successful closes perform no diagnostic call.

The exact creation-time conversion follows the installed psutil 7.2.2 integer
epoch subtraction, then double conversion and division, without tolerance.
See [psutil's conversion implementation](https://github.com/giampaolo/psutil/blob/release-7.2.2/psutil/arch/windows/init.c)
and [the process-time caller](https://github.com/giampaolo/psutil/blob/release-7.2.2/psutil/arch/windows/proc.c).
Focused tests passed 122 checks in 7.16 s. The final harness's Designer, EDT-parent
and EDT-src initial-population/completion/definition probes passed in
`artifacts/task5-initial-index-r25/evidence.json`; the strict same-code proof
validator passed. Existing browser/protocol evidence remains on the unchanged
installed product, not on a rebuilt wheel. A possible live recurrence is now
more observable, not established as fixed. The second approved live attempt
subsequently exercised this diagnostic and observed an identity-matched running
descendant, as recorded in the live outcome above.

### Historical contained descendant termination ordering (Ruling 26)

One separately approved LS-only probe used the same admitted read-only source
bundle, with no runtime startup, database access, Worker load or canary. Its
instance-local diagnostic API proxy recorded five stages while preserving the
installed product's original calls/returns and close path. Artifact:
`artifacts/task5-job-membership-r26/evidence.json`.

Before close, both exact-identity processes were listed in the owned job and
`IsProcessInJob` confirmed membership; active count was 2. After successful
`TerminateJobObject`, and again at the product's first returned active count 0,
the job list was empty but both process handles were still nonsignaled. Before
the job handle closed, the root was signaled but the contained descendant was
still nonsignaled. The descendant remained nonsignaled at the immediate
`arm.close` observation, which raised the same cleanup failure. In this observed
run, an empty job accounting/list result was not a full descendant termination
signal barrier. It is not evidence that the descendant escaped its owned job or
that user code continued executing during termination.

All diagnostic query/synchronize handles closed within their individual
snapshots; none were retained across product close. Both identities and all five
stages were recorded without overflow or collector failure. The complete source
oracle passed, and a later inventory at 15:41:29.546 UTC found zero scoped native
or probe processes. Added observation and evidence-write latency may affect
ordering; these timings are diagnostic only, not acceptance/performance results.
No product change or third live attempt was authorized by this probe alone.

Verification gap identified before the later owner fix: the then-current proof
identity includes six product modules but omits `lsp_process.py`. Its strict
validator must not be treated as same-owner evidence after changing that file.
Any owner correction requires adding the owner and relevant helper dependencies
to the proof identity, rebuilding/reinstalling and regenerating compatible native
evidence. R27 subsequently made that separately approved correction and expanded
the proof to nine modules; its corrected-byte evidence is retained above.
