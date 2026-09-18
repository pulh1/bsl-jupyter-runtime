from __future__ import annotations

from dataclasses import replace
from hashlib import sha256

import pytest

from onec_runtime.bsl.module_catalog import (
    CommonModuleCatalogSnapshot,
    CommonModuleDescriptor,
    CommonModuleScope,
)
from onec_runtime.bsl.source_maps import SourceSpan
from onec_runtime.bsl.worker_dependency_resolver import (
    AMBIGUOUS_BINDING,
    MODULE_SCOPE_DEPENDENCY,
    resolve_worker_dependencies,
    worker_model_candidate_names,
)
from onec_runtime.bsl.worker_projection_model import (
    BareName,
    BareNameKind,
    ParsedMethodModel,
    ParsedModuleModel,
)
from onec_runtime.errors import ModuleUniverseAdmissionError


def _bare(name: str, kinds: BareNameKind = BareNameKind.READ) -> BareName:
    return BareName(name, name.casefold(), kinds)


def _method(
    *bare_names: BareName,
    name: str = "Выполнить",
    declared_names: tuple[str, ...] = (),
) -> ParsedMethodModel:
    return ParsedMethodModel(
        name=name,
        normalized_name=name.casefold(),
        exported=False,
        declaration_span=SourceSpan(10, 40),
        alias_declaration_offset=20,
        alias_initializer_offset=25,
        declared_names=declared_names,
        bare_names=bare_names,
    )


def _model(
    *methods: ParsedMethodModel,
    module_variables: tuple[str, ...] = (),
    module_bare_names: tuple[BareName, ...] = (),
    identity: str = "source-a",
) -> ParsedModuleModel:
    return ParsedModuleModel(
        source_sha256=sha256(identity.encode()).hexdigest(),
        module_variables=module_variables,
        module_bare_names=module_bare_names,
        methods=methods,
        parser_identity=("1" * 64, "2" * 64),
    )


def _snapshot(*names: str, revision: int = 1) -> CommonModuleCatalogSnapshot:
    return CommonModuleCatalogSnapshot.create(
        profile="server",
        preprocessor_profile="server",
        revision=revision,
        modules=(
            CommonModuleDescriptor(name, CommonModuleScope.SERVER) for name in names
        ),
    )


def test_candidate_names_cover_module_and_method_scopes_once() -> None:
    models = (
        _model(
            _method(_bare("МодульБ"), _bare("МодульА")),
            module_bare_names=(_bare("МодульА"),),
        ),
        _model(_method(_bare("МодульВ")), identity="source-b"),
    )

    assert worker_model_candidate_names(models) == (
        "модульа",
        "модульб",
        "модульв",
    )


@pytest.mark.parametrize(
    ("method", "module_variables"),
    [
        (_method(_bare("Каталог"), declared_names=("каталог",)), ()),
        (_method(_bare("КАТАЛОГ")), ("каталог",)),
    ],
)
def test_hard_declaration_shadows_catalog_dependency(method, module_variables):
    plan = resolve_worker_dependencies(
        _model(method, module_variables=module_variables), _snapshot("Каталог")
    )

    assert plan.methods[0].local_names == ("каталог",)
    assert plan.methods[0].dependencies == ()


def test_internal_method_and_platform_global_shadow_catalog_names():
    model = _model(
        _method(_bare("Внутренний"), _bare("Метаданные")),
        _method(name="Внутренний"),
    )

    plan = resolve_worker_dependencies(
        model, _snapshot("Внутренний", "Метаданные")
    )

    assert plan.methods[0].dependencies == ()


def test_document_write_mode_is_platform_global_even_if_catalog_has_same_name():
    model = _model(_method(_bare("РежимЗаписиДокумента")))

    plan = resolve_worker_dependencies(
        model, _snapshot("РежимЗаписиДокумента")
    )

    assert plan.methods[0].dependencies == ()


def test_known_read_is_dependency_in_catalog_order_and_unknown_read_is_ignored():
    model = _model(
        _method(_bare("Бета"), _bare("Неизвестный"), _bare("АЛЬФА"))
    )

    plan = resolve_worker_dependencies(model, _snapshot("Бета", "Альфа"))

    assert plan.methods[0].dependencies == ("Альфа", "Бета")
    assert plan.dependencies == ("Альфа", "Бета")


def test_absent_bare_write_is_implicit_local():
    plan = resolve_worker_dependencies(
        _model(_method(_bare("Локальная", BareNameKind.BARE_WRITE))),
        _snapshot(),
    )

    assert plan.methods[0].implicit_local_names == ("локальная",)
    assert plan.methods[0].local_names == ("локальная",)
    assert plan.methods[0].forbidden_global_writes == ()


