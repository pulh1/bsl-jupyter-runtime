from onec_runtime.experiment import KernelExperiment, bsl_string_literal


def test_integer_presentation_accepts_locale_group_separator() -> None:
    assert KernelExperiment._int("1,000") == 1000
    assert KernelExperiment._int("10 000") == 10000


def test_bsl_string_literal_escapes_quotes_and_newlines() -> None:
    assert bsl_string_literal('А = "Б";\nВ = 1;') == (
        '"А = ""Б"";" + Символы.ПС + "В = 1;"'
    )
