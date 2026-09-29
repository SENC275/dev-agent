from io import StringIO
from threading import Event

import pytest
from rich.console import Console

from dev_agent.progress import activity


def test_nonterminal_heartbeat_reports_elapsed_and_stops() -> None:
    output = StringIO()
    console = Console(file=output, force_terminal=False, width=120)
    ticks = 0
    received = Event()

    def stage() -> str:
        nonlocal ticks
        ticks += 1
        if ticks >= 3:
            received.set()
        return "revise:review"

    with activity(console, "Working", stage, interval=0.01, heartbeat=0.01):
        assert received.wait(2)
    text = output.getvalue()
    assert text.count("revise:review") >= 2
    assert "elapsed" in text
    count = ticks
    Event().wait(0.03)
    assert ticks == count


def test_progress_cleanup_on_error() -> None:
    with pytest.raises(ValueError), activity(Console(file=StringIO()), "Working"):
        raise ValueError("failed")
