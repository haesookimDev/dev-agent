import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from ci_partition import partition_for, pytest_collection_modifyitems


def partition_items(root, items, partition):
    removed = []
    config = SimpleNamespace(
        rootpath=root, getoption=lambda name: partition,
        hook=SimpleNamespace(pytest_deselected=lambda *, items: removed.extend(items)),
    )
    retained = list(items)
    pytest_collection_modifyitems(config, retained)
    return retained, removed


def test_default_collection_is_unchanged(tmp_path):
    items = [SimpleNamespace(path=tmp_path / f"test_{number}.py") for number in range(100)]
    retained, removed = partition_items(tmp_path, items, None)
    assert retained == items
    assert removed == []


def test_two_partitions_cover_every_case_once_and_keep_files_together(tmp_path):
    items = [SimpleNamespace(path=tmp_path / f"test_{number}.py", case=case)
             for number in range(100) for case in range(3)]
    first, second = [partition_items(tmp_path, items, number)[0] for number in (1, 2)]
    assert first and second
    assert {id(item) for item in first}.isdisjoint(id(item) for item in second)
    assert {id(item) for item in first + second} == {id(item) for item in items}
    for retained in (first, second):
        for path in {item.path for item in retained}:
            assert [item.case for item in retained if item.path == path] == [0, 1, 2]
    for number, retained in ((1, first), (2, second)):
        assert retained == [item for item in items if item in retained]
        assert partition_items(tmp_path, items, number)[1] == [
            item for item in items if item not in retained]


def test_checkout_location_and_collection_order_do_not_change_membership(tmp_path):
    names = [f"tests/nested/test_{number}.py" for number in range(100)]
    memberships = []
    for root, paths in ((tmp_path / "local", names), (tmp_path / "ci", names[::-1])):
        items = [SimpleNamespace(path=root / name) for name in paths]
        memberships.append({item.path.relative_to(root).as_posix()
                            for item in partition_items(root, items, 1)[0]})
    assert memberships[0] == memberships[1]


@pytest.fixture
def isolated_pytest(tmp_path):
    (tmp_path / "conftest.py").write_text(
        "from ci_partition import pytest_addoption, pytest_collection_modifyitems\n")
    for partition in (1, 2):
        name = next(f"test_{number}.py" for number in range(100)
                    if partition_for(f"test_{number}.py") == partition)
        (tmp_path / name).write_text(
            "import pytest\n@pytest.mark.parametrize('case', [1, 2])\n"
            f"def test_case(case):\n    assert {partition} == 1\n")
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).parent),
                       PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")
    environment.pop("PYTEST_ADDOPTS", None)

    def run(*arguments):
        return subprocess.run(
            [sys.executable, "-m", "pytest", "-q", "--no-header", *arguments],
            cwd=tmp_path, env=environment, capture_output=True, text=True, timeout=20,
        )

    return run


def test_actual_pytest_retains_failure_exit_code_and_complete_default(isolated_pytest):
    complete = isolated_pytest()
    passing = isolated_pytest("--api-partition=1")
    failing = isolated_pytest("--api-partition=2")
    assert complete.returncode == failing.returncode == 1
    assert passing.returncode == 0
    assert "2 failed, 2 passed" in complete.stdout
    assert "2 passed, 2 deselected" in passing.stdout
    assert "2 failed, 2 deselected" in failing.stdout


@pytest.mark.parametrize("value", ["0", "3", "1/2", "", "1.5"])
def test_actual_pytest_rejects_invalid_partition(isolated_pytest, value):
    result = isolated_pytest(f"--api-partition={value}")
    assert result.returncode == 4
    assert "error:" in result.stderr
    assert "test_case" not in result.stdout
