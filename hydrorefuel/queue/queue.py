"""加注队列、预约时段与优先放行。

预约策略固定为“到点放行”：未到预约时间不预留排队位置，也不占用加注能力；
车辆到场入队后，到点才获得预约优先。已经被叫号或开始服务的车辆不受后续重排
影响，任何插队都只能调整仍在等待的车辆。
"""

from __future__ import annotations

import threading
from dataclasses import dataclass

from ..config import QueueLimits
from ..errors import InterlockError

WALK_IN = "walk_in"
APPOINTMENT = "appointment"
URGENT = "urgent"

STATUS_QUEUED = "queued"
STATUS_RELEASED = "released"
STATUS_NO_SHOW = "no_show"
STATUS_ACTIVE = "active"

POLICY_RELEASE_AT_DUE = "release_at_due_time"
NO_SHOW_VOID = "void_and_requeue"
NO_SHOW_RETAIN = "retain_to_shift_end"


@dataclass
class Entry:
    """队列中的一辆车。"""

    vehicle: str
    kind: str
    enqueued_at: float
    seq: int
    status: str = STATUS_RELEASED
    scheduled_at: float | None = None
    released_at: float | None = None
    promoted_at: float | None = None
    reservation: "Reservation | None" = None

    def effective_at(self) -> float:
        """同类优先次序使用的生效时刻。"""

        if self.kind == URGENT:
            return self.promoted_at if self.promoted_at is not None else self.enqueued_at
        if self.kind == APPOINTMENT and self.scheduled_at is not None:
            return self.scheduled_at
        return self.enqueued_at

    def as_dict(self, position: int) -> dict:
        return {
            "vehicle": self.vehicle,
            "position": position,
            "kind": self.kind,
            "status": self.status,
            "since": round(self.enqueued_at, 3),
            "scheduled_at": round(self.scheduled_at, 3) if self.scheduled_at is not None else None,
            "released_at": round(self.released_at, 3) if self.released_at is not None else None,
            "effective_at": round(self.effective_at(), 3),
        }


@dataclass
class Reservation:
    """预约记录；预约本身不等于排队车位。"""

    vehicle: str
    scheduled_at: float
    booked_at: float
    seq: int
    status: str = "booked"
    request_id: str | None = None

    def as_dict(self) -> dict:
        return {
            "vehicle": self.vehicle,
            "scheduled_at": round(self.scheduled_at, 3),
            "booked_at": round(self.booked_at, 3),
            "status": self.status,
        }


