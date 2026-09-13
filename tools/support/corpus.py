from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from xml.etree import ElementTree


EXCLUSION_MARKERS = (
    "регламентирован*отчет*",
    "реглотчет",
    "regulatedreport",
    "regulatoryreport",
)


@dataclass(frozen=True, slots=True)
class ModuleSample:
    total_modules: int
    excluded_by_subsystem: int
    excluded_by_path: int
    excluded_by_content: int
    eligible_modules: int
    target_modules: int
    selected: tuple[str, ...]
    exclusion_markers: tuple[str, ...] = EXCLUSION_MARKERS


_METADATA_DIRECTORIES = {
    "AccountingRegister": "AccountingRegisters",
    "AccumulationRegister": "AccumulationRegisters",
    "BusinessProcess": "BusinessProcesses",
    "Catalog": "Catalogs",
    "CalculationRegister": "CalculationRegisters",
    "ChartOfAccounts": "ChartsOfAccounts",
    "ChartOfCalculationTypes": "ChartsOfCalculationTypes",
    "ChartOfCharacteristicTypes": "ChartsOfCharacteristicTypes",
    "CommonCommand": "CommonCommands",
    "CommonForm": "CommonForms",
    "CommonModule": "CommonModules",
    "CommonPicture": "CommonPictures",
    "CommonTemplate": "CommonTemplates",
    "Constant": "Constants",
    "DataProcessor": "DataProcessors",
    "DefinedType": "DefinedTypes",
    "Document": "Documents",
    "DocumentJournal": "DocumentJournals",
    "Enum": "Enums",
    "EventSubscription": "EventSubscriptions",
    "ExchangePlan": "ExchangePlans",
    "FilterCriterion": "FilterCriteria",
    "FunctionalOption": "FunctionalOptions",
    "InformationRegister": "InformationRegisters",
    "IntegrationService": "IntegrationServices",
    "Report": "Reports",
    "Role": "Roles",
    "ScheduledJob": "ScheduledJobs",
    "Sequence": "Sequences",
    "SessionParameter": "SessionParameters",
    "SettingsStorage": "SettingsStorages",
    "StyleItem": "StyleItems",
    "Task": "Tasks",
    "WebService": "WebServices",
}


def _normalized(value: str) -> str:
    return "".join(
        character
        for character in value.casefold().replace("ё", "е")
        if character.isalnum()
    )


def relates_to_regulated_reporting(value: str) -> bool:
    normalized = _normalized(value)
    regulated = normalized.find("регламентирован")
    report = normalized.find("отчет", regulated + 1, regulated + 96)
    return (
        regulated >= 0
        and report >= 0
        or "реглотчет" in normalized
        or "regulatedreport" in normalized
        or "regulatoryreport" in normalized
    )


def _is_excluded_subsystem(relative: str) -> bool:
    normalized = _normalized(relative)
    return (
        relates_to_regulated_reporting(relative)
        or normalized.startswith("отчетность")
        or "персонифицированныйучет" in normalized
        or "контролирующимиорганами" in normalized
    )


def _regulated_subsystem_prefixes(root: Path) -> set[str]:
    subsystems = root / "Subsystems"
    if not subsystems.is_dir():
        return set()
    roots = [
        path
        for path in subsystems.rglob("*.xml")
        if _is_excluded_subsystem(path.relative_to(subsystems).as_posix())
    ]
    metadata_files: set[Path] = set(roots)
    for path in roots:
        children = path.with_suffix("")
        if children.is_dir():
            metadata_files.update(children.rglob("*.xml"))

    prefixes: set[str] = set()
    for path in metadata_files:
        for item in ElementTree.parse(path).getroot().iter():
            reference = (item.text or "").strip()
            if "." not in reference:
                continue
            object_type, name = reference.split(".", 1)
            directory = _METADATA_DIRECTORIES.get(object_type)
            if directory is not None:
                prefixes.add(f"{directory}/{name}/".casefold())
    return prefixes


def select_module_sample(root: Path, *, fraction: float = 0.10) -> ModuleSample:
    if not 0 < fraction <= 1:
        raise ValueError("fraction must be greater than zero and at most one")

    modules = sorted(path for path in root.rglob("*.bsl") if path.is_file())
    subsystem_prefixes = _regulated_subsystem_prefixes(root)
    candidates: list[str] = []
    excluded_by_subsystem = 0
    excluded_by_path = 0
    for path in modules:
        relative = path.relative_to(root).as_posix()
        if any(relative.casefold().startswith(prefix) for prefix in subsystem_prefixes):
            excluded_by_subsystem += 1
            continue
        if relates_to_regulated_reporting(relative):
            excluded_by_path += 1
            continue
        candidates.append(relative)

    ranked = sorted(
        candidates,
        key=lambda relative: (sha256(relative.encode("utf-8")).digest(), relative),
    )
    requested = max(1, round(len(ranked) * fraction)) if ranked else 0
    selected: list[str] = []
    excluded_by_content = 0
    for relative in ranked:
        source = (root / Path(relative)).read_text(
            encoding="utf-8-sig", errors="replace"
        )
        if relates_to_regulated_reporting(source):
            excluded_by_content += 1
            continue
        selected.append(relative)
        if len(selected) == requested:
            break
    return ModuleSample(
        total_modules=len(modules),
        excluded_by_subsystem=excluded_by_subsystem,
        excluded_by_path=excluded_by_path,
        excluded_by_content=excluded_by_content,
        eligible_modules=len(candidates) - excluded_by_content,
        target_modules=len(selected),
        selected=tuple(selected),
    )
