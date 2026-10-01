"""加注队列状态快照。"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class QueueSnapshot:
    """加注队列某一时刻的状态。"""

    size: int
    capacity: int
    waiting: list[dict]
    outstanding: list[str]
    reservations: list[dict]
    active: list[str]
    order_changes: list[dict]
    compensations: list[dict]
    rules: dict

    def as_dict(self) -> dict:
        return {
            "size": self.size,
            "capacity": self.capacity,
            "waiting": self.waiting,
            "outstanding": self.outstanding,
            "reservations": self.reservations,
            "active": self.active,
            "order_changes": self.order_changes,
            "compensations": self.compensations,
            "rules": self.rules,
        }
