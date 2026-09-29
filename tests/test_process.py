import asyncio
import os
import sys
from pathlib import Path

import pytest

from dev_agent.process import run_process


def test_output_input_cwd_and_metadata(tmp_path: Path) -> None:
    command = [
        sys.executable,
        "-c",
        (
            "import os,sys; print(os.getcwd()); print(sys.stdin.read()); "
            "print('diagnostic', file=sys.stderr)"
        ),
    ]
    result = asyncio.run(run_process(command, cwd=tmp_path, stdin="你好\ninput"))
    assert result.exit_code == 0
    assert not result.timed_out
    assert result.stdout == f"{tmp_path.resolve()}\n你好\ninput\n"
    assert result.stderr == "diagnostic\n"
    assert result.command == tuple(command)
    assert result.cwd == tmp_path.resolve()
    assert result.started_at.tzinfo is not None
    assert result.duration_seconds >= 0


def test_failure_is_result(tmp_path: Path) -> None:
    result = asyncio.run(
        run_process(
            [sys.executable, "-c", "import sys; print('failed', file=sys.stderr); sys.exit(7)"],
            cwd=tmp_path,
        )
    )
    assert result.exit_code == 7
    assert result.stderr == "failed\n"
    assert not result.timed_out


def test_no_shell_and_default_stdin_eof(tmp_path: Path) -> None:
    literal = "space ; $(touch surprise) *"
    result = asyncio.run(
        run_process(
            [
                sys.executable,
                "-c",
                "import sys; print(sys.argv[1]); print(repr(sys.stdin.read()))",
                literal,
            ],
            cwd=tmp_path,
        )
    )
    assert result.stdout == f"{literal}\n''\n"
    assert not (tmp_path / "surprise").exists()


def test_launch_errors(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        asyncio.run(run_process([str(tmp_path / "missing-program")], cwd=tmp_path))
    with pytest.raises(FileNotFoundError):
        asyncio.run(run_process([sys.executable], cwd=tmp_path / "missing-directory"))


def test_timeout_retains_output(tmp_path: Path) -> None:
    result = asyncio.run(
        run_process(
            [sys.executable, "-c", "import time; print('started', flush=True); time.sleep(60)"],
            cwd=tmp_path,
            timeout=0.5,
        )
    )
    assert result.timed_out
    assert result.exit_code != 0
    assert result.stdout == "started\n"
    assert result.duration_seconds < 5


def test_large_output_and_invalid_utf8(tmp_path: Path) -> None:
    result = asyncio.run(
        run_process(
            [
                sys.executable,
                "-c",
                "import os; os.write(1, b'x'*200000); os.write(2, b'y'*200000+b'\\xff')",
            ],
            cwd=tmp_path,
        )
    )
    assert result.stdout == "x" * 200000
    assert result.stderr == "y" * 200000 + "\ufffd"


@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan")])
def test_invalid_timeout(tmp_path: Path, timeout: float) -> None:
    with pytest.raises(ValueError, match="timeout"):
        asyncio.run(run_process([sys.executable], cwd=tmp_path, timeout=timeout))


@pytest.mark.parametrize("command", [[], "echo hello"])
def test_invalid_command(tmp_path: Path, command: list[str] | str) -> None:
    with pytest.raises(ValueError, match="command"):
        asyncio.run(run_process(command, cwd=tmp_path))


async def wait_for_file(path: Path) -> None:
    async with asyncio.timeout(5):
        while not path.exists():
            await asyncio.sleep(0.01)


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
def test_cancellation_reaps_process(tmp_path: Path) -> None:
    async def exercise() -> None:
        pid_file = tmp_path / "pid"
        task = asyncio.create_task(
            run_process(
                [
                    sys.executable,
                    "-c",
                    "import os,time; from pathlib import Path; "
                    "Path('pid').write_text(str(os.getpid())); time.sleep(60)",
                ],
                cwd=tmp_path,
            )
        )
        try:
            await wait_for_file(pid_file)
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        with pytest.raises(ProcessLookupError):
            os.kill(int(pid_file.read_text()), 0)

    asyncio.run(exercise())


@pytest.mark.skipif(os.name != "posix", reason="POSIX process group cleanup")
def test_timeout_kills_descendant_holding_pipes(tmp_path: Path) -> None:
    # Parent exits immediately; grandchild inherits stdout/stderr. Killing only
    # the parent would leave communicate blocked and allow the marker write.
    child = "import time; from pathlib import Path; time.sleep(1.5); Path('leaked').touch()"
    parent = f"import subprocess,sys; subprocess.Popen([sys.executable, '-c', {child!r}])"

    async def exercise() -> None:
        result = await asyncio.wait_for(
            run_process(
                [sys.executable, "-c", parent],
                cwd=tmp_path,
                timeout=0.5,
            ),
            timeout=5,
        )
        assert result.timed_out
        await asyncio.sleep(1.6)
        assert not (tmp_path / "leaked").exists()

    asyncio.run(exercise())


def test_commands_can_run_concurrently(tmp_path: Path) -> None:
    # Each process waits for the other to start. A blocking implementation
    # cannot complete both successfully within the timeout.
    async def exercise() -> None:
        def command(own: str, other: str) -> list[str]:
            return [
                sys.executable,
                "-c",
                f"import time; from pathlib import Path; Path({own!r}).touch(); "
                f"\nwhile not Path({other!r}).exists(): time.sleep(0.01)\nprint('ready')",
            ]

        results = await asyncio.gather(
            run_process(command("first", "second"), cwd=tmp_path, timeout=5),
            run_process(command("second", "first"), cwd=tmp_path, timeout=5),
        )
        assert all(result.exit_code == 0 and not result.timed_out for result in results)
        assert all(result.stdout == "ready\n" for result in results)

    asyncio.run(exercise())