@pytest.mark.parametrize(
    "kinds",
    [BareNameKind.BARE_WRITE, BareNameKind.READ | BareNameKind.BARE_WRITE],
)
def test_known_common_module_write_is_forbidden_and_never_aliased(kinds):
    plan = resolve_worker_dependencies(
        _model(_method(_bare("КадровыйУчет", kinds))),
        _snapshot("КадровыйУчет"),
    )

    method = plan.methods[0]
    assert method.forbidden_global_writes == ("КадровыйУчет",)
    assert method.dependencies == ()
    assert method.implicit_local_names == ()


def test_catalog_extension_resolves_saved_read_without_reparse():
    model = _model(_method(_bare("НовыйМодуль")))
    first = resolve_worker_dependencies(model, _snapshot(revision=1))

    second = resolve_worker_dependencies(
        model, _snapshot("НовыйМодуль", revision=2), previous=first
    )

    assert first.dependencies == ()
    assert second.dependencies == ("НовыйМодуль",)
    assert second.source is model


def test_resolved_plan_carries_exact_catalog_snapshot_identity():
    snapshot = _snapshot("КадровыйУчет", revision=7)

    plan = resolve_worker_dependencies(_model(), snapshot)

    assert plan.catalog_identity == (
        snapshot.profile,
        snapshot.preprocessor_profile,
        snapshot.revision,
        snapshot.sha256,
    )


def test_catalog_extension_does_not_steal_frozen_method_implicit_local():
    model = _model(
        _method(
            _bare(
                "БудущийМодуль", BareNameKind.READ | BareNameKind.BARE_WRITE
            )
        )
    )
    first = resolve_worker_dependencies(model, _snapshot(revision=1))

    second = resolve_worker_dependencies(
        model, _snapshot("БудущийМодуль", revision=2), previous=first
    )

    assert second.methods[0].implicit_local_names == ("будущиймодуль",)
    assert second.methods[0].dependencies == ()
    assert second.methods[0].forbidden_global_writes == ()


def test_catalog_extension_does_not_steal_frozen_module_implicit_local():
    model = _model(
        module_bare_names=(
            _bare("БудущийМодуль", BareNameKind.BARE_WRITE),
        )
    )
    first = resolve_worker_dependencies(model, _snapshot(revision=1))

    second = resolve_worker_dependencies(
        model, _snapshot("БудущийМодуль", revision=2), previous=first
    )

    assert second.implicit_local_names == ("будущиймодуль",)
    assert second.forbidden_global_writes == ()


def test_frozen_module_implicit_local_shadows_method_read_after_catalog_growth():
    model = _model(
        _method(_bare("БудущийМодуль")),
        module_bare_names=(
            _bare("БудущийМодуль", BareNameKind.BARE_WRITE),
        ),
    )
    first = resolve_worker_dependencies(model, _snapshot(revision=1))

    second = resolve_worker_dependencies(
        model,
        _snapshot("БудущийМодуль", revision=2),
        previous=first,
    )

    assert first.implicit_local_names == second.implicit_local_names == (
        "будущиймодуль",
    )
    assert first.methods[0].local_names == second.methods[0].local_names == (
        "будущиймодуль",
    )
    assert first.methods[0].dependencies == second.methods[0].dependencies == ()


def test_known_module_scope_write_is_forbidden():
    plan = resolve_worker_dependencies(
        _model(
            module_bare_names=(
                _bare("КадровыйУчет", BareNameKind.BARE_WRITE),
            )
        ),
        _snapshot("КадровыйУчет"),
    )

    assert plan.forbidden_global_writes == ("КадровыйУчет",)


def test_module_scope_dependency_fails_with_stable_code():
    model = _model(module_bare_names=(_bare("КадровыйУчет"),))

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        resolve_worker_dependencies(model, _snapshot("КадровыйУчет"))

    assert caught.value.code == MODULE_SCOPE_DEPENDENCY


def test_previous_plan_must_belong_to_same_source_version():
    previous = resolve_worker_dependencies(_model(identity="old"), _snapshot())

    with pytest.raises(ValueError, match="same source"):
        resolve_worker_dependencies(
            _model(identity="new"), _snapshot(), previous=previous
        )


def test_previous_plan_must_use_the_same_parser_artifact() -> None:
    model = _model()
    previous = resolve_worker_dependencies(model, _snapshot())
    reparsed = replace(model, parser_identity=("3" * 64, "4" * 64))

    with pytest.raises(ValueError, match="same source"):
        resolve_worker_dependencies(reparsed, _snapshot(), previous=previous)


def test_casefold_duplicate_method_declarations_fail_closed():
    model = _model(_method(name="Расчет"), _method(name="РАСЧЕТ"))

    with pytest.raises(ModuleUniverseAdmissionError) as caught:
        resolve_worker_dependencies(model, _snapshot())

    assert caught.value.code == AMBIGUOUS_BINDING
    assert caught.value.span == SourceSpan(10, 40)
