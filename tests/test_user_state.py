"""Tests for the user-state provider chain.

Covers `get_state()` semantics (registry + override ordering + crash
isolation) and each built-in provider's contract.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

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
# activity_provider — derived from input_events
# ---------------------------------------------------------------------------


async def _insert_input(db, minutes_ago: int, intent_hint: str | None = None,
                        expires_at: str | None = None) -> None:
    """Helper: insert one input_events row at NOW - minutes_ago."""
    import json as _json
    import uuid as _uuid
    when = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    await db.execute(
        """INSERT INTO input_events
             (id, surface, raw_payload, intent_hint, source, sensitivity,
              expires_at, context_json, reason, emitted_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            str(_uuid.uuid4())[:12], "chat", "test", intent_hint, "user",
            "personal", expires_at, _json.dumps({}), "test",
            when.isoformat(),
        ),
    )
    await db.commit()


@pytest.mark.asyncio
async def test_activity_no_data_when_no_inputs(temp_db):
    out = await user_state.activity_provider()
    assert out == {"activity": "no_data"}


@pytest.mark.asyncio
async def test_activity_active_when_input_in_last_5_min(temp_db):
    await _insert_input(temp_db, minutes_ago=2)
    out = await user_state.activity_provider()
    assert out["activity"] == "active"
    assert out["idle_minutes"] < 5
    assert out["last_input_at"]


@pytest.mark.asyncio
async def test_activity_idle_between_5_and_60_min(temp_db):
    await _insert_input(temp_db, minutes_ago=30)
    out = await user_state.activity_provider()
    assert out["activity"] == "idle"
    assert 25 <= out["idle_minutes"] <= 35


@pytest.mark.asyncio
async def test_activity_long_idle_past_60_min(temp_db):
    await _insert_input(temp_db, minutes_ago=120)
    out = await user_state.activity_provider()
    assert out["activity"] == "long_idle"
    assert out["idle_minutes"] >= 60


@pytest.mark.asyncio
async def test_activity_picks_most_recent_when_multiple(temp_db):
    await _insert_input(temp_db, minutes_ago=200)  # old
    await _insert_input(temp_db, minutes_ago=1)    # fresh
    out = await user_state.activity_provider()
    assert out["activity"] == "active"


# ---------------------------------------------------------------------------
# busy_provider — derived from input_events with intent_hint='busy'
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_silent_when_no_busy_events(temp_db):
    await _insert_input(temp_db, minutes_ago=1)  # not a busy event
    assert await user_state.busy_provider() == {}


@pytest.mark.asyncio
async def test_busy_emits_when_window_currently_active(temp_db):
    future = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
    await _insert_input(
        temp_db, minutes_ago=1, intent_hint="busy", expires_at=future,
    )
    out = await user_state.busy_provider()
    assert out["busy"] is True
    assert out["busy_until"] == future


@pytest.mark.asyncio
async def test_busy_silent_when_window_expired(temp_db):
    past = (datetime.now(timezone.utc) - timedelta(minutes=10)).isoformat()
    await _insert_input(
        temp_db, minutes_ago=60, intent_hint="busy", expires_at=past,
    )
    assert await user_state.busy_provider() == {}


@pytest.mark.asyncio
async def test_busy_accepts_namespaced_intent_hint(temp_db):
    """intent_hint='busy:calendar' or 'busy:focus' should also match."""
    future = (datetime.now(timezone.utc) + timedelta(minutes=15)).isoformat()
    await _insert_input(
        temp_db, minutes_ago=1, intent_hint="busy:meeting", expires_at=future,
    )
    out = await user_state.busy_provider()
    assert out["busy"] is True


@pytest.mark.asyncio
async def test_busy_picks_latest_window_when_multiple(temp_db):
    near = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()
    far = (datetime.now(timezone.utc) + timedelta(minutes=60)).isoformat()
    await _insert_input(
        temp_db, minutes_ago=1, intent_hint="busy", expires_at=near,
    )
    await _insert_input(
        temp_db, minutes_ago=1, intent_hint="busy", expires_at=far,
    )
    out = await user_state.busy_provider()
    assert out["busy_until"] == far


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
    # activity key is always emitted (no_data if no inputs)
    assert state["activity"] == "no_data"
    # device_online derived from bus (no devices in test → False)
    assert state.get("device_online") is False
    # busy/do_not_disturb are absent unless triggered
    assert "busy" not in state
    assert "do_not_disturb" not in state
