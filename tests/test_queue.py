"""加注队列、预约优先、过号策略、补偿与幂等。"""

from __future__ import annotations

from dataclasses import replace

import pytest

from hydrorefuel.config import default_config
from hydrorefuel.app import build_platform
from hydrorefuel.errors import InterlockError


def test_queue_is_first_come_first_served(platform):
    platform.enqueue_vehicle("v1")
    platform.enqueue_vehicle("v2")
    assert platform.queue.position("v2") == 2
    assert platform.queue.waiting()[0]["vehicle"] == "v1"
    assert platform.dequeue_vehicle()["vehicle"] == "v1"
    assert platform.queue.size() == 1


def test_queue_capacity_is_limited(platform):
    for index in range(platform.config.queue.max_waiting):
        platform.enqueue_vehicle(f"v{index}")
    with pytest.raises(InterlockError):
        platform.enqueue_vehicle("overflow")


def test_dequeue_is_idempotent_until_confirmation_is_consumed(platform, clock):
    platform.enqueue_vehicle("v1")
    platform.enqueue_vehicle("v2")

    first = platform.dequeue_vehicle()
    second = platform.dequeue_vehicle()
    assert first["vehicle"] == second["vehicle"] == "v1"
    assert second["idempotent_replayed"] is True
    assert platform.queue.active() == ["v1"]

    clock.advance(platform.config.queue.confirm_timeout_s + 1.0)
    platform.cycle()
    assert platform.queue.active() == []
    assert platform.dequeue_vehicle()["vehicle"] == "v2"


def test_appointment_does_not_reserve_a_place_before_due_time(platform, clock):
    scheduled_at = clock.now() + 10.0
    platform.book_appointment("appt", scheduled_at)
    platform.enqueue_vehicle("appt")
    platform.enqueue_vehicle("walk")
    assert platform.dequeue_vehicle()["vehicle"] == "walk"
    assert "walk" in platform.queue.active()

    snapshot = platform.queue_snapshot()
    assert snapshot["rules"]["appointment_policy"] == "release_at_due_time"
    assert [item["vehicle"] for item in snapshot["waiting"]] == ["appt"]
    assert snapshot["reservations"][0]["status"] == "booked"


def test_due_appointment_is_released_without_displacing_active_vehicle(platform, clock):
    platform.enqueue_vehicle("walk1")
    platform.enqueue_vehicle("walk2")
    platform.book_appointment("appt", clock.now() + 5.0)
    platform.enqueue_vehicle("appt")
    assert platform.dequeue_vehicle()["vehicle"] == "walk1"

    clock.advance(5.0)
    platform.cycle()

    waiting = [item["vehicle"] for item in platform.queue.waiting()]
    assert waiting == ["appt", "walk2"]
    assert "walk1" in platform.queue.active()
    compensation = platform.queue.compensations()[0]
    assert compensation["vehicle"] == "walk2"
    assert compensation["caused_by"] == "appt"
    assert compensation["credit_s"] == platform.config.queue.compensation_credit_s_per_place


def test_order_change_is_visible_in_console_snapshot_and_audit(platform, clock):
    platform.enqueue_vehicle("walk")
    platform.book_appointment("appt", clock.now() + 1.0)
    platform.enqueue_vehicle("appt")
    clock.advance(1.0)
    platform.cycle()

    change = platform.queue_snapshot()["order_changes"][0]
    assert change["before_order"] == ["walk", "appt"]
    assert change["after_order"] == ["appt", "walk"]
    assert change["reason"] == "scheduled_time_reached"
    audit = [
        entry
        for entry in platform.audit.entries()
        if entry["action"] == "order_changed"
    ]
    assert audit
    assert audit[0]["payload"]["change_id"] == change["id"]
    assert audit[0]["payload"]["compensation_ids"]
    assert audit[0]["payload"]["compensations"][0]["vehicle"] == "walk"


def test_urgent_insert_creates_compensation_and_is_request_idempotent(platform):
    platform.enqueue_vehicle("walk1")
    platform.enqueue_vehicle("walk2")
    first = platform.urgent_insert("urgent", request_id="req-1")
    second = platform.urgent_insert("urgent", request_id="req-1")

    assert first["changed"] is True
    assert second["idempotent_replayed"] is True
    assert second["order_change"]["id"] == first["order_change"]["id"]
    assert [item["vehicle"] for item in platform.queue.waiting()] == ["urgent", "walk1", "walk2"]
    assert [item["vehicle"] for item in platform.queue.compensations()] == ["walk1", "walk2"]
    order_audits = [
        entry
        for entry in platform.audit.entries()
        if entry["action"] == "order_changed" and entry["payload"]["change_action"] == "urgent_insert"
    ]
    assert len(order_audits) == 1


def test_appointment_cannot_be_urgently_released_before_due_time(platform, clock):
    platform.book_appointment("appt", clock.now() + 10.0)
    platform.enqueue_vehicle("appt")
    with pytest.raises(InterlockError):
        platform.urgent_insert("appt")
    with pytest.raises(InterlockError):
        platform.release_appointment("appt")


def test_void_no_show_policy_requires_requeue_as_walk_in(platform, clock):
    grace_s = platform.config.queue.appointment_grace_s
    platform.book_appointment("appt", clock.now() + 1.0)
    clock.advance(1.0 + grace_s + 1.0)
    result = platform.cycle()

    assert result["queue_no_shows"][0]["status"] == "void"
    requeued = platform.enqueue_vehicle("appt")
    assert requeued["kind"] == "walk_in"
    assert requeued["status"] == "released"
    assert platform.queue.reservations()[0]["status"] == "void"


def test_retained_no_show_policy_kept_until_shift_end(tmp_path, clock):
    cfg = replace(
        default_config(),
        queue=replace(default_config().queue, no_show_policy="retain_to_shift_end"),
    )
    platform = build_platform(cfg, clock=clock, storage_root=str(tmp_path / "state"))
    grace_s = cfg.queue.appointment_grace_s

    platform.book_appointment("appt", clock.now() + 1.0)
    clock.advance(1.0 + grace_s + 1.0)
    result = platform.cycle()
    assert result["queue_no_shows"][0]["status"] == "no_show"

    platform.enqueue_vehicle("walk")
    platform.enqueue_vehicle("appt")
    assert platform.queue.waiting()[0]["vehicle"] == "walk"

    released = platform.release_appointment("appt")
    assert released["changed"] is True
    assert platform.queue.waiting()[0]["vehicle"] == "appt"

    platform.book_appointment("late", clock.now() + 1.0)
    clock.advance(1.0 + cfg.queue.appointment_grace_s + 1.0)
    platform.cycle()
    closed = platform.close_shift()
    assert closed["changed"] is True


def test_confirmation_accepts_only_the_first(platform, clock):
    confirm = platform.confirm
    confirm.open("v1", clock.now())
    assert confirm.confirm("v1", clock.now() + 1.0) is True
    assert confirm.confirm("v1", clock.now() + 2.0) is False
    assert confirm.outstanding() == []
    confirm.open("v2", clock.now())
    assert confirm.pending("v2") is True
    clock.advance(platform.config.queue.confirm_timeout_s + 1.0)
    assert confirm.expired("v2", clock.now()) is True
    assert confirm.clear("v2") is True
