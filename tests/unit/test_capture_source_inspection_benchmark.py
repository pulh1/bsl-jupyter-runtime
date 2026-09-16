import json

from tools import capture_source_inspection_benchmark as benchmark


def test_large_designer_and_edt_trees_use_batched_resolution_and_parse_cache(
    tmp_path,
) -> None:
    result = benchmark.run_benchmark(tmp_path)

    assert result["schema"] == "onec-capture-source-inspection-benchmark-v1"
    assert result["frame_count"] == 18
    assert [case["case"] for case in result["cases"]] == [
        "designer",
        "edt_project_root",
        "edt_src_root",
    ]
    for case in result["cases"]:
        assert case["methods_resolved"] == 18
        assert case["stack_reads"] == 1
        assert case["fast_stack"] == {
            "directory_scans": 1,
            "metadata_reads": 19,
            "source_reads": 0,
            "parses": 0,
            "cache_hits": 0,
        }
        assert case["first_enrichment"] == {
            "directory_scans": 0,
            "metadata_reads": 0,
            "source_reads": 18,
            "parses": 18,
            "cache_hits": 0,
        }
        assert case["cached_enrichment"] == {
            "directory_scans": 0,
            "metadata_reads": 0,
            "source_reads": 18,
            "parses": 0,
            "cache_hits": 18,
        }


def test_benchmark_json_is_bounded_and_does_not_disclose_generated_paths(
    tmp_path,
) -> None:
    result = benchmark.run_benchmark(tmp_path)
    rendered = json.dumps(result, ensure_ascii=False, sort_keys=True)

    assert len(rendered.encode("utf-8")) < 8_000
    assert str(tmp_path) not in rendered
    assert "Module00" not in rendered


def test_benchmark_cli_emits_the_validated_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        benchmark,
        "run_benchmark",
        lambda _workspace: {
            "schema": benchmark.SCHEMA,
            "frame_count": benchmark.FRAME_COUNT,
            "cases": [],
        },
    )

    assert benchmark.main([]) == 0
    assert json.loads(capsys.readouterr().out) == {
        "schema": benchmark.SCHEMA,
        "frame_count": benchmark.FRAME_COUNT,
        "cases": [],
    }
