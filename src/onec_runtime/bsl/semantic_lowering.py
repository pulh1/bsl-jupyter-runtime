from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, fields, is_dataclass
from enum import Enum
from re import fullmatch
from typing import Any

from onec_runtime.bsl.lexer import BslLexError, tokenize
from onec_runtime.bsl.parser_target import PythonParserTarget, parse_raw_module
from onec_runtime.bsl.platform_globals import NOTEBOOK_PLATFORM_GLOBALS
from onec_runtime.bsl.source_maps import (
    MappedSource,
    SourceArtifactKind,
    SourceMap,
    SourceSpan,
    SourceTransformBuilder,
    SourceUnitKind,
    SourceUnitRef,
    mapped_visible_source,
    source_sha256,
)


class LoweringMode(str, Enum):
    MAIN = "main"
    CAPTURE = "capture"


class CaptureNamespaceRule(str, Enum):
    FORBIDDEN = "forbidden"
    MEMBER_ROOT = "member_root"


@dataclass(frozen=True, slots=True)
class LoweringProfile:
    result_channel: str
    capture_namespace_rule: CaptureNamespaceRule
    source_map_tag: str

    def __post_init__(self) -> None:
        try:
            result_tokens = tokenize(self.result_channel)
        except BslLexError as error:
            raise ValueError("result channel must be a BSL identifier") from error
        if (
            len(result_tokens) != 1
            or result_tokens[0].type != "ID"
            or result_tokens[0].text != self.result_channel
        ):
            raise ValueError("result channel must be a BSL identifier")
        if type(self.capture_namespace_rule) is not CaptureNamespaceRule:
            raise ValueError("capture namespace rule must be a CaptureNamespaceRule")
        if not isinstance(self.source_map_tag, str) or not self.source_map_tag:
            raise ValueError("source map tag must be a nonempty string")


MAIN_LOWERING_PROFILE = LoweringProfile(
    result_channel="Результат",
    capture_namespace_rule=CaptureNamespaceRule.FORBIDDEN,
    source_map_tag=LoweringMode.MAIN.value,
)
CAPTURE_LOWERING_PROFILE = LoweringProfile(
    result_channel="РезультатИнструкции",
    capture_namespace_rule=CaptureNamespaceRule.MEMBER_ROOT,
    source_map_tag=LoweringMode.CAPTURE.value,
)


class SemanticLoweringError(ValueError):
    def __init__(
        self,
        message: str,
        *,
        span: SourceSpan = SourceSpan(0, 0),
        code: str = "semantic_lowering_error",
    ) -> None:
        super().__init__(message)
        self.span = span
        self.code = code


@dataclass(frozen=True, slots=True)
class WorkerExport:
    public_path: str
    method: str
    receiver_module: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.public_path, str)
            or not isinstance(self.method, str)
            or not self.public_path
            or not self.method
        ):
            raise ValueError("worker export names must not be empty")
        if self.receiver_module is not None:
            if not isinstance(self.receiver_module, str) or not self.receiver_module:
                raise ValueError("worker export receiver must not be empty")
            qualified = f"{self.receiver_module}.{self.method}"
            if (
                "." in self.public_path
                and self.receiver_module.casefold() != "worker"
                and self.public_path.casefold() != qualified.casefold()
            ):
                raise ValueError("worker export receiver does not match public path")


def _worker_export_identifier(value: str) -> bool:
    return (
        fullmatch(r"[A-Za-zА-Яа-яЁё_][0-9A-Za-zА-Яа-яЁё_]*", value)
        is not None
    )


@dataclass(frozen=True, slots=True)
class SourceEdit:
    start: int
    end: int
    replacement: str
    reason: str


@dataclass(frozen=True, slots=True)
class _CopyFragment:
    text: str
    span: SourceSpan


@dataclass(frozen=True, slots=True)
class _DerivedFragment:
    text: str
    span: SourceSpan
    region: str


@dataclass(frozen=True, slots=True)
class _SyntheticFragment:
    text: str
    anchor: SourceSpan
    region: str


type _SourceFragment = _CopyFragment | _DerivedFragment | _SyntheticFragment


@dataclass(frozen=True, slots=True)
class _MappedEdit:
    observed: SourceEdit
    fragments: tuple[_SourceFragment, ...]


@dataclass(frozen=True, slots=True)
class MethodScope:
    name: str
    parameters: tuple[str, ...]
    local_names: tuple[str, ...]
    references: tuple[NameBinding, ...]


@dataclass(frozen=True, slots=True)
class NameBinding:
    name: str
    kind: str
    span: SourceSpan


@dataclass(slots=True)
class _MethodScopeState:
    name: str
    parameters: dict[str, str]
    local_names: dict[str, str]
    references: list[NameBinding]


@dataclass(frozen=True, slots=True)
class ModuleBinding:
    source: str
    module_names: tuple[str, ...]
    method_names: tuple[str, ...]
    exported_method_names: tuple[str, ...]
    method_scopes: tuple[MethodScope, ...]


@dataclass(frozen=True, slots=True)
class SemanticLoweringResult:
    mapped_source: MappedSource
    context_names: tuple[str, ...]
    dirty_roots: tuple[str, ...]
    persistent_write_roots: tuple[str, ...]
    worker_dependencies: tuple[str, ...]
    messages_intercepted: int
    edits: tuple[SourceEdit, ...]

    @property
    def source(self) -> str:
        return self.mapped_source.text

    @property
    def source_map(self) -> SourceMap:
        return self.mapped_source.source_map


