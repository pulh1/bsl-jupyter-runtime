"""Project the generated full BSL AST into the Worker-facing module model."""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, fields

from onec_runtime.bsl import generated_semantic_parser as generated
from onec_runtime.bsl.lexer import Token, tokenize
from onec_runtime.bsl.parser_target import BslParseError, PythonParserTarget
from onec_runtime.bsl.source_maps import SourceSpan, source_sha256
from onec_runtime.bsl.worker_preprocessor import select_server_effective_tokens
from onec_runtime.bsl.worker_projection_model import (
    BareName,
    BareNameKind,
    ParsedMethodModel,
    ParsedModuleModel,
)
from onec_runtime.performance_profile import PhaseRecorder


_IGNORED = 0
_ONE = 1
_MANY = 2
_POSTFIX = 3
_HOT = 4
_CARRIER = 5

# Every generated dataclass field is explicit: typed semantic child edge,
# direct hot-path responsibility, linked carrier, or intentionally ignored
# scalar/non-fact field. Field order mirrors the generated dataclasses.
_NODE_FIELD_SCHEMA: dict[str, tuple[tuple[str, int], ...]] = {
    "Module": (("Declarations", _MANY), ("Elements", _ONE), ("span", _IGNORED)),
    "ModuleElements": (("Item", _CARRIER), ("Rest", _CARRIER), ("span", _IGNORED)),
    "NotebookCell": (("Elements", _ONE), ("span", _IGNORED)),
    "ModuleVariableDeclaration": (("Variables", _MANY), ("span", _IGNORED)),
    "ModuleVariable": (("Name", _IGNORED), ("Export", _IGNORED), ("span", _IGNORED)),
    "Method": (
        ("Decorations", _MANY), ("Async", _IGNORED),
        ("Declaration", _ONE), ("span", _IGNORED),
    ),
    "ProcedureDeclaration": (
        ("Name", _IGNORED), ("Parameters", _ONE), ("Export", _IGNORED),
        ("Body", _ONE), ("span", _IGNORED),
    ),
    "FunctionDeclaration": (
        ("Name", _IGNORED), ("Parameters", _ONE), ("Export", _IGNORED),
        ("Body", _ONE), ("span", _IGNORED),
    ),
    "MethodBody": (("LocalDeclarations", _MANY), ("Code", _ONE), ("span", _IGNORED)),
    "LocalVariableDeclaration": (("Names", _HOT), ("span", _IGNORED)),
    "ParameterList": (("Items", _MANY), ("span", _IGNORED)),
    "Parameter": (
        ("Decorations", _HOT), ("ByValue", _IGNORED), ("Name", _HOT),
        ("Default", _HOT), ("span", _IGNORED),
    ),
    "SignedConstant": (("Sign", _IGNORED), ("Value", _IGNORED), ("span", _IGNORED)),
    "Decoration": (("Name", _IGNORED), ("Parameters", _ONE), ("span", _IGNORED)),
    "AnnotationParameters": (("Items", _MANY), ("span", _IGNORED)),
    "NamedAnnotationParameter": (("Name", _IGNORED), ("Value", _ONE), ("span", _IGNORED)),
    "PositionalAnnotationParameter": (("Value", _ONE), ("span", _IGNORED)),
    "CodeBlock": (("First", _CARRIER), ("Rest", _CARRIER), ("span", _IGNORED)),
    "LabeledStatement": (("Label", _IGNORED), ("Statement", _ONE), ("span", _IGNORED)),
    "Label": (("Name", _IGNORED), ("span", _IGNORED)),
    "ContinueStatement": (("span", _IGNORED),),
    "BreakStatement": (("span", _IGNORED),),
    "GotoStatement": (("Label", _IGNORED), ("span", _IGNORED)),
    "IfStatement": (
        ("Condition", _ONE), ("Then", _ONE), ("ElseIf", _MANY),
        ("Else", _ONE), ("span", _IGNORED),
    ),
    "ElseIfClause": (("Condition", _ONE), ("Body", _ONE), ("span", _IGNORED)),
    "ElseClause": (("Body", _ONE), ("span", _IGNORED)),
    "WhileStatement": (("Condition", _ONE), ("Body", _ONE), ("span", _IGNORED)),
    "ForEachStatement": (
        ("Variable", _HOT), ("Iterable", _HOT), ("Body", _HOT),
        ("span", _IGNORED),
    ),
    "ForRangeStatement": (
        ("Variable", _HOT), ("Start", _HOT), ("End", _HOT),
        ("Body", _HOT), ("span", _IGNORED),
    ),
    "TryStatement": (("TryBody", _ONE), ("ExceptBody", _ONE), ("span", _IGNORED)),
    "ReturnStatement": (("Value", _ONE), ("span", _IGNORED)),
    "RaiseStatement": (("Parameters", _ONE), ("span", _IGNORED)),
    "RaiseCallParameters": (
        ("Arguments", _ONE), ("Postfix", _POSTFIX), ("Tail", _ONE),
        ("span", _IGNORED),
    ),
    "ContinuedExpression": (
        ("PrefixOperators", _IGNORED), ("Primary", _ONE),
        ("Tail", _ONE), ("span", _IGNORED),
    ),
    "PostfixedPrimary": (("Base", _ONE), ("Postfix", _POSTFIX), ("span", _IGNORED)),
    "ExpressionTail": (
        ("MultiplicativeOperators", _IGNORED),
        ("MultiplicativeOperands", _MANY),
        ("AdditiveOperators", _IGNORED), ("AdditiveOperands", _MANY),
        ("ComparisonOperators", _IGNORED), ("ComparisonOperands", _MANY),
        ("AndOperands", _MANY), ("OrOperands", _MANY),
        ("span", _IGNORED),
    ),
    "ExecuteStatement": (("Value", _ONE), ("span", _IGNORED)),
    "AddHandlerStatement": (("Event", _ONE), ("Handler", _ONE), ("span", _IGNORED)),
    "RemoveHandlerStatement": (("Event", _ONE), ("Handler", _ONE), ("span", _IGNORED)),
    "AwaitStatement": (("Value", _ONE), ("span", _IGNORED)),
    "SimpleStatement": (("Target", _HOT), ("Value", _HOT), ("span", _IGNORED)),
    "OrExpression": (("First", _HOT), ("Operands", _HOT), ("span", _IGNORED)),
    "AndExpression": (("First", _HOT), ("Operands", _HOT), ("span", _IGNORED)),
    "ComparisonExpression": (
        ("First", _HOT), ("Operators", _IGNORED), ("Operands", _HOT),
        ("span", _IGNORED),
    ),
    "AdditiveExpression": (
        ("First", _HOT), ("Operators", _IGNORED), ("Operands", _HOT),
        ("span", _IGNORED),
    ),
    "MultiplicativeExpression": (
        ("First", _HOT), ("Operators", _IGNORED), ("Operands", _HOT),
        ("span", _IGNORED),
    ),
    "UnaryExpression": (("Operators", _IGNORED), ("Value", _HOT), ("span", _IGNORED)),
    "ParenthesizedExpression": (("Value", _ONE), ("Postfix", _POSTFIX), ("span", _IGNORED)),
    "AccessChain": (("Root", _HOT), ("Arguments", _HOT), ("Postfix", _HOT), ("span", _IGNORED)),
    "MemberAccess": (("Name", _IGNORED), ("Arguments", _ONE), ("span", _IGNORED)),
    "IndexAccess": (("Index", _ONE), ("span", _IGNORED)),
    "CallArguments": (("Items", _ONE), ("span", _IGNORED)),
    "ArgumentList": (("Items", _MANY), ("span", _IGNORED)),
    "OmittedArgument": (("span", _IGNORED),),
    "NewExpression": (("Type", _IGNORED), ("Arguments", _ONE), ("span", _IGNORED)),
    "TernaryExpression": (("Condition", _ONE), ("Then", _ONE), ("Else", _ONE), ("span", _IGNORED)),
    "ConstantLiteral": (("Value", _IGNORED), ("span", _IGNORED)),
    "RegionDirective": (("Name", _IGNORED), ("span", _IGNORED)),
    "EndRegionDirective": (("span", _IGNORED),),
    "PreprocessorIfDirective": (("Condition", _ONE), ("span", _IGNORED)),
    "PreprocessorElseIfDirective": (("Condition", _ONE), ("span", _IGNORED)),
    "PreprocessorElseDirective": (("span", _IGNORED),),
    "PreprocessorEndIfDirective": (("span", _IGNORED),),
    "UseDirective": (("Library", _ONE), ("span", _IGNORED)),
    "NativeDirective": (("span", _IGNORED),),
    "StackDirective": (("span", _IGNORED),),
    "UsedLibrary": (("Value", _IGNORED), ("span", _IGNORED)),
    "PreprocessorOr": (("First", _ONE), ("Operands", _MANY), ("span", _IGNORED)),
    "PreprocessorAnd": (("First", _ONE), ("Operands", _MANY), ("span", _IGNORED)),
    "PreprocessorNot": (("Value", _ONE), ("span", _IGNORED)),
    "PreprocessorIdentifier": (("Name", _IGNORED), ("span", _IGNORED)),
}


