"""预约时段、到点放行、临时加急与过号处置。

规矩一览（与站点策略对应）：

- 预约语义定死为"到点放行"：未到点按入队序排队且不许提前放行，到点提序，
  被越过的普通车逐条拿补偿；
- 已放行、已插枪的车离开等待队列，任何加急都挤不掉它们；
- 加急只能放行队首，指名放行非队首被拒（插队与放行的时序不能乱）；
- 过号按站点策略处置：void 作废重排、hold 保留到本班末；
- 每次顺序变更都进顺序账本（依据 + 前后位次），被挤车进补偿账本；
- 预约、到场、加急、放行、插枪全部幂等。
"""

from __future__ import annotations

import dataclasses
import threading

import pytest

from hydrorefuel.config import default_config
from hydrorefuel.errors import ConfigError, InterlockError
from hydrorefuel.queue.policy import NOSHOW_HOLD, NOSHOW_VOID


def _queue(clock, *, noshow_policy=NOSHOW_VOID, grace_s=60.0, urgent_quota=2, max_waiting=12):
    cfg = default_config()
    queue_cfg = dataclasses.replace(
        cfg.queue,
        noshow_policy=noshow_policy,
        grace_s=grace_s,
        urgent_quota_per_shift=urgent_quota,
        max_waiting=max_waiting,
    )
    from hydrorefuel.queue.queue import FillQueue

    return FillQueue(queue_cfg)


def test_reservation_does_not_reserve_a_slot_before_due(clock):
    queue = _queue(clock)
    queue.reserve("r1", clock.now() + 100, clock.now())
    # 没到场就不在队列，不占车位
    assert queue.size() == 0
    assert queue.arrive("r1", clock.now())["position"] == 1
    queue.enqueue("w1", clock.now() + 1)
    # 未到点：按先来后到，预约车虽早到也不能越过 w1……这里 r1 更早，排第 1
    assert [item["vehicle"] for item in queue.waiting()] == ["r1", "w1"]


