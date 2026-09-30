"""Validated settings and readings for OECT with reference feedback."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path


PROJECT_DIR = Path(__file__).resolve().parent


@dataclass(frozen=True)
class RunConfig:
    simulation: bool = True
    adcmt_ip: str = "192.168.1.204"
    dmm_ip: str = "192.168.1.103"
    # Raw TCP port for ADCMT only; DMM6500 uses TCPIP0::<IP>::inst0::INSTR.
    port: int = 5025
    vds_v: float = -0.01
    sweep_start_v: float = 0.0
    sweep_stop_v: float = -0.8
    sweep_step_v: float = -0.1
    hold_s: float = 60.0
    delay_s: float = 1.0
    final_keep_s: float = 3600.0
    sample_interval_s: float = 1.0
    drain_limit_a: float = 0.003
    gate_limit_a: float = 0.003
    gate_initial_v: float = 0.0
    gate_min_v: float = -1.5
    gate_max_v: float = 1.5
    correction_factor: float = 1.0
    # +1 means Vg and Eref move in the same direction. With the default
    # same-sign convention, decreasing Vg lowers Eref.
    feedback_polarity: int = 1
    feedback_max_step_v: float = 0.1
    feedback_tolerance_v: float = 0.005
    output_file: str = "outputs/oect_reference.txt"

    @property
    def levels(self) -> tuple[float, ...]:
        start, stop, step = self.sweep_start_v, self.sweep_stop_v, self.sweep_step_v
        if not all(math.isfinite(x) for x in (start, stop, step)):
            raise ValueError("Sweep values must be finite")
        if step == 0 or (stop - start) * step < 0:
            raise ValueError("Sweep step must point from initial to final value")
        n = int(round((stop - start) / step)) if start != stop else 0
        if n < 0 or n > 1000 or not math.isclose(start + n * step, stop, abs_tol=1e-8):
            raise ValueError("Sweep must reach the final value in at most 1000 steps")
        return tuple(round(start + i * step, 10) for i in range(n + 1))

    @property
    def duration_s(self) -> float:
        return len(self.levels) * self.hold_s + self.final_keep_s

    @property
    def output_path(self) -> Path:
        path = Path(self.output_file).expanduser()
        return path if path.is_absolute() else PROJECT_DIR / path

    def validate(self) -> None:
        if not self.adcmt_ip.strip() or not self.dmm_ip.strip():
            raise ValueError("Both instrument IP addresses are required")
        if not 1 <= self.port <= 65535:
            raise ValueError("Port must be between 1 and 65535")
        _ = self.levels
        numeric = (
            self.vds_v, self.hold_s, self.delay_s, self.final_keep_s,
            self.sample_interval_s, self.drain_limit_a, self.gate_limit_a,
            self.gate_initial_v, self.gate_min_v, self.gate_max_v,
            self.correction_factor, self.feedback_max_step_v, self.feedback_tolerance_v,
        )
        if not all(math.isfinite(x) for x in numeric):
            raise ValueError("All numeric settings must be finite")
        if not 0.1 <= self.sample_interval_s <= 60:
            raise ValueError("Read interval must be 0.1 to 60 seconds")
        if self.hold_s < self.sample_interval_s:
            raise ValueError("Period must be at least one read interval")
        if not 0 <= self.delay_s < self.hold_s:
            raise ValueError("Delay must be within each period")
        if self.final_keep_s < 0:
            raise ValueError("Final keep time cannot be negative")
        if not (0 < self.drain_limit_a <= 0.5 and 0 < self.gate_limit_a <= 0.5):
            raise ValueError("Current limits must be above 0 and at most 0.5 A")
        if not -3 <= self.vds_v <= 3:
            raise ValueError("Vds must be within the ADCMT 3 V source range")
        if not -3 <= self.gate_min_v < self.gate_max_v <= 3:
            raise ValueError("Gate bounds must lie within -3 to +3 V")
        if not self.gate_min_v <= self.gate_initial_v <= self.gate_max_v:
            raise ValueError("Initial gate voltage must be inside the gate bounds")
        if self.feedback_polarity not in {-1, 1}:
            raise ValueError("Feedback polarity must be +1 or -1")
        if self.correction_factor <= 0 or self.feedback_max_step_v <= 0:
            raise ValueError("Correction factor and maximum step must be positive")
        if self.feedback_tolerance_v < 0:
            raise ValueError("Feedback tolerance cannot be negative")
        if not self.output_file.strip():
            raise ValueError("Choose a raw-data file")
        if Path(self.output_file).suffix.lower() != ".txt":
            raise ValueError("Raw-data file must have a .txt extension")


@dataclass(frozen=True)
class Sample:
    utc: str
    elapsed_s: float
    stage_index: int
    phase: str
    target_v: float
    gate_i_a: float
    gate_v: float
    drain_i_a: float
    drain_v: float
    vref_v: float
    e_reference_v: float
    adcmt_index: int
    adcmt_nominal_elapsed_s: float
    adcmt_skipped: int
    dmm_read_utc: str
    within_tolerance: bool