def _validate_node_field_schema(
    ast_classes: Mapping[str, type[object]],
) -> None:
    actual_types = frozenset(ast_classes)
    classified_types = frozenset(_NODE_FIELD_SCHEMA)
    if actual_types != classified_types:
        raise ValueError(
            "generated AST type schema mismatch: "
            f"generated={sorted(actual_types)!r}, "
            f"classified={sorted(classified_types)!r}"
        )
    valid_kinds = {_IGNORED, _ONE, _MANY, _POSTFIX, _HOT, _CARRIER}
    for node_name, node_type in ast_classes.items():
        actual_fields = tuple(field.name for field in fields(node_type))
        classified = _NODE_FIELD_SCHEMA[node_name]
        classified_fields = tuple(field_name for field_name, _kind in classified)
        if actual_fields != classified_fields:
            raise ValueError(
                f"generated AST field schema mismatch for {node_name}: "
                f"generated={actual_fields!r}, classified={classified_fields!r}"
            )
        if any(kind not in valid_kinds for _field_name, kind in classified):
            raise ValueError(f"invalid AST field classification for {node_name}")


_validate_node_field_schema(generated.AST_CLASSES)
_TYPED_CHILD_EDGES: dict[type[object], tuple[tuple[str, int], ...]] = {
    generated.AST_CLASSES[node_name]: tuple(
        (field_name, kind)
        for field_name, kind in field_schema
        if kind in {_ONE, _MANY, _POSTFIX}
    )
    for node_name, field_schema in _NODE_FIELD_SCHEMA.items()
}


