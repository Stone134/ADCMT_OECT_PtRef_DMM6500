"""Independent OECT schedules and validated settings for the dual program."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from math import gcd
from pathlib import Path

from models import RunConfig


PROJECT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class APlan(RunConfig):
    """OECT A retains the single-OECT reference feedback and monitor settings."""

    output_file: str = "outputs/oect_a.txt"

    @property
    def output_path(self) -> Path:
        path = Path(self.output_file).expanduser()
        return path if path.is_absolute() else PROJECT_DIR / path


@dataclass(frozen=True)
class BPlan:
    """OECT B uses CH3/CH4 directly, without a reference input."""

    vds_v: float = -0.01
    sweep_start_v: float = 0.0
    sweep_stop_v: float = -0.8
    sweep_step_v: float = -0.1
    sweep_pattern: str = "one_way"
    sweep_cycles: int = 1
    hold_s: float = 60.0
    delay_s: float = 1.0
    final_keep_s: float = 3600.0
    sample_interval_s: float = 1.0
    gate_min_v: float = -1.5
    gate_max_v: float = 1.5
    drain_limit_a: float = 0.003
    gate_limit_a: float = 0.003
    output_file: str = "outputs/oect_b.txt"

    @property
    def levels(self) -> tuple[float, ...]:
        # The single-OECT monitor mode defines the same inclusive and cyclic
        # gate sweep; share that exact sequence rather than maintain two copies.
        return RunConfig(
            mode="monitor", sweep_start_v=self.sweep_start_v,
            sweep_stop_v=self.sweep_stop_v, sweep_step_v=self.sweep_step_v,
            sweep_pattern=self.sweep_pattern, sweep_cycles=self.sweep_cycles,
        ).levels

    @property
    def duration_s(self) -> float:
        return len(self.levels) * self.hold_s + self.final_keep_s

    @property
    def output_path(self) -> Path:
        path = Path(self.output_file).expanduser()
        return path if path.is_absolute() else PROJECT_DIR / path

    def validate(self) -> None:
        # Reuse the same voltage, timing, current-limit, and path checks as a
        # direct Vg sweep on OECT A. B never opens or reads the DMM.
        RunConfig(
            mode="monitor", vds_v=self.vds_v,
            sweep_start_v=self.sweep_start_v, sweep_stop_v=self.sweep_stop_v,
            sweep_step_v=self.sweep_step_v, sweep_pattern=self.sweep_pattern,
            sweep_cycles=self.sweep_cycles, hold_s=self.hold_s,
            delay_s=self.delay_s, final_keep_s=self.final_keep_s,
            sample_interval_s=self.sample_interval_s,
            gate_min_v=self.gate_min_v, gate_max_v=self.gate_max_v,
            drain_limit_a=self.drain_limit_a, gate_limit_a=self.gate_limit_a,
            output_file=self.output_file,
        ).validate()


@dataclass(frozen=True)
class DualPlan:
    a: APlan = field(default_factory=APlan)
    b: BPlan = field(default_factory=BPlan)

    @property
    def hardware_interval_s(self) -> float:
        intervals_ms: list[int] = []
        for interval in (self.a.sample_interval_s, self.b.sample_interval_s):
            if not math.isfinite(interval):
                raise ValueError("Read intervals must be finite")
            nearest_ms = round(interval * 1000)
            if not math.isclose(interval * 1000, nearest_ms, abs_tol=1e-5):
                raise ValueError("Read intervals must be whole milliseconds")
            intervals_ms.append(nearest_ms)
        common_ms = gcd(*intervals_ms)
        if common_ms < 100:
            raise ValueError(
                "The two read intervals need a common ADCMT clock of at least 0.1 s"
            )
        return common_ms / 1000

    def validate(self) -> None:
        self.a.validate()
        self.b.validate()
        if self.a.output_path.resolve() == self.b.output_path.resolve():
            raise ValueError("Choose different raw-data files for OECT A and OECT B")
        _ = self.hardware_interval_s


@dataclass(frozen=True)
class BSample:
    utc: str
    elapsed_s: float
    stage_index: int
    phase: str
    target_v: float
    gate_i_a: float
    gate_v: float
    drain_i_a: float
    drain_v: float
    adcmt_index: int
    adcmt_nominal_elapsed_s: float
    adcmt_skipped: int
