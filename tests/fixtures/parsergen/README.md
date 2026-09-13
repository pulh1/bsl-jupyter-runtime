# Parser generator fixture

This is the `tools/parsergen/src` source snapshot from `pulh1/QueryConsole1C` commit `bb373cfd5ad7dc67abed2bc0b9122063f262f249`. It is included so parser generation and drift tests run from a fresh checkout without a second repository. The upstream source is MIT licensed; its license is in [LICENSE](LICENSE).

`tools/generate_bsl_semantic_parser.py` uses `src/` here by default. Set `ONEC_PARSERGEN_SRC` only to test a different parsergen checkout. Keep the source snapshot, generated parser, package SHA-256 assertions, and this provenance together when updating it.
