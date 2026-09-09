"""Optional, deterministic two-way partition of the complete pytest collection."""

from hashlib import sha256


def partition_for(relative_path: str) -> int:
    # Keep a file's parametrized tests and module fixtures on the same runner.
    return sha256(relative_path.encode("utf-8")).digest()[0] % 2 + 1


def pytest_addoption(parser):
    parser.getgroup("API CI").addoption(
        "--api-partition", type=int, choices=(1, 2), default=None,
        help="run one of two complete-collection partitions; omitted runs all tests",
    )


def pytest_collection_modifyitems(config, items):
    selected_partition = config.getoption("api_partition")
    if selected_partition is None:
        return
    selected, deselected = [], []
    for item in items:
        relative_path = item.path.relative_to(config.rootpath).as_posix()
        target = selected if partition_for(relative_path) == selected_partition else deselected
        target.append(item)
    items[:] = selected
    config.hook.pytest_deselected(items=deselected)
