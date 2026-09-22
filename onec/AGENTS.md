# 1C sources

`OnecInteractiveRuntime` contains product extension sources; `Kernel` and `Worker` are supporting 1C sources. The current product extension is artifact `0.1.11`, protocol `5`: keep `Configuration.xml`, both handshake BSL modules, the packaged CFE, and `src/onec_runtime/resources/extension/extension-manifest.json` in sync. Changes to protocol-bearing BSL or metadata require a fresh bundle built from canonical `onec/OnecInteractiveRuntime` sources with `tools/build_runtime_extension_bundle.py` and an installed 1C Designer; its source round-trip and manifest checks must pass. Run focused `tests/unit/test_extension_bundle.py` and `tests/unit/test_extension_bundle_build.py` before any opt-in live extension test.

The server service exposes distinct MAIN and stopped CAPTURE execution entry points. Keep their extension handshake and execution contracts aligned with the Python route-specific executors; Worker publication stages use the admitted route and must not create a second RDBG owner. Do not infer that loading or validating the extension proves its server entry or debugger target runs; verify those in an opt-in test with a temporary infobase.

Versioned HotReloadWorker `v1` and `v2` examples are test-only fixtures in `tests/fixtures/onec/HotReloadWorker`, not product source. Do not put real customer configuration exports or infobases under `onec/`.
