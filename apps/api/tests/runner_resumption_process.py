"""Owned lifecycle fixture: real Runner/HTTP/git/checks, scripted agent turns only."""

import asyncio
import json
import sys
from unittest.mock import patch

from kelpie_runner import main


class ScriptedSession:
    def __init__(self, repository, control):
        self.repository, self.control, self.turn = repository, control, 0

    async def start(self):
        pass

    async def run_turn(self, prompt):
        self.turn += 1
        # First turn fails independent checks; every authorized revision fixes the file.
        if self.turn > 1:
            (self.repository / "result.txt").write_text(f"fixed revision {self.turn}\n")
        evidence = self.repository / ".kelpie" / "artifacts"
        evidence.mkdir(parents=True, exist_ok=True)
        (evidence / "revision.json").write_text(json.dumps({"turn": self.turn}))
        await self.control.event("fixture.turn.completed", "Scripted agent fixture turn",
                                 payload={"turn": self.turn, "prompt": prompt})

    async def close(self):
        await self.control.event("fixture.session.closed", "Scripted agent fixture closed")


class ObservedControl(main.ControlClient):
    async def commands(self, after_feedback, after_approval):
        commands = await super().commands(after_feedback, after_approval)
        await self.event("fixture.commands.observed", "Actual HTTP commands observed", payload={
            "status": commands["status"], "after_feedback": after_feedback,
            "after_approval": after_approval,
        })
        return commands


class PollClock:
    def __getattr__(self, name):
        return getattr(asyncio, name)

    async def sleep(self, seconds):
        await asyncio.sleep(min(seconds, 0.05))


if __name__ == "__main__":
    with (patch.object(main, "CodexAppServer", ScriptedSession),
          patch.object(main, "ControlClient", ObservedControl),
          patch.object(main, "asyncio", PollClock() if "--fast-poll" in sys.argv else asyncio)):
        asyncio.run(main.run())
