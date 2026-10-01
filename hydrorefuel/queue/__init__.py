"""加注队列组件。

普通车辆按先来先服务排队；预约采用“到点放行、不提前预留位”。队列保留每次
顺序变更的前后次序与补偿记录，控制周期负责到点自动放行和过号判定。
"""

from __future__ import annotations

from .confirm import Confirmation
from .queue import (
    APPOINTMENT,
    POLICY_RELEASE_AT_DUE,
    URGENT,
    WALK_IN,
    FillQueue,
    Reservation,
)
from .snapshot import QueueSnapshot

__all__ = [
    "FillQueue",
    "Confirmation",
    "QueueSnapshot",
    "Reservation",
    "APPOINTMENT",
    "URGENT",
    "WALK_IN",
    "POLICY_RELEASE_AT_DUE",
]
