"""Explicit control-host scheduler; never starts inside API/VM processes."""

import argparse
import asyncio
import json
import signal
from pathlib import Path

from .artifact_retention_job import run_job, validate_options


async def schedule(run_once, *, interval_seconds: int, once: bool, stop, emit):
    if type(interval_seconds) is not int or not 1 <= interval_seconds <= 86400:
        raise ValueError("interval must be between 1 and 86400 seconds")
    while not stop.is_set():
        running = asyncio.create_task(run_once())
        stopping = asyncio.create_task(stop.wait())
        try:
            done, _ = await asyncio.wait((running, stopping), return_when=asyncio.FIRST_COMPLETED)
            if running not in done:
                return 0
            result = await running
        finally:
            # Join cancellation before closing database pools. No orphan deletion task
            # can continue after this worker reports that it has stopped.
            running.cancel()
            stopping.cancel()
            await asyncio.gather(running, stopping, return_exceptions=True)
        emit(result)
        if not result["checkpoint_saved"] or result["batch"]["counts"].get("failed"):
            return 2
        if once:
            return 0
        try:
            await asyncio.wait_for(stop.wait(), interval_seconds)
        except TimeoutError:
            pass
    return 0


async def execute(arguments):
    validate_options(arguments.retain_days, arguments.mode == "apply",
                     arguments.work_id, arguments.limit)
    if not 1 <= arguments.interval_seconds <= 86400:
        raise ValueError("invalid interval")
    from .config import get_settings
    from .db import SessionLocal, engine, get_schema_readiness

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    signals = (signal.SIGINT, signal.SIGTERM)
    for value in signals:
        loop.add_signal_handler(value, stop.set)

    async def tick():
        if not (await get_schema_readiness()).ready:
            raise ValueError("database schema must match the deployed API")
        return await run_job(SessionLocal, Path(get_settings().artifact_root),
            retain_days=arguments.retain_days, apply=arguments.mode == "apply",
            work_id=arguments.work_id, limit=arguments.limit)

    def emit(result):
        print(json.dumps(result, sort_keys=True), flush=True)

    try:
        return await schedule(tick, interval_seconds=arguments.interval_seconds,
                              once=arguments.once, stop=stop, emit=emit)
    finally:
        for value in signals:
            loop.remove_signal_handler(value)
        await engine.dispose()


def parser():
    result = argparse.ArgumentParser(description="Schedule bounded ordinary-artifact retention. "
        "Stores scan progress in DATABASE_URL; dry-run changes only job progress, not artifacts. "
        "Requires a reviewed retention policy and the same ARTIFACT_ROOT as the API.")
    result.add_argument("--retain-days", type=int, required=True, help="explicit policy, 1..36500")
    result.add_argument("--mode", choices=("dry-run", "apply"), default="dry-run")
    result.add_argument("--work-id", help="restrict the entire scheduled job to one canonical UUID")
    result.add_argument("--limit", type=int, default=100, help="candidates per tick, 1..1000")
    result.add_argument("--interval-seconds", type=int, default=300,
                        help="delay after each completed batch, 1..86400 (default 300)")
    result.add_argument("--once", action="store_true", help="run one resumable batch then exit")
    return result


def main():
    command_parser = parser()
    arguments = command_parser.parse_args()
    try:
        status = asyncio.run(execute(arguments))
    except Exception:
        command_parser.exit(2, "artifact retention worker failed; private details withheld\n")
    if status:
        command_parser.exit(status,
                            "artifact retention batch incomplete; investigate before retry\n")


if __name__ == "__main__":
    main()
