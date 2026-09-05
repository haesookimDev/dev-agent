"""Local administrative CLI. Credential values go only to new mode-0600 files."""

import argparse
import asyncio
import json
import os
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from .models import WorkerCredential, WorkerHost
from .worker_credentials import (
    DEFAULT_LIFETIME_SECONDS,
    aware,
    issue_credential,
    revoke_credential,
    rotate_credential,
)


class CredentialFileError(RuntimeError):
    pass


def write_new_token(path: Path, token: str) -> tuple[int, int]:
    try:
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except OSError:
        raise CredentialFileError("output must be a new file in a writable directory") from None
    identity = None
    try:
        opened = os.fstat(descriptor)
        identity = opened.st_dev, opened.st_ino
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=False) as destination:
            destination.write(token + "\n")
            destination.flush()
            os.fsync(descriptor)
        return identity
    except OSError:
        if identity is not None:
            remove_created_file(path, identity)
        raise CredentialFileError("could not persist credential file") from None
    finally:
        os.close(descriptor)


def remove_created_file(path: Path, identity: tuple[int, int]) -> None:
    try:
        current = path.stat(follow_symlinks=False)
        if (current.st_dev, current.st_ino) == identity:
            path.unlink()
    except FileNotFoundError:
        pass


async def execute(session: AsyncSession, arguments: argparse.Namespace) -> dict | list:
    actor = f"uid:{os.getuid()}"
    if arguments.command == "list":
        rows = (await session.execute(select(WorkerCredential, WorkerHost.name).join(
            WorkerHost, WorkerCredential.worker_id == WorkerHost.id,
        ).order_by(WorkerCredential.created_at))).all()
        return [{"worker_id": credential.worker_id, "worker_name": name,
                 "credential_id": credential.id,
                 "expires_at": aware(credential.expires_at).isoformat(),
                 "revoked_at": (aware(credential.revoked_at).isoformat()
                                if credential.revoked_at else None),
                 "last_used_at": (aware(credential.last_used_at).isoformat()
                                  if credential.last_used_at else None)}
                for credential, name in rows]
    if arguments.command == "revoke":
        await revoke_credential(session, arguments.credential_id, actor=actor,
                                reason=arguments.reason)
        return {"credential_id": arguments.credential_id, "revoked": True}
    if arguments.command == "issue":
        issued = await issue_credential(session, arguments.worker_name, actor=actor,
            reason=arguments.reason, lifetime_seconds=arguments.lifetime_seconds)
    else:
        issued = await rotate_credential(session, arguments.credential_id, actor=actor,
            reason=arguments.reason, lifetime_seconds=arguments.lifetime_seconds,
            overlap_seconds=arguments.overlap_seconds)
    identity = write_new_token(arguments.output, issued.token)
    try:
        await session.commit()
    except BaseException:
        remove_created_file(arguments.output, identity)
        raise
    return {"worker_id": issued.worker_id, "credential_id": issued.credential_id,
            "expires_at": issued.expires_at.isoformat()}


async def provision(arguments: argparse.Namespace) -> dict | list:
    from .db import SessionLocal, engine, get_schema_readiness

    try:
        if not (await get_schema_readiness()).ready:
            raise ValueError("run database migrations before managing worker credentials")
        async with SessionLocal() as session:
            result = await execute(session, arguments)
            await session.commit()
            return result
    finally:
        await engine.dispose()


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Manage worker credentials on the control host")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("list", help="list metadata without tokens or hashes")
    for command in ("issue", "rotate", "revoke"):
        child = commands.add_parser(command)
        child.add_argument("--reason", required=True)
        if command == "issue":
            child.add_argument("--worker-name", required=True)
        else:
            child.add_argument("--credential-id", required=True)
        if command != "revoke":
            child.add_argument("--output", type=Path, required=True,
                               help="new mode-0600 token file")
            child.add_argument("--lifetime-seconds", type=int, default=DEFAULT_LIFETIME_SECONDS)
        if command == "rotate":
            child.add_argument("--overlap-seconds", type=int, default=600)
    return root


def main() -> None:
    command_parser = parser()
    arguments = command_parser.parse_args()
    try:
        result = asyncio.run(provision(arguments))
    except (CredentialFileError, ValueError) as error:
        command_parser.exit(2, f"{error}\n")
    except Exception:
        command_parser.exit(2, "credential operation failed; no credential was printed\n")
    print(json.dumps(result))


if __name__ == "__main__":
    main()
