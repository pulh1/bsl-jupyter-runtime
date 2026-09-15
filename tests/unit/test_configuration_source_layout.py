from pathlib import Path
import importlib
import shutil
import pytest
from onec_runtime.errors import ProtocolError

FIXTURES = Path(__file__).parents[1] / "fixtures" / "onec" / "capture_sources"


def layout_api():
    assert importlib.util.find_spec("onec_runtime.configuration_source") is not None, (
        "shared configuration layout is missing"
    )
    return importlib.import_module("onec_runtime.configuration_source")


@pytest.mark.parametrize(
    "name,suffix,expected",
    [
        ("designer_base", "", "designer"),
        ("edt_base", "", "edt"),
        ("edt_base", "src", "edt"),
    ],
)
def test_normalizes_designer_and_edt_roots(name, suffix, expected):
    api = layout_api()
    configured = FIXTURES / name / suffix
    layout = api.ConfigurationSourceLayout(configured)
    binding = layout.bind("demo")
    assert binding.configured_root == configured.resolve()
    assert (
        binding.normalized_root
        == (FIXTURES / name / ("src" if expected == "edt" else "")).resolve()
    )
    assert binding.layout.value == expected
    assert binding.layer.value == "base"


@pytest.mark.parametrize("layout", ["designer", "edt"])
def test_binding_checks_native_layer_and_exact_extension_name(layout):
    api = layout_api()
    base = api.ConfigurationSourceLayout(FIXTURES / f"{layout}_base")
    extension = api.ConfigurationSourceLayout(FIXTURES / f"{layout}_extension")
    assert extension.bind("demo").extension_name == "Дополнение"
    assert (
        extension.bind(
            "demo", layer="extension", extension_name="Дополнение"
        ).layer.value
        == "extension"
    )
    assert base.bind("demo", layer="base").extension_name is None
    for tree, layer, name in [
        (base, "extension", "Дополнение"),
        (extension, "base", None),
        (extension, "extension", "дополнение"),
        (extension, "extension", None),
        (base, "auto", "Дополнение"),
    ]:
        with pytest.raises(ProtocolError):
            tree.bind("demo", layer=layer, extension_name=name)


def test_rejects_ambiguous_direct_and_nested_metadata(tmp_path):
    api = layout_api()
    shutil.copytree(FIXTURES / "designer_base", tmp_path / "project")
    shutil.copytree(FIXTURES / "edt_base" / "src", tmp_path / "project" / "src")
    with pytest.raises(ProtocolError, match="ambiguous"):
        api.ConfigurationSourceLayout(tmp_path / "project")


def test_rejects_link_in_root_ancestry(tmp_path):
    api = layout_api()
    link = tmp_path / "link"
    try:
        link.symlink_to(FIXTURES / "edt_base", target_is_directory=True)
    except OSError as error:
        pytest.skip(str(error))
    with pytest.raises(ProtocolError, match="unsafe"):
        api.ConfigurationSourceLayout(link / "src")


def test_rejects_reparse_metadata_tree_before_scan(tmp_path, monkeypatch):
    api = layout_api()
    shutil.copytree(FIXTURES / "designer_base", tmp_path / "project")
    original = Path.is_junction
    monkeypatch.setattr(
        Path, "is_junction", lambda p: p.name == "Documents" or original(p)
    )
    with pytest.raises(ProtocolError, match="unsafe"):
        api.ConfigurationSourceLayout(tmp_path / "project")