class SemanticNotebookLowerer:
    _RESERVED_WORKER_CONTEXT_MEMBERS = frozenset(
        {
            "runtimeworkeractivegeneration",
            "runtimeworkerpinnedoperationgeneration",
        }
    )
    _RESERVED_WORKER_LOCAL_PREFIX = "__onecpinnedworkergeneration"
    _CONTEXT_MUTATION_METHODS = frozenset(
        {"вставить", "insert", "удалить", "delete"}
    )
    _CONTEXT_READ_METHODS = frozenset(
        {"количество", "count", "получить", "get", "свойство", "property"}
    )
    _CONTEXT_ESCAPE_RESULT_NAMES = frozenset(
        {"результат", "результатинструкции"}
    )
    _CONTEXT_ABRUPT_STATEMENTS = frozenset(
        {
            "BreakStatement",
            "ContinueStatement",
            "GotoStatement",
            "RaiseStatement",
            "ReturnStatement",
        }
    )
    _CONTEXT_UNSTRUCTURED_TRANSFERS = frozenset(
        {"BreakStatement", "ContinueStatement", "GotoStatement"}
    )
    _KNOWN_PLATFORM_GLOBALS = NOTEBOOK_PLATFORM_GLOBALS

    def __init__(
        self,
        parser_target: PythonParserTarget,
        *,
        context_names: Iterable[str] = (),
        platform_globals: Iterable[str] = (),
        worker_exports: Iterable[WorkerExport] = (),
    ) -> None:
        self.parser_target = parser_target
        self._initial_context = self._ordered_names(context_names)
        self._module_names: dict[str, str] = {}
        self._platform_globals = self._KNOWN_PLATFORM_GLOBALS | {
            name.casefold() for name in platform_globals
        }
        self._module_context_aliases: set[str] = set()
        self._exports: dict[str, WorkerExport] = {}
        for export in worker_exports:
            key = export.public_path.casefold()
            if key in self._exports:
                raise ValueError(f"duplicate worker export {export.public_path!r}")
            self._exports[key] = export

    def set_worker_exports(self, exports: Iterable[WorkerExport]) -> None:
        """Replace the active worker generation's public-call catalog."""
        self.commit_worker_exports(self.prepare_worker_exports(exports))

    @staticmethod
    def prepare_worker_exports(exports: Iterable[WorkerExport]) -> tuple[WorkerExport, ...]:
        """Validate a catalog before an external worker activation can begin."""
        catalog: dict[str, WorkerExport] = {}
        for export in exports:
            if not isinstance(export, WorkerExport):
                raise ValueError("worker export catalog contains an invalid entry")
            if (
                not _worker_export_identifier(export.method)
                or any(
                    not _worker_export_identifier(part)
                    for part in export.public_path.split(".")
                )
                or (
                    export.receiver_module is not None
                    and not _worker_export_identifier(export.receiver_module)
                )
            ):
                raise ValueError("worker export components must be BSL identifiers")
            key = export.public_path.casefold()
            if key in catalog:
                raise ValueError(f"duplicate worker export {export.public_path!r}")
            catalog[key] = export
        return tuple(catalog.values())

    def commit_worker_exports(self, exports: tuple[WorkerExport, ...]) -> None:
        """Commit an already validated immutable catalog without revalidation."""
        self._exports = {export.public_path.casefold(): export for export in exports}

    def force_commit_worker_exports(self, exports: tuple[WorkerExport, ...]) -> None:
        """Infallible recovery commit used after the server worker has swapped."""
        self._exports = {export.public_path.casefold(): export for export in exports}

    @property
    def worker_export_identity(
        self,
    ) -> tuple[tuple[str, str, str | None], ...]:
        """Immutable identity of the active lowering catalog.

        Runtime preparation uses this identity only as a stale-catalog fence;
        it is never an execution artifact or a serialized Worker payload.
        """
        return tuple(
            sorted(
                (
                    export.public_path.casefold(),
                    export.method.casefold(),
                    (
                        None
                        if export.receiver_module is None
                        else export.receiver_module.casefold()
                    ),
                )
                for export in self._exports.values()
            )
        )

    @property
    def context_names(self) -> frozenset[str]:
        """Read-only normalized persistent-name catalog."""
        return frozenset(self._initial_context)

    @property
    def persistent_names(self) -> tuple[str, ...]:
        """Persistent names with their first committed BSL spelling."""
        return tuple(self._initial_context.values())

    def restore_persistent_names(self, names: tuple[str, ...]) -> None:
        """Restore a pre-execution catalog after a notebook cell fails."""
        self._initial_context = self._ordered_names(names)

    def bind_module(self, source: str) -> ModuleBinding:
        """Register worker-module symbols without lowering its executable source."""
        root, _tokens = parse_raw_module(source, self.parser_target)
        module_names, method_names, exported_method_names = self._module_symbols(root)
        self._module_names.update(module_names)
        self._module_names.update(method_names)
        method_scopes: list[_MethodScopeState] = []

        stack: list[tuple[Any, _MethodScopeState | None]] = [(root, None)]
        while stack:
            node, scope = stack.pop()
            if not is_dataclass(node):
                continue
            kind = type(node).__name__
            if kind == "ModuleVariableDeclaration":
                continue
            if kind in {"ProcedureDeclaration", "FunctionDeclaration"}:
                local_names = self._ordered_names(
                    name
                    for declaration in node.Body.LocalDeclarations
                    for name in declaration.Names
                )
                local_names.update(
                    (key, name)
                    for key, name in self._method_implicit_locals(
                        node.Body.Code
                    ).items()
                    if key not in self._module_names
                )
                method_scope = _MethodScopeState(
                    node.Name,
                    self._ordered_names(
                        item.Name for item in node.Parameters.Items
                    )
                    if node.Parameters is not None
                    else {},
                    local_names,
                    [],
                )
                method_scopes.append(method_scope)
                stack.append((node.Body.Code, method_scope))
                continue
            if kind == "AccessChain" and scope is not None:
                root_span = SourceSpan(
                    node.span.start, node.span.start + len(node.Root)
                )
                if source[root_span.start:root_span.end] != node.Root:
                    raise ValueError("method root span does not match its identifier")
                scope.references.append(
                    NameBinding(
                        node.Root, self._resolve_method_name(node, scope), root_span
                    )
                )
            self._push_bound_children(stack, node, scope)

        return ModuleBinding(
            source=source,
            module_names=tuple(module_names.values()),
            method_names=tuple(method_names.values()),
            exported_method_names=tuple(exported_method_names.values()),
            method_scopes=tuple(
                MethodScope(
                    scope.name,
                    tuple(scope.parameters.values()),
                    tuple(scope.local_names.values()),
                    tuple(scope.references),
                )
                for scope in method_scopes
            ),
        )

    @classmethod
    def _method_implicit_locals(cls, body: Any) -> dict[str, str]:
        """BSL bare assignments and loop counters are local throughout a method."""
        names: dict[str, str] = {}
        stack = [body]
        while stack:
            node = stack.pop()
            if not is_dataclass(node):
                continue
            kind = type(node).__name__
            if kind == "SimpleStatement" and node.Value is not None:
                target = node.Target
                if (
                    type(target).__name__ == "AccessChain"
                    and target.Arguments is None
                    and not target.Postfix
                ):
                    names.setdefault(target.Root.casefold(), target.Root)
            elif kind in {"ForEachStatement", "ForRangeStatement"}:
                names.setdefault(node.Variable.casefold(), node.Variable)
            children: list[Any] = []
            for field in fields(node):
                if field.name == "span":
                    continue
                value = getattr(node, field.name)
                if is_dataclass(value):
                    children.append(value)
                elif isinstance(value, tuple):
                    children.extend(
                        item for item in value if is_dataclass(item)
                    )
            stack.extend(reversed(children))
        return names

    def _module_symbols(
        self,
        root: Any,
    ) -> tuple[dict[str, str], dict[str, str], dict[str, str]]:
        module_names = self._ordered_names(
            variable.Name
            for declaration in root.Declarations
            for variable in declaration.Variables
        )
        method_names: dict[str, str] = {}
        exported_method_names: dict[str, str] = {}
        elements = root.Elements
        while elements.Item is not None:
            item = elements.Item
            if type(item).__name__ == "Method":
                declaration = item.Declaration
                method_names.setdefault(
                    declaration.Name.casefold(),
                    declaration.Name,
                )
                if declaration.Export is not None:
                    exported_method_names.setdefault(
                        declaration.Name.casefold(),
                        declaration.Name,
                    )
            elements = elements.Rest
        return module_names, method_names, exported_method_names

    def _resolve_method_name(
        self,
        node: Any,
        scope: _MethodScopeState,
    ) -> str:
        normalized = node.Root.casefold()
        if normalized in scope.parameters:
            return "parameter"
        if normalized in scope.local_names:
            return "local"
        if normalized in self._module_names:
            return "module"
        if normalized == "сообщить" or normalized in self._platform_globals:
            return "platform"
        call = self._called_path(node)
        if call is not None and call[0].casefold() in self._exports:
            return "worker-export"
        if normalized in self._initial_context:
            return "persistent"
        return "unknown"

    def lower(
        self,
        source: str,
        *,
        mode: LoweringMode | None = None,
        profile: LoweringProfile | None = None,
        message_collector_key: str = "__onec_cell_messages",
        worker_exports: tuple[WorkerExport, ...] | None = None,
    ) -> SemanticLoweringResult:
        digest = source_sha256(source)
        visible = mapped_visible_source(
            source,
            SourceUnitRef(
                SourceUnitKind.NOTEBOOK_CELL,
                f"anonymous-semantic-lowering:{digest}",
                0,
                digest,
            ),
        )
        return self.lower_mapped(
            visible,
            mode=mode,
            profile=profile,
            message_collector_key=message_collector_key,
            worker_exports=worker_exports,
        )

    def lower_mapped(
        self,
        source: MappedSource,
        *,
        mode: LoweringMode | None = None,
        profile: LoweringProfile | None = None,
        message_collector_key: str = "__onec_cell_messages",
        worker_exports: tuple[WorkerExport, ...] | None = None,
    ) -> SemanticLoweringResult:
        resolved_profile = self._resolve_profile(mode=mode, profile=profile)
        if worker_exports is not None and type(worker_exports) is not tuple:
            raise TypeError("worker exports must be an immutable tuple")
        catalog = (
            tuple(self._exports.values())
            if worker_exports is None
            else self.prepare_worker_exports(worker_exports)
        )
        self._call_exports = {
            export.public_path.casefold(): export for export in catalog
        }
        try:
            return self._lower_mapped(
                source,
                profile=resolved_profile,
                message_collector_key=message_collector_key,
            )
        finally:
            del self._call_exports

    @staticmethod
    def _resolve_profile(
        *,
        mode: LoweringMode | None,
        profile: LoweringProfile | None,
    ) -> LoweringProfile:
        if profile is not None:
            if mode is not None:
                raise ValueError("mode and profile cannot both be provided")
            if not isinstance(profile, LoweringProfile):
                raise TypeError("profile must be a LoweringProfile")
            return profile
        if mode is None:
            raise TypeError("mode or profile is required")
        if not isinstance(mode, LoweringMode):
            raise TypeError("mode must be a LoweringMode")
        if mode is LoweringMode.MAIN:
            return MAIN_LOWERING_PROFILE
        return CAPTURE_LOWERING_PROFILE

    def _lower_mapped(
        self,
        source: MappedSource,
        *,
        profile: LoweringProfile,
        message_collector_key: str,
    ) -> SemanticLoweringResult:
        if not isinstance(source, MappedSource):
            raise ValueError("mapped lowering source must be a MappedSource")
        try:
            collector_tokens = tokenize(message_collector_key)
        except BslLexError as error:
            raise ValueError("message collector key must be a BSL identifier") from error
        if (
            len(collector_tokens) != 1
            or collector_tokens[0].type != "ID"
            or collector_tokens[0].text != message_collector_key
        ):
            raise ValueError("message collector key must be a BSL identifier")
        root = self.parser_target.parse_ast(source.text, "БлокНоутбука")
        self._source = source.text
        self._tokens = tokenize(source.text)
        self._profile = profile
        self._context = dict(self._initial_context)
        self._cell_local_names = self._loop_variables(root)
        result_channel = profile.result_channel
        self._cell_local_names.setdefault(result_channel.casefold(), result_channel)
        module_context_aliases = self._validate_worker_context_flow(root)
        for normalized, name in self._assignment_roots(root).items():
            self._context.setdefault(normalized, name)
        self._dirty: dict[str, str] = {}
        self._persistent_writes: dict[str, str] = {}
        self._dependencies: dict[str, str] = {}
        self._messages = 0
        self._message_collector_key = message_collector_key
        self._edits: list[_MappedEdit] = []

        stack: list[Any] = [root]
        while stack:
            node = stack.pop()
            if not is_dataclass(node):
                continue
            kind = type(node).__name__
            if kind == "SimpleStatement":
                self._bind_statement(node, stack)
                continue
            if kind == "RaiseStatement":
                self._validate_raise_statement(node)
            if kind == "AccessChain":
                self._bind_access(node)
            self._push_children(stack, node)

        mapped_edits = self._validated_mapped_edits(
            self._edits,
            source_length=len(source.text),
        )
        lowered = self._build_mapped_source(source, mapped_edits, profile=profile)
        self._initial_context = dict(self._context)
        self._module_context_aliases = module_context_aliases
        return SemanticLoweringResult(
            mapped_source=lowered,
            context_names=tuple(self._context.values()),
            dirty_roots=tuple(self._dirty.values()),
            persistent_write_roots=tuple(self._persistent_writes.values()),
            worker_dependencies=tuple(self._dependencies.values()),
            messages_intercepted=self._messages,
            edits=tuple(item.observed for item in mapped_edits),
        )

    def _bind_statement(self, node: Any, stack: list[Any]) -> None:
        target = node.Target
        value = node.Value
        normalized = target.Root.casefold()
        self._reject_reserved_worker_local(target)
        if value is None:
            call = self._called_path(target)
            intercept_message = (
                normalized == "сообщить"
                and target.Arguments is not None
                and normalized not in self._module_names
                and (call is None or call[0].casefold() not in self._call_exports)
            )
            if intercept_message:
                first_argument = self._bind_message_call(target)
                if first_argument is not None:
                    stack.append(first_argument)
                return
            if normalized == "e1cruntimeконтекстотладки":
                self._bind_access(target)
            elif not self._has_call(target):
                raise SemanticLoweringError(
                    f"bare access-chain statement is forbidden at {target.span.start}",
                    span=SourceSpan(target.span.start, target.span.end),
                    code="bare_access_chain_statement",
                )
            else:
                self._bind_access(target)
        elif self._is_direct_name(target) and normalized in {
            "e1cruntimeконтекст",
            "e1cruntimeконтекстотладки",
        }:
            raise SemanticLoweringError(
                f"runtime namespace {target.Root!r} is reserved at {target.span.start}",
                span=SourceSpan(target.span.start, target.span.end),
                code="reserved_runtime_namespace",
            )
        elif normalized == "e1cruntimeконтекстотладки":
            self._bind_capture_target(target)
        elif normalized in self._cell_local_names:
            self._bind_access(target)
        elif normalized in self._module_names:
            self._bind_access(target)
        elif self._is_direct_name(target):
            self._bind_persistent_assignment(node, target, value)
        else:
            self._bind_access(target)
        self._push_children(stack, target, skip_root=True)
        if value is not None:
            stack.append(value)

    def _validate_worker_context_flow(self, root: Any) -> set[str]:
        has_goto = self._context_contains_node_kind(
            root,
            frozenset({"GotoStatement"}),
        )
        clear_taint = not self._context_contains_node_kind(
            root,
            self._CONTEXT_UNSTRUCTURED_TRANSFERS,
        )
        entry = set(self._module_context_aliases)
        aliases = self._validate_context_block(
            root,
            entry,
            clear_taint=clear_taint,
        )
        if has_goto:
            for _attempt in range(self._context_fixed_point_limit()):
                next_entry = entry | aliases
                if next_entry == entry:
                    break
                entry = next_entry
                aliases = self._validate_context_block(
                    root,
                    entry,
                    clear_taint=False,
                )
            else:
                self._raise_context_flow_not_converged(root)
        return aliases & set(self._module_names)

    def _context_fixed_point_limit(self) -> int:
        return len(set(self._module_names) | set(self._cell_local_names)) + 2

    @staticmethod
    def _context_contains_node_kind(root: Any, kinds: frozenset[str]) -> bool:
        stack = [root]
        while stack:
            node = stack.pop()
            if not is_dataclass(node):
                continue
            if type(node).__name__ in kinds:
                return True
            for field in fields(node):
                if field.name == "span":
                    continue
                value = getattr(node, field.name)
                if is_dataclass(value):
                    stack.append(value)
                elif isinstance(value, tuple):
                    stack.extend(item for item in value if is_dataclass(item))
        return False

    def _validate_context_block(
        self,
        block: Any,
        aliases: set[str],
        *,
        clear_taint: bool = True,
    ) -> set[str]:
        current = set(aliases)
        abrupt = set[str]()
        cursor = block
        while type(cursor).__name__ == "CodeBlock" and cursor.First is not None:
            statement = cursor.First
            if type(statement).__name__ == "LabeledStatement":
                statement = statement.Statement
            current.update(abrupt)
            current = self._validate_context_statement(
                statement,
                current,
                clear_taint=clear_taint,
            )
            if type(statement).__name__ in self._CONTEXT_ABRUPT_STATEMENTS:
                abrupt.update(current)
            cursor = cursor.Rest
        return current | abrupt

    def _validate_context_statement(
        self,
        statement: Any,
        aliases: set[str],
        *,
        clear_taint: bool,
    ) -> set[str]:
        kind = type(statement).__name__
        current = set(aliases)
        if kind == "SimpleStatement":
            target = statement.Target
            value = statement.Value
            self._validate_context_access_chain(
                target,
                current,
                is_assignment=value is not None,
            )
            if value is None:
                return current
            self._validate_context_expression(value, current)
            tainted = self._expression_may_be_context(value, current)
            normalized = target.Root.casefold()
            if self._is_direct_name(target):
                if normalized in self._CONTEXT_ESCAPE_RESULT_NAMES:
                    if tainted:
                        self._raise_context_alias_escape(value)
                    current.discard(normalized)
                elif (
                    normalized in self._cell_local_names
                    or normalized in self._module_names
                ):
                    if tainted:
                        current.add(normalized)
                    elif clear_taint:
                        current.discard(normalized)
                elif tainted:
                    self._raise_context_alias_escape(value)
            elif tainted:
                self._raise_context_alias_escape(value)
            return current
        if kind == "ReturnStatement":
            value = statement.Value
            if value is not None:
                self._validate_context_expression(value, current)
                if self._expression_may_be_context(value, current):
                    self._raise_context_alias_escape(value)
            return current
        if kind == "ExecuteStatement":
            raise SemanticLoweringError(
                "Dynamic notebook execution is forbidden by the Context protocol at "
                f"{statement.span.start}",
                span=SourceSpan(statement.span.start, statement.span.end),
                code="context_protocol_dynamic_execution",
            )
        if kind == "IfStatement":
            self._validate_context_expression(statement.Condition, current)
            outcomes = [
                self._validate_context_block(
                    statement.Then,
                    set(current),
                    clear_taint=clear_taint,
                )
            ]
            for clause in statement.ElseIf:
                self._validate_context_expression(clause.Condition, current)
                outcomes.append(
                    self._validate_context_block(
                        clause.Body,
                        set(current),
                        clear_taint=clear_taint,
                    )
                )
            if statement.Else is None:
                outcomes.append(current)
            else:
                outcomes.append(
                    self._validate_context_block(
                        statement.Else.Body,
                        set(current),
                        clear_taint=clear_taint,
                    )
                )
            return set().union(*outcomes)
        if kind == "WhileStatement":
            self._validate_context_expression(statement.Condition, current)
            body = self._validate_context_loop_body(
                statement.Body,
                set(current),
                clear_taint=clear_taint,
            )
            return current | body
        if kind == "ForEachStatement":
            self._validate_context_expression(statement.Iterable, current)
            body_input = set(current)
            reset = frozenset({statement.Variable.casefold()})
            body_input.difference_update(reset)
            body = self._validate_context_loop_body(
                statement.Body,
                body_input,
                clear_taint=clear_taint,
                reset_each_iteration=reset,
            )
            return current | body
        if kind == "ForRangeStatement":
            self._validate_context_expression(statement.Start, current)
            self._validate_context_expression(statement.End, current)
            body_input = set(current)
            reset = frozenset({statement.Variable.casefold()})
            body_input.difference_update(reset)
            body = self._validate_context_loop_body(
                statement.Body,
                body_input,
                clear_taint=clear_taint,
                reset_each_iteration=reset,
            )
            return current | body
        if kind == "TryStatement":
            try_state = self._validate_context_block(
                statement.TryBody,
                set(current),
                clear_taint=clear_taint,
            )
            if clear_taint:
                exception_state = self._validate_context_block(
                    statement.TryBody,
                    set(current),
                    clear_taint=False,
                )
            else:
                exception_state = try_state
            except_state = self._validate_context_block(
                statement.ExceptBody,
                set(current) | exception_state,
                clear_taint=clear_taint,
            )
            return try_state | except_state
        self._validate_context_expression(statement, current)
        return current

    def _validate_context_loop_body(
        self,
        body: Any,
        aliases: set[str],
        *,
        clear_taint: bool,
        reset_each_iteration: frozenset[str] = frozenset(),
    ) -> set[str]:
        entry = set(aliases)
        output = set(aliases)
        for _attempt in range(self._context_fixed_point_limit()):
            output = self._validate_context_block(
                body,
                entry,
                clear_taint=clear_taint,
            )
            next_entry = set(aliases)
            next_entry.update(output - reset_each_iteration)
            if next_entry <= entry:
                return entry | output
            entry.update(next_entry)
        self._raise_context_flow_not_converged(body)

    @staticmethod
    def _raise_context_flow_not_converged(node: Any) -> None:
        raise SemanticLoweringError(
            f"Context flow analysis did not converge at {node.span.start}",
            span=SourceSpan(node.span.start, node.span.end),
            code="context_flow_not_converged",
        )

    def _validate_context_expression(
        self,
        node: Any,
        aliases: set[str],
    ) -> None:
        if not is_dataclass(node):
            return
        if type(node).__name__ == "AccessChain":
            self._validate_context_access_chain(node, aliases, is_assignment=False)
        if type(node).__name__ == "NewExpression":
            for item in self._context_call_arguments(node.Arguments):
                if self._expression_may_be_context(item, aliases):
                    self._raise_context_alias_escape(item)
        for field in fields(node):
            if field.name == "span":
                continue
            value = getattr(node, field.name)
            if is_dataclass(value):
                self._validate_context_expression(value, aliases)
            elif isinstance(value, tuple):
                for item in value:
                    if is_dataclass(item):
                        self._validate_context_expression(item, aliases)

    def _validate_context_access_chain(
        self,
        node: Any,
        aliases: set[str],
        *,
        is_assignment: bool,
    ) -> None:
        argument_lists = []
        if node.Arguments is not None:
            argument_lists.append(node.Arguments)
        argument_lists.extend(
            postfix.Arguments
            for postfix in node.Postfix
            if type(postfix).__name__ == "MemberAccess"
            and postfix.Arguments is not None
        )
        for arguments in argument_lists:
            for item in self._context_call_arguments(arguments):
                if self._expression_may_be_context(item, aliases):
                    self._raise_context_alias_escape(item)

        normalized = node.Root.casefold()
        tainted_receiver = normalized == "e1cruntimeконтекст" or normalized in aliases
        if not tainted_receiver or not node.Postfix:
            return
        first = node.Postfix[0]
        kind = type(first).__name__
        if is_assignment:
            if kind == "MemberAccess":
                if first.Name.casefold() in self._RESERVED_WORKER_CONTEXT_MEMBERS:
                    self._raise_reserved_worker_protocol(first)
            elif kind == "IndexAccess" and self._worker_context_key_may_be_reserved(
                first.Index
            ):
                self._raise_reserved_worker_protocol(first)
            return
        if kind != "MemberAccess" or first.Arguments is None:
            return
        method = first.Name.casefold()
        if method in self._CONTEXT_MUTATION_METHODS:
            items = self._context_call_arguments(first.Arguments)
            if items and self._worker_context_key_may_be_reserved(items[0]):
                self._raise_reserved_worker_protocol(first)
            return
        if method not in self._CONTEXT_READ_METHODS:
            self._raise_context_alias_escape(first)

    def _expression_may_be_context(
        self,
        node: Any,
        aliases: set[str],
    ) -> bool:
        if not is_dataclass(node):
            return False
        kind = type(node).__name__
        if kind == "AccessChain":
            return (
                node.Arguments is None
                and not node.Postfix
                and (
                    node.Root.casefold() == "e1cruntimeконтекст"
                    or node.Root.casefold() in aliases
                )
            )
        if kind in {"ParenthesizedExpression", "PostfixedPrimary"} and node.Postfix:
            return False
        for field in fields(node):
            if field.name == "span":
                continue
            value = getattr(node, field.name)
            if is_dataclass(value) and self._expression_may_be_context(
                value,
                aliases,
            ):
                return True
            if isinstance(value, tuple) and any(
                is_dataclass(item)
                and self._expression_may_be_context(item, aliases)
                for item in value
            ):
                return True
        return False

    @staticmethod
    def _context_call_arguments(arguments: Any) -> tuple[Any, ...]:
        if arguments is None or arguments.Items is None:
            return ()
        items = arguments.Items.Items
        return () if items is None else tuple(items)

    @staticmethod
    def _raise_context_alias_escape(node: Any) -> None:
        raise SemanticLoweringError(
            f"e1cRuntimeКонтекст cannot escape into opaque state at {node.span.start}",
            span=SourceSpan(node.span.start, node.span.end),
            code="context_alias_escape",
        )

    @staticmethod
    def _raise_reserved_worker_protocol(node: Any) -> None:
        raise SemanticLoweringError(
            "Worker generation Context protocol member is reserved at "
            f"{node.span.start}",
            span=SourceSpan(node.span.start, node.span.end),
            code="reserved_worker_protocol_member",
        )

    def _worker_context_key_may_be_reserved(self, expression: Any) -> bool:
        tokens = tokenize(self._source[expression.span.start : expression.span.end])
        if len(tokens) != 1 or tokens[0].type != "STRING":
            return True
        literal = tokens[0].text[1:-1].replace('""', '"')
        return literal.casefold() in self._RESERVED_WORKER_CONTEXT_MEMBERS

    def _bind_persistent_assignment(
        self,
        statement: Any,
        target: Any,
        value: Any,
    ) -> None:
        name = target.Root
        normalized = name.casefold()
        if normalized in self._platform_globals or normalized == "сообщить":
            raise SemanticLoweringError(
                f"cannot assign platform global {name!r} at {target.span.start}",
                span=SourceSpan(target.span.start, target.span.end),
                code="platform_global_assignment",
            )
        self._context.setdefault(normalized, name)
        self._persistent_writes.setdefault(normalized, name)
        equals = next(
            (
                token
                for token in self._tokens
                if token.type == "="
                and target.span.end <= token.start < value.span.start
            ),
            None,
        )
        if equals is None:
            raise SemanticLoweringError(
                f"assignment operator is missing at {statement.span.start}",
                span=SourceSpan(statement.span.start, statement.span.end),
                code="missing_assignment_operator",
            )
        gap = self._source[target.span.end : equals.start]
        if gap.isspace():
            self._edit(
                target.span.start,
                equals.end,
                "persistent-assignment-target",
                _SyntheticFragment(
                    'e1cRuntimeКонтекст.Вставить("',
                    SourceSpan(target.span.start, target.span.end),
                    "persistent_assignment_open",
                ),
                _DerivedFragment(
                    name,
                    SourceSpan(target.span.start, target.span.end),
                    "persistent_name",
                ),
                _SyntheticFragment(
                    '",',
                    SourceSpan(equals.start, equals.end),
                    "persistent_assignment_operator",
                ),
            )
        else:
            self._edit(
                target.span.start,
                target.span.end,
                "persistent-assignment-target",
                _SyntheticFragment(
                    'e1cRuntimeКонтекст.Вставить("',
                    SourceSpan(target.span.start, target.span.end),
                    "persistent_assignment_open",
                ),
                _DerivedFragment(
                    name,
                    SourceSpan(target.span.start, target.span.end),
                    "persistent_name",
                ),
                _SyntheticFragment(
                    '"',
                    SourceSpan(target.span.end, target.span.end),
                    "persistent_assignment_name_close",
                ),
            )
            self._edit(
                equals.start,
                equals.end,
                "persistent-assignment-operator",
                _SyntheticFragment(
                    ",",
                    SourceSpan(equals.start, equals.end),
                    "persistent_assignment_operator",
                ),
            )
        self._edit(
            statement.span.end,
            statement.span.end,
            "persistent-assignment-close",
            _SyntheticFragment(
                ")",
                SourceSpan(statement.span.end, statement.span.end),
                "persistent_assignment_close",
            ),
        )

    def _bind_capture_target(self, target: Any) -> None:
        self._require_capture_namespace(target)
        if self._is_direct_capture_root(target):
            root = target.Postfix[0].Name
            self._dirty.setdefault(root.casefold(), root)

    def _bind_access(self, node: Any) -> None:
        root = node.Root
        normalized = root.casefold()
        self._reject_reserved_worker_local(node)
        if normalized == "e1cruntimeконтекст":
            if self._is_context_capture_alias(node):
                raise SemanticLoweringError(
                    "e1cRuntimeКонтекст.e1cRuntimeКонтекстОтладки is forbidden at "
                    f"{node.Postfix[0].span.start}",
                    span=SourceSpan(
                        node.Postfix[0].span.start,
                        node.Postfix[0].span.end,
                    ),
                    code="capture_namespace_alias",
                )
            return
        if normalized == "e1cruntimeконтекстотладки":
            self._require_capture_namespace(node)
            return
        if normalized in self._cell_local_names:
            return
        if normalized in self._module_names:
            return

        call = self._called_path(node)
        if call is not None:
            path, arguments_start = call
            export = self._call_exports.get(path.casefold())
            if export is not None:
                self._bind_worker_export(node, arguments_start, export)
                return
        if normalized in self._context:
            self._edit(
                node.span.start,
                node.span.start,
                "persistent-reference",
                _SyntheticFragment(
                    "e1cRuntimeКонтекст.",
                    SourceSpan(node.span.start, node.span.start),
                    "persistent_reference_prefix",
                ),
            )
            return
        if normalized in self._platform_globals:
            return
        # Unknown reads belong to the native BSL name resolver. Only names
        # established by notebook writes or imported context are persistent.
        return

    def _bind_message_call(self, node: Any) -> Any | None:
        arguments = node.Arguments.Items
        items = () if arguments is None else arguments.Items
        prefix = "e1cRuntimeКонтекст." + self._message_collector_key + ".Добавить(Строка("
        if not items:
            self._edit(
                node.span.start,
                node.span.end,
                "message-interception",
                _SyntheticFragment(
                    prefix,
                    SourceSpan(node.span.start, node.span.end),
                    "message_interception_open",
                ),
                _SyntheticFragment(
                    "Неопределено",
                    SourceSpan(node.span.start, node.span.end),
                    "message_interception_default",
                ),
                _SyntheticFragment(
                    "))",
                    SourceSpan(node.span.start, node.span.end),
                    "message_interception_close",
                ),
            )
            self._messages += 1
            return None
        first = items[0]
        self._edit(
            node.span.start,
            first.span.start,
            "message-interception-open",
            _SyntheticFragment(
                prefix,
                SourceSpan(node.span.start, first.span.start),
                "message_interception_open",
            ),
        )
        self._edit(
            first.span.end,
            node.span.end,
            "message-interception-close",
            _SyntheticFragment(
                "))",
                SourceSpan(first.span.end, node.span.end),
                "message_interception_close",
            ),
        )
        self._messages += 1
        return first

    @staticmethod
    def _is_context_capture_alias(node: Any) -> bool:
        return (
            bool(node.Postfix)
            and type(node.Postfix[0]).__name__ == "MemberAccess"
            and node.Postfix[0].Name.casefold() == "e1cruntimeконтекстотладки"
        )

    def _loop_variables(self, root: Any) -> dict[str, str]:
        names: dict[str, str] = {}
        stack: list[Any] = [root]
        while stack:
            node = stack.pop()
            if not is_dataclass(node):
                continue
            if type(node).__name__ in {"ForEachStatement", "ForRangeStatement"}:
                names.setdefault(node.Variable.casefold(), node.Variable)
            self._push_children(stack, node)
        return names

    def _assignment_roots(self, root: Any) -> dict[str, str]:
        names: dict[str, str] = {}
        stack: list[Any] = [root]
        while stack:
            node = stack.pop()
            if not is_dataclass(node):
                continue
            if type(node).__name__ == "SimpleStatement" and node.Value is not None:
                target = node.Target
                normalized = target.Root.casefold()
                if (
                    self._is_direct_name(target)
                    and normalized not in self._cell_local_names
                    and normalized not in self._module_names
                    and normalized not in self._platform_globals
                    and normalized not in {
                        "сообщить", "e1cruntimeконтекст",
                        "e1cruntimeконтекстотладки",
                    }
                ):
                    names.setdefault(normalized, target.Root)
            self._push_children(stack, node)
        return names

    @staticmethod
    def _validate_raise_statement(node: Any) -> None:
        parameters = node.Parameters
        if type(parameters).__name__ != "RaiseCallParameters":
            return
        arguments = parameters.Arguments.Items
        items = () if arguments is None else arguments.Items
        if len(items) > 1 and (
            parameters.Postfix
            or parameters.Tail.span.start != parameters.Tail.span.end
        ):
            raise SemanticLoweringError(
                "multi-argument ВызватьИсключение cannot have a continuation at "
                f"{parameters.Tail.span.start}",
                span=SourceSpan(parameters.Tail.span.start, parameters.Tail.span.end),
                code="raise_continuation",
            )

    def _bind_worker_export(
        self,
        node: Any,
        arguments_start: int,
        export: WorkerExport,
    ) -> None:
        method_span = self._called_name_span(node.span.start, arguments_start)
        visible_method = self._source[method_span.start : method_span.end]
        method_fragment: _SourceFragment = (
            _CopyFragment(export.method, method_span)
            if export.method == visible_method
            else _DerivedFragment(export.method, method_span, "worker_method")
        )
        receiver = (
            "e1cRuntimeКонтекст.RuntimeWorker."
            if export.receiver_module is None
            else (
                "__OnecPinnedWorkerGeneration.Modules.Получить("
                f'"{export.receiver_module.replace(chr(34), chr(34) * 2)}").'
            )
        )
        self._edit(
            node.span.start,
            arguments_start,
            "worker-export",
            _SyntheticFragment(
                receiver,
                SourceSpan(node.span.start, arguments_start),
                "worker_receiver",
            ),
            method_fragment,
        )
        self._dependencies.setdefault(
            export.public_path.casefold(),
            export.public_path,
        )

    def _reject_reserved_worker_local(self, node: Any) -> None:
        if node.Root.casefold().startswith(self._RESERVED_WORKER_LOCAL_PREFIX):
            raise SemanticLoweringError(
                "Worker generation kernel handoff local is reserved at "
                f"{node.span.start}",
                span=SourceSpan(node.span.start, node.span.end),
                code="reserved_worker_protocol_local",
            )

    def _called_name_span(self, start: int, arguments_start: int) -> SourceSpan:
        names = tuple(
            token
            for token in self._tokens
            if token.type == "ID" and start <= token.start < arguments_start
        )
        if not names:
            raise SemanticLoweringError(
                f"worker call name is missing at {start}",
                span=SourceSpan(start, start),
                code="missing_worker_call_name",
            )
        name = names[-1]
        return SourceSpan(name.start, name.end)

    def _require_capture_namespace(self, node: Any) -> None:
        if self._profile.capture_namespace_rule is CaptureNamespaceRule.FORBIDDEN:
            raise SemanticLoweringError(
                f"e1cRuntimeКонтекстОтладки is only available in CAPTURE at {node.span.start}",
                span=SourceSpan(node.span.start, node.span.end),
                code="capture_namespace_mode",
            )
        if node.Arguments is not None:
            raise SemanticLoweringError(
                f"capture root must start with a member access at {node.span.start}",
                span=SourceSpan(node.span.start, node.span.end),
                code="capture_root_shape",
            )
        if not node.Postfix:
            raise SemanticLoweringError(
                f"bare capture namespace is forbidden at {node.span.start}",
                span=SourceSpan(node.span.start, node.span.end),
                code="bare_capture_namespace",
            )
        first = node.Postfix[0]
        if (
            type(first).__name__ != "MemberAccess"
            or first.Arguments is not None
        ):
            raise SemanticLoweringError(
                f"capture root must start with a member access at {node.span.start}",
                span=SourceSpan(node.span.start, node.span.end),
                code="capture_root_shape",
            )

    @staticmethod
    def _is_direct_name(node: Any) -> bool:
        return node.Arguments is None and not node.Postfix

    @staticmethod
    def _has_call(node: Any) -> bool:
        return node.Arguments is not None or any(
            type(postfix).__name__ == "MemberAccess"
            and postfix.Arguments is not None
            for postfix in node.Postfix
        )

    @staticmethod
    def _is_direct_capture_root(node: Any) -> bool:
        return (
            node.Arguments is None
            and len(node.Postfix) == 1
            and type(node.Postfix[0]).__name__ == "MemberAccess"
            and node.Postfix[0].Arguments is None
        )

    @staticmethod
    def _called_path(node: Any) -> tuple[str, int] | None:
        parts = [node.Root]
        if node.Arguments is not None:
            return ".".join(parts), node.Arguments.span.start
        for postfix in node.Postfix:
            if type(postfix).__name__ != "MemberAccess":
                return None
            parts.append(postfix.Name)
            if postfix.Arguments is not None:
                return ".".join(parts), postfix.Arguments.span.start
        return None

    def _push_children(
        self,
        stack: list[Any],
        node: Any,
        *,
        skip_root: bool = False,
    ) -> None:
        children: list[Any] = []
        for field in fields(node):
            if field.name == "span" or (skip_root and field.name == "Root"):
                continue
            value = getattr(node, field.name)
            if is_dataclass(value):
                children.append(value)
            elif isinstance(value, tuple):
                children.extend(item for item in value if is_dataclass(item))
        stack.extend(reversed(children))

    def _push_bound_children(
        self,
        stack: list[tuple[Any, _MethodScopeState | None]],
        node: Any,
        scope: _MethodScopeState | None,
    ) -> None:
        children: list[Any] = []
        for field in fields(node):
            if field.name == "span":
                continue
            value = getattr(node, field.name)
            if is_dataclass(value):
                children.append(value)
            elif isinstance(value, tuple):
                children.extend(item for item in value if is_dataclass(item))
        stack.extend((child, scope) for child in reversed(children))

    def _edit(
        self,
        start: int,
        end: int,
        reason: str,
        *fragments: _SourceFragment,
    ) -> None:
        replacement = "".join(fragment.text for fragment in fragments)
        self._edits.append(
            _MappedEdit(SourceEdit(start, end, replacement, reason), tuple(fragments))
        )

    def _build_mapped_source(
        self,
        source: MappedSource,
        edits: tuple[_MappedEdit, ...],
        *,
        profile: LoweringProfile,
    ) -> MappedSource:
        builder = SourceTransformBuilder(source)
        cursor = 0
        for mapped_edit in edits:
            edit = mapped_edit.observed
            if cursor < edit.start:
                builder.copy(SourceSpan(cursor, edit.start))
            for fragment in mapped_edit.fragments:
                if isinstance(fragment, _CopyFragment):
                    builder.copy(fragment.span)
                elif isinstance(fragment, _DerivedFragment):
                    builder.derived(fragment.text, fragment.span, fragment.region)
                else:
                    builder.synthetic(fragment.text, fragment.anchor, fragment.region)
            cursor = edit.end
        if cursor < len(source.text) or not edits:
            builder.copy(SourceSpan(cursor, len(source.text)))
        return builder.build(
            SourceArtifactKind.SEMANTIC_LOWERING,
            mode=profile.source_map_tag,
        )

    @classmethod
    def _validated_mapped_edits(
        cls,
        edits: Sequence[_MappedEdit],
        *,
        source_length: int,
    ) -> tuple[_MappedEdit, ...]:
        ordered = tuple(
            sorted(edits, key=lambda item: (item.observed.start, item.observed.end))
        )
        cls._validated_edits(
            tuple(item.observed for item in ordered),
            source_length=source_length,
        )
        return ordered

    @staticmethod
    def _validated_edits(
        edits: Sequence[SourceEdit],
        *,
        source_length: int,
    ) -> tuple[SourceEdit, ...]:
        ordered = tuple(sorted(edits, key=lambda item: (item.start, item.end)))
        previous_end = 0
        for edit in ordered:
            if edit.start < 0 or edit.end < edit.start or edit.end > source_length:
                offset = max(0, min(edit.start, source_length))
                raise SemanticLoweringError(
                    "invalid lowering source edit",
                    span=SourceSpan(offset, offset),
                    code="invalid_source_edit",
                )
            if edit.start < previous_end:
                raise SemanticLoweringError(
                    "overlapping lowering source edits",
                    span=SourceSpan(edit.start, edit.end),
                    code="overlapping_source_edits",
                )
            previous_end = max(previous_end, edit.end)
        return ordered

    @staticmethod
    def _ordered_names(names: Iterable[str]) -> dict[str, str]:
        result: dict[str, str] = {}
        for name in names:
            if not name:
                raise ValueError("context name must not be empty")
            result.setdefault(name.casefold(), name)
        return result
