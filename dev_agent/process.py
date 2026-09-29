"""Asynchronous, shell-free process execution with bounded runtime.

Launch errors remain OSError subclasses. A launched command returns its real
exit code, including on timeout. Cancellation cleans up and propagates to the
caller. POSIX children share a new process group that is killed on timeout or
cancellation; on other platforms cleanup covers the direct child only.
"""

import asyncio
import math
import os
import signal
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic


@dataclass(frozen=True)
class ProcessResult:
    command: tuple[str, ...]
    cwd: Path
    started_at: datetime
    duration_seconds: float
    exit_code: int
    stdout: str
    stderr: str
    timed_out: bool


def _kill(process: asyncio.subprocess.Process) -> None:
    try:
        if os.name == "posix":
            # The parent may have exited while a descendant still holds a pipe.
            os.killpg(process.pid, signal.SIGKILL)
        elif process.returncode is None:
            process.kill()
    except ProcessLookupError:
        # Normal race: the process/group exited before cleanup reached it.
        pass


async def run_process(
    command: Sequence[str],
    *,
    cwd: Path,
    stdin: str | None = None,
    timeout: float = 60.0,
) -> ProcessResult:
    """Run argv in cwd, capturing UTF-8 output (invalid bytes are replaced).

    stdin=None supplies EOF, never the caller's terminal. Output is buffered in
    memory. timeout must be finite and positive; it bounds communication after
    successful process creation. There are no retries or implicit shell parsing.
    """
    if isinstance(command, (str, bytes)) or not command:
        raise ValueError("command must be a non-empty sequence of arguments")
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout must be finite and greater than zero")
    argv = tuple(command)
    directory = cwd.resolve()
    started_at = datetime.now(UTC)
    started = monotonic()
    process = await asyncio.create_subprocess_exec(
        *argv,
        cwd=directory,
        stdin=asyncio.subprocess.PIPE if stdin is not None else asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=os.name == "posix",
    )
    communication = asyncio.create_task(
        process.communicate(None if stdin is None else stdin.encode("utf-8"))
    )
    timed_out = False
    try:
        try:
            stdout, stderr = await asyncio.wait_for(asyncio.shield(communication), timeout)
        except TimeoutError:
            timed_out = True
            _kill(process)
            stdout, stderr = await asyncio.shield(communication)
    except BaseException:
        _kill(process)
        # Reap the child and drain pipes before propagating cancellation/errors.
        await asyncio.shield(communication)
        raise
    assert process.returncode is not None
    return ProcessResult(
        command=argv,
        cwd=directory,
        started_at=started_at,
        duration_seconds=monotonic() - started,
        exit_code=process.returncode,
        stdout=stdout.decode("utf-8", errors="replace"),
        stderr=stderr.decode("utf-8", errors="replace"),
        timed_out=timed_out,
    )
