from onec_runtime import bsl


def test_public_bsl_namespace_exposes_structured_diagnostics() -> None:
    assert bsl.DiagnosticStage.PARSING.value == "parsing"
    assert bsl.MappingConfidence.UNKNOWN.value == "unknown"


def test_removes_hash_and_ampersand_lines_and_preserves_line_count() -> None:
    source = (
        "#Область Проверка\n"
        "#Если Сервер И НЕ Клиент Тогда\n"
        "&НаСервере\n"
        "Сторона = \"Сервер\";\n"
        "#ИначеЕсли Клиент Тогда\n"
        "Сторона = \"Клиент\";\n"
        "#Иначе\n"
        "Сторона = \"Другая\";\n"
        "#КонецЕсли\n"
        "#КонецОбласти\n"
        "#Использовать \"ОбщаяБиблиотека\"\n"
    )
    assert hasattr(bsl, "preprocess_server_source")

    effective = bsl.preprocess_server_source(source)

    assert 'Сторона = "Сервер";' in effective
    assert 'Сторона = "Клиент";' in effective
    assert 'Сторона = "Другая";' in effective
    assert not any(
        line.lstrip().startswith(("#", "&")) for line in effective.splitlines()
    )
    assert len(effective.splitlines()) == len(source.splitlines())
