"""加注队列状态快照。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QueueSnapshot:
    """加注队列某一时刻的状态。"""

    size: int
    capacity: int
    waiting: list[dict]
    outstanding: list[str]
    policy: dict = field(default_factory=dict)
    released: list[str] = field(default_factory=list)
    plugged: list[str] = field(default_factory=list)
    reservations: list[dict] = field(default_factory=list)
    order_changes: list[dict] = field(default_factory=list)
    compensations: list[dict] = field(default_factory=list)
    urgent_quota: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "size": self.size,
            "capacity": self.capacity,
            "waiting": self.waiting,
            "outstanding": self.outstanding,
            "policy": self.policy,
            "released": self.released,
            "plugged": self.plugged,
            "reservations": self.reservations,
            "order_changes": self.order_changes,
            "compensations": self.compensations,
            "urgent_quota": self.urgent_quota,
        }