@dataclass(frozen=True, slots=True)
class _ScopeFacts:
    declared_names: tuple[str, ...]
    bare_names: tuple[BareName, ...]


def _linked_items(value: object) -> tuple[object, ...]:
    """Flatten generated linked sequence nodes without recursive Python calls."""
    result: list[object] = []
    current = value
    while isinstance(current, generated.ModuleElements):
        if current.Item is not None:
            result.append(current.Item)
        current = current.Rest
    if current is not None:
        if isinstance(current, tuple):
            result.extend(current)
        else:
            result.append(current)
    return tuple(result)


def _collect_scope_facts(roots: Iterable[object]) -> _ScopeFacts:
    declared_names: set[str] = set()
    # normalized -> [first span start, source-order tie-breaker, spelling, flags]
    names: dict[str, list[object]] = {}
    sequence = 0
    read_kind = int(BareNameKind.READ)
    write_kind = int(BareNameKind.BARE_WRITE)
    stack = list(reversed(tuple(roots)))

    while stack:
        value = stack.pop()
        value_type = type(value)

        # These six precedence wrappers are over 60% of a real ZUP AST. Keep
        # their typed edges at the front of the dispatch chain.
        if (
            value_type is generated.OrExpression
            or value_type is generated.AndExpression
            or value_type is generated.ComparisonExpression
            or value_type is generated.AdditiveExpression
            or value_type is generated.MultiplicativeExpression
        ):
            if value.Operands:
                stack.extend(reversed(value.Operands))
            if value.First is not None:
                stack.append(value.First)
            continue
        if value_type is generated.UnaryExpression:
            if value.Value is not None:
                stack.append(value.Value)
            continue
        if (
            value_type is generated.SimpleStatement
            or value_type is generated.AccessChain
        ):
            if value_type is generated.SimpleStatement:
                target = value.Target
                if type(target) is not generated.AccessChain:
                    raise TypeError(
                        "generated SimpleStatement has an unsupported target"
                    )
                if value.Value is None:
                    has_call = target.Arguments is not None or any(
                        type(marker) is generated.MemberAccess
                        and marker.Arguments is not None
                        for marker in target.Postfix
                    )
                    if not has_call:
                        raise BslParseError(
                            "bare access-chain statement is forbidden at "
                            f"{target.span.start}",
                            span=target.span,
                            code="bare_access_chain_statement",
                        )
                else:
                    stack.append(value.Value)
                direct_write = bool(
                    value.Value is not None
                    and target.Root
                    and target.Arguments is None
                    and not target.Postfix
                )
            else:
                target = value
                direct_write = False

            name = str(target.Root)
            normalized = name.casefold()
            fact_kind = write_kind if direct_write else read_kind
            fact = names.get(normalized)
            if fact is None:
                names[normalized] = [
                    target.span.start, sequence, name, fact_kind,
                ]
            else:
                fact[3] = int(fact[3]) | fact_kind
                if (target.span.start, sequence) < (int(fact[0]), int(fact[1])):
                    fact[0] = target.span.start
                    fact[1] = sequence
                    fact[2] = name
            sequence += 1

            if not direct_write:
                for marker in reversed(target.Postfix):
                    if type(marker) is generated.MemberAccess:
                        if marker.Arguments is not None:
                            stack.append(marker.Arguments)
                    elif type(marker) is generated.IndexAccess:
                        stack.append(marker.Index)
                    else:
                        raise TypeError(
                            "generated AccessChain has an unsupported postfix"
                        )
                if target.Arguments is not None:
                    stack.append(target.Arguments)
            continue

        if value_type is generated.Parameter:
            declared_names.add(str(value.Name).casefold())
            if value.Default is not None:
                stack.append(value.Default)
            if value.Decorations:
                stack.extend(reversed(value.Decorations))
        elif value_type is generated.LocalVariableDeclaration:
            declared_names.update(str(name).casefold() for name in value.Names)
        elif value_type is generated.ForEachStatement:
            declared_names.add(str(value.Variable).casefold())
            if value.Body is not None:
                stack.append(value.Body)
            if value.Iterable is not None:
                stack.append(value.Iterable)
        elif value_type is generated.ForRangeStatement:
            declared_names.add(str(value.Variable).casefold())
            if value.Body is not None:
                stack.append(value.Body)
            if value.End is not None:
                stack.append(value.End)
            if value.Start is not None:
                stack.append(value.Start)
        elif value_type is generated.CodeBlock:
            items: list[object] = []
            current: object = value
            while isinstance(current, generated.CodeBlock):
                if current.First is not None:
                    items.append(current.First)
                current = current.Rest
            if current is not None:
                items.append(current)
            if items:
                stack.extend(reversed(items))
        elif value_type is generated.ModuleElements:
            items = list(_linked_items(value))
            if items:
                stack.extend(reversed(items))
        else:
            child_edges = _TYPED_CHILD_EDGES.get(value_type)
            if child_edges is None:
                raise TypeError(
                    f"unclassified generated AST node: {value_type.__name__}"
                )
            for field_name, edge_kind in reversed(child_edges):
                child = getattr(value, field_name)
                if child is None:
                    continue
                if edge_kind == _ONE:
                    stack.append(child)
                elif edge_kind == _MANY:
                    if child:
                        stack.extend(reversed(child))
                else:
                    for marker in reversed(child):
                        if type(marker) is generated.MemberAccess:
                            if marker.Arguments is not None:
                                stack.append(marker.Arguments)
                        elif type(marker) is generated.IndexAccess:
                            stack.append(marker.Index)
                        else:
                            raise TypeError(
                                "generated expression has an unsupported postfix"
                            )

    ordered_names = sorted(names.items(), key=lambda item: (item[1][0], item[1][1]))
    result = tuple(
        BareName(str(fact[2]), normalized, BareNameKind(int(fact[3])))
        for normalized, fact in ordered_names
    )
    return _ScopeFacts(tuple(sorted(declared_names)), tuple(result))


