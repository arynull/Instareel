"""Regression tests for _await_blocking (app/api/resources.py).

Background: several endpoints did ``pool.submit(...).result(timeout)``
directly on the event-loop thread. With a single uvicorn worker that froze
the whole dashboard for up to ``timeout`` seconds — the phone view calls
GET /bios/{id}/current on every mount, so one slow Instagram read hung the
entire site. _await_blocking must offload without ever blocking the loop.
"""
import asyncio
import concurrent.futures
import time

import pytest

from app.api.resources import _await_blocking


def _slow(seconds: float) -> str:
    time.sleep(seconds)
    return "done"


def test_returns_result():
    async def main():
        return await _await_blocking(_slow, 0.05, timeout=5)

    assert asyncio.run(main()) == "done"


def test_kwargs_pass_through():
    def _kw(a, b=0):
        return a + b

    async def main():
        return await _await_blocking(_kw, 1, b=2, timeout=5)

    assert asyncio.run(main()) == 3


def test_timeout_raises_concurrent_timeout_error():
    async def main():
        start = time.monotonic()
        with pytest.raises(concurrent.futures.TimeoutError):
            await _await_blocking(_slow, 4, timeout=0.3)
        # Measured inside the loop: asyncio.run's teardown wait for the
        # detached worker must not pollute the assertion.
        return time.monotonic() - start

    elapsed = asyncio.run(main())
    # Fails promptly at the timeout — must not wait out the 4s of work.
    assert elapsed < 2


def test_event_loop_stays_responsive_while_blocked():
    """The core regression: while a 2s blocking call runs, a 50ms ticker
    must keep ticking. The old .result() code let it tick ~0 times."""
    ticks = 0

    async def main():
        nonlocal ticks

        async def ticker():
            nonlocal ticks
            for _ in range(100):
                await asyncio.sleep(0.05)
                ticks += 1

        ticker_task = asyncio.ensure_future(ticker())
        result = await _await_blocking(_slow, 2.0, timeout=10)
        await ticker_task
        return result

    assert asyncio.run(main()) == "done"
    # 2s of blocking work at 50ms ticks -> ~40 ticks if the loop is free.
    assert ticks >= 20, f"event loop was blocked, only {ticks} ticks in 2s"


def test_timeout_does_not_block_loop_either():
    ticks = 0

    async def main():
        nonlocal ticks

        async def ticker():
            nonlocal ticks
            for _ in range(40):
                await asyncio.sleep(0.05)
                ticks += 1

        ticker_task = asyncio.ensure_future(ticker())
        with pytest.raises(concurrent.futures.TimeoutError):
            await _await_blocking(_slow, 4, timeout=0.5)
        await ticker_task

    asyncio.run(main())
    assert ticks >= 10, f"event loop was blocked during timeout wait ({ticks} ticks)"
