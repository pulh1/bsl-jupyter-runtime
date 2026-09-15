from __future__ import annotations

from hashlib import sha256
from pathlib import Path

import pytest

from onec_runtime.capture_source import (
    CapturePointRequest as CoreCapturePointRequest,
    CommonModuleCaptureResolver as CoreCommonModuleCaptureResolver,
)
from onec_runtime_mcp.agent.capture_contracts import CapturePointRequest
from onec_runtime_mcp.agent.source_capture import CommonModuleCaptureResolver
from onec_runtime.errors import ProtocolError


MODULE_UUID = "11111111-2222-3333-4444-555555555555"


def test_mcp_capture_imports_reexport_core_owned_types() -> None:
    assert CapturePointRequest is CoreCapturePointRequest
    assert CommonModuleCaptureResolver is CoreCommonModuleCaptureResolver


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    root = tmp_path / "ut"
    module = root / "CommonModules" / "Продажи"
    module.mkdir(parents=True)
    (module / "Продажи.mdo").write_text(
        f'<mdclass:CommonModule xmlns:mdclass="urn:test" uuid="{MODULE_UUID}"/>',
        encoding="utf-8",
    )
    source = """Процедура ПровестиДокумент() Экспорт
    ПодготовитьДанные();
    ЗаписатьДвижения();
КонецПроцедуры

Процедура Другая()
    Сообщить("готово");
КонецПроцедуры
"""
    source_path = module / "Module.bsl"
    source_path.write_text(source, encoding="utf-8")
    return root, source_path


def test_common_module_resolver_maps_explicit_ut_location(tmp_path: Path) -> None:
    source_root, source_path = _source_tree(tmp_path)
    resolver = CommonModuleCaptureResolver("ut", source_root)
    request = CapturePointRequest(
        name="before_posting",
        project="ut",
        module="Продажи",
        procedure="ПровестиДокумент",
        line=3,
    )

    binding, = resolver.resolve((request,))

    assert binding.point.project == "ut"
    assert binding.point.module == "Продажи"
    assert binding.point.line == 3
    assert binding.point.executable_line == 3
    assert binding.point.excerpt == "ЗаписатьДвижения();"
    assert binding.point.source_sha256 == sha256(source_path.read_bytes()).hexdigest()
    assert binding.location.line == 3
    assert str(binding.location.object_id) == MODULE_UUID


def test_common_module_resolver_qualifies_designer_extension_location(
    tmp_path: Path,
) -> None:
    source_root, source_path = _source_tree(tmp_path)
    # This fixture is a Designer export, including its module paths.
    designer_source = source_path.parent / "Ext" / "Module.bsl"
    designer_source.parent.mkdir()
    designer_source.write_bytes(source_path.read_bytes())
    (source_root / "CommonModules" / "Продажи.xml").write_text(
        f'<MetaDataObject><CommonModule uuid="{MODULE_UUID}">'
        '<Properties><Name>Продажи</Name></Properties>'
        '</CommonModule></MetaDataObject>', encoding="utf-8",
    )
    (source_root / "Configuration.xml").write_text(
        """<MetaDataObject xmlns="http://v8.1c.ru/8.3/MDClasses">
  <Configuration uuid="aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee">
    <Properties>
      <ObjectBelonging>Adopted</ObjectBelonging>
      <Name>JupyterBslTestFixture</Name>
      <ConfigurationExtensionPurpose>AddOn</ConfigurationExtensionPurpose>
    </Properties>
  </Configuration>
</MetaDataObject>
""",
        encoding="utf-8",
    )
    resolver = CommonModuleCaptureResolver("ut", source_root)

    binding, = resolver.resolve((CapturePointRequest(
        "point", "ut", "Продажи", "ПровестиДокумент", 3
    ),))

    assert binding.location.module_type == "ExtensionModule"
    assert binding.location.extension_name == "JupyterBslTestFixture"


def test_common_module_resolver_rejects_wrong_project(tmp_path: Path) -> None:
    source_root, _ = _source_tree(tmp_path)
    resolver = CommonModuleCaptureResolver("ut", source_root)

    with pytest.raises(ValueError, match="project"):
        resolver.resolve((CapturePointRequest("point", "erp", "Продажи", "ПровестиДокумент", 3),))


def test_common_module_resolver_rejects_line_outside_procedure(tmp_path: Path) -> None:
    source_root, _ = _source_tree(tmp_path)
    resolver = CommonModuleCaptureResolver("ut", source_root)

    with pytest.raises(ValueError, match="procedure"):
        resolver.resolve((CapturePointRequest("point", "ut", "Продажи", "ПровестиДокумент", 7),))


def test_common_module_resolver_rejects_ambiguous_fragment(tmp_path: Path) -> None:
    source_root, source_path = _source_tree(tmp_path)
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace(
            "    ПодготовитьДанные();", "    ЗаписатьДвижения();"
        ),
        encoding="utf-8",
    )
    resolver = CommonModuleCaptureResolver("ut", source_root)

    with pytest.raises(ValueError, match="exactly one"):
        resolver.resolve((CapturePointRequest("point", source_fragment="ЗаписатьДвижения();"),))


def test_common_module_resolver_detects_source_hash_drift(tmp_path: Path) -> None:
    source_root, source_path = _source_tree(tmp_path)
    resolver = CommonModuleCaptureResolver("ut", source_root)
    bindings = resolver.resolve(
        (CapturePointRequest("point", "ut", "Продажи", "ПровестиДокумент", 3),)
    )
    source_path.write_text(
        source_path.read_text(encoding="utf-8") + "// changed\n",
        encoding="utf-8",
    )

    with pytest.raises(ProtocolError, match="changed"):
        resolver.verify(bindings)
