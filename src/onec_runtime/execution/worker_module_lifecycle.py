"""Bounded public Worker module lifecycle on the one stopped-route arbiter.

Pure module analysis and packaging are supplied by a composition-owned
preparer. Promotion and release enter the existing Worker universe through an
admitted ``SessionPort``. This port accepts a confirmed catalog snapshot and
delegates breakpoint-bearing promotion to the same shared
``WorkerBreakpointWorkspace`` used by notebook Worker activation.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from threading import RLock
from typing import Protocol

from onec_runtime.bsl.module_catalog import CommonModuleCatalogSnapshot
from onec_runtime.bsl.module_universe import (
    WorkerModuleUnit, analyze_worker_module, lower_worker_module,
)
from onec_runtime.bsl.diagnostics import VisibleSourceContext
from onec_runtime.bsl.parser_target import PythonParserTarget
from onec_runtime.bsl.source_maps import SourceUnitRef
from onec_runtime.errors import ProtocolError, StaleWorkerGeneration
from onec_runtime.execution.arbiter import (
    OutcomeUnknown, RdbgArbiter, SessionPort, Settlement,
)
from onec_runtime.execution.worker_mutation import (
    CapturePausedWorkerRoute, MainPausedWorkerRoute, WorkerMutationRoute,
)
from onec_runtime.execution.worker import WorkerActivationUnknown
from onec_runtime.performance_profile import PhaseRecorder
from onec_runtime.worker_breakpoints import (
    WorkerBreakpointReloadOutcome, WorkerBreakpointReloadPolicy,
    WorkerBreakpointReloadReport,
)
from onec_runtime.worker_universe import (
    WorkerGenerationHandle, WorkerModuleArtifact, WorkerModuleArtifactBuilder,
)


class WorkerModuleArtifactPreparer:
    """Analyze and package a complete source graph without target access.

    This bounded preparer accepts an already confirmed common-module catalog
    snapshot. Resolving a SessionCommonModuleCatalog's lazy dependencies is a
    separate composition port and must happen before calling this preparer.
    """

    def __init__(
        self, parser: PythonParserTarget, builder: WorkerModuleArtifactBuilder,
    ) -> None:
        if not isinstance(parser, PythonParserTarget):
            raise TypeError("Worker module parser target is required")
        if not isinstance(builder, WorkerModuleArtifactBuilder):
            raise TypeError("Worker module artifact builder is required")
        self._parser = parser
        self._builder = builder

    def __call__(
        self,
        units: tuple[WorkerModuleUnit, ...],
        catalog: CommonModuleCatalogSnapshot,
        profiler: PhaseRecorder | None,
    ) -> tuple[WorkerModuleArtifact, ...]:
        artifacts: list[WorkerModuleArtifact] = []
        for unit in units:
            analysis = analyze_worker_module(
                unit, catalog, self._parser, profiler=profiler,
            )
            lowered = lower_worker_module(analysis, profiler=profiler)
            references = {
                reference
                for segment in unit.mapped_source.source_map.segments
                for reference in (segment.origin_ref, segment.anchor_ref)
                if isinstance(reference, SourceUnitRef)
            }
            context = VisibleSourceContext({
                reference: unit.mapped_source.text for reference in references
            })
            artifacts.append(self._builder.build(
                lowered, visible_source_context=context, profiler=profiler,
            ))
        return tuple(artifacts)


class WorkerModulePublisher(Protocol):
    """The persistent Worker activation owner, never a second target registry."""

    def snapshot(self) -> object: ...

    def publish_modules(
        self, artifacts: tuple[WorkerModuleArtifact, ...], *, port: SessionPort,
        reload_policy: WorkerBreakpointReloadPolicy,
    ) -> WorkerGenerationHandle: ...

    def release_generation(
        self, handle: WorkerGenerationHandle, *, port: SessionPort,
    ) -> None: ...

    def last_worker_breakpoint_reload_report(
        self,
    ) -> WorkerBreakpointReloadReport | None: ...


class WorkerModuleLifecycleService:
    """Upsert modules and retain confirmed source units for the active root.

    ``require_mutation_boundary`` must reject a CAPTURE continuation admission
    as well as any unsafe MAIN or CAPTURE route. The caller provides it from
    the controller and supplies an artifact preparer that performs only local
    analysis and packaging. Both the route and boundary are rechecked in the
    admitted ticket before the publisher can enter RDBG.
    """

    def __init__(
        self,
        arbiter: RdbgArbiter,
        publisher: WorkerModulePublisher,
        *,
        prepare_artifacts: Callable[
            [tuple[WorkerModuleUnit, ...], CommonModuleCatalogSnapshot,
             PhaseRecorder | None], tuple[WorkerModuleArtifact, ...]
        ],
        route_provider: Callable[[], WorkerMutationRoute],
        require_mutation_boundary: Callable[[], None],
        worker_breakpoints_present: Callable[[], bool],
        wait_handoff: Callable[[], AbstractContextManager[None]] = nullcontext,
    ) -> None:
        if not isinstance(arbiter, RdbgArbiter):
            raise TypeError("one RDBG arbiter is required")
        if any(not callable(item) for item in (
            prepare_artifacts, route_provider, require_mutation_boundary,
            worker_breakpoints_present, wait_handoff,
        )):
            raise TypeError("Worker lifecycle collaborators must be callable")
        if not callable(getattr(publisher, "publish_modules", None)) or not callable(
            getattr(publisher, "release_generation", None)
        ) or not callable(getattr(publisher, "snapshot", None)) or not callable(
            getattr(publisher, "last_worker_breakpoint_reload_report", None)
        ):
            raise TypeError("Worker module publisher is required")
        self._arbiter = arbiter
        self._publisher = publisher
        self._prepare_artifacts = prepare_artifacts
        self._route_provider = route_provider
        self._require_mutation_boundary = require_mutation_boundary
        self._worker_breakpoints_present = worker_breakpoints_present
        self._wait_handoff = wait_handoff
        self._lock = RLock()
        self._units: dict[str, WorkerModuleUnit] = {}
        self._catalog: CommonModuleCatalogSnapshot | None = None
        self._revision = 0
        self._api_owned_handle: WorkerGenerationHandle | None = None

    def load_worker_modules(
        self,
        units: tuple[WorkerModuleUnit, ...],
        *,
        common_modules: CommonModuleCatalogSnapshot,
        breakpoint_policy: WorkerBreakpointReloadPolicy = (
            WorkerBreakpointReloadPolicy.STRICT
        ),
        profiler: PhaseRecorder | None = None,
    ) -> WorkerGenerationHandle:
        """Publish a complete upserted graph; no remote BSL deadline is added."""

        if type(units) is not tuple or not units:
            raise ProtocolError("Worker module units must be a non-empty tuple")
        if any(not isinstance(unit, WorkerModuleUnit) for unit in units):
            raise ProtocolError("Worker module catalog binding does not match")
        names = tuple(unit.logical_name.casefold() for unit in units)
        if len(names) != len(set(names)):
            raise ProtocolError("Worker module names must be unique")
        if "worker" in names:
            raise ProtocolError("Worker is reserved for notebook methods")
        if not isinstance(common_modules, CommonModuleCatalogSnapshot):
            raise ProtocolError("A confirmed common-module catalog snapshot is required")
        if type(breakpoint_policy) is not WorkerBreakpointReloadPolicy:
            raise TypeError("Worker breakpoint reload policy is invalid")
        if profiler is not None and not isinstance(profiler, PhaseRecorder):
            raise TypeError("Worker lifecycle profiler is invalid")

        route = self._admit_route()
        with self._lock:
            self._require_monotonic_catalog(common_modules)
            desired = dict(self._units)
            desired.update((unit.logical_name.casefold(), unit) for unit in units)
            ordered = tuple(desired[name] for name in sorted(desired))
            revision = self._revision
        for unit in ordered:
            common_modules.require(unit.logical_name)
        artifacts = self._prepare_artifacts(ordered, common_modules, profiler)
        if (
            type(artifacts) is not tuple
            or len(artifacts) != len(ordered)
            or any(
                getattr(artifact, "logical_name", "").casefold()
                != unit.logical_name.casefold()
                or getattr(artifact, "revision", None) != unit.revision
                for artifact, unit in zip(artifacts, ordered, strict=True)
            )
        ):
            raise ProtocolError("Prepared Worker module graph does not match sources")
        token = self._arbiter.current_route

        def plan(port: SessionPort) -> Settlement:
            self._require_current_route(route, token)
            with self._lock:
                if self._revision != revision:
                    raise ProtocolError("Worker module source inventory changed")
            breakpoint_report_required = self._worker_breakpoints_present()
            publish = lambda: self._publisher.publish_modules(
                artifacts, port=port, reload_policy=breakpoint_policy,
            )
            try:
                handle = (
                    publish() if profiler is None
                    else profiler.measure("worker_generation_publication", publish)
                )
            except WorkerActivationUnknown as error:
                error.lease.retain_outcome_unknown(port=port)
                raise
            if not isinstance(handle, WorkerGenerationHandle):
                raise OutcomeUnknown("Worker module publication returned no generation")
            if breakpoint_report_required:
                report = self._publisher.last_worker_breakpoint_reload_report()
                if (
                    not isinstance(report, WorkerBreakpointReloadReport)
                    or report.candidate_handle is not handle
                    or report.policy is not breakpoint_policy
                    or report.outcome is not WorkerBreakpointReloadOutcome.COMMITTED
                ):
                    raise OutcomeUnknown(
                        "Worker breakpoint publication report is unconfirmed"
                    )
            with self._lock:
                self._units = desired
                self._catalog = common_modules
                self._revision += 1
                self._api_owned_handle = handle
            return Settlement(handle)

        return self._submit_and_wait(token, plan)

    def confirmed_worker_module_units(
        self, handle: WorkerGenerationHandle,
    ) -> tuple[WorkerModuleUnit, ...]:
        """Read only the source set of the current confirmed generation."""

        if not isinstance(handle, WorkerGenerationHandle):
            raise ProtocolError("Worker source generation is not current")
        with self._lock:
            active = getattr(self._publisher.snapshot(), "active_handle", None)
            if active is not handle:
                raise ProtocolError("Worker source generation is not current")
            return tuple(self._units[name] for name in sorted(self._units))

    def release_worker_generation(self, handle: WorkerGenerationHandle) -> None:
        """Release only the currently API-owned handle on the arbiter worker."""

        with self._lock:
            if not isinstance(handle, WorkerGenerationHandle) or (
                handle is not self._api_owned_handle
                or getattr(self._publisher.snapshot(), "active_handle", None) is not handle
            ):
                raise StaleWorkerGeneration("Worker generation handle is stale or released")
            revision = self._revision
        route = self._admit_route()
        token = self._arbiter.current_route

        def plan(port: SessionPort) -> Settlement:
            self._require_current_route(route, token)
            with self._lock:
                if (
                    self._revision != revision
                    or self._api_owned_handle is not handle
                    or getattr(self._publisher.snapshot(), "active_handle", None)
                    is not handle
                ):
                    raise StaleWorkerGeneration("Worker generation handle is stale or released")
            self._publisher.release_generation(handle, port=port)
            with self._lock:
                self._api_owned_handle = None
                self._revision += 1
            return Settlement(None)

        self._submit_and_wait(token, plan)

    def last_worker_breakpoint_reload_report(
        self,
    ) -> WorkerBreakpointReloadReport | None:
        """Read the shared workspace owner's latest local reload evidence."""

        report = self._publisher.last_worker_breakpoint_reload_report()
        if report is not None and not isinstance(report, WorkerBreakpointReloadReport):
            raise ProtocolError("Worker breakpoint reload report is invalid")
        return report

    def _admit_route(self) -> WorkerMutationRoute:
        self._require_mutation_boundary()
        if self._worker_breakpoints_present() and not getattr(
            self._publisher, "supports_breakpoint_reload", False
        ):
            raise ProtocolError("Worker breakpoint reload requires a policy/report port")
        if self._arbiter.has_pending_operations:
            raise ProtocolError("RDBG activity prevents Worker module lifecycle")
        route = self._route_provider()
        if not isinstance(route, (MainPausedWorkerRoute, CapturePausedWorkerRoute)):
            raise ProtocolError("Worker mutation route is unavailable")
        return route

    def _require_current_route(self, expected: WorkerMutationRoute, token: object) -> None:
        self._require_mutation_boundary()
        if self._worker_breakpoints_present() and not getattr(
            self._publisher, "supports_breakpoint_reload", False
        ):
            raise ProtocolError("Worker breakpoint reload requires a policy/report port")
        current = self._route_provider()
        if self._arbiter.current_route != token or not _same_route(expected, current):
            raise ProtocolError("Worker module stopped route changed")

    def _require_monotonic_catalog(self, candidate: CommonModuleCatalogSnapshot) -> None:
        current = self._catalog
        if current is None:
            return
        if (
            candidate.profile != current.profile
            or candidate.preprocessor_profile != current.preprocessor_profile
            or candidate.revision < current.revision
        ):
            raise ProtocolError("Worker common-module catalog is not monotonic")
        previous = {item.canonical_name.casefold(): item for item in current.modules}
        following = {item.canonical_name.casefold(): item for item in candidate.modules}
        if (
            candidate.revision == current.revision and candidate != current
        ) or any(following.get(name) != item for name, item in previous.items()):
            raise ProtocolError("Worker common-module catalog is not monotonic")

    def _submit_and_wait(self, token, plan):
        ticket = self._arbiter.submit(token, plan)
        try:
            self._arbiter.dispatch(ticket)
        except BaseException:
            ticket.cancel_queued()
            raise
        try:
            with self._wait_handoff():
                if ticket.wait_unknown():
                    raise OutcomeUnknown("Worker module lifecycle outcome is unknown")
                return ticket.wait_settled(0)
        except KeyboardInterrupt:
            ticket.detach_waiter()
            raise


def _same_route(expected: WorkerMutationRoute, current: object) -> bool:
    if isinstance(expected, MainPausedWorkerRoute):
        return (
            isinstance(current, MainPausedWorkerRoute)
            and expected.target_id == current.target_id
            and expected.previous_main is current.previous_main
        )
    return (
        isinstance(current, CapturePausedWorkerRoute)
        and expected.scope is current.scope
        and expected.main_operation is current.main_operation
    )
