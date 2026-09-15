# Task 8 foundation report

## Delivered foundation

- `AdmissionEnvelopeV1` is a closed protocol-2 result parser shared by the
  compact-table and recursive-value transports. It accepts only ready,
  denied, and confirmed-admission-error envelopes before a context payload is
  read.
- The extension is protocol `2`, artifact `0.1.3`. Its fingerprint covers the
  managed and kernel handshakes and both transfer serializer sources.
- Both BSL serializers classify the root before traversal and classify each
  descendant before encoding. Worker-generation values produce the sealed
  denied envelope and no context payload publication.
- Materialization builds generic server-side table serialization. Python no
  longer performs a schema read before target admission, and the old
  precomputed-schema instruction path has been removed.
- The obsolete remote public-value guard and its evaluation kind have been
  removed. Core, Jupyter, MCP, and the ZUP adapter call target-I/O-free local
  `validate_value_reference()` before publication; dynamic policy stays in the
  consuming materialization request.

## Protocol and bundle evidence

`tools/build_runtime_extension_bundle.py` ran with the installed 1C Designer
`8.3.27.2170`, artifact `0.1.3`, and protocol `2`. It produced
`OnecInteractiveRuntime.cfe` (21,210 bytes), whose SHA-256 is
`87a8e7ac511ff919f083f4355ed8d3b3fe6fa6460754a3782dad5f2f0e9ca3e6`.
This is a Designer compilation and bundle build, not live-infobase
qualification.

The lifecycle suite includes a MANUAL-mode predecessor-protocol regression:
protocol `1` fails during handshake before any target materialization or
inspection request.

## Deferred by plan dependency

Coordinator pending/late-result lifecycle integration remains for the work
that follows Task 7. This foundation does not claim that integration.

## Validation

The combined focused and nearby suite completed with `1261 passed, 1 skipped,
1 warning` in 63.26 seconds. The warning is the existing Windows Proactor
`add_reader` warning from the real-ipykernel test. The command included the
capture evaluation, both transports, extension bundle/build/lifecycle/session,
runtime API, prototype, Jupyter, MCP, and ZUP Worker-universe suites.

`python -m compileall` and `git diff --check` are run with the final source
before commit. The final source inventory also checks that no executable
standalone public-value guard, guard evaluation kind, or batch guard API
remains.
