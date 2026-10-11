"""Run the Docker CLI without blocking the event loop. The CLI finds the daemon through the
standard DOCKER_HOST / DOCKER_TLS_VERIFY / DOCKER_CERT_PATH variables (dind over TLS in local
dev); nothing here mounts or names a socket."""

import asyncio
import os
import shutil
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool = False

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out

    @property
    def output(self) -> str:
        return self.stdout + self.stderr


class CommandRunner(Protocol):
    async def __call__(
        self,
        args: list[str],
        *,
        timeout: float,
        stdin: str | None = None,
        env: dict[str, str] | None = None,
    ) -> CommandResult: ...


async def run_docker(
    args: list[str],
    *,
    timeout: float,
    stdin: str | None = None,
    env: dict[str, str] | None = None,
) -> CommandResult:
    """`docker <args>`. On timeout the CLI is killed (BuildKit cancels the build when its client
    goes away) and the result has timed_out=True. Secrets go in through stdin or files, never
    args: argv is visible to every process on the host."""
    docker = shutil.which("docker")
    if docker is None:
        return CommandResult(127, "", "docker CLI not found")
    proc = await asyncio.create_subprocess_exec(
        docker,
        *args,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env={**os.environ, **(env or {})},
    )
    try:
        out, err = await asyncio.wait_for(
            proc.communicate(stdin.encode() if stdin is not None else None), timeout
        )
    except TimeoutError:
        proc.kill()
        out, err = await proc.communicate()
        return CommandResult(-9, out.decode(errors="replace"), err.decode(errors="replace"), True)
    return CommandResult(
        proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
    )
