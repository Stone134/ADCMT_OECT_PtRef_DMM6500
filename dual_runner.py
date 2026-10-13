"""Run two independently scheduled OECTs on one four-channel ADCMT 6541.

CH1 is the synchronization master for all four current channels. A uses CH1/2
and the high-impedance DMM6500 reference input; B uses CH3/4 with direct Vg
control. Completed cells stay at verified 0 V while the other cell runs.
"""

from __future__ import annotations

import csv
import math
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from instruments import DMM6500
from models import Sample
from runner import RawDataWriter, next_feedback_gate

from dual_instrument import DualADCMT6541, DualADCMTSample
from dual_plan import APlan, BPlan, BSample, DualPlan


B_FIELDS = ("Time", "GateI", "GateV", "DrainI", "DrainV")


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


@dataclass(frozen=True)
class DualResult:
    output_a: Path
    output_b: Path
    samples_a: int
    samples_b: int
    stopped: bool


class BDataWriter:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._file = path.open("w", encoding="utf-8", newline="")
        self._writer = csv.writer(self._file, delimiter="\t", lineterminator="\n")
        self._writer.writerow(B_FIELDS)
        self._file.flush()
        self.samples = 0

    def write(self, sample: BSample) -> None:
        self._writer.writerow((
            f"{sample.elapsed_s:.7E}", f"{sample.gate_i_a:.7E}",
            f"{sample.gate_v:.7E}", f"{sample.drain_i_a:.7E}",
            f"{sample.drain_v:.7E}",
        ))
        self._file.flush()
        self.samples += 1

    def close(self) -> None:
        self._file.close()


class SimulatedDualADCMT:
    """Four-source simulation with the same public operations as the LAN driver."""

    def __init__(self, interval_s: float, feedback_polarity: int = 1) -> None:
        self.interval_s = interval_s
        self.feedback_polarity = feedback_polarity
        self.vds_a_v = self.gate_a_v = 0.0
        self.vds_b_v = self.gate_b_v = 0.0
        self.started: float | None = None
        self.last_index = -1
        self.zeroed_cells: list[str] = []
        self.closed = False

    def connect(self) -> str:
        return "SIMULATED ADCMT 6541 • four synchronized channels"

    def configure(
        self, *, vds_a_v: float, gate_a_v: float, vds_b_v: float,
        gate_b_v: float, limits_a: tuple[float, float, float, float],
        interval_s: float,
    ) -> None:
        self.vds_a_v, self.gate_a_v = vds_a_v, gate_a_v
        self.vds_b_v, self.gate_b_v = vds_b_v, gate_b_v
        self.interval_s = interval_s

    def start(self) -> None:
        self.started = time.monotonic()

    @property
    def clock_started_monotonic(self) -> float:
        if self.started is None:
            raise RuntimeError("Simulation has not started")
        return self.started

    def read_next(self, timeout_s: float) -> DualADCMTSample:
        if self.started is None:
            raise RuntimeError("Simulation has not started")
        index = self.last_index + 1
        next_time = self.started + (index + 1) * self.interval_s
        wait = next_time - time.monotonic()
        if wait > timeout_s:
            raise TimeoutError("Simulated ADCMT current sample timed out")
        if wait > 0:
            time.sleep(wait)
        self.last_index = index
        elapsed = time.monotonic() - self.started
        e_a = self.feedback_polarity * 0.82 * self.gate_a_v + 0.003 * math.sin(elapsed / 12)
        drain_a = self.vds_a_v * (1.5e-6 + 4.0e-5 * abs(e_a))
        gate_a = -self.gate_a_v * 1.2e-8
        drain_b = self.vds_b_v * (1.5e-6 + 4.0e-5 * abs(self.gate_b_v))
        gate_b = -self.gate_b_v * 1.2e-8
        return DualADCMTSample(index, drain_a, gate_a, drain_b, gate_b,
                               (index + 1) * self.interval_s, 0)

    def read_available(
        self, timeout_s: float = 0.05, max_batch: int = 512
    ) -> list[DualADCMTSample]:
        if self.started is None:
            raise RuntimeError("Simulation has not started")
        next_time = self.started + (self.last_index + 2) * self.interval_s
        wait = next_time - time.monotonic()
        if wait > 0:
            time.sleep(min(wait, timeout_s))
        available = int((time.monotonic() - self.started) / self.interval_s) - self.last_index - 1
        return [self.read_next(timeout_s=0.001)
                for _ in range(min(max(0, available), max_batch))]

    def set_gate(self, channel: int, voltage_v: float) -> float:
        if channel == 2:
            self.gate_a_v = voltage_v
        elif channel == 4:
            self.gate_b_v = voltage_v
        else:
            raise ValueError("Gate channel must be CH2 or CH4")
        return voltage_v

    def zero_cell(self, cell: str) -> None:
        if cell == "a":
            self.gate_a_v = self.vds_a_v = 0.0
        elif cell == "b":
            self.gate_b_v = self.vds_b_v = 0.0
        else:
            raise ValueError("Cell must be a or b")
        self.zeroed_cells.append(cell)

    def shutdown(self) -> None:
        self.gate_a_v = self.vds_a_v = 0.0
        self.gate_b_v = self.vds_b_v = 0.0
        self.closed = True


