"""排队条目与预约记录。

队列条目是不可变快照的可变载体，``moved_by`` 记录最近一次顺序变更依据，
控制台按它回答"这辆车为什么在这个位置"。预约记录独立于队列条目存在：
未到场的预约不在队列里（不占车位），到场登记后才产生队列条目并引用预约。
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 预约生命周期状态。
RESERVATION_BOOKED = "booked"
RESERVATION_ARRIVED = "arrived"
RESERVATION_RELEASED = "released"
RESERVATION_NOSHOW_VOID = "noshow_void"
RESERVATION_NOSHOW_HELD = "noshow_held"
RESERVATION_SHIFT_VOID = "shift_end_void"
RESERVATION_CANCELLED = "cancelled"


@dataclass
class Reservation:
    """一个预约时段。"""

    reservation_id: str
    vehicle: str
    slot_ts: float
    state: str = RESERVATION_BOOKED

    def as_dict(self) -> dict:
        return {
            "reservation_id": self.reservation_id,
            "vehicle": self.vehicle,
            "slot_ts": round(self.slot_ts, 3),
            "state": self.state,
        }


@dataclass
class QueueEntry:
    """队列中的一辆车。

    ``seq`` 是入队先后（FCFS 稳定键）；``rank_ts`` 是优先级生效时刻，等于
    预约到点时刻（到点放行）或加急提出时刻；``lapsed`` 标记过号保留到本班末
    的车——它们留在队尾但任何放行都不会取到。
    """

    vehicle: str
    seq: int
    enqueued_ts: float
    tier: str
    rank_ts: float | None = None
    slot_ts: float | None = None
    reservation_id: str | None = None
    lapsed: bool = False
    moved_by: str = ""

    def as_dict(self, position: int) -> dict:
        return {
            "vehicle": self.vehicle,
            "position": position,
            "tier": self.tier,
            "since": round(self.enqueued_ts, 3),
            "rank_ts": None if self.rank_ts is None else round(self.rank_ts, 3),
            "slot_ts": None if self.slot_ts is None else round(self.slot_ts, 3),
            "reservation_id": self.reservation_id,
            "lapsed": self.lapsed,
            "moved_by": self.moved_by,
        }


@dataclass(frozen=True)
class OrderChange:
    """一次顺序变更的依据与前后位次。"""

    ts: float
    seq: int
    reason: str
    vehicle: str
    before: int
    after: int
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ts": round(self.ts, 3),
            "seq": self.seq,
            "reason": self.reason,
            "vehicle": self.vehicle,
            "before": self.before,
            "after": self.after,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class Compensation:
    """被挤掉的车留下的补偿记录。"""

    ts: float
    vehicle: str
    kind: str
    caused_by: str
    detail: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "ts": round(self.ts, 3),
            "vehicle": self.vehicle,
            "kind": self.kind,
            "caused_by": self.caused_by,
            "detail": self.detail,
        }
