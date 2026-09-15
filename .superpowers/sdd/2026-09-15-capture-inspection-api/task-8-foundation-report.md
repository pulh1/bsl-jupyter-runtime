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
`OnecInteractiveRuntime.cfe` (21,371 bytes), whose SHA-256 is
`5bdda3410e15f4ca59d755e8ee2a1cd05f7e5f8a5086b75a02ab49d5f2ef554b`.
This is a Designer compilation and bundle build, not live-infobase
qualification.

The lifecycle suite includes a MANUAL-mode predecessor-protocol regression:
protocol `1` fails during handshake before any target materialization or
inspection request.

## Deferred by plan dependency

Coordinator pending/late-result lifecycle integration remains for the work
that follows Task 7. This foundation does not claim that integration.

## Validation

The focused remediation suite completed with `310 passed` in 6.12 seconds. It
includes both BSL serializers, their Python instruction builders, generic
table transport, Runtime API projection/materialization routes, completion,
and capture-value redaction models.

The nearby suite completed with `707 passed, 1 skipped` in 11.88 seconds. It
includes capture-evaluation models, both transports, extension
bundle/build/lifecycle/session, Runtime API, completion, Jupyter value proxy,
and the materialization bridge. Its lifecycle coverage retains the MANUAL
protocol-`1` rejection before any target materialization or inspection call.

`python -m compileall -q src/onec_runtime packages/jupyter packages/mcp` and
`git diff --check` pass with the final source.

## Review remediation

- Both successful BSL serializers now return `Доступ = Истина`. A behavioral
  unit test faithfully executes the emitted BSL `Структура.Вставить` success
  branches against each generated protocol-2 instruction and reaches `R`, so
  a missing dynamic property cannot be hidden by Designer syntax compilation.
- `materialization_kind`, completion, and projection paths generate inline
  root admission before target type/schema/descendant reads. Projection builds
  and serializes its bounded result in that same instruction; only admitted
  Base64 is written to the temporary context key.
- Value-inspection backends now return admission decisions as part of each
  projection. There is no callable `CaptureValuePolicy.private_guard`; a
  non-exact denial is an immutable exact wire object containing only `name`,
  `access: "denied"`, and `expandable: false`.
- Generic compact-table classification uses declared types before observations,
  preserving enumerations and all-null reference columns for per-column
  reference modes. The obsolete `columns` and `schema_reader` compatibility
  parameters are absent from the generic Python transport.