class SimulatedDMM:
    def __init__(self, adcmt: SimulatedDualADCMT) -> None:
        self.adcmt = adcmt

    def connect(self) -> str:
        return "SIMULATED DMM6500"

    def configure(self) -> None:
        return None

    def read_vref(self) -> float:
        elapsed = (0.0 if self.adcmt.started is None else
                   time.monotonic() - self.adcmt.started)
        return (self.adcmt.feedback_polarity * 0.82 * self.adcmt.gate_a_v
                + 0.003 * math.sin(elapsed / 12))

    def close(self) -> None:
        return None


@dataclass
class _CellState:
    levels: tuple[float, ...]
    hold_s: float
    delay_s: float
    keep_s: float
    ticks_per_read: int
    gate_v: float
    drain_v: float
    stage_index: int = 0
    stage_start: float = 0.0
    stage_end: float = 0.0
    stage_has_sample: bool = False
    done: bool = False
    gate_changed_elapsed: float | None = None
    saturated_count: int = 0
    samples: int = 0

    @property
    def target_v(self) -> float:
        return self.levels[self.stage_index]

    def start(self, now_elapsed: float) -> None:
        self.stage_start = now_elapsed
        self.stage_end = now_elapsed + self.hold_s
        if self.stage_index == len(self.levels) - 1:
            self.stage_end += self.keep_s
        self.stage_has_sample = False

    def phase(self, elapsed: float) -> str:
        age = elapsed - self.stage_start
        if self.stage_index == len(self.levels) - 1 and age >= self.hold_s:
            return "final_keep"
        return "settling" if age < self.delay_s else "measure"


