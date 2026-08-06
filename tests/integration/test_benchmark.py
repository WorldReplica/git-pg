from __future__ import annotations

import os

import pytest

from git_pg.benchmark import get_preset, run_session_lifecycle_benchmark


@pytest.mark.integration
@pytest.mark.benchmark
async def test_benchmark_session_lifecycle(engine) -> None:
    if os.environ.get("BENCHMARK") != "1":
        pytest.skip("set BENCHMARK=1 to run benchmarks")

    preset_id = os.environ.get("BENCHMARK_PRESET", "requests-shallow")
    preset = get_preset(preset_id)
    repo_name = os.environ.get("BENCHMARK_REPO")

    await run_session_lifecycle_benchmark(
        engine,
        preset,
        repo_name=repo_name,
        log=True,
    )
