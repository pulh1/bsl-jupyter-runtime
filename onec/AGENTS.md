# 1C sources

`OnecInteractiveRuntime` contains product extension sources; `Kernel` and `Worker` are supporting 1C sources. Keep source and metadata XML in sync when changing extension behavior. Build through `tools/build_runtime_extension_bundle.py` and verify the related unit tests before a live 1C run.

Versioned HotReloadWorker `v1` and `v2` examples are test-only fixtures in `tests/fixtures/onec/HotReloadWorker`, not product source. Do not put real customer configuration exports or infobases under `onec/`.
