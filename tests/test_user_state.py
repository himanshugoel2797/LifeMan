"""Tests for the user-state provider chain.

Covers `get_state()` semantics (registry + override ordering + crash
isolation) and each built-in provider's contract.
"""

from __future__ import annotations

from datetime import time as dtime

import pytest

from lifeman import user_state


@pytest.fixture(autouse=True)
def _isolate_providers():
    user_state.clear_providers()
    yield
    user_state.clear_providers()


# ---------------------------------------------------------------------------
# Registry semantics
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_state_merges_provider_slices():
    async def a():
        return {"x": 1}

    async def b():
        return {"y": 2}

    user_state.register_provider("a", a)
    user_state.register_provider("b", b)
    assert await user_state.get_state() == {"x": 1, "y": 2}


@pytest.mark.asyncio
async def test_later_provider_overrides_earlier_on_collision():
    async def a():
        return {"x": 1}

    async def b():
        return {"x": 2}

    user_state.register_provider("a", a)
    user_state.register_provider("b", b)
    assert (await user_state.get_state())["x"] == 2


@pytest.mark.asyncio
async def test_crashed_provider_does_not_break_state():
    async def bad():
        raise RuntimeError("boom")

    async def ok():
        return {"ok": True}

    user_state.register_provider("bad", bad)
    user_state.register_provider("ok", ok)
    state = await user_state.get_state()
    assert state == {"ok": True}


@pytest.mark.asyncio
async def test_non_dict_return_is_ignored():
    async def bad():
        return ["nope"]

    user_state.register_provider("bad", bad)
    assert await user_state.get_state() == {}


# ---------------------------------------------------------------------------
# time_of_day_provider — minimal smoke test (it depends on wall clock)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_time_of_day_provider_emits_expected_keys():
    out = await user_state.time_of_day_provider()
    assert set(out) == {"hour", "weekday", "is_weekend", "period"}
    assert 0 <= out["hour"] < 24
    assert out["period"] in ("morning", "afternoon", "evening", "night")
    assert out["weekday"] in user_state._WEEKDAYS
    assert isinstance(out["is_weekend"], bool)


def test_period_classification():
    assert user_state._classify_period(7) == "morning"
    assert user_state._classify_period(13) == "afternoon"
    assert user_state._classify_period(20) == "evening"
    assert user_state._classify_period(2) == "night"
    assert user_state._classify_period(23) == "night"


# ---------------------------------------------------------------------------
# dnd_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_dnd_provider_emits_when_set(temp_db):
    from lifeman.user_settings import set_setting
    await set_setting("do_not_disturb", True)
    assert await user_state.dnd_provider() == {"do_not_disturb": True}


@pytest.mark.asyncio
async def test_dnd_provider_silent_when_unset(temp_db):
    assert await user_state.dnd_provider() == {}


@pytest.mark.asyncio
async def test_dnd_provider_silent_when_false(temp_db):
    from lifeman.user_settings import set_setting
    await set_setting("do_not_disturb", False)
    assert await user_state.dnd_provider() == {}


# ---------------------------------------------------------------------------
# sleep_schedule_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_sleep_schedule_provider_handles_overnight_window(temp_db, monkeypatch):
    """The common case: bedtime 23:00, wake 07:00. Asleep at 02:00."""
    from lifeman.user_settings import set_setting
    await set_setting("sleep_schedule", {"start": "23:00", "end": "07:00"})

    class _FakeNow:
        @staticmethod
        def time() -> dtime:
            return dtime(2, 0)

    monkeypatch.setattr(
        "lifeman.user_state.datetime",
        type("_D", (), {
            "now": staticmethod(lambda: type("_X", (), {
                "astimezone": staticmethod(lambda: _FakeNow()),
            })()),
        }),
    )
    assert await user_state.sleep_schedule_provider() == {"asleep": True}


@pytest.mark.asyncio
async def test_sleep_schedule_provider_silent_during_waking_hours(temp_db, monkeypatch):
    from lifeman.user_settings import set_setting
    await set_setting("sleep_schedule", {"start": "23:00", "end": "07:00"})

    class _FakeNow:
        @staticmethod
        def time() -> dtime:
            return dtime(14, 0)  # afternoon

    monkeypatch.setattr(
        "lifeman.user_state.datetime",
        type("_D", (), {
            "now": staticmethod(lambda: type("_X", (), {
                "astimezone": staticmethod(lambda: _FakeNow()),
            })()),
        }),
    )
    assert await user_state.sleep_schedule_provider() == {}


@pytest.mark.asyncio
async def test_sleep_schedule_provider_silent_when_unconfigured(temp_db):
    assert await user_state.sleep_schedule_provider() == {}


@pytest.mark.asyncio
async def test_sleep_schedule_provider_ignores_malformed(temp_db):
    from lifeman.user_settings import set_setting
    await set_setting("sleep_schedule", {"start": "not-a-time"})
    assert await user_state.sleep_schedule_provider() == {}


# ---------------------------------------------------------------------------
# device_status_provider
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_device_status_offline_when_no_devices(temp_db):
    state = await user_state.device_status_provider()
    assert state == {"device_online": False}


@pytest.mark.asyncio
async def test_device_status_online_when_subscriber_present(temp_db):
    """Pair a device, subscribe its audience, expect device_online=True."""
    from lifeman.devices import issue_pairing_code, consume_pairing_code
    from lifeman.sse import bus

    pairing = await issue_pairing_code()
    issued = await consume_pairing_code(
        pairing.code, name="phone", platform="android",
    )
    sub = bus.subscribe(audience=f"device:{issued.device_id}")
    # Pull the initial sync sentinel so the subscriber is registered.
    await sub.__anext__()
    try:
        state = await user_state.device_status_provider()
        assert state == {"device_online": True}
    finally:
        await sub.aclose()


# ---------------------------------------------------------------------------
# install_builtin_providers
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_install_builtin_providers_populates_state(temp_db):
    user_state.install_builtin_providers()
    state = await user_state.get_state()
    # time_of_day keys always present
    assert "hour" in state
    assert "period" in state
    assert state.get("device_online") is False