def test_reservation_waiting_turn_is_fcfs_before_due(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    queue.reserve("r1", clock.now() + 100, clock.now())
    queue.arrive("r1", clock.now() + 1)
    assert [item["vehicle"] for item in queue.waiting()] == ["w1", "w2", "r1"]


def test_due_reservation_is_promoted_and_others_get_compensation(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    queue.reserve("r1", clock.now() + 100, clock.now())
    queue.arrive("r1", clock.now() + 1)
    clock.advance(100)
    tick = queue.tick(clock.now())
    assert tick["promoted"] == ["r1"]
    assert queue.position("r1") == 1
    moved = [item["vehicle"] for item in queue.waiting()]
    assert moved == ["r1", "w1", "w2"]
    comp = queue.compensations()
    by_car = {item["vehicle"]: item for item in comp}
    assert by_car["w1"]["caused_by"] == "r1"
    assert by_car["w1"]["detail"]["before"] == 1 and by_car["w1"]["detail"]["after"] == 2
    assert by_car["w2"]["detail"]["before"] == 2 and by_car["w2"]["detail"]["after"] == 3
    # 顺序账本给出每辆车的前后位次与依据
    changes = queue.order_changes()
    assert all(item["reason"] == "reservation_due" for item in changes)
    assert {item["vehicle"] for item in changes} == {"r1", "w1", "w2"}
    r1 = next(item for item in changes if item["vehicle"] == "r1")
    assert (r1["before"], r1["after"]) == (3, 1)


def test_reservation_cannot_be_released_before_due(clock):
    queue = _queue(clock)
    queue.reserve("r1", clock.now() + 100, clock.now())
    queue.arrive("r1", clock.now())
    with pytest.raises(InterlockError):
        queue.release(now=clock.now())
    # 指名提前放行同样拒绝
    with pytest.raises(InterlockError):
        queue.release(vehicle="r1", now=clock.now())


def test_release_at_due_uses_reservation_reason(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.reserve("r1", clock.now() + 50, clock.now())
    queue.arrive("r1", clock.now())
    clock.advance(50)
    result = queue.release(now=clock.now())
    assert result["vehicle"] == "r1"
    assert result["reason"] == "reservation_due"
    # 普通车再走时是 fcfs
    assert queue.release(now=clock.now())["reason"] == "fcfs"


def test_release_is_idempotent(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.reserve("r1", clock.now() + 10, clock.now())
    queue.arrive("r1", clock.now())
    clock.advance(10)
    first = queue.release(now=clock.now())
    assert first["idempotent"] is False
    again = queue.release(vehicle="r1", now=clock.now())
    assert again["idempotent"] is True
    # 已放行车不能重复入队
    with pytest.raises(InterlockError):
        queue.enqueue("r1", clock.now())


def test_only_head_may_be_named_for_release(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    with pytest.raises(InterlockError):
        queue.release(vehicle="w2", now=clock.now())
    assert queue.release(vehicle="w1", now=clock.now())["vehicle"] == "w1"


def test_urgent_insert_overtakes_only_waiting_cars(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    queue.reserve("r1", clock.now() + 100, clock.now())
    queue.arrive("r1", clock.now())
    # 先放行 w1（已离开等待队列，已插枪）
    queue.release(vehicle="w1", now=clock.now())
    queue.mark_plugged("w1", clock.now())
    result = queue.urgent("u1", clock.now(), reason="应急")
    assert result["position"] == 1
    head = queue.release(now=clock.now())
    assert head["vehicle"] == "u1" and head["reason"] == "urgent_priority"
    # w1 全程不在被挤补偿里——它已插枪，谁也挤不掉
    assert all(item["vehicle"] != "w1" for item in queue.compensations())
    assert all(item["caused_by"] != "w1" for item in queue.compensations())
    # 被挤的是仍在等待的 w2 / r1
    overtaken = {c["vehicle"] for c in queue.compensations() if c["kind"] == "overtaken"}
    assert overtaken == {"w2", "r1"}
    assert "u1" in queue.plugged() or queue.mark_plugged("u1", clock.now())["plugged"]
    # 插枪后加急自己也不可再被别的加急影响：再放一辆加急只能排它之后
    queue.urgent("u2", clock.now())
    assert queue.position("u2") == 1  # u1 已放行离开等待队列，u2 是队首


def test_urgent_insert_is_idempotent_and_quota_bounded(clock):
    queue = _queue(clock, urgent_quota=1)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    first = queue.urgent("u1", clock.now())
    assert first["idempotent"] is False
    second = queue.urgent("u1", clock.now() + 1)
    assert second["idempotent"] is True
    assert queue.urgent_quota()["used"] == 1
    with pytest.raises(InterlockError):
        queue.urgent("u3", clock.now())
    # 交班后配额重置
    queue.end_shift(clock.now())
    assert queue.urgent_quota()["used"] == 0 and queue.urgent_quota()["remaining"] == 1


def test_urgent_cannot_overtake_already_released_car(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.release(now=clock.now())  # w1 已放行
    queue.enqueue("w2", clock.now())
    queue.urgent("u1", clock.now())
    # 队首是 u1，但 w1 的放行顺序在前；加急不能让 u1 插回 w1 前面——
    # 体现在 released 顺序不可变
    assert queue.released() == ["w1"]
    queue.mark_plugged("w1", clock.now())
    assert queue.plugged() == ["w1"]
    # 完成 w1 后才能轮到 u1
    queue.complete_serve("w1", clock.now())
    assert queue.release(now=clock.now())["vehicle"] == "u1"


def test_noshow_void_policy_removes_booking(clock):
    queue = _queue(clock, noshow_policy=NOSHOW_VOID, grace_s=30)
    queue.enqueue("w1", clock.now())
    queue.reserve("r1", clock.now() + 10, clock.now())
    clock.advance(41)
    tick = queue.tick(clock.now())
    assert tick["noshows"][0]["state"] == "noshow_void"
    record = queue.reservation(tick["noshows"][0]["reservation_id"])
    assert record["state"] == "noshow_void"
    # 作废车留下补偿记录，可按车查到
    comp = queue.compensations("r1")
    assert comp and comp[0]["kind"] == "noshow_void"
    # 重新到场只能按普通车重排：arrive 直接拒绝，应普通 enqueue
    with pytest.raises(InterlockError):
        queue.arrive("r1", clock.now())
    queue.enqueue("r1", clock.now())
    assert queue.position("r1") == queue.size()


def test_noshow_hold_policy_keeps_until_shift_end(clock):
    queue = _queue(clock, noshow_policy=NOSHOW_HOLD, grace_s=30)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    queue.enqueue("w3", clock.now())
    queue.reserve("r1", clock.now() + 10, clock.now())
    queue.arrive("r1", clock.now())
    clock.advance(41)
    tick = queue.tick(clock.now())
    assert tick["lapsed"] == ["r1"]
    # 沉到队尾（在 w3 之后），且不能放行
    waiting = [item["vehicle"] for item in queue.waiting()]
    assert waiting == ["w1", "w2", "w3", "r1"]
    for expected in ("w1", "w2", "w3"):
        assert queue.release(now=clock.now() + 1)["vehicle"] == expected
    # 只剩过号保留车：任何放行都拒绝
    with pytest.raises(InterlockError):
        queue.release(now=clock.now() + 1)
    # 交班作废，需重新取号
    shift = queue.end_shift(clock.now() + 1)
    assert shift["voided"] == ["r1"]
    assert queue.size() == 0


def test_held_noshow_late_arrival_rejoins_at_tail_as_walkin(clock):
    queue = _queue(clock, noshow_policy=NOSHOW_HOLD, grace_s=30)
    queue.enqueue("w1", clock.now())
    queue.reserve("r1", clock.now() + 10, clock.now())  # 从不到场
    clock.advance(41)
    queue.tick(clock.now())
    # 迟到：按普通车入队尾，带预约号但 lapsed，只能等 w1 走完
    result = queue.arrive("r1", clock.now())
    assert result["late_held"] is True
    assert result["position"] == 2
    assert queue.waiting()[-1]["lapsed"] is True
    assert queue.release(now=clock.now())["vehicle"] == "w1"
    with pytest.raises(InterlockError):
        queue.release(now=clock.now())


def test_held_and_booked_noshow_both_voided_at_shift_end(clock):
    queue = _queue(clock, noshow_policy=NOSHOW_HOLD, grace_s=10)
    queue.reserve("a1", clock.now() + 5, clock.now())
    clock.advance(20)
    queue.tick(clock.now())  # a1 未到场，预约挂起
    shift = queue.end_shift(clock.now())
    assert shift["voided"] == ["a1"]
    assert queue.reservation("rsv-a1-" + str(int(clock.now()) - 15))["state"] == "shift_end_void"


def test_plugged_vehicle_is_protected_from_all_reordering(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.release(now=clock.now())
    queue.mark_plugged("w1", clock.now())
    queue.enqueue("w2", clock.now())
    queue.reserve("r1", clock.now() + 10, clock.now())
    queue.arrive("r1", clock.now())
    clock.advance(10)
    queue.tick(clock.now())
    queue.urgent("u1", clock.now())
    assert queue.plugged() == ["w1"]
    reordering = [item for item in queue.order_changes() if item["reason"] != "release"]
    assert "w1" not in [item["vehicle"] for item in reordering]


def test_reserve_and_arrive_are_idempotent(clock):
    queue = _queue(clock)
    first = queue.reserve("r1", clock.now() + 50, clock.now(), reservation_id="X1")
    dup = queue.reserve("r1", clock.now() + 99, clock.now(), reservation_id="X2")
    assert dup["idempotent"] is True and dup["reservation_id"] == "X1"
    a1 = queue.arrive("r1", clock.now(), reservation_id="X1")
    a2 = queue.arrive("r1", clock.now() + 1, reservation_id="X1")
    assert a2["idempotent"] is True and a2["position"] == a1["position"]


def test_unknown_reservation_and_mismatched_vehicle_are_rejected(clock):
    queue = _queue(clock)
    queue.reserve("r1", clock.now() + 10, clock.now(), reservation_id="X1")
    with pytest.raises(InterlockError):
        queue.arrive("r9", clock.now(), reservation_id="X1")
    with pytest.raises(InterlockError):
        queue.arrive("r1", clock.now(), reservation_id="NOPE")


def test_capacity_still_limited(clock):
    queue = _queue(clock, max_waiting=2)
    queue.enqueue("w1", clock.now())
    queue.enqueue("w2", clock.now())
    with pytest.raises(InterlockError):
        queue.enqueue("w3", clock.now())


def test_duelling_due_reservations_stay_in_enqueue_order(clock):
    queue = _queue(clock)
    queue.enqueue("w1", clock.now())
    queue.reserve("r1", clock.now() + 100, clock.now())
    queue.reserve("r2", clock.now() + 100, clock.now())
    queue.arrive("r1", clock.now() + 1)
    queue.arrive("r2", clock.now() + 2)
    clock.advance(100)
    queue.tick(clock.now())
    # 同层级按入队先后，r1 先于 r2
    assert [item["vehicle"] for item in queue.waiting()[:2]] == ["r1", "r2"]


def test_scheduling_mode_is_pinned_to_on_time_release():
    cfg = dataclasses.replace(default_config().queue, scheduling_mode="reserve_slot")
    from hydrorefuel.config import QueueLimits

    with pytest.raises(ConfigError):
        QueueLimits.from_mapping(
            {
                "max_waiting": cfg.max_waiting,
                "confirm_timeout_s": cfg.confirm_timeout_s,
                "scheduling_mode": "reserve_slot",
                "noshow_policy": "void",
                "grace_s": 10.0,
                "urgent_quota_per_shift": 1,
            }
        )


def test_concurrent_releases_converge_to_one_dequeue(clock):
    queue = _queue(clock)
    for name in ("w1", "w2", "w3"):
        queue.enqueue(name, clock.now())

    outcomes: list[tuple[str, str]] = []
    guard = threading.Lock()

    def worker(vehicle):
        try:
            result = queue.release(vehicle=vehicle, now=clock.now())
            kind = "idempotent" if result["idempotent"] else "released"
        except InterlockError:
            kind = "rejected"
        with guard:
            outcomes.append((vehicle, kind))

    # 两个线程同时抢队首 w1：一个出队，另一个必须看到幂等结果而不是再出一辆。
    t1 = threading.Thread(target=worker, args=("w1",))
    t2 = threading.Thread(target=worker, args=("w1",))
    t1.start(); t2.start(); t1.join(); t2.join()
    assert sorted(outcomes) == [("w1", "idempotent"), ("w1", "released")]
    assert queue.size() == 2

    # 指名非队首：在任何并发时序下都被时序联锁拒绝。
    outcomes.clear()
    t3 = threading.Thread(target=worker, args=("w3",))
    t4 = threading.Thread(target=worker, args=("w2",))  # w2 是当前队首
    t3.start(); t4.start(); t3.join(); t4.join()
    kinds = dict(outcomes)
    assert kinds["w3"] == "rejected"
    assert kinds["w2"] == "released"
    assert queue.size() == 1
    assert queue.released() == ["w1", "w2"]
