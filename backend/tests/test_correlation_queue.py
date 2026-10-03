"""The account-wide correlation queue: serial, shared, honest about waiting and timeouts.

Everything runs against a scripted fake client and a fake clock, so nothing reaches BRAIN
and no test waits in real time.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from alpha_harness.brain.client import BrainResponse
from alpha_harness.brain.corrqueue import CorrelationQueue, correlation_readings
from alpha_harness.brain.errors import BrainPollTimeout, BrainServerError

PENDING = object()


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0)


class FakeClient:
    """Answers each path from a script: PENDING (Retry-After 1), a body, or an exception.

    The last step repeats, so a script of ``[PENDING]`` stays pending forever.
    """

    def __init__(self, script: dict[str, list[Any]]) -> None:
        self.script = {path: list(steps) for path, steps in script.items()}
        self.calls: list[str] = []

    async def request_retrying(self, method: str, path: str, **_: Any) -> BrainResponse:
        assert method == "GET"
        self.calls.append(path)
        await asyncio.sleep(0)
        steps = self.script[path]
        step = steps.pop(0) if len(steps) > 1 else steps[0]
        if isinstance(step, Exception):
            raise step
        if step is PENDING:
            return BrainResponse(200, httpx.Headers({"Retry-After": "1"}), None, 1.0, None, None)
        return BrainResponse(200, httpx.Headers(), step, None, None, None)


def queue_for(client: FakeClient, clock: FakeClock, **kw: Any) -> CorrelationQueue:
    return CorrelationQueue(
        client,  # type: ignore[arg-type]
        clock=clock,
        wall=lambda: 1_790_000_000.0,
        sleep=clock.sleep,
        **kw,
    )


PROD_A = "/alphas/A/correlations/prod"
PROD_B = "/alphas/B/correlations/prod"


@pytest.mark.asyncio
async def test_jobs_reach_brain_one_at_a_time() -> None:
    client = FakeClient(
        {PROD_A: [PENDING, PENDING, {"max": 0.61}], PROD_B: [PENDING, {"max": 0.55}]}
    )
    queue = queue_for(client, FakeClock())
    a, b = await asyncio.gather(queue.run("prod", "A"), queue.run("prod", "B"))
    assert a["max"] == 0.61 and b["max"] == 0.55
    # Every poll of A comes before the first poll of B: never two jobs at BRAIN at once.
    assert client.calls == [PROD_A, PROD_A, PROD_A, PROD_B, PROD_B]
    await queue.stop()


@pytest.mark.asyncio
async def test_asking_twice_joins_the_same_job() -> None:
    client = FakeClient({PROD_A: [PENDING, {"max": 0.6}]})
    queue = queue_for(client, FakeClock())
    first = asyncio.create_task(queue.run("prod", "A"))
    second = asyncio.create_task(queue.run("prod", "A"))
    await asyncio.sleep(0)
    snapshot = queue.status()
    jobs = ([snapshot["running"]] if snapshot["running"] else []) + snapshot["queued"]
    assert len(jobs) == 1 and jobs[0]["waiters"] == 2
    assert (await first) == (await second) == {"max": 0.6}
    assert client.calls == [PROD_A, PROD_A]
    await queue.stop()


@pytest.mark.asyncio
async def test_a_job_pending_too_long_is_a_timeout_and_the_queue_moves_on() -> None:
    client = FakeClient({PROD_A: [PENDING], PROD_B: [{"max": 0.4}]})
    clock = FakeClock()
    queue = queue_for(client, clock, job_timeout=30.0, held_after=10.0)
    a = asyncio.create_task(queue.run("prod", "A"))
    b = asyncio.create_task(queue.run("prod", "B"))
    with pytest.raises(BrainPollTimeout, match="one correlation job per account"):
        await a
    assert (await b) == {"max": 0.4}
    recent = queue.status()["recent"]
    assert [(job["alphaId"], job["outcome"]) for job in recent] == [("B", "done"), ("A", "timeout")]
    await queue.stop()


@pytest.mark.asyncio
async def test_status_says_the_slot_is_held_once_a_job_waits_past_the_threshold() -> None:
    client = FakeClient({PROD_A: [PENDING]})
    clock = FakeClock()
    queue = queue_for(client, clock, job_timeout=10_000.0, held_after=60.0)
    task = asyncio.create_task(queue.run("prod", "A"))
    for _ in range(20):
        await asyncio.sleep(0)
    assert queue.status()["held"] is False
    for _ in range(10_000):  # bounded: the fake clock moves only when the worker sleeps
        if clock.now >= 1000.0 + 61.0:
            break
        await asyncio.sleep(0)
    status = queue.status()
    assert status["held"] is True
    assert status["running"]["alphaId"] == "A"
    assert "one correlation job at a time" in status["message"]
    task.cancel()
    await queue.stop()


@pytest.mark.asyncio
async def test_a_brain_error_reaches_its_caller_and_the_next_job_still_runs() -> None:
    client = FakeClient({PROD_A: [BrainServerError("boom", status=500)], PROD_B: [{"max": 0.5}]})
    queue = queue_for(client, FakeClock())
    a = asyncio.create_task(queue.run("prod", "A"))
    b = asyncio.create_task(queue.run("prod", "B"))
    with pytest.raises(BrainServerError):
        await a
    assert (await b) == {"max": 0.5}
    assert queue.status()["recent"][1]["outcome"] == "error"
    await queue.stop()


@pytest.mark.asyncio
async def test_a_caller_leaving_does_not_abandon_the_job() -> None:
    client = FakeClient({PROD_A: [PENDING, PENDING, {"max": 0.66}]})
    queue = queue_for(client, FakeClock())
    task = asyncio.create_task(queue.run("prod", "A"))
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    for _ in range(50):
        await asyncio.sleep(0)
    assert queue.status()["recent"][0]["outcome"] == "done"
    assert client.calls.count(PROD_A) == 3
    await queue.stop()


@pytest.mark.asyncio
async def test_checks_share_the_queue_with_correlations() -> None:
    check = "/alphas/A/check"
    client = FakeClient({check: [PENDING, {"is": {"checks": []}}], PROD_B: [{"max": 0.3}]})
    queue = queue_for(client, FakeClock())
    await asyncio.gather(queue.run("check", "A"), queue.run("prod", "B"))
    assert client.calls == [check, check, PROD_B]
    await queue.stop()


def test_readings_separate_a_timeout_from_a_high_correlation() -> None:
    body = {
        "is": {
            "checks": [
                {"name": "LOW_SHARPE", "result": "PASS", "value": 2.1, "limit": 1.58},
                {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.52, "limit": 0.7},
                {"name": "PROD_CORRELATION", "result": "WARNING", "value": None},
                {"name": "POWER_POOL_CORRELATION", "result": "ERROR", "value": None},
            ]
        }
    }
    readings = {r["name"]: r for r in correlation_readings(body)}
    assert set(readings) == {"SELF_CORRELATION", "PROD_CORRELATION", "POWER_POOL_CORRELATION"}
    assert readings["SELF_CORRELATION"]["reading"] == "measured"
    assert readings["PROD_CORRELATION"]["reading"] == "unmeasured"
    assert "not treat it as a high correlation" in readings["PROD_CORRELATION"]["note"]
    assert readings["POWER_POOL_CORRELATION"]["reading"] == "unmeasured"


def test_a_warning_with_a_value_is_a_real_measurement() -> None:
    body = {"is": {"checks": [{"name": "PROD_CORRELATION", "result": "WARNING", "value": 0.79}]}}
    (reading,) = correlation_readings(body)
    assert reading["reading"] == "measured" and reading["value"] == 0.79


def test_pending_is_reported_as_pending() -> None:
    body = {"is": {"checks": [{"name": "SELF_CORRELATION", "result": "PENDING"}]}}
    (reading,) = correlation_readings(body)
    assert reading["reading"] == "pending"


def test_readings_tolerate_an_empty_or_odd_body() -> None:
    assert correlation_readings({}) == []
    assert correlation_readings({"is": None}) == []


def test_the_app_serves_the_queue_and_labels_check_readings() -> None:
    """End to end through the real app: the status route, and /check routed via the queue."""
    from fastapi.testclient import TestClient

    from alpha_harness.main import create_app

    body = {
        "is": {
            "checks": [
                {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.5, "limit": 0.7},
                {"name": "PROD_CORRELATION", "result": "WARNING", "value": None},
            ]
        }
    }
    fake = FakeClient(
        {
            "/alphas/X1/check": [PENDING, body],
            "/alphas/X1/correlations/prod": [{"max": 0.63, "records": []}],
        }
    )
    # The app only trusts localhost Host headers (DNS rebinding guard), so no "testserver".
    with TestClient(create_app(), base_url="http://127.0.0.1") as client:
        state = client.app.state.harness  # type: ignore[attr-defined]
        state.correlations.client = fake
        state.correlations.sleep = FakeClock().sleep

        idle = client.get("/api/correlations/queue").json()
        assert idle["running"] is None and idle["queued"] == [] and idle["held"] is False

        check = client.get("/api/alphas/X1/check").json()
        readings = {r["name"]: r["reading"] for r in check["correlationReadings"]}
        assert readings == {"SELF_CORRELATION": "measured", "PROD_CORRELATION": "unmeasured"}
        assert check["is"]["checks"][1]["result"] == "WARNING"  # BRAIN's JSON untouched

        prod = client.get("/api/alphas/X1/correlations/prod").json()
        assert prod["max"] == 0.63

        after = client.get("/api/correlations/queue").json()
        assert [(j["kind"], j["alphaId"], j["outcome"]) for j in after["recent"]] == [
            ("prod", "X1", "done"),
            ("check", "X1", "done"),
        ]
    assert fake.calls == ["/alphas/X1/check", "/alphas/X1/check", "/alphas/X1/correlations/prod"]


# -- review fixes: shutdown, bounded waits, kept answers, no loop errors ----------------------


class RealTimeClock(FakeClock):
    """A fake clock whose sleeps also take a little real time, for tests of ``max_wait``,
    which asyncio measures on the real loop clock."""

    async def sleep(self, seconds: float) -> None:
        self.now += seconds
        await asyncio.sleep(0.01)


async def _until(predicate: Any, rounds: int = 2_000) -> None:
    for _ in range(rounds):
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("condition never became true")


@pytest.mark.asyncio
async def test_stop_releases_a_caller_waiting_on_the_running_job() -> None:
    client = FakeClient({PROD_A: [PENDING]})
    queue = queue_for(client, FakeClock(), job_timeout=10_000.0)
    caller = asyncio.create_task(queue.run("prod", "A"))
    await _until(lambda: queue.status()["running"] is not None)
    await queue.stop()
    await _until(caller.done)
    assert caller.cancelled()
    assert queue.status()["recent"][0]["outcome"] == "cancelled"


@pytest.mark.asyncio
async def test_stop_releases_queued_callers_too() -> None:
    client = FakeClient({PROD_A: [PENDING], PROD_B: [{"max": 0.1}]})
    queue = queue_for(client, FakeClock(), job_timeout=10_000.0)
    a = asyncio.create_task(queue.run("prod", "A"))
    b = asyncio.create_task(queue.run("prod", "B"))
    await _until(lambda: len(queue.status()["queued"]) == 1)
    await queue.stop()
    await _until(lambda: a.done() and b.done())
    assert a.cancelled() and b.cancelled()
    assert PROD_B not in client.calls


@pytest.mark.asyncio
async def test_a_caller_waits_at_most_max_wait_and_the_answer_is_kept() -> None:
    client = FakeClient({PROD_A: [PENDING] * 20 + [{"max": 0.58}]})
    queue = queue_for(client, RealTimeClock(), job_timeout=10_000.0, max_wait=0.05)
    with pytest.raises(BrainPollTimeout, match="keeps its place"):
        await queue.run("prod", "A")
    await _until(lambda: bool(queue.status()["recent"]))
    polls = len(client.calls)
    assert (await queue.run("prod", "A")) == {"max": 0.58}
    assert len(client.calls) == polls  # served from the kept answer, not asked again
    await queue.stop()


@pytest.mark.asyncio
async def test_a_kept_answer_expires() -> None:
    client = FakeClient({PROD_A: [{"max": 0.5}]})
    clock = FakeClock()
    queue = queue_for(client, clock, keep_for=60.0)
    await queue.run("prod", "A")
    clock.now += 61.0
    await queue.run("prod", "A")
    assert client.calls == [PROD_A, PROD_A]
    await queue.stop()


@pytest.mark.asyncio
@pytest.mark.parametrize("max_wait", [None, 60.0])
async def test_an_abandoned_job_that_fails_reports_nothing_to_the_loop(
    max_wait: float | None,
) -> None:
    import gc

    loop = asyncio.get_running_loop()
    reported: list[dict[str, Any]] = []
    loop.set_exception_handler(lambda _loop, context: reported.append(context))
    client = FakeClient({PROD_A: [PENDING, PENDING, BrainServerError("boom", status=500)]})
    queue = queue_for(client, FakeClock(), max_wait=max_wait)
    caller = asyncio.create_task(queue.run("prod", "A"))
    await asyncio.sleep(0)
    caller.cancel()
    await _until(lambda: bool(queue.status()["recent"]))
    gc.collect()
    await asyncio.sleep(0)
    assert queue.status()["recent"][0]["outcome"] == "error"
    assert reported == []
    loop.set_exception_handler(None)
    await queue.stop()


@pytest.mark.asyncio
async def test_clear_drops_queued_jobs_but_not_the_running_one() -> None:
    client = FakeClient({PROD_A: [PENDING] * 30 + [{"max": 0.2}], PROD_B: [{"max": 0.3}]})
    queue = queue_for(client, RealTimeClock(), job_timeout=10_000.0)
    a = asyncio.create_task(queue.run("prod", "A"))
    b = asyncio.create_task(queue.run("prod", "B"))
    await _until(lambda: len(queue.status()["queued"]) == 1)
    assert queue.clear() == 1
    assert (await a) == {"max": 0.2}
    with pytest.raises(asyncio.CancelledError):
        await b
    assert PROD_B not in client.calls
    await queue.stop()


@pytest.mark.asyncio
async def test_waiters_count_only_callers_still_waiting() -> None:
    client = FakeClient({PROD_A: [PENDING]})
    queue = queue_for(client, FakeClock(), job_timeout=10_000.0)
    first = asyncio.create_task(queue.run("prod", "A"))
    second = asyncio.create_task(queue.run("prod", "A"))
    await _until(lambda: (queue.status()["running"] or {}).get("waiters") == 2)
    first.cancel()
    await _until(lambda: queue.status()["running"]["waiters"] == 1)
    second.cancel()
    await queue.stop()


def test_pass_without_a_value_is_not_a_measurement() -> None:
    body = {"is": {"checks": [{"name": "SELF_CORRELATION", "result": "PASS", "value": None}]}}
    (reading,) = correlation_readings(body)
    assert reading["reading"] == "no-value"


def test_submittable_verdict_counts_an_unmeasured_correlation_as_pending() -> None:
    from alpha_harness.vault.yields import verdict

    sharp = {"name": "LOW_SHARPE", "result": "PASS", "value": 2.0}
    unmeasured = {"name": "SELF_CORRELATION", "result": "ERROR", "value": None}
    measured_fail = {"name": "SELF_CORRELATION", "result": "FAIL", "value": 0.82}
    assert verdict([sharp, unmeasured]) == "pending"
    assert verdict([sharp, measured_fail]) == "refused"
    assert verdict([sharp, {"name": "SELF_CORRELATION", "result": "PASS", "value": 0.4}]) == (
        "submittable"
    )
