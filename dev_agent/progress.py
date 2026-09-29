"""Visible liveness, without pretending to know provider completion percentages."""

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from threading import Event, Thread
from time import monotonic

from rich.console import Console
from rich.text import Text


@contextmanager
def activity(
    console: Console,
    label: str,
    stage: Callable[[], str] | None = None,
    *,
    interval: float = 0.5,
    heartbeat: float = 15,
) -> Iterator[None]:
    started = monotonic()
    stop = Event()
    console.print(label, markup=False)
    with console.status(Text(label), spinner="dots", refresh_per_second=8) as display:

        def monitor() -> None:
            previous = ""
            last_print = started
            while not stop.wait(interval):
                current = stage() if stage else label
                elapsed = monotonic() - started
                message = f"{current} · {elapsed:.0f}s elapsed · waiting for completion"
                display.update(Text(message))
                if current != previous or (
                    not console.is_terminal and monotonic() - last_print >= heartbeat
                ):
                    console.print(message, markup=False)
                    previous = current
                    last_print = monotonic()

        thread = Thread(target=monitor, daemon=True)
        thread.start()
        try:
            yield
        finally:
            stop.set()
            thread.join()