def _initializer_offset(
    body: generated.MethodBody,
    tokens: tuple[Token, ...],
    token_starts: tuple[int, ...],
) -> int:
    declarations = body.LocalDeclarations
    if not declarations:
        return body.span.start
    declaration_end = declarations[-1].span.end
    token_index = bisect_left(token_starts, declaration_end)
    if token_index >= len(tokens) or tokens[token_index].type != ";":
        raise BslParseError(
            f"local declaration terminator is missing at {declaration_end}",
            span=SourceSpan(declaration_end, declaration_end),
            code="projection_invariant",
        )
    return tokens[token_index].end


def _project_method(
    method: generated.Method,
    tokens: tuple[Token, ...],
    token_starts: tuple[int, ...],
) -> ParsedMethodModel:
    declaration = method.Declaration
    if not isinstance(
        declaration,
        (generated.ProcedureDeclaration, generated.FunctionDeclaration),
    ):
        raise TypeError("generated Method has an unsupported declaration")
    body = declaration.Body
    if not isinstance(body, generated.MethodBody):
        raise TypeError("generated method declaration has an unsupported body")
    name = str(declaration.Name)
    scope = _collect_scope_facts((declaration,))
    return ParsedMethodModel(
        name=name,
        normalized_name=name.casefold(),
        exported=declaration.Export is not None,
        declaration_span=method.span,
        alias_declaration_offset=body.span.start,
        alias_initializer_offset=_initializer_offset(body, tokens, token_starts),
        declared_names=scope.declared_names,
        bare_names=scope.bare_names,
    )


