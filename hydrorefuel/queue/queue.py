"""加注队列：到点放行的优先级队列。

队列里同时有普通车（walkin）、预约车（reserved）和临时加急车（urgent）。
排序规则定死为：过号保留车恒在队尾，其余按层级

    加急 > 已到点预约 > 普通（含未到点预约）

同层级按入队先后（``seq``）稳定排列。预约语义为"到点放行"：预约车在到点
前按入队序排队，到点后由 :meth:`FillQueue.tick` 提序，提序越过的每一辆普通
车都记一条补偿；未到点的预约车不允许提前放行。已放行（出队）或已插枪的车
离开等待队列，此后任何插队都碰不到它们——插队只能改变等待队列的顺序。

所有可能重复触发的动作都幂等：重复预约返回原预约，重复到场登记返回原条目，
加急同车重复不追加配额，放行已放行车辆返回幂等结果。每一次顺序变更都写入
顺序账本（依据 + 前后位次），被挤掉的车写入补偿账本，两份账本都供控制台
展示并经事件总线落审计。
"""

from __future__ import annotations

import threading

from ..config import QueueLimits
from ..errors import InterlockError
from .entry import (
    RESERVATION_ARRIVED,
    RESERVATION_BOOKED,
    RESERVATION_CANCELLED,
    RESERVATION_NOSHOW_HELD,
    RESERVATION_NOSHOW_VOID,
    RESERVATION_RELEASED,
    RESERVATION_SHIFT_VOID,
    Compensation,
    OrderChange,
    QueueEntry,
    Reservation,
)
from .policy import (
    COMP_NOSHOW_HELD,
    COMP_NOSHOW_VOID,
    COMP_OVERTAKEN,
    ORDER_ENQUEUE,
    ORDER_NOSHOW_HOLD,
    ORDER_NOSHOW_VOID,
    ORDER_RELEASE,
    ORDER_RESERVATION_DUE,
    ORDER_SHIFT_END,
    ORDER_URGENT_INSERT,
    RELEASE_FCFS,
    RELEASE_RESERVATION_DUE,
    RELEASE_URGENT,
    TIER_RESERVED,
    TIER_URGENT,
    TIER_WALKIN,
)

_TIER_RANK = {TIER_URGENT: 0, TIER_RESERVED: 1, TIER_WALKIN: 2}
_ACTIVE_RESERVATIONS = (RESERVATION_BOOKED, RESERVATION_ARRIVED)


