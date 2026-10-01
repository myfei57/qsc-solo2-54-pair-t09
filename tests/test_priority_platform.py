"""预约/加急在平台层的编排：控制台展示、审计落盘与 HTTP 接口。"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from hydrorefuel.console.server import ConsoleServer


def _get(url: str) -> tuple[int, dict]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


def _post(url: str, payload: dict) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=5) as response:
            return response.status, json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode("utf-8"))


def test_queue_snapshot_carries_policy_and_ledgers(platform, clock):
    platform.enqueue_vehicle("w1")
    slot = clock.now() + 100
    platform.reserve_vehicle("r1", slot)
    platform.arrive_vehicle("r1")
    snapshot = platform.queue_snapshot()
    assert snapshot["policy"]["scheduling_mode"] == "on_time_release"
    assert snapshot["policy"]["noshow_policy"] in ("void", "hold")
    assert snapshot["reservations"][0]["vehicle"] == "r1"
    assert snapshot["urgent_quota"]["remaining"] == platform.config.queue.urgent_quota_per_shift


def test_due_promotion_happens_in_cycle_and_is_audited(platform, clock):
    platform.enqueue_vehicle("w1")
    platform.reserve_vehicle("r1", clock.now() + 50)
    platform.arrive_vehicle("r1")
    clock.advance(50)
    cycle = platform.cycle()
    assert cycle["queue_promoted"] == ["r1"]
    assert platform.queue.position("r1") == 1
    entries = platform.audit.entries()
    assert any(item["action"] == "reservation_due" for item in entries)
    released = platform.dequeue_vehicle()
    assert released["vehicle"] == "r1" and released["reason"] == "reservation_due"
    # 被挤掉的 w1 有补偿记录
    comps = platform.queue_compensations("w1")["compensations"]
    assert comps and comps[0]["caused_by"] == "r1"


def test_order_changes_are_visible_and_journaled(platform, clock):
    platform.enqueue_vehicle("w1")
    platform.urgent_vehicle("u1", reason="应急保障")
    changes = platform.queue_order_changes()["changes"]
    assert any(item["reason"] == "urgent_insert" for item in changes)
    # 审计流水落盘，跨重启可回放
    replayed = platform.audit.replay_from_journal()
    assert any(item["action"] == "urgent_insert" for item in replayed)


def test_console_exposes_priority_endpoints(platform, clock):
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        assert _post(f"{base}/api/enqueue", {"vehicle": "w1"})[0] == 200
        slot = clock.now() + 60
        assert _post(f"{base}/api/reserve", {"vehicle": "r1", "slot_ts": slot})[0] == 200
        assert _post(f"{base}/api/arrive", {"vehicle": "r1"})[0] == 200
        clock.advance(60)
        assert _post(f"{base}/api/step", {"dt": 1.0})[0] == 200
        status, payload = _post(f"{base}/api/release", {"vehicle": "r1"})
        assert status == 200 and payload["reason"] == "reservation_due"
        # 时序：w1 不能越过已放行的 r1，且 r1 重复放行幂等
        dup = _post(f"{base}/api/release", {"vehicle": "r1"})
        assert dup[1]["idempotent"] is True
        status, payload = _get(f"{base}/api/queue/changes")
        assert status == 200 and payload["changes"]
        status, payload = _get(f"{base}/api/queue/compensations?vehicle=w1")
        assert status == 200 and payload["compensations"]
        status, queue = _get(f"{base}/api/queue")
        assert status == 200 and queue["queue"]["released"] == ["r1"]
    finally:
        server.stop()


def test_console_urgent_quota_exhaustion_returns_409(platform):
    quota = platform.config.queue.urgent_quota_per_shift
    platform.enqueue_vehicle("w1")
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        for index in range(quota):
            assert _post(f"{base}/api/urgent", {"vehicle": f"u{index}"})[0] == 200
        status, payload = _post(f"{base}/api/urgent", {"vehicle": "ux"})
        assert status == 409
        assert payload["error"] == "InterlockError"
    finally:
        server.stop()


def test_fill_flow_respects_plugged_protection(platform, clock):
    # 直接走 fill：车辆入队 → 放行 → 插枪 → 加注 → 完成
    report = platform.fill(vehicle="v9", steps=2)
    assert report["ok"] is True
    assert "v9" not in platform.queue.released()
    # 加注期间到点的加急不能挤掉已插枪车：模拟先放行再加急
    platform.enqueue_vehicle("a1")
    platform.dequeue_vehicle()
    platform.queue.mark_plugged("a1", clock.now())
    platform.enqueue_vehicle("a2")
    platform.urgent_vehicle("u1")
    assert platform.queue.plugged() == ["a1"]
    assert all(item["vehicle"] != "a1" for item in platform.queue.compensations())


def test_fill_refuses_vehicle_that_is_not_at_queue_head(platform):
    platform.enqueue_vehicle("w1")
    platform.enqueue_vehicle("w2")
    report = platform.fill(vehicle="w2", steps=1)
    assert report["ok"] is False
    assert report["violations"] == ["queue_not_releasable"]
    # 时序联锁在审计里留痕，且队列原序未被破坏
    assert [item["vehicle"] for item in platform.queue.waiting()] == ["w1", "w2"]
    alarms = platform.audit.alarms()
    assert any("队首" in item["message"] for item in alarms)


def test_release_endpoint_rejects_out_of_order(platform):
    platform.enqueue_vehicle("w1")
    platform.enqueue_vehicle("w2")
    server = ConsoleServer(platform, port=0)
    base = server.start()
    try:
        status, payload = _post(f"{base}/api/release", {"vehicle": "w2"})
        assert status == 409 and payload["component"] == "queue"
    finally:
        server.stop()


def test_hold_station_shift_end_clears_lapsed(clock):
    import dataclasses
    import tempfile

    # 直接从 default config 构造 hold 站点
    from hydrorefuel.app import build_platform
    from hydrorefuel.config import default_config

    hold_cfg = dataclasses.replace(
        default_config(),
        queue=dataclasses.replace(default_config().queue, noshow_policy="hold", grace_s=10),
    )
    station = build_platform(hold_cfg, clock=clock, storage_root=tempfile.mkdtemp())
    station.reserve_vehicle("h1", clock.now() + 5)
    clock.advance(20)
    station.cycle()  # 过号挂起
    result = station.end_shift()
    assert result["voided"] == ["h1"]
    entries = station.audit.entries()
    assert any(item["action"] == "shift_end" for item in entries)
