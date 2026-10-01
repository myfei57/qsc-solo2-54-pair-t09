"""加注队列组件。

车辆按优先级排队：临时加急优先于已到点预约，已到点预约优先于普通车（含
未到点预约）；预约语义固定为"到点放行"，过号处置（作废重排 / 保留到本班
末）按站点配置。轮到某辆车时先发起确认，确认必须在超时前到达才允许开加注
阀。确认对象带锁，保证并发确认只认第一次结果。
"""

from __future__ import annotations

from .confirm import Confirmation
from .entry import Compensation, OrderChange, QueueEntry, Reservation
from .policy import QueuePolicy
from .queue import FillQueue
from .snapshot import QueueSnapshot

__all__ = [
    "FillQueue",
    "Confirmation",
    "QueueSnapshot",
    "QueueEntry",
    "Reservation",
    "OrderChange",
    "Compensation",
    "QueuePolicy",
]
