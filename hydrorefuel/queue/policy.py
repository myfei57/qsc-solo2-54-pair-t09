"""排队优先级与站点策略。

预约语义在装配期就定死：``scheduling_mode`` 只接受 ``on_time_release``
（到点放行），不存在"提前预留车位"的模式——预约车在到点前与普通车同序排队，
到点那一刻才允许越过普通车。过号处置按站点配置：``void``（作废重排）或
``hold``（保留到本班末，落在队尾且不得放行，交班时统一作废）。
"""

from __future__ import annotations

from dataclasses import dataclass

#: 调度模式：到点放行。唯一合法取值，配置成别的一律拒绝启动。
MODE_ON_TIME_RELEASE = "on_time_release"

#: 过号作废，重新到场按普通车重排。
NOSHOW_VOID = "void"
#: 过号保留到本班末。
NOSHOW_HOLD = "hold"

NOSHOW_POLICIES = (NOSHOW_VOID, NOSHOW_HOLD)

#: 排队层级：普通车 / 预约车 / 临时加急车。
TIER_WALKIN = "walkin"
TIER_RESERVED = "reserved"
TIER_URGENT = "urgent"

#: 放行依据。
RELEASE_FCFS = "fcfs"
RELEASE_RESERVATION_DUE = "reservation_due"
RELEASE_URGENT = "urgent_priority"
RELEASE_MANUAL = "manual"

#: 顺序变更依据（审计 reason 字段的取值域）。
ORDER_ENQUEUE = "enqueue"
ORDER_RESERVATION_DUE = "reservation_due"
ORDER_URGENT_INSERT = "urgent_insert"
ORDER_NOSHOW_VOID = "noshow_void"
ORDER_NOSHOW_HOLD = "noshow_hold"
ORDER_SHIFT_END = "shift_end_void"
ORDER_RELEASE = "release"

#: 补偿记录类型。
COMP_OVERTAKEN = "overtaken"
COMP_NOSHOW_VOID = "noshow_void"
COMP_NOSHOW_HELD = "lapsed_held"


@dataclass(frozen=True)
class QueuePolicy:
    """站点排队策略：预约语义、过号处置、加急配额。"""

    scheduling_mode: str
    noshow_policy: str
    grace_s: float
    urgent_quota_per_shift: int

    @classmethod
    def from_fields(
        cls,
        scheduling_mode: str,
        noshow_policy: str,
        grace_s: float,
        urgent_quota_per_shift: int,
    ) -> "QueuePolicy":
        mode = str(scheduling_mode)
        if mode != MODE_ON_TIME_RELEASE:
            raise ValueError(f"调度模式只支持 {MODE_ON_TIME_RELEASE!r}（到点放行），收到 {mode!r}")
        policy = str(noshow_policy)
        if policy not in NOSHOW_POLICIES:
            raise ValueError(f"过号处置必须是 {NOSHOW_POLICIES} 之一，收到 {policy!r}")
        grace = float(grace_s)
        if grace < 0.0:
            raise ValueError("过号宽限期不得为负")
        quota = int(urgent_quota_per_shift)
        if quota < 0:
            raise ValueError("每班加急配额不得为负")
        return cls(mode, policy, grace, quota)

    def holds_lapsed(self) -> bool:
        """本站过号车是否保留到本班末。"""

        return self.noshow_policy == NOSHOW_HOLD

    def as_dict(self) -> dict:
        return {
            "scheduling_mode": self.scheduling_mode,
            "noshow_policy": self.noshow_policy,
            "grace_s": self.grace_s,
            "urgent_quota_per_shift": self.urgent_quota_per_shift,
        }
