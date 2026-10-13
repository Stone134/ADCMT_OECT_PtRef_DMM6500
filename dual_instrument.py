"""Four-channel ADCMT 6541 driver for two independently scheduled OECTs.

CH1/CH2 source drain/gate for OECT A; CH3/CH4 do the same for OECT B.
CH1 supplies the shared sample clock.  A completed cell is held at zero
volts with its outputs still operating until the other cell finishes.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass

from instruments import ADCMT6541, InstrumentError, _first_float, _number


@dataclass(frozen=True)
class DualADCMTSample:
    index: int
    drain_a_i_a: float
    gate_a_i_a: float
    drain_b_i_a: float
    gate_b_i_a: float
    nominal_elapsed_s: float
    skipped: int


class DualADCMT6541(ADCMT6541):
    """Operate four synchronized sources and return matched current samples."""

    @property
    def clock_started_monotonic(self) -> float:
        """Host time used as the origin of ADCMT nominal sample times."""
        if not self._started:
            raise InstrumentError("ADCMT sampling has not started")
        return self._start_monotonic

    def configure(
        self,
        *,
        vds_a_v: float,
        gate_a_v: float,
        vds_b_v: float,
        gate_b_v: float,
        limits_a: tuple[float, float, float, float],
        interval_s: float,
    ) -> None:
        if not self.io.connected or not self.identity:
            raise InstrumentError("Connect to ADCMT before configuring")
        if self._started:
            raise InstrumentError("ADCMT is already sampling")
        if len(limits_a) != 4:
            raise InstrumentError("Specify a current limit for each of the four ADCMT channels")
        voltages = (vds_a_v, gate_a_v, vds_b_v, gate_b_v)
        if not all(math.isfinite(v) and abs(v) <= 3 for v in voltages):
            raise InstrumentError("ADCMT source voltages must be finite and within ±3 V")
        if not all(math.isfinite(limit) and 0 < limit <= 0.5 for limit in limits_a):
            raise InstrumentError("ADCMT current limits must be finite, above 0, and at most 0.5 A")
        if not math.isfinite(interval_s) or not 0.1 <= interval_s <= 60:
            raise InstrumentError("ADCMT sample interval must be 0.1 to 60 s")

        # Start from Standby even if a previous application used different
        # master/slave roles on the same four physical channels.
        for channel in (1, 2, 3, 4):
            self._select(channel)
            self.io.write("SBY")

        self.io.write("DL1")
        self.io.write("OH0")
        period_ms = interval_s * 1000
        timing_adjust_ms = 1.0 if period_ms <= 6000 else 2.0
        for channel, (voltage, limit) in enumerate(zip(voltages, limits_a), start=1):
            self._select(channel)
            for command in (
                "SBY", "MD0", "VF", "SVR4", f"LMI{_number(limit)}",
                f"SOV{_number(voltage)}", "FX0", "F2", "R0", "RD0",
                "IT5", "AZ0", "RE5", "SPN1", "ST0", "RL",
                f"RNM{self.MEMORY_CAPACITY}", "ST1",
            ):
                self.io.write(command)
            if channel == 1:
                timing = (
                    f"SP0,0.055,{_number(period_ms)}", "SD0.035",
                    "OP3", "CP6", "TG0", "M1",
                )
            else:
                timing = (
                    f"SP0,0.050,{_number(period_ms - timing_adjust_ms)}",
                    "SD0.030", "OP1", "CP7", "TG1", "M1",
                )
            for command in timing:
                self.io.write(command)
            self._check_source_setup(channel, voltage, limit)

        self.interval_s = interval_s
        self._configured = True
        self._next_address = 0
        self._epoch_index_base = 0
        self._epoch_elapsed_base = 0.0
        self._last_returned_index = -1

    def _set_channel_voltage(self, channel: int, voltage_v: float) -> float:
        if not self._configured or not math.isfinite(voltage_v) or abs(voltage_v) > 3:
            raise InstrumentError("ADCMT voltage setpoint must be finite and within ±3 V")
        self._select(channel)
        self.io.write(f"SOV{_number(voltage_v)}")
        actual = _first_float(self.io.query("SOV?"), label=f"ADCMT CH{channel} voltage")
        if not math.isclose(actual, voltage_v, abs_tol=0.00011):
            raise InstrumentError(
                f"ADCMT CH{channel} voltage readback {actual:g} V "
                f"differs from request {voltage_v:g} V"
            )
        return actual

    def set_gate(self, channel: int, voltage_v: float) -> float:
        if channel not in (2, 4):
            raise InstrumentError("ADCMT gate voltage channels are CH2 and CH4")
        return self._set_channel_voltage(channel, voltage_v)

    def zero_cell(self, cell: str) -> None:
        """Hold the specified cell at Vg = Vds = 0 without stopping its clock.

        CH1 in particular must remain operating when A finishes before B:
        putting it in Standby would remove the sync signal for all followers.
        """
        if cell not in ("a", "b"):
            raise InstrumentError("OECT cell must be 'a' or 'b'")
        gate, drain = (2, 1) if cell == "a" else (4, 3)
        errors: list[str] = []
        # Attempt both zero commands even if the first readback fails.
        for channel in (gate, drain):
            try:
                self._set_channel_voltage(channel, 0.0)
            except Exception as exc:
                errors.append(f"CH{channel}: {exc}")
        if not errors:
            for channel in (gate, drain):
                self._select(channel)
                state = self.io.query("OPR?").strip().upper()
                if state != "OPR":
                    errors.append(f"CH{channel} is {state!r} rather than Operate")
        if errors:
            raise InstrumentError(f"Could not hold OECT {cell.upper()} at zero: " + "; ".join(errors))

    def _rollover(self) -> None:
        # Pause the common master before clearing any channel memory; all four
        # counters must agree or the global sample index would be ambiguous.
        self._select(1)
        self.io.write("M1")
        time.sleep(0.03)
        counts = tuple(self._count(channel) for channel in (1, 2, 3, 4))
        if len(set(counts)) != 1:
            time.sleep(0.03)
            counts = tuple(self._count(channel) for channel in (1, 2, 3, 4))
        if len(set(counts)) != 1:
            raise InstrumentError(
                "ADCMT memory rollover found mismatched channel counts "
                f"{counts}; sampling stopped"
            )
        self._epoch_index_base += counts[0]
        for channel in (1, 2, 3, 4):
            self._select(channel)
            for command in ("ST0", "RL", f"RNM{self.MEMORY_CAPACITY}", "ST1"):
                self.io.write(command)
        self._next_address = 0
        self._epoch_elapsed_base = time.monotonic() - self._start_monotonic
        self._select(1)
        self.io.write("M0")

    def _currents_batch(self, channel: int, start: int, stop: int) -> list[float]:
        """Fetch one matching memory range, rejecting partial or invalid replies."""
        self._select(channel)
        self.io.write(f"RDN{start},{stop}")
        response = self.io.query("RDT?")
        parts = response.split(",") if response else []
        expected = stop - start + 1
        if len(parts) != expected:
            raise InstrumentError(
                f"ADCMT CH{channel} returned {len(parts)} currents for "
                f"addresses {start}..{stop}, expected {expected}"
            )
        currents = [
            _first_float(part, label=f"ADCMT CH{channel} current") for part in parts
        ]
        if any(abs(current) >= 8e30 or abs(current) > 0.5 for current in currents):
            raise InstrumentError(
                f"ADCMT CH{channel} returned an invalid current in {start}..{stop}"
            )
        return currents

    def read_available(
        self, timeout_s: float = 0.05, max_batch: int = 512
    ) -> list[DualADCMTSample]:
        """Read up to ``max_batch`` oldest common four-channel samples.

        The 512-point ceiling keeps each ADCMT text response well below the
        transport's one-megabyte limit.  If the memory is already nearing
        capacity, fetch the newest batch and explicitly report the discarded
        backlog before resetting all four memories together.
        """
        if not self._started:
            raise InstrumentError("ADCMT sampling has not started")
        if not math.isfinite(timeout_s) or timeout_s < 0:
            raise InstrumentError("ADCMT read timeout must be nonnegative and finite")
        if isinstance(max_batch, bool) or not isinstance(max_batch, int) or max_batch < 1:
            raise InstrumentError("ADCMT maximum batch size must be a positive integer")
        max_batch = min(max_batch, 512)
        deadline = time.monotonic() + timeout_s
        while True:
            counts = tuple(self._count(channel) for channel in (1, 2, 3, 4))
            common = min(counts)
            if common < self._next_address:
                raise InstrumentError("ADCMT sample memory was cleared unexpectedly")
            if common > self._next_address:
                if common >= self.ROLLOVER_AT:
                    start = max(self._next_address, common - max_batch)
                else:
                    start = self._next_address
                stop = min(common, start + max_batch) - 1
                currents = tuple(
                    self._currents_batch(channel, start, stop)
                    for channel in (1, 2, 3, 4)
                )
                samples: list[DualADCMTSample] = []
                for offset, address in enumerate(range(start, stop + 1)):
                    global_index = self._epoch_index_base + address
                    sample = DualADCMTSample(
                        index=global_index,
                        drain_a_i_a=currents[0][offset],
                        gate_a_i_a=currents[1][offset],
                        drain_b_i_a=currents[2][offset],
                        gate_b_i_a=currents[3][offset],
                        nominal_elapsed_s=self._epoch_elapsed_base
                        + (address + 1) * self.interval_s,
                        skipped=global_index - self._last_returned_index - 1,
                    )
                    samples.append(sample)
                    self._last_returned_index = global_index
                self._next_address = stop + 1
                if common >= self.ROLLOVER_AT:
                    self._rollover()
                return samples
            if common >= self.ROLLOVER_AT:
                self._rollover()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return []
            time.sleep(min(0.02, remaining))

    def read_next(self, timeout_s: float) -> DualADCMTSample:
        if not self._started:
            raise InstrumentError("ADCMT sampling has not started")
        if not math.isfinite(timeout_s) or timeout_s <= 0:
            raise InstrumentError("ADCMT read timeout must be positive and finite")
        deadline = time.monotonic() + timeout_s
        while True:
            counts = tuple(self._count(channel) for channel in (1, 2, 3, 4))
            common = min(counts)
            if common < self._next_address:
                raise InstrumentError("ADCMT sample memory was cleared unexpectedly")
            if common > self._next_address:
                # Drain the oldest matched sample so a temporarily slow DMM
                # does not discard OECT B's earlier readings.  Near memory
                # capacity, skip backlog once to leave room for safe rollover.
                address = common - 1 if common >= self.ROLLOVER_AT else self._next_address
                global_index = self._epoch_index_base + address
                currents = tuple(self._current(ch, address) for ch in (1, 2, 3, 4))
                result = DualADCMTSample(
                    index=global_index,
                    drain_a_i_a=currents[0],
                    gate_a_i_a=currents[1],
                    drain_b_i_a=currents[2],
                    gate_b_i_a=currents[3],
                    nominal_elapsed_s=self._epoch_elapsed_base + (address + 1) * self.interval_s,
                    skipped=global_index - self._last_returned_index - 1,
                )
                self._last_returned_index = global_index
                self._next_address = address + 1
                if common >= self.ROLLOVER_AT:
                    self._rollover()
                return result
            if common >= self.ROLLOVER_AT:
                self._rollover()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstrumentError("Timed out waiting for the next synchronized ADCMT sample")
            time.sleep(min(0.02, remaining))

    def shutdown(self) -> None:
        """Zero all sources, verify Standby on all four, and close LAN."""
        errors: list[str] = []
        if self._outputs_may_be_on and not self.io.connected:
            errors.append("LAN connection was lost while output may be on; verify Standby at the ADCMT")
        if self.io.connected:
            for channel in (2, 4, 1, 3):
                try:
                    self._select(channel)
                    self.io.write("SOV0")
                    readback = _first_float(
                        self.io.query("SOV?"), label=f"ADCMT CH{channel} voltage"
                    )
                    if not math.isclose(readback, 0.0, abs_tol=0.00011):
                        raise InstrumentError(f"CH{channel} remained at {readback:g} V")
                except Exception as exc:
                    errors.append(f"CH{channel} zero: {exc}")
            for channel in (1, 2, 3, 4):
                try:
                    self._select(channel)
                    self.io.write("SBY")
                    response = self.io.query("SBY?").strip().upper()
                    if response != "SBY":
                        raise InstrumentError(f"CH{channel} Standby was not verified: {response!r}")
                except Exception as exc:
                    errors.append(f"CH{channel} standby: {exc}")
        self.io.close()
        self._started = False
        self._configured = False
        self._outputs_may_be_on = False
        if errors:
            raise InstrumentError("ADCMT shutdown incomplete: " + "; ".join(errors))
