"""Adjust paste throughput using elapsed server ticks, within configured limits."""

from dataclasses import dataclass, field


@dataclass(slots=True)
class PastePacer:
    maximum: int
    limit: int = field(init=False)
    last_tick: float | None = None
    healthy_ticks: int = 0

    def __post_init__(self) -> None:
        self.maximum = max(1, int(self.maximum))
        self.limit = min(256, self.maximum)

    def observe(self, now: float, active: bool) -> None:
        elapsed = None if self.last_tick is None else now - self.last_tick
        self.last_tick = now
        if not active:
            self.limit = min(256, self.maximum)
            self.healthy_ticks = 0
            return
        if elapsed is None:
            return
        if elapsed > 0.075:
            self.limit = max(min(64, self.maximum), self.limit // 2)
            self.healthy_ticks = 0
        elif 0.040 <= elapsed <= 0.060:
            self.healthy_ticks += 1
            if self.healthy_ticks >= 10:
                self.limit = min(self.maximum, self.limit + max(64, self.limit // 4))
                self.healthy_ticks = 0
        else:
            # Catch-up callbacks and borderline ticks are not proof of spare capacity.
            self.healthy_ticks = 0