class FillQueue:
    """带预约、加急和审计轨迹的加注队列。"""

    def __init__(self, limits: QueueLimits) -> None:
        self._limits = limits
        self._entries: list[Entry] = []
        self._reservations: dict[str, Reservation] = {}
        self._active: set[str] = set()
        self._order_changes: list[dict] = []
        self._compensations: list[dict] = []
        self._request_results: dict[str, dict] = {}
        self._seq = 0
        self._change_seq = 0
        self._comp_seq = 0
        self._lock = threading.RLock()

    @property
    def limited(self) -> QueueLimits:
        return self._limits

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    def _waiting_entries(self) -> list[Entry]:
        return [entry for entry in self._entries if entry.vehicle not in self._active]

    def _sorted(self, entries: list[Entry]) -> list[Entry]:
        eligible = [entry for entry in entries if entry.status == STATUS_RELEASED]
        scheduled = [
            entry
            for entry in entries
            if entry.status == STATUS_QUEUED and entry.kind == APPOINTMENT
        ]
        no_shows = [entry for entry in entries if entry.status == STATUS_NO_SHOW]
        rank = {URGENT: 0, APPOINTMENT: 1, WALK_IN: 2}
        eligible.sort(key=lambda item: (rank.get(item.kind, 9), item.effective_at(), item.seq))
        scheduled.sort(key=lambda item: (item.scheduled_at or item.enqueued_at, item.seq))
        no_shows.sort(key=lambda item: (item.scheduled_at or item.enqueued_at, item.seq))
        return eligible + scheduled + no_shows

    def _ordered_entries(self) -> list[Entry]:
        return self._sorted(self._waiting_entries())

    def _find(self, vehicle: str) -> Entry | None:
        for entry in self._entries:
            if entry.vehicle == vehicle:
                return entry
        return None

    def _idempotent(self, request_id: str | None, natural_key: str) -> dict | None:
        if request_id:
            previous = self._request_results.get(request_id)
            if previous is not None:
                if previous.get("idempotency_key") != natural_key:
                    raise InterlockError("同一幂等号被用于不同队列动作", component="queue")
                return {**previous, "idempotent_replayed": True}
        return None

    def _remember(self, request_id: str | None, natural_key: str, result: dict) -> dict:
        result["idempotency_key"] = natural_key
        if request_id:
            self._request_results[request_id] = result
        return result

    def _record_order(
        self,
        now: float,
        action: str,
        reason: str,
        before: list[str],
        after: list[Entry],
        *,
        actor: str = "console",
        request_id: str | None = None,
        promoted: str | None = None,
        grant_compensation: bool = True,
    ) -> dict:
        after_vehicles = [item.vehicle for item in after]
        self._change_seq += 1
        change_id = f"Q{self._change_seq:06d}"
        before_index = {vehicle: index + 1 for index, vehicle in enumerate(before)}
        after_index = {vehicle: index + 1 for index, vehicle in enumerate(after_vehicles)}
        compensation_ids: list[str] = []

        if grant_compensation:
            after_entries = {item.vehicle: item for item in after}
            for vehicle, old_position in before_index.items():
                new_position = after_index.get(vehicle)
                if new_position is None or after_entries[vehicle].status != STATUS_RELEASED:
                    continue
                if new_position > old_position:
                    self._comp_seq += 1
                    compensation_id = f"C{self._comp_seq:06d}"
                    places = new_position - old_position
                    compensation = {
                        "id": compensation_id,
                        "ts": round(float(now), 3),
                        "vehicle": vehicle,
                        "change_id": change_id,
                        "reason": reason,
                        "caused_by": promoted,
                        "before_position": old_position,
                        "after_position": new_position,
                        "displaced_places": places,
                        "credit_s": round(places * self._limits.compensation_credit_s_per_place, 3),
                        "status": "granted",
                    }
                    self._compensations.append(compensation)
                    compensation_ids.append(compensation_id)

        changed = before != after_vehicles or action in {"dispatch", "shift_close"}
        record = {
            "id": change_id,
            "ts": round(float(now), 3),
            "action": action,
            "reason": reason,
            "actor": actor,
            "request_id": request_id,
            "promoted_vehicle": promoted,
            "before_order": before,
            "after_order": after_vehicles,
            "changed": changed,
            "compensation_ids": compensation_ids,
        }
        self._order_changes.append(record)
        return record

    def reserve(
        self,
        vehicle: str,
        scheduled_at: float,
        now: float,
        *,
        request_id: str | None = None,
    ) -> dict:
        """登记预约；不到点不占排队位置。"""

        if not vehicle:
            raise InterlockError("车辆标识不能为空", component="queue")
        scheduled_at = float(scheduled_at)
        with self._lock:
            natural_key = f"reserve:{vehicle}:{round(scheduled_at, 3)}"
            cached = self._idempotent(request_id, natural_key)
            if cached is not None:
                return cached
            existing = self._reservations.get(vehicle)
            if existing is not None and existing.status in {"booked", "no_show", "retained", "released"}:
                if existing.scheduled_at == scheduled_at:
                    result = {
                        "changed": False,
                        "reservation": existing.as_dict(),
                        "policy": POLICY_RELEASE_AT_DUE,
                    }
                    return self._remember(request_id, natural_key, result)
                raise InterlockError(f"车辆 {vehicle} 已有不同时段预约", component="queue")
            if self._find(vehicle) is not None or vehicle in self._active:
                raise InterlockError(f"车辆 {vehicle} 已在站内队列中，不能重复预约", component="queue")
            if scheduled_at < float(now):
                raise InterlockError("预约时段不能早于当前时间", component="queue")

            reservation = Reservation(
                vehicle=vehicle,
                scheduled_at=scheduled_at,
                booked_at=float(now),
                seq=self._next_seq(),
                request_id=request_id,
            )
            self._reservations[vehicle] = reservation
            result = {
                "changed": True,
                "reservation": reservation.as_dict(),
                "policy": POLICY_RELEASE_AT_DUE,
                "position": 0,
            }
            return self._remember(request_id, natural_key, result)

    def enqueue(self, vehicle: str, now: float) -> int:
        """兼容旧接口：普通现场车入队。"""

        return self.checkin(vehicle, float(now))["position"]

    def checkin(self, vehicle: str, now: float, *, use_reservation: bool = True) -> dict:
        """车辆到场入队；有预约则按预约时段决定是否放行。"""

        if not vehicle:
            raise InterlockError("车辆标识不能为空", component="queue")
        with self._lock:
            if vehicle in self._active:
                raise InterlockError(f"车辆 {vehicle} 已在加注服务中", component="queue")
            if self._find(vehicle) is not None:
                raise InterlockError(f"车辆 {vehicle} 已在队列中", component="queue")
            if len(self._waiting_entries()) >= self._limits.max_waiting:
                raise InterlockError("加注队列已满", component="queue")

            moment = float(now)
            reservation = self._reservations.get(vehicle)
            kind = WALK_IN
            status = STATUS_RELEASED
            scheduled_at: float | None = None
            released_at: float | None = moment
            reason = "walk_in"
            if reservation is not None:
                if reservation.status == "void" and use_reservation:
                    # 过号记录保留为 void，车辆本次按临时车从队尾重排。
                    reservation = None
                elif not use_reservation:
                    self._reservations[vehicle].status = "fulfilled"
                    reservation = None
                elif reservation.status == "fulfilled":
                    reservation = None
                else:
                    kind = APPOINTMENT
                    scheduled_at = reservation.scheduled_at
                    grace_end = scheduled_at + self._limits.appointment_grace_s
                    if moment < scheduled_at:
                        status = STATUS_QUEUED
                        released_at = None
                        reason = "appointment_early_checkin"
                    elif moment <= grace_end or reservation.status == "released":
                        status = STATUS_RELEASED
                        released_at = max(moment, scheduled_at)
                        reservation.status = "released"
                        reason = "appointment_due"
                    elif self._limits.no_show_policy == NO_SHOW_VOID:
                        reservation.status = "void"
                        kind = WALK_IN
                        status = STATUS_RELEASED
                        scheduled_at = None
                        released_at = moment
                        reservation = None
                        reason = "appointment_void_requeue"
                    else:
                        status = STATUS_NO_SHOW
                        released_at = None
                        reservation.status = "retained"
                        reason = "appointment_no_show_waiting_release"

            entry = Entry(
                vehicle=vehicle,
                kind=kind,
                enqueued_at=moment,
                seq=self._next_seq(),
                status=status,
                scheduled_at=scheduled_at,
                released_at=released_at,
                reservation=reservation if reservation is not None and reservation.status != "fulfilled" else None,
            )
            before = [item.vehicle for item in self._ordered_entries()]
            self._entries.append(entry)
            after = self._ordered_entries()

            result = {
                "vehicle": vehicle,
                "position": self.position(vehicle),
                "size": self.size(),
                "kind": kind,
                "status": status,
                "scheduled_at": round(scheduled_at, 3) if scheduled_at is not None else None,
                "policy": POLICY_RELEASE_AT_DUE,
                "changed": True,
            }
            if status == STATUS_RELEASED and kind == APPOINTMENT:
                change = self._record_order(
                    moment,
                    "appointment_release",
                    reason,
                    before,
                    after,
                    actor="driver",
                    promoted=vehicle,
                )
                result["order_change"] = change
                result["compensation_ids"] = change["compensation_ids"]
            return result

    def _release_entry(self, entry: Entry, now: float, reason: str, **kwargs) -> dict:
        before = [item.vehicle for item in self._ordered_entries()]
        entry.status = STATUS_RELEASED
        entry.released_at = float(now)
        if entry.reservation is not None:
            entry.reservation.status = "released"
        after = self._ordered_entries()
        return self._record_order(
            float(now),
            "appointment_release",
            reason,
            before,
            after,
            promoted=entry.vehicle,
            **kwargs,
        )

    def release_appointment(
        self,
        vehicle: str,
        now: float,
        *,
        request_id: str | None = None,
    ) -> dict:
        """放行一辆已到场且到点的预约车。重复放行幂等。"""

        with self._lock:
            natural_key = f"release:{vehicle}"
            cached = self._idempotent(request_id, natural_key)
            if cached is not None:
                return cached
            moment = float(now)
            entry = self._find(vehicle)
            if entry is None:
                raise InterlockError(f"车辆 {vehicle} 不在队列中", component="queue")
            if entry.kind != APPOINTMENT or entry.scheduled_at is None:
                raise InterlockError(f"车辆 {vehicle} 不是预约车", component="queue")
            if entry.status in {STATUS_RELEASED, STATUS_NO_SHOW}:
                was_no_show = entry.status == STATUS_NO_SHOW
                if was_no_show:
                    # _release_entry 会完成状态切换、重排和补偿记录。
                    change = self._release_entry(
                        entry,
                        moment,
                        "manual_no_show_release",
                        request_id=request_id,
                    )
                else:
                    change = self._order_changes[-1] if self._order_changes else None
                result = {
                    "changed": was_no_show,
                    "vehicle": vehicle,
                    "position": self.position(vehicle),
                    "order_change": change,
                }
                if result["changed"]:
                    result["compensation_ids"] = change["compensation_ids"]
                return self._remember(request_id, natural_key, result)

            grace_end = entry.scheduled_at + self._limits.appointment_grace_s
            if moment < entry.scheduled_at:
                raise InterlockError("预约采用到点放行，未到时段不能提前放行", component="queue")
            if moment > grace_end and self._limits.no_show_policy == NO_SHOW_VOID:
                raise InterlockError("预约已过号并按本站规则作废", component="queue")
            change = self._release_entry(entry, moment, "manual_appointment_release", request_id=request_id)
            result = {
                "changed": True,
                "vehicle": vehicle,
                "position": self.position(vehicle),
                "order_change": change,
                "compensation_ids": change["compensation_ids"],
            }
            return self._remember(request_id, natural_key, result)

    def urgent(
        self,
        vehicle: str,
        now: float,
        *,
        reason: str = "manual_urgent",
        request_id: str | None = None,
    ) -> dict:
        """给已到场车辆临时加急；不能提前放行未到点预约车。"""

        if not vehicle:
            raise InterlockError("车辆标识不能为空", component="queue")
        with self._lock:
            natural_key = f"urgent:{vehicle}"
            cached = self._idempotent(request_id, natural_key)
            if cached is not None:
                return cached
            moment = float(now)
            entry = self._find(vehicle)
            if entry is None:
                if len(self._waiting_entries()) >= self._limits.max_waiting:
                    raise InterlockError("加注队列已满", component="queue")
                before = [item.vehicle for item in self._ordered_entries()]
                entry = Entry(
                    vehicle=vehicle,
                    kind=URGENT,
                    enqueued_at=moment,
                    seq=self._next_seq(),
                    status=STATUS_RELEASED,
                    released_at=moment,
                    promoted_at=moment,
                )
                self._entries.append(entry)
                after = self._ordered_entries()
                change = self._record_order(
                    moment,
                    "urgent_insert",
                    reason,
                    before,
                    after,
                    promoted=vehicle,
                    request_id=request_id,
                )
            else:
                if entry.kind == APPOINTMENT and entry.status == STATUS_QUEUED:
                    raise InterlockError("未到预约时段的预约车不能用加急提前放行", component="queue")
                if entry.kind == URGENT and entry.status == STATUS_RELEASED:
                    result = {
                        "changed": False,
                        "vehicle": vehicle,
                        "position": self.position(vehicle),
                        "order_change": self._order_changes[-1] if self._order_changes else None,
                    }
                    return self._remember(request_id, natural_key, result)
                before = [item.vehicle for item in self._ordered_entries()]
                entry.kind = URGENT
                entry.status = STATUS_RELEASED
                entry.promoted_at = moment
                entry.released_at = moment
                if entry.reservation is not None:
                    entry.reservation.status = "released"
                after = self._ordered_entries()
                change = self._record_order(
                    moment,
                    "urgent_insert",
                    reason,
                    before,
                    after,
                    promoted=vehicle,
                    request_id=request_id,
                )
            result = {
                "changed": True,
                "vehicle": vehicle,
                "position": self.position(vehicle),
                "order_change": change,
                "compensation_ids": change["compensation_ids"],
            }
            return self._remember(request_id, natural_key, result)

    def sweep_due(self, now: float) -> dict:
        """控制周期调用：到点自动放行，过宽限期按站点策略处理。"""

        with self._lock:
            moment = float(now)
            released: list[dict] = []
            no_shows: list[dict] = []
            for entry in list(self._entries):
                if (
                    entry.kind == APPOINTMENT
                    and entry.status == STATUS_QUEUED
                    and entry.scheduled_at is not None
                    and moment >= entry.scheduled_at
                ):
                    change = self._release_entry(entry, moment, "scheduled_time_reached", actor="system")
                    released.append({"vehicle": entry.vehicle, "order_change": change})

            for vehicle, reservation in list(self._reservations.items()):
                if reservation.status != "booked":
                    continue
                grace_end = reservation.scheduled_at + self._limits.appointment_grace_s
                if moment <= grace_end:
                    continue
                if self._find(vehicle) is not None:
                    # 已入场车辆到点会被先放行，不应再判成过号。
                    continue
                if self._limits.no_show_policy == NO_SHOW_VOID:
                    reservation.status = "void"
                else:
                    reservation.status = "no_show"
                no_shows.append(
                    {
                        "vehicle": vehicle,
                        "scheduled_at": round(reservation.scheduled_at, 3),
                        "status": reservation.status,
                        "policy": self._limits.no_show_policy,
                    }
                )

            return {
                "ts": round(moment, 3),
                "released": released,
                "no_shows": no_shows,
                "order_changes": [item["order_change"] for item in released],
                "compensations": [
                    compensation
                    for compensation in self._compensations
                    if compensation["change_id"] in {item["order_change"]["id"] for item in released}
                ],
            }

    def close_shift(self, now: float) -> dict:
        """本班结束：按保留策略留下的过号预约到此作废。"""

        with self._lock:
            moment = float(now)
            before = [item.vehicle for item in self._ordered_entries()]
            voided: list[str] = []
            for reservation in self._reservations.values():
                if reservation.status in {"booked", "no_show", "retained"}:
                    reservation.status = "void"
                    voided.append(reservation.vehicle)
            removed: list[str] = []
            kept: list[Entry] = []
            for entry in self._entries:
                reservation = entry.reservation
                if reservation is not None and reservation.status == "void" and entry.status != STATUS_RELEASED:
                    removed.append(entry.vehicle)
                else:
                    kept.append(entry)
            self._entries = kept
            after = self._ordered_entries()
            change = self._record_order(
                moment,
                "shift_close",
                "shift_end_no_show_expired",
                before,
                after,
                actor="system",
                grant_compensation=False,
            )
            return {
                "changed": bool(voided or removed),
                "voided_reservations": voided,
                "removed_waiting": removed,
                "order_change": change,
            }

    def _take(self, vehicle: str | None, now: float, reason: str) -> dict:
        if self._active:
            if vehicle is not None and vehicle in self._active:
                return {"vehicle": vehicle, "from_queue": True, "position": 0, "idempotent_replayed": True}
            active = next(iter(sorted(self._active)))
            raise InterlockError(f"车辆 {active} 已叫号或已插枪，不能放行其他车辆", component="queue")

        ordered = self._ordered_entries()
        eligible = [entry for entry in ordered if entry.status == STATUS_RELEASED]
        if not eligible:
            if self._waiting_entries():
                raise InterlockError("暂无可放行车辆：预约车未到点或过号未处理", component="queue")
            if vehicle is None:
                raise InterlockError("加注队列为空", component="queue")
            # 兼容未入队直接加注的旧操作；站内没有等待车辆时不构成插队。
            self._active.add(vehicle)
            return {"vehicle": vehicle, "from_queue": False, "position": 0}
        if vehicle is None:
            entry = eligible[0]
        else:
            entry = self._find(vehicle)
            if entry is None:
                raise InterlockError(f"车辆 {vehicle} 未排队，不能越过队列中的车辆", component="queue")
            if entry.status != STATUS_RELEASED:
                raise InterlockError(f"车辆 {vehicle} 尚未放行", component="queue")
            if eligible[0].vehicle != vehicle:
                raise InterlockError(f"车辆 {vehicle} 不是当前队首，不能越过前车", component="queue")

        before = [item.vehicle for item in ordered]
        self._entries.remove(entry)
        self._active.add(entry.vehicle)
        if entry.reservation is not None:
            entry.reservation.status = "fulfilled"
        after = self._ordered_entries()
        change = self._record_order(
            float(now),
            "dispatch",
            reason,
            before,
            after,
            actor="system",
            promoted=entry.vehicle,
            grant_compensation=False,
        )
        return {
            "vehicle": entry.vehicle,
            "from_queue": True,
            "position": 0,
            "order_change": change,
        }

    def dequeue(self) -> str:
        """兼容旧接口：取出当前可放行的队首车辆。"""

        return self.dispatch(0.0)["vehicle"]

    def dispatch(self, now: float) -> dict:
        """叫号队首车辆；已在服务中的车不会被队列重排影响。"""

        with self._lock:
            return self._take(None, float(now), "manual_dispatch")

    def begin_service(self, vehicle: str, now: float) -> dict:
        """为指定车辆开始加注；未入队车辆不视为插队。"""

        with self._lock:
            return self._take(vehicle, float(now), "service_start")

    def complete_service(self, vehicle: str) -> dict:
        """车辆加注结束，离开活动车位集合。"""

        with self._lock:
            removed = vehicle in self._active
            self._active.discard(vehicle)
            return {"vehicle": vehicle, "completed": removed}

    def contains(self, vehicle: str) -> bool:
        with self._lock:
            return self._find(vehicle) is not None

    def position(self, vehicle: str) -> int:
        with self._lock:
            for index, entry in enumerate(self._ordered_entries()):
                if entry.vehicle == vehicle:
                    return index + 1
            return 0

    def size(self) -> int:
        with self._lock:
            return len(self._waiting_entries())

    def waiting(self) -> list[dict]:
        with self._lock:
            return [entry.as_dict(index + 1) for index, entry in enumerate(self._ordered_entries())]

    def reservations(self) -> list[dict]:
        with self._lock:
            return [reservation.as_dict() for reservation in sorted(self._reservations.values(), key=lambda item: (item.scheduled_at, item.seq))]

    def active(self) -> list[str]:
        with self._lock:
            return sorted(self._active)

    def order_changes(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return list(self._order_changes[-limit:])

    def compensations(self, limit: int = 100) -> list[dict]:
        with self._lock:
            return list(self._compensations[-limit:])

    def rules(self) -> dict:
        return {
            "appointment_policy": POLICY_RELEASE_AT_DUE,
            "appointment_policy_label": "到点放行，不提前预留位",
            "no_show_policy": self._limits.no_show_policy,
            "appointment_grace_s": self._limits.appointment_grace_s,
            "compensation_credit_s_per_place": self._limits.compensation_credit_s_per_place,
        }

    def reset(self) -> int:
        """清空队列、预约、活动车和本内存队列的轨迹。"""

        with self._lock:
            cleared = len(self._entries) + len(self._active)
            self._entries.clear()
            self._reservations.clear()
            self._active.clear()
            self._order_changes.clear()
            self._compensations.clear()
            self._request_results.clear()
            self._seq = 0
            self._change_seq = 0
            self._comp_seq = 0
            return cleared
