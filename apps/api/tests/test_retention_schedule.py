import asyncio

import pytest

from app.artifact_retention_worker import parser, schedule

GOOD = {"checkpoint_saved": True, "batch": {"counts": {}}}


async def test_once_executes_exactly_one_batch_without_waiting():
    emitted = []
    async def tick():
        return GOOD
    result = await asyncio.wait_for(schedule(tick, interval_seconds=300, once=True,
        stop=asyncio.Event(), emit=emitted.append), 1)
    assert result == 0 and emitted == [GOOD]


@pytest.mark.parametrize("bad", [
    {"checkpoint_saved": False, "batch": {"counts": {}}},
    {"checkpoint_saved": False, "batch": {"counts": {"failed": 1}}},
])
async def test_failed_or_contended_checkpoint_stops_without_scheduling_more_work(bad):
    emitted = []
    async def tick():
        return bad
    result = await schedule(tick, interval_seconds=300, once=False,
                            stop=asyncio.Event(), emit=emitted.append)
    assert result == 2 and emitted == [bad]


async def test_stop_joins_an_inflight_batch_before_returning():
    stop, entered, released = asyncio.Event(), asyncio.Event(), asyncio.Event()
    emitted = []
    async def tick():
        entered.set()
        try:
            await asyncio.Event().wait()
        finally:
            await asyncio.sleep(0)
            released.set()
    task = asyncio.create_task(schedule(tick, interval_seconds=300, once=False,
        stop=stop, emit=emitted.append))
    await asyncio.wait_for(entered.wait(), 1)
    stop.set()
    assert await asyncio.wait_for(task, 1) == 0
    assert released.is_set() and not emitted


async def test_stop_interrupts_the_interval_without_an_extra_batch():
    stop = asyncio.Event()
    emitted = []
    async def tick():
        return GOOD
    def emit(result):
        emitted.append(result)
        stop.set()
    assert await schedule(tick, interval_seconds=86400, once=False, stop=stop, emit=emit) == 0
    assert emitted == [GOOD]


@pytest.mark.parametrize("interval", [0, -1, 86401, True, 1.5])
async def test_invalid_interval_never_starts_a_batch(interval):
    async def tick():
        pytest.fail("invalid schedule started work")
    with pytest.raises(ValueError):
        await schedule(tick, interval_seconds=interval, once=True,
                       stop=asyncio.Event(), emit=lambda result: None)


def test_defaults_require_policy_and_never_enable_deletion():
    arguments = parser().parse_args(["--retain-days", "30"])
    assert arguments.mode == "dry-run" and arguments.interval_seconds == 300
    assert arguments.limit == 100 and not arguments.once
    with pytest.raises(SystemExit) as missing:
        parser().parse_args([])
    assert missing.value.code == 2