class FillQueue:
    """带预约时段、到点放行与临时加急的加注队列。"""

    def __init__(self, limits: QueueLimits) -> None:
        self._limits = limits
        self._policy = limits.policy
        self._entries: list[QueueEntry] = []
        self._reservations: dict[str, Reservation] = {}
        self._served: set[str] = set()
        self._released: list[str] = []
        self._plugged: list[str] = []
        self._changes: list[OrderChange] = []
        self._compensations: list[Compensation] = []
        self._seq_counter = 0
        self._change_seq = 0
        self._urgent_used = 0
        self._shift = 1
        self._lock = threading.RLock()

    @property
    def limited(self) -> QueueLimits:
        return self._limits

    # -- 查询 ------------------------------------------------------------

    def contains(self, vehicle: str) -> bool:
        """车辆是否在等待队列中。"""

        with self._lock:
            return any(entry.vehicle == vehicle for entry in self._entries)

    def position(self, vehicle: str) -> int:
        """返回车辆当前位次（从 1 开始），不在等待队列返回 0。"""

        with self._lock:
            for index, entry in enumerate(self._entries):
                if entry.vehicle == vehicle:
                    return index + 1
            return 0

    def size(self) -> int:
        """等待队列长度（含过号保留车）。"""

        with self._lock:
            return len(self._entries)

    def waiting(self) -> list[dict]:
        """返回队列内容，含位次、层级与最近一次顺序变更依据。"""

        with self._lock:
            return [entry.as_dict(index + 1) for index, entry in enumerate(self._entries)]

    def released(self) -> list[str]:
        """已放行（含已插枪）车辆，按放行先后。"""

        with self._lock:
            return list(self._released)

    def plugged(self) -> list[str]:
        """已插枪车辆，按插枪先后，任何插队都不能挤掉它们。"""

        with self._lock:
            return list(self._plugged)

    def reservations(self) -> list[dict]:
        """返回全部预约记录（按到点时刻升序）。"""

        with self._lock:
            ordered = sorted(self._reservations.values(), key=lambda item: item.slot_ts)
            return [item.as_dict() for item in ordered]

    def reservation(self, reservation_id: str) -> dict | None:
        with self._lock:
            item = self._reservations.get(reservation_id)
            return None if item is None else item.as_dict()

    def order_changes(self, limit: int = 200) -> list[dict]:
        """顺序变更账本（最新的在前）。"""

        with self._lock:
            return [change.as_dict() for change in reversed(self._changes[-limit:])]

    def compensations(self, vehicle: str | None = None, limit: int = 200) -> list[dict]:
        """补偿账本，可按车辆过滤（最新的在前）。"""

        with self._lock:
            items = self._compensations if vehicle is None else [c for c in self._compensations if c.vehicle == vehicle]
            return [item.as_dict() for item in reversed(items[-limit:])]

    def urgent_quota(self) -> dict:
        """本班加急配额使用情况。"""

        with self._lock:
            return {
                "shift": self._shift,
                "limit": self._policy.urgent_quota_per_shift,
                "used": self._urgent_used,
                "remaining": self._policy.urgent_quota_per_shift - self._urgent_used,
            }

    # -- 普通入队 --------------------------------------------------------

    def enqueue(self, vehicle: str, now: float) -> int:
        """普通车加入队尾，返回其位次。

        普通追加不改变任何已有车辆的相对次序，因此不产生顺序变更与补偿。
        """

        with self._lock:
            self._require_vehicle(vehicle)
            self._reject_known_vehicle(vehicle)
            self._require_capacity()
            entry = self._add_entry(vehicle, float(now), TIER_WALKIN)
            entry.moved_by = ORDER_ENQUEUE
            return self.position(vehicle)

    # -- 预约 ------------------------------------------------------------

    def reserve(self, vehicle: str, slot_ts: float, now: float, reservation_id: str | None = None) -> dict:
        """登记预约时段（幂等：同车存在生效预约时返回原预约）。

        预约不预留车位：在车辆到场登记前，队列里没有它。
        """

        with self._lock:
            self._require_vehicle(vehicle)
            if vehicle in self._served:
                raise InterlockError(f"车辆 {vehicle} 已完成加注，不能再预约", component="queue")
            for existing in self._reservations.values():
                if existing.vehicle == vehicle and existing.state in _ACTIVE_RESERVATIONS:
                    return {"idempotent": True, **existing.as_dict()}
            slot = float(slot_ts)
            if slot < float(now):
                raise InterlockError("预约时段不能早于当前时刻", component="queue")
            rid = reservation_id or f"rsv-{vehicle}-{int(slot)}"
            if rid in self._reservations:
                raise InterlockError(f"预约号 {rid} 已存在", component="queue")
            record = Reservation(rid, vehicle, slot)
            self._reservations[rid] = record
            return {"idempotent": False, **record.as_dict()}

    def arrive(self, vehicle: str, now: float, reservation_id: str | None = None) -> dict:
        """预约车到场登记并入队（幂等：已在队列返回当前位次）。

        ``hold`` 站点过号后迟到：原预约已挂起，车辆按普通车重新入队，
        条目仍带预约号便于对账，但不再享受任何提序。
        """

        with self._lock:
            existing = self._find_entry(vehicle)
            if existing is not None:
                return {
                    "idempotent": True,
                    "vehicle": vehicle,
                    "position": self.position(vehicle),
                    "reservation_id": existing.reservation_id,
                }
            self._reject_known_vehicle(vehicle)
            moment = float(now)
            held: Reservation | None = None
            record: Reservation | None
            if reservation_id is not None:
                record = self._reservations.get(reservation_id)
                if record is None:
                    raise InterlockError(f"预约号 {reservation_id} 不存在", component="queue")
                if record.vehicle != vehicle:
                    raise InterlockError(f"预约号 {reservation_id} 不属于车辆 {vehicle}", component="queue")
                if record.state == RESERVATION_NOSHOW_HELD:
                    held, record = record, None
                elif record.state not in _ACTIVE_RESERVATIONS:
                    raise InterlockError(f"预约 {reservation_id} 已处于 {record.state}", component="queue")
            else:
                active = [
                    item
                    for item in self._reservations.values()
                    if item.vehicle == vehicle and item.state in _ACTIVE_RESERVATIONS
                ]
                if active:
                    active.sort(key=lambda item: item.slot_ts)
                    record = active[0]
                else:
                    held_records = [
                        item
                        for item in self._reservations.values()
                        if item.vehicle == vehicle and item.state == RESERVATION_NOSHOW_HELD
                    ]
                    if held_records and self._policy.holds_lapsed():
                        held_records.sort(key=lambda item: item.slot_ts)
                        held, record = held_records[0], None
                    else:
                        raise InterlockError(
                            f"车辆 {vehicle} 没有生效预约，请先登记预约或按普通车排队",
                            component="queue",
                        )
            self._require_capacity()
            if held is not None:
                entry = self._add_entry(
                    vehicle,
                    moment,
                    TIER_WALKIN,
                    slot_ts=held.slot_ts,
                    reservation_id=held.reservation_id,
                )
                entry.lapsed = True
                entry.moved_by = ORDER_NOSHOW_HOLD
                self._resort(moment, ORDER_NOSHOW_HOLD, held.vehicle)
                return {
                    "idempotent": False,
                    "vehicle": vehicle,
                    "position": self.position(vehicle),
                    "reservation_id": held.reservation_id,
                    "slot_ts": round(held.slot_ts, 3),
                    "late_held": True,
                }
            assert record is not None
            entry = self._add_entry(vehicle, moment, TIER_RESERVED, slot_ts=record.slot_ts, reservation_id=record.reservation_id)
            record.state = RESERVATION_ARRIVED
            if moment >= record.slot_ts:
                # 踩着点或晚到场（宽限期内）：入队即到点，立即提序并给被越过车补偿。
                entry.rank_ts = record.slot_ts
                self._resort(moment, ORDER_RESERVATION_DUE, entry.vehicle)
            else:
                entry.moved_by = ORDER_ENQUEUE
            return {
                "idempotent": False,
                "vehicle": vehicle,
                "position": self.position(vehicle),
                "reservation_id": record.reservation_id,
                "slot_ts": round(record.slot_ts, 3),
            }

    def cancel_reservation(self, reservation_id: str, now: float) -> dict:
        """取消一个尚未放行的预约。"""

        with self._lock:
            record = self._reservations.get(reservation_id)
            if record is None:
                raise InterlockError(f"预约号 {reservation_id} 不存在", component="queue")
            if record.state not in _ACTIVE_RESERVATIONS:
                raise InterlockError(f"预约 {reservation_id} 已处于 {record.state}，不能取消", component="queue")
            record.state = RESERVATION_CANCELLED
            entry = self._find_entry(record.vehicle)
            if entry is not None and entry.reservation_id == reservation_id:
                # 已到场但还没到点：降级成普通车，留在原位不影响他人。
                entry.tier = TIER_WALKIN
                entry.rank_ts = None
                entry.reservation_id = None
                entry.slot_ts = None
                entry.moved_by = "reservation_cancelled"
                self._resort(float(now), "reservation_cancelled", entry.vehicle)
            return record.as_dict()

    # -- 放行（出队） ----------------------------------------------------

    def release(self, vehicle: str | None = None, now: float | None = None) -> dict:
        """放行队首车辆；指定车辆时只允许放行队首那辆（顺序时序不可乱）。

        幂等：车辆已放行时返回 ``idempotent=True``，不重复出队、不重复补偿。
        已插枪车辆不在等待队列，任何放行都不可能挤掉它们。
        """

        with self._lock:
            moment = float(now) if now is not None else 0.0
            if vehicle is not None and vehicle in self._released:
                return self._idempotent_release(vehicle)
            self._tick_locked(moment)
            if not self._entries:
                raise InterlockError("加注队列为空", component="queue")
            head = self._entries[0]
            if head.lapsed:
                raise InterlockError(f"车辆 {head.vehicle} 已过号保留到本班末，不能放行", component="queue")
            if vehicle is not None and vehicle != head.vehicle:
                raise InterlockError(
                    f"只能放行当前队首 {head.vehicle}，{vehicle} 排在第 {self.position(vehicle) or '?'} 位",
                    component="queue",
                )
            if head.tier == TIER_RESERVED and head.rank_ts is None:
                slot = head.slot_ts if head.slot_ts is not None else moment
                if moment < slot:
                    raise InterlockError(
                        f"预约车 {head.vehicle} 未到点（{round(slot - moment, 1)}s 后），到点放行不可提前",
                        component="queue",
                    )
            reason, reservation_released = self._release_reason(head)
            self._entries.pop(0)
            self._released.append(head.vehicle)
            if reservation_released:
                record = self._reservations.get(reservation_released)
                if record is not None:
                    record.state = RESERVATION_RELEASED
            self._log_change(
                moment,
                ORDER_RELEASE,
                head.vehicle,
                0,
                0,
                {"tier": head.tier, "release_reason": reason},
            )
            return {
                "idempotent": False,
                "vehicle": head.vehicle,
                "tier": head.tier,
                "reason": reason,
                "reservation_id": head.reservation_id,
                "size": len(self._entries),
            }

    def dequeue(self) -> str:
        """取出当前队首（兼容旧调用；新代码请用 :meth:`release` 以拿到依据）。"""

        with self._lock:
            moment = self._reference_now()
            result = self.release(now=moment)
            return str(result["vehicle"])

    def _reference_now(self) -> float:
        """无显式时钟时，用各条目的入队/到点时刻推断当前时刻。"""

        moments = [entry.enqueued_ts for entry in self._entries]
        moments.extend(entry.rank_ts for entry in self._entries if entry.rank_ts is not None)
        return max(moments, default=0.0)

    def mark_plugged(self, vehicle: str, now: float) -> dict:
        """登记车辆已插枪（幂等）。插枪后它脱离插队竞争，谁也挤不掉。"""

        with self._lock:
            if vehicle not in self._released:
                raise InterlockError(f"车辆 {vehicle} 尚未放行，不能登记插枪", component="queue")
            if vehicle in self._plugged:
                return {"idempotent": True, "vehicle": vehicle, "plugged": True}
            self._plugged.append(vehicle)
            return {"idempotent": False, "vehicle": vehicle, "plugged": True, "ts": round(float(now), 3)}

    def complete_serve(self, vehicle: str, now: float) -> dict:
        """完成一辆车的加注（幂等），释放其放行/插枪占用。"""

        with self._lock:
            if vehicle in self._served:
                return {"idempotent": True, "vehicle": vehicle, "served": True}
            if vehicle not in self._released:
                raise InterlockError(f"车辆 {vehicle} 未放行，没有进行中的加注", component="queue")
            self._served.add(vehicle)
            self._released = [item for item in self._released if item != vehicle]
            self._plugged = [item for item in self._plugged if item != vehicle]
            return {"idempotent": False, "vehicle": vehicle, "served": True, "ts": round(float(now), 3)}

    # -- 临时加急 --------------------------------------------------------

    def urgent(self, vehicle: str, now: float, reason: str = "") -> dict:
        """临时加急插队（幂等：同车已在队则只提序一次）。

        加急只能越过"还在等待"的车；已放行、已插枪的车不在等待队列，加急
        不可能越过它们。多个加急车彼此按提出先后排队，后来的加急不能越过
        先前放行的任何车（放行先于本次加急发生，时序不回溯）。
        """

        with self._lock:
            self._require_vehicle(vehicle)
            moment = float(now)
            existing = self._find_entry(vehicle)
            if existing is not None:
                if existing.tier == TIER_URGENT:
                    return {"idempotent": True, "vehicle": vehicle, "position": self.position(vehicle)}
                if existing.lapsed:
                    raise InterlockError(f"车辆 {vehicle} 已过号，不能加急", component="queue")
                if self._urgent_used >= self._policy.urgent_quota_per_shift:
                    raise InterlockError("本班临时加急配额已用完", component="queue")
                self._urgent_used += 1
                existing.tier = TIER_URGENT
                existing.rank_ts = moment
                existing.moved_by = ORDER_URGENT_INSERT
                self._resort(moment, ORDER_URGENT_INSERT, vehicle, detail={"reason": reason})
                return {"idempotent": False, "vehicle": vehicle, "position": self.position(vehicle)}
            self._reject_known_vehicle(vehicle)
            if self._urgent_used >= self._policy.urgent_quota_per_shift:
                raise InterlockError("本班临时加急配额已用完", component="queue")
            self._require_capacity()
            self._urgent_used += 1
            entry = self._add_entry(vehicle, moment, TIER_URGENT, rank_ts=moment)
            entry.moved_by = ORDER_URGENT_INSERT
            self._resort(moment, ORDER_URGENT_INSERT, vehicle, detail={"reason": reason})
            return {"idempotent": False, "vehicle": vehicle, "position": self.position(vehicle)}

    # -- 周期推进：到点提序 / 过号 / 交班 -------------------------------

    def tick(self, now: float) -> dict:
        """推进一个控制周期：到点预约提序、过号按站点策略处置。"""

        with self._lock:
            return self._tick_locked(float(now))

    def end_shift(self, now: float) -> dict:
        """交班：作废本班全部过号保留车（含挂起未到的预约），配额随新班重置。"""

        with self._lock:
            moment = float(now)
            lapsed = [entry for entry in self._entries if entry.lapsed]
            vehicles = [entry.vehicle for entry in lapsed]
            for entry in lapsed:
                self._void_lapsed(entry, moment, ORDER_SHIFT_END)
            for record in self._reservations.values():
                if record.state == RESERVATION_NOSHOW_HELD:
                    record.state = RESERVATION_SHIFT_VOID
                    if record.vehicle not in vehicles:
                        self._log_change(
                            moment, ORDER_SHIFT_END, record.vehicle, 0, 0,
                            {"reservation_id": record.reservation_id},
                        )
                        vehicles.append(record.vehicle)
            self._shift += 1
            self._urgent_used = 0
            return {"shift": self._shift, "voided": vehicles}

    # -- 内部 ------------------------------------------------------------

    def _tick_locked(self, now: float) -> dict:
        lapsed: list[str] = []
        noshows: list[dict] = []
        promoted: list[str] = []

        # 先处理过号：宽限期 = 到点时刻 + grace_s。未到场的预约作"过号未到"，
        # 已到场却仍在等待的预约车作"过号滞留"。
        deadline = now - self._policy.grace_s
        for record in list(self._reservations.values()):
            if record.state not in _ACTIVE_RESERVATIONS or record.slot_ts > deadline:
                continue
            entry = self._find_entry(record.vehicle)
            if entry is None or record.state == RESERVATION_BOOKED:
                self._noshow(record, now)
                noshows.append(record.as_dict())
                continue
            self._lapse(entry, record, now)
            lapsed.append(entry.vehicle)

        # 再做到点提序：到点放行在每个周期都可能改变顺序。
        for entry in self._entries:
            if (
                entry.tier == TIER_RESERVED
                and not entry.lapsed
                and entry.rank_ts is None
                and entry.slot_ts is not None
                and now >= entry.slot_ts
            ):
                entry.rank_ts = entry.slot_ts
                entry.moved_by = ORDER_RESERVATION_DUE
                promoted.append(entry.vehicle)
        if promoted:
            self._resort(now, ORDER_RESERVATION_DUE, ",".join(promoted))
        return {"promoted": promoted, "lapsed": lapsed, "noshows": noshows}

    def _noshow(self, record: Reservation, now: float) -> None:
        """预约车到点（含宽限）未到场：作废或挂起到本班末。"""

        if self._policy.holds_lapsed():
            record.state = RESERVATION_NOSHOW_HELD
            self._compensations.append(
                Compensation(
                    now,
                    record.vehicle,
                    COMP_NOSHOW_HELD,
                    ORDER_NOSHOW_HOLD,
                    {"reservation_id": record.reservation_id, "slot_ts": round(record.slot_ts, 3)},
                )
            )
            self._log_change(now, ORDER_NOSHOW_HOLD, record.vehicle, 0, 0, {"reservation_id": record.reservation_id})
        else:
            record.state = RESERVATION_NOSHOW_VOID
            self._compensations.append(
                Compensation(
                    now,
                    record.vehicle,
                    COMP_NOSHOW_VOID,
                    ORDER_NOSHOW_VOID,
                    {"reservation_id": record.reservation_id, "slot_ts": round(record.slot_ts, 3)},
                )
            )
            self._log_change(now, ORDER_NOSHOW_VOID, record.vehicle, 0, 0, {"reservation_id": record.reservation_id})

    def _lapse(self, entry: QueueEntry, record: Reservation, now: float) -> None:
        """已到场但过号仍在等待：按站点策略作废或沉到队尾保留。"""

        if self._policy.holds_lapsed():
            record.state = RESERVATION_NOSHOW_HELD
            entry.lapsed = True
            entry.tier = TIER_WALKIN
            entry.moved_by = ORDER_NOSHOW_HOLD
            self._compensations.append(
                Compensation(
                    now,
                    entry.vehicle,
                    COMP_NOSHOW_HELD,
                    ORDER_NOSHOW_HOLD,
                    {"reservation_id": record.reservation_id},
                )
            )
            self._resort(now, ORDER_NOSHOW_HOLD, entry.vehicle)
        else:
            record.state = RESERVATION_NOSHOW_VOID
            self._entries = [item for item in self._entries if item.vehicle != entry.vehicle]
            self._compensations.append(
                Compensation(
                    now,
                    entry.vehicle,
                    COMP_NOSHOW_VOID,
                    ORDER_NOSHOW_VOID,
                    {"reservation_id": record.reservation_id},
                )
            )
            self._log_change(now, ORDER_NOSHOW_VOID, entry.vehicle, 0, 0, {"reservation_id": record.reservation_id})

    def _void_lapsed(self, entry: QueueEntry, now: float, reason: str) -> None:
        """交班时把过号保留车作废重排（移出队列，需重新取号）。"""

        self._entries = [item for item in self._entries if item.vehicle != entry.vehicle]
        if entry.reservation_id is not None:
            record = self._reservations.get(entry.reservation_id)
            if record is not None:
                record.state = RESERVATION_SHIFT_VOID
        self._log_change(now, reason, entry.vehicle, 0, 0, {"reservation_id": entry.reservation_id})

    def _resort(self, now: float, reason: str, actor: str, detail: dict | None = None) -> None:
        """按当前层级重排等待队列，并记录顺序变更与越位补偿。

        位次真正变化的每一辆车都写一条顺序变更；在到点提序/加急插队中被
        优先级车越过的每一辆车，按"被谁越过"逐条写补偿。过号沉底带来的
        顺位前移不产生补偿——那不是优先级挤压。
        """

        before = {entry.vehicle: index + 1 for index, entry in enumerate(self._entries)}
        self._entries.sort(key=self._sort_key)
        after = {entry.vehicle: index + 1 for index, entry in enumerate(self._entries)}
        new_order = [entry.vehicle for entry in self._entries]

        overtakers: list[str] = []
        for entry in self._entries:
            old, new = before[entry.vehicle], after[entry.vehicle]
            if old == new:
                continue
            change_detail = dict(detail or {})
            change_detail.setdefault("caused_by", actor)
            self._log_change(now, reason, entry.vehicle, old, new, change_detail)
            if new < old:
                entry.moved_by = reason
                overtakers.append(entry.vehicle)
            elif entry.vehicle == actor or reason == ORDER_NOSHOW_HOLD:
                # 动作主体自身后移（取消预约降级、过号沉底），原因就是动作本身。
                entry.moved_by = reason
            else:
                # 被挤后移的车，记录的是"被谁挤"而不是它自己的动作。
                entry.moved_by = f"overtaken_by:{actor}"

        if reason in (ORDER_RESERVATION_DUE, ORDER_URGENT_INSERT):
            for entry in self._entries:
                old, new = before[entry.vehicle], after[entry.vehicle]
                if new <= old or entry.lapsed:
                    continue
                for cause in new_order[: new - 1]:
                    if cause not in overtakers:
                        continue
                    self._compensations.append(
                        Compensation(
                            now,
                            entry.vehicle,
                            COMP_OVERTAKEN,
                            cause,
                            {
                                "reason": reason,
                                "before": old,
                                "after": new,
                                "reservation_id": entry.reservation_id,
                                "tier": entry.tier,
                            },
                        )
                    )

    def _sort_key(self, entry: QueueEntry) -> tuple:
        # 未到点的预约与普通车同序（到点放行，不提前占位）；到点后进入预约层级。
        if entry.lapsed:
            return (3, entry.seq)
        if entry.tier == TIER_RESERVED and entry.rank_ts is None:
            return (2, entry.seq)
        return (_TIER_RANK[entry.tier], entry.seq)

    def _release_reason(self, head: QueueEntry) -> tuple[str, str | None]:
        if head.tier == TIER_URGENT:
            return RELEASE_URGENT, head.reservation_id
        if head.tier == TIER_RESERVED and head.rank_ts is not None:
            return RELEASE_RESERVATION_DUE, head.reservation_id
        return RELEASE_FCFS, head.reservation_id

    def _idempotent_release(self, vehicle: str) -> dict:
        entry_record = None
        for record in self._reservations.values():
            if record.vehicle == vehicle:
                entry_record = record
                break
        return {
            "idempotent": True,
            "vehicle": vehicle,
            "tier": None,
            "reason": "already_released",
            "reservation_id": entry_record.reservation_id if entry_record else None,
            "size": len(self._entries),
        }

    def _add_entry(
        self,
        vehicle: str,
        now: float,
        tier: str,
        *,
        rank_ts: float | None = None,
        slot_ts: float | None = None,
        reservation_id: str | None = None,
    ) -> QueueEntry:
        self._seq_counter += 1
        entry = QueueEntry(
            vehicle=vehicle,
            seq=self._seq_counter,
            enqueued_ts=now,
            tier=tier,
            rank_ts=rank_ts,
            slot_ts=slot_ts,
            reservation_id=reservation_id,
        )
        self._entries.append(entry)
        return entry

    def _find_entry(self, vehicle: str) -> QueueEntry | None:
        for entry in self._entries:
            if entry.vehicle == vehicle:
                return entry
        return None

    def _reject_known_vehicle(self, vehicle: str) -> None:
        if self._find_entry(vehicle) is not None:
            raise InterlockError(f"车辆 {vehicle} 已在队列中", component="queue")
        if vehicle in self._released:
            raise InterlockError(f"车辆 {vehicle} 已放行待加注或已插枪，不能重复入队", component="queue")
        if vehicle in self._served:
            raise InterlockError(f"车辆 {vehicle} 已完成加注", component="queue")

    def _require_capacity(self) -> None:
        if len(self._entries) >= self._limits.max_waiting:
            raise InterlockError("加注队列已满", component="queue")

    @staticmethod
    def _require_vehicle(vehicle: str) -> None:
        if not vehicle:
            raise InterlockError("车辆标识不能为空", component="queue")

    def _log_change(self, now: float, reason: str, vehicle: str, before: int, after: int, detail: dict) -> None:
        self._change_seq += 1
        self._changes.append(OrderChange(float(now), self._change_seq, reason, vehicle, before, after, dict(detail)))

    def reset(self) -> int:
        """清空等待队列与预约（补偿与顺序账本保留，审计不丢）。"""

        with self._lock:
            cleared = len(self._entries)
            self._entries.clear()
            self._reservations.clear()
            self._released.clear()
            self._plugged.clear()
            self._served.clear()
            self._urgent_used = 0
            return cleared