def project_full_ast_module(
    source: str,
    root: object,
    tokens: tuple[Token, ...],
    *,
    parser_identity: tuple[str, str],
) -> ParsedModuleModel:
    """Build the exact immutable Worker model without retaining AST or tokens."""
    if not isinstance(root, generated.Module):
        raise TypeError("root must be a generated Module")
    if type(tokens) is not tuple:
        raise TypeError("tokens must be an immutable tuple")

    module_variables = {
        str(variable.Name).casefold()
        for declaration in root.Declarations
        for variable in declaration.Variables
    }
    module_roots: list[object] = []
    methods: list[generated.Method] = []
    for item in _linked_items(root.Elements):
        if isinstance(item, generated.Method):
            methods.append(item)
        else:
            module_roots.append(item)

    token_starts = tuple(token.start for token in tokens)
    module_scope = _collect_scope_facts(module_roots)
    module_variables.update(module_scope.declared_names)
    return ParsedModuleModel(
        source_sha256=source_sha256(source),
        module_variables=tuple(sorted(module_variables)),
        module_bare_names=module_scope.bare_names,
        methods=tuple(
            _project_method(method, tokens, token_starts) for method in methods
        ),
        parser_identity=parser_identity,
    )


def _target_parser_identity(target: PythonParserTarget) -> tuple[str, str]:
    package_sha256 = target.metadata.parsergen_package_sha256
    if package_sha256 is None:
        raise RuntimeError("full AST parser package identity is unavailable")
    return target.metadata.parser_identity_sha256, package_sha256


def full_ast_parser_identity() -> tuple[str, str]:
    """Return provenance embedded in the committed full-AST parser artifact."""
    return _target_parser_identity(PythonParserTarget.from_generated())


def parse_full_ast_module(
    source: str,
    *,
    profiler: PhaseRecorder | None = None,
) -> ParsedModuleModel:
    """Tokenize and parse once, then retain only the hot-reload module model."""
    tokens = tuple(tokenize(source))
    effective = select_server_effective_tokens(source, tokens)
    target = PythonParserTarget.from_generated()
    parser_identity = _target_parser_identity(target)

    def parse_root() -> object:
        return target.parse_tokens_ast(effective, "Модуль")

    root = (
        parse_root()
        if profiler is None
        else profiler.measure_parser("full_module_parses", parse_root)
    )

    def extract() -> ParsedModuleModel:
        return project_full_ast_module(
            source,
            root,
            effective,
            parser_identity=parser_identity,
        )

    return extract() if profiler is None else profiler.measure("ast_model_extract", extract)


def parse_full_ast_projected_module(
    source: str,
    *,
    profiler: PhaseRecorder | None = None,
) -> ParsedModuleModel:
    """Compatibility name for benchmarks that compare full AST with Worker."""
    return parse_full_ast_module(source, profiler=profiler)


__all__ = [
    "full_ast_parser_identity",
    "parse_full_ast_module",
    "parse_full_ast_projected_module",
    "project_full_ast_module",
]