def run_experiment(
    plan: DualPlan,
    on_sample_a: Callable[[Sample], None] | None = None,
    on_sample_b: Callable[[BSample], None] | None = None,
    on_status: Callable[[str], None] | None = None,
    stop_event: threading.Event | None = None,
) -> DualResult:
    """Run independent A/B schedules and keep both text files separate."""
    plan.validate()
    a, b = plan.a, plan.b
    base_s = plan.hardware_interval_s
    ticks_a = round(a.sample_interval_s / base_s)
    ticks_b = round(b.sample_interval_s / base_s)
    stop = stop_event or threading.Event()
    status = on_status or (lambda _message: None)
    report_a = on_sample_a or (lambda _sample: None)
    report_b = on_sample_b or (lambda _sample: None)

    adcmt = (SimulatedDualADCMT(base_s, a.feedback_polarity) if a.simulation
             else DualADCMT6541(a.adcmt_ip, a.port, timeout_s=max(5.0, base_s * 3)))
    dmm = (SimulatedDMM(adcmt) if a.simulation
           else DMM6500(a.dmm_ip, timeout_s=max(5.0, a.sample_interval_s * 3)))
    gate_a = a.gate_initial_v if a.mode == "feedback" else a.levels[0]
    state_a = _CellState(a.levels, a.hold_s, a.delay_s, a.final_keep_s,
                         ticks_a, gate_a, a.vds_v)
    state_b = _CellState(b.levels, b.hold_s, b.delay_s, b.final_keep_s,
                         ticks_b, b.levels[0], b.vds_v)
    writer_a: RawDataWriter | None = None
    writer_b: BDataWriter | None = None
    started: float | None = None
    failure: Exception | None = None
    try:
        # Verify the passive DMM before enabling any of the four ADCMT outputs.
        status("Connecting DMM6500 and verifying high-impedance reference input")
        dmm_id = dmm.connect()
        dmm.configure()
        status(f"DMM ready: {dmm_id}")
        if stop.is_set():
            return DualResult(a.output_path, b.output_path, 0, 0, True)

        status("Connecting ADCMT 6541; configuring four synchronized channels")
        adcmt_id = adcmt.connect()
        adcmt.configure(
            vds_a_v=a.vds_v, gate_a_v=gate_a,
            vds_b_v=b.vds_v, gate_b_v=b.levels[0],
            limits_a=(a.drain_limit_a, a.gate_limit_a,
                      b.drain_limit_a, b.gate_limit_a),
            interval_s=base_s,
        )
        status(f"ADCMT ready: {adcmt_id}")
        writer_a = RawDataWriter(a.output_path)
        writer_b = BDataWriter(b.output_path)
        adcmt.start()
        # Both stage deadlines and gate-change checks must use the same
        # origin as nominal ADCMT sample times. The live start() call itself
        # performs LAN readbacks after the hardware clock has begun.
        started = adcmt.clock_started_monotonic
        state_a.start(0.0)
        state_b.start(0.0)
        status(f"Both OECTs running; common ADCMT interval {base_s:g} s")

        def advance_due(name: str, state: _CellState) -> None:
            nonlocal writer_a, writer_b
            if (state.done or not state.stage_has_sample or
                    time.monotonic() - started < state.stage_end):
                return
            if state.stage_index + 1 == len(state.levels):
                # SOV0 holds both terminals at zero. CH1 remains Operating as
                # the timing master until B also finishes.
                adcmt.zero_cell(name.lower())
                state.gate_v = state.drain_v = 0.0
                state.done = True
                if name == "A":
                    assert writer_a is not None
                    writer_a.close()
                    writer_a = None
                else:
                    assert writer_b is not None
                    writer_b.close()
                    writer_b = None
                status(f"OECT {name} complete; Vds and Vg verified at 0 V; logging stopped")
                return

            state.stage_index += 1
            state.start(time.monotonic() - started)
            state.saturated_count = 0
            # The batch fetched before this transition belongs to the old
            # level, including in feedback mode where the gate setpoint may
            # remain unchanged. Wait for a pair sampled after the transition.
            state.gate_changed_elapsed = time.monotonic() - started
            if name == "B" or a.mode == "monitor":
                channel = 2 if name == "A" else 4
                state.gate_v = adcmt.set_gate(channel, state.target_v)
                state.gate_changed_elapsed = time.monotonic() - started
            quantity = "Eref target" if name == "A" and a.mode == "feedback" else "Vg"
            status(f"OECT {name} level {state.stage_index + 1}/{len(state.levels)}: "
                   f"{quantity} {state.target_v:+.4f} V")

        while not stop.is_set() and not (state_a.done and state_b.done):
            # Read buffered ADCMT pairs before changing any source setpoint.
            # This keeps earlier B readings assigned to the gate level that
            # was actually active when the hardware sampled them.
            pairs = adcmt.read_available(timeout_s=0.05)
            if stop.is_set():
                break

            for pair in pairs:
                if state_b.done or (pair.index + 1) % state_b.ticks_per_read:
                    continue
                fresh_b = (state_b.gate_changed_elapsed is None or
                           pair.nominal_elapsed_s > state_b.gate_changed_elapsed)
                within_stage = (not state_b.stage_has_sample or
                                pair.nominal_elapsed_s <= state_b.stage_end + 1e-6)
                if not fresh_b or not within_stage:
                    continue
                sample_b = BSample(
                    utc=_utc_now(), elapsed_s=pair.nominal_elapsed_s,
                    stage_index=state_b.stage_index + 1,
                    phase=state_b.phase(pair.nominal_elapsed_s),
                    target_v=state_b.target_v,
                    gate_i_a=pair.gate_b_i_a, gate_v=state_b.gate_v,
                    drain_i_a=pair.drain_b_i_a, drain_v=state_b.drain_v,
                    adcmt_index=pair.index,
                    adcmt_nominal_elapsed_s=pair.nominal_elapsed_s,
                    adcmt_skipped=pair.skipped,
                )
                assert writer_b is not None
                writer_b.write(sample_b)
                state_b.stage_has_sample = True
                state_b.samples += 1
                state_b.gate_changed_elapsed = None
                report_b(sample_b)
                if stop.is_set():
                    break

            if stop.is_set():
                break

            # B's next gate command or zero hold can happen before the DMM
            # transaction for A. After this point all pairs in the batch are
            # old, so A will discard them if its gate/target just changed.
            advance_due("B", state_b)
            advance_due("A", state_a)

            if not state_a.done:
                eligible_a = [
                    pair for pair in pairs
                    if (pair.index + 1) % state_a.ticks_per_read == 0
                    and (state_a.gate_changed_elapsed is None or
                         pair.nominal_elapsed_s > state_a.gate_changed_elapsed)
                    and (not state_a.stage_has_sample or
                         pair.nominal_elapsed_s <= state_a.stage_end + 1e-6)
                ]
                # A's DMM reading is a live host query. Associate it only with
                # the newest eligible ADCMT pair, never a long backlog of old
                # pairs; B's complete hardware batch has already been saved.
                if eligible_a:
                    pair = eligible_a[-1]
                    vref = dmm.read_vref()
                    if stop.is_set():
                        break
                    dmm_time = _utc_now()
                    if not math.isfinite(vref) or abs(vref) > 10:
                        raise ValueError(f"DMM returned invalid Vref={vref!r}")
                    elapsed_a = time.monotonic() - started
                    sample_a = Sample(
                        utc=_utc_now(), elapsed_s=elapsed_a,
                        stage_index=state_a.stage_index + 1,
                        phase=state_a.phase(elapsed_a),
                        target_v=state_a.target_v,
                        gate_i_a=pair.gate_a_i_a, gate_v=state_a.gate_v,
                        drain_i_a=pair.drain_a_i_a, drain_v=state_a.drain_v,
                        vref_v=vref, e_reference_v=vref,
                        adcmt_index=pair.index,
                        adcmt_nominal_elapsed_s=pair.nominal_elapsed_s,
                        adcmt_skipped=pair.skipped,
                        dmm_read_utc=dmm_time,
                        within_tolerance=(abs(state_a.target_v - vref) <= a.feedback_tolerance_v
                                          if a.mode == "feedback" else None),
                    )
                    assert writer_a is not None
                    writer_a.write(sample_a)
                    state_a.stage_has_sample = True
                    state_a.samples += 1
                    state_a.gate_changed_elapsed = None
                    report_a(sample_a)
                    if stop.is_set():
                        break
                    if a.mode == "feedback":
                        new_vg = next_feedback_gate(state_a.gate_v, state_a.target_v,
                                                    vref, a)
                        if not math.isclose(new_vg, state_a.gate_v, abs_tol=1e-12):
                            state_a.gate_v = adcmt.set_gate(2, new_vg)
                            state_a.gate_changed_elapsed = time.monotonic() - started
                        at_limit = (math.isclose(state_a.gate_v, a.gate_min_v, abs_tol=1e-8) or
                                    math.isclose(state_a.gate_v, a.gate_max_v, abs_tol=1e-8))
                        if at_limit and abs(state_a.target_v - vref) > max(0.05, 2 * a.feedback_tolerance_v):
                            state_a.saturated_count += 1
                            if state_a.saturated_count >= 10:
                                raise RuntimeError(
                                    "OECT A feedback could not reach target before gate limit"
                                )
                        else:
                            state_a.saturated_count = 0

            advance_due("A", state_a)
            advance_due("B", state_b)

        return DualResult(a.output_path, b.output_path,
                          state_a.samples, state_b.samples, stop.is_set())
    except Exception as exc:
        failure = exc
        raise
    finally:
        errors: list[str] = []
        try:
            adcmt.shutdown()
        except Exception as exc:
            errors.append(f"ADCMT four-channel shutdown could not be verified: {exc}")
        try:
            dmm.close()
        except Exception as exc:
            errors.append(f"DMM close failed: {exc}")
        for name, writer in (("A", writer_a), ("B", writer_b)):
            if writer is not None:
                try:
                    writer.close()
                except Exception as exc:
                    errors.append(f"OECT {name} file close failed: {exc}")
        if errors:
            message = "; ".join(errors)
            status(message)
            if failure is not None:
                raise RuntimeError(f"{failure}; {message}") from failure
            raise RuntimeError(message)
        if started is not None:
            status("Stopped: all four ADCMT channels set to 0 V and Standby")
