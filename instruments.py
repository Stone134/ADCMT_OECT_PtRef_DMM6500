"""LAN drivers for one ADCMT 6541 OECT and a passive Pt-reference DMM6500.

The ADCMT 6541 clocks both current measurements internally.  The DMM6500 is
queried separately over LAN, so its reference reading is *near* the ADCMT
sample in time, not hardware synchronous with it.  No instrument is contacted
when this module is imported.
"""

from __future__ import annotations

import math
import re
import socket
import time
from dataclasses import dataclass


class InstrumentError(RuntimeError):
    """A connection, instrument-state, or measurement error."""


@dataclass(frozen=True)
class ADCMTSample:
    index: int
    drain_i_a: float
    gate_i_a: float
    nominal_elapsed_s: float
    skipped: int


_FLOAT = re.compile(r"[-+]?(?:\d+(?:\.\d*)?|\.\d+)(?:[Ee][-+]?\d+)?")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise InstrumentError("Instrument setpoint must be finite")
    return f"{value:.12g}"


def _first_float(response: str, *, label: str) -> float:
    match = _FLOAT.search(response)
    if match is None:
        raise InstrumentError(f"No numeric {label} in instrument response {response!r}")
    value = float(match.group())
    if not math.isfinite(value):
        raise InstrumentError(f"Invalid {label} in instrument response {response!r}")
    return value


class _LineSocket:
    """LF-terminated, single-command raw TCP transport shared by both devices."""

    def __init__(self, host: str, port: int, timeout_s: float) -> None:
        if not host.strip() or not 1 <= port <= 65535 or timeout_s <= 0:
            raise InstrumentError("Invalid instrument address or socket timeout")
        self.host = host.strip()
        self.port = port
        self.timeout_s = timeout_s
        self._sock: socket.socket | None = None
        self._pending = bytearray()

    @property
    def connected(self) -> bool:
        return self._sock is not None

    def connect(self) -> None:
        if self._sock is not None:
            raise InstrumentError(f"Already connected to {self.host}:{self.port}")
        try:
            self._sock = socket.create_connection((self.host, self.port), self.timeout_s)
            self._sock.settimeout(self.timeout_s)
            self._pending.clear()
        except OSError as exc:
            self.close()
            raise InstrumentError(f"Cannot connect to {self.host}:{self.port}: {exc}") from exc

    def close(self) -> None:
        sock, self._sock = self._sock, None
        self._pending.clear()
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

    def write(self, command: str) -> None:
        if self._sock is None:
            raise InstrumentError(f"Not connected to {self.host}:{self.port}")
        if not command or any(c in command for c in "\r\n"):
            raise InstrumentError("Instrument command must be one nonempty line")
        try:
            self._sock.sendall(command.encode("ascii") + b"\n")
        except (UnicodeError, OSError) as exc:
            self.close()
            raise InstrumentError(f"Failed sending {command!r} to {self.host}: {exc}") from exc

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        self.write(command)
        sock = self._sock
        assert sock is not None
        original_timeout = sock.gettimeout()
        try:
            if timeout_s is not None:
                sock.settimeout(timeout_s)
            while b"\n" not in self._pending:
                chunk = sock.recv(4096)
                if not chunk:
                    raise InstrumentError(f"{self.host} closed the connection during {command!r}")
                self._pending.extend(chunk)
                if len(self._pending) > 1_000_000:
                    raise InstrumentError(f"Oversized response to {command!r} from {self.host}")
            raw, _, remainder = self._pending.partition(b"\n")
            self._pending = bytearray(remainder)
            return raw.decode("ascii", errors="strict").strip()
        except (OSError, UnicodeError, InstrumentError) as exc:
            # A timed-out query can leave part of a response pending.  Closing
            # avoids interpreting that stale line as the next query response.
            self.close()
            raise InstrumentError(f"Failed reading {command!r} from {self.host}: {exc}") from exc
        finally:
            if self._sock is sock:
                sock.settimeout(original_timeout)


class ADCMT6541:
    """CH1 drain/master, CH2 gate/slave, CH3/4 parked in Standby."""

    MEMORY_CAPACITY = 10_000
    ROLLOVER_AT = 9_000

    def __init__(self, host: str, port: int = 5025, timeout_s: float = 5) -> None:
        self.io = _LineSocket(host, port, timeout_s)
        self.identity = ""
        self.interval_s = 0.0
        self._configured = False
        self._started = False
        self._next_address = 0
        self._epoch_index_base = 0
        self._epoch_elapsed_base = 0.0
        self._start_monotonic = 0.0
        self._last_returned_index = -1
        self._outputs_may_be_on = False

    def connect(self) -> str:
        self.io.connect()
        try:
            identity = self.io.query("*IDN?")
            if "6541" not in identity.upper():
                raise InstrumentError(f"Expected an ADCMT 6541, received {identity!r}")
            self.identity = identity
            return identity
        except Exception:
            self.io.close()
            raise

    def _select(self, channel: int) -> None:
        if channel not in (1, 2, 3, 4):
            raise InstrumentError(f"Invalid ADCMT channel {channel}")
        self.io.write(f"SCH{channel}")

    def _check_source_setup(self, channel: int, voltage: float, limit: float) -> None:
        self._select(channel)
        checks = (("MD?", "MD0"), ("SVR?", "SVR4"), ("F?", "F2"), ("R?", "R0"))
        for command, expected in checks:
            response = self.io.query(command).replace(" ", "").upper()
            if response != expected:
                raise InstrumentError(
                    f"ADCMT CH{channel} {command} was {response!r}, expected {expected}"
                )
        returned_v = _first_float(self.io.query("SOV?"), label="ADCMT source voltage")
        if not math.isclose(returned_v, voltage, abs_tol=0.00011):
            raise InstrumentError(
                f"ADCMT CH{channel} source voltage readback {returned_v:g} V "
                f"differs from {voltage:g} V"
            )
        limit_reply = self.io.query("LMI?")
        limit_values = [float(part) for part in _FLOAT.findall(limit_reply[3:])]
        if len(limit_values) < 2 or not (
            math.isclose(limit_values[0], limit, rel_tol=0.005, abs_tol=1e-8)
            and math.isclose(limit_values[1], -limit, rel_tol=0.005, abs_tol=1e-8)
        ):
            raise InstrumentError(
                f"ADCMT CH{channel} current limit readback {limit_reply!r} "
                f"differs from ±{limit:g} A"
            )

    def configure(
        self,
        vds_v: float,
        gate_initial_v: float,
        drain_limit_a: float,
        gate_limit_a: float,
        interval_s: float,
    ) -> None:
        if not self.io.connected or not self.identity:
            raise InstrumentError("Connect to ADCMT before configuring")
        if self._started:
            raise InstrumentError("ADCMT is already sampling")
        values = (vds_v, gate_initial_v, drain_limit_a, gate_limit_a, interval_s)
        if not all(math.isfinite(v) for v in values):
            raise InstrumentError("ADCMT configuration contains a nonfinite value")
        if abs(vds_v) > 3 or abs(gate_initial_v) > 3:
            raise InstrumentError("ADCMT source voltages must be within ±3 V")
        if not (0 < drain_limit_a <= 0.5 and 0 < gate_limit_a <= 0.5):
            raise InstrumentError("ADCMT current limits must be above 0 and at most 0.5 A")
        if not 0.1 <= interval_s <= 60:
            raise InstrumentError("ADCMT sample interval must be 0.1 to 60 s")

        # Park all four outputs first.  This is necessary if an earlier program
        # left CH3/4 armed or another synchronization role configured.
        for channel in (1, 2, 3, 4):
            self._select(channel)
            self.io.write("SBY")
        for channel in (3, 4):
            self._select(channel)
            for command in ("OP5", "CP7", "TG0", "M1"):
                self.io.write(command)

        self.io.write("DL1")  # ADCMT responses end with LF.
        self.io.write("OH0")  # Numeric readings have no header.
        period_ms = interval_s * 1000
        timing_adjust_ms = 1.0 if period_ms <= 6000 else 2.0
        channel_setups = (
            (1, vds_v, drain_limit_a, period_ms),
            (2, gate_initial_v, gate_limit_a, period_ms - timing_adjust_ms),
        )
        for channel, voltage, limit, sample_period_ms in channel_setups:
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
                    f"SP0,0.055,{_number(sample_period_ms)}", "SD0.035",
                    "OP3", "CP6", "TG0", "M1",
                )
            else:
                timing = (
                    f"SP0,0.050,{_number(sample_period_ms)}", "SD0.030",
                    "OP1", "CP7", "TG1", "M1",
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

    def start(self) -> None:
        if not self._configured or self._started:
            raise InstrumentError("Configure ADCMT before starting")
        self._select(1)
        self._outputs_may_be_on = True
        self.io.write("OPR")
        self._start_monotonic = time.monotonic()
        self.io.write("M0")
        output_state = self.io.query("OPR?").strip().upper()
        trigger_mode = self.io.query("M?").strip().upper()
        if output_state != "OPR" or trigger_mode != "M0":
            raise InstrumentError(
                f"ADCMT did not start: output {output_state!r}, trigger {trigger_mode!r}"
            )
        self._started = True

    def _count(self, channel: int) -> int:
        self._select(channel)
        response = self.io.query("SZ?")
        count = int(_first_float(response, label="ADCMT sample count"))
        if not 0 <= count <= self.MEMORY_CAPACITY:
            raise InstrumentError(f"ADCMT CH{channel} returned invalid sample count {response!r}")
        return count

    def _current(self, channel: int, address: int) -> float:
        self._select(channel)
        self.io.write(f"RDN{address},{address}")
        response = self.io.query("RDT?")
        current = _first_float(response, label=f"ADCMT CH{channel} current")
        if abs(current) >= 8e30 or abs(current) > 0.5:
            raise InstrumentError(f"ADCMT CH{channel} returned invalid current {response!r}")
        return current

    def _rollover(self) -> None:
        # HOLD pauses the master, and therefore the synchronized slave.  Read
        # both stable counters before erasing; mismatched counts would make
        # subsequent global indices uncertain, so stop rather than guess.
        self._select(1)
        self.io.write("M1")
        time.sleep(0.03)
        count1, count2 = self._count(1), self._count(2)
        if count1 != count2:
            # The two counters are read sequentially; a final in-flight
            # synchronized sample can briefly leave one count ahead.
            time.sleep(0.03)
            count1, count2 = self._count(1), self._count(2)
        if count1 != count2:
            raise InstrumentError(
                f"ADCMT memory rollover found mismatched channel counts "
                f"({count1}, {count2}); sampling stopped"
            )
        self._epoch_index_base += count1
        for channel in (1, 2):
            self._select(channel)
            for command in ("ST0", "RL", f"RNM{self.MEMORY_CAPACITY}", "ST1"):
                self.io.write(command)
        self._next_address = 0
        self._epoch_elapsed_base = time.monotonic() - self._start_monotonic
        self._select(1)
        self.io.write("M0")

    def read_next(self, timeout_s: float) -> ADCMTSample:
        if not self._started:
            raise InstrumentError("ADCMT sampling has not started")
        if timeout_s <= 0 or not math.isfinite(timeout_s):
            raise InstrumentError("ADCMT read timeout must be positive and finite")
        deadline = time.monotonic() + timeout_s
        while True:
            count1, count2 = self._count(1), self._count(2)
            common = min(count1, count2)
            if common < self._next_address:
                raise InstrumentError("ADCMT sample memory was cleared unexpectedly")
            if common > self._next_address:
                # A slow DMM or busy host may allow several ADCMT ticks to
                # pass.  Only return the newest common pair, and report the
                # omitted earlier pairs as skipped to the caller.
                address = common - 1
                global_index = self._epoch_index_base + address
                skipped = global_index - self._last_returned_index - 1
                drain = self._current(1, address)
                gate = self._current(2, address)
                result = ADCMTSample(
                    index=global_index,
                    drain_i_a=drain,
                    gate_i_a=gate,
                    nominal_elapsed_s=self._epoch_elapsed_base + (address + 1) * self.interval_s,
                    skipped=skipped,
                )
                self._last_returned_index = global_index
                self._next_address = common
                if common >= self.ROLLOVER_AT:
                    self._rollover()
                return result
            if common >= self.ROLLOVER_AT:
                self._rollover()
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise InstrumentError("Timed out waiting for the next synchronized ADCMT sample")
            time.sleep(min(0.02, remaining))

    def set_gate(self, voltage_v: float) -> float:
        if not self._configured or not math.isfinite(voltage_v) or abs(voltage_v) > 3:
            raise InstrumentError("ADCMT gate setpoint must be finite and within ±3 V")
        self._select(2)
        self.io.write(f"SOV{_number(voltage_v)}")
        actual = _first_float(self.io.query("SOV?"), label="ADCMT gate voltage")
        if not math.isclose(actual, voltage_v, abs_tol=0.00011):
            raise InstrumentError(
                f"ADCMT gate readback {actual:g} V differs from request {voltage_v:g} V"
            )
        return actual

    def shutdown(self) -> None:
        """Set both active sources to zero, park all channels, and close TCP."""
        errors: list[str] = []
        if self._outputs_may_be_on and not self.io.connected:
            errors.append("LAN connection was lost while output may be on; verify Standby at the ADCMT")
        if self.io.connected:
            for channel in (2, 1):
                try:
                    self._select(channel)
                    self.io.write("SOV0")
                except Exception as exc:
                    errors.append(f"CH{channel} zero: {exc}")
            for channel in (1, 2, 3, 4):
                try:
                    self._select(channel)
                    self.io.write("SBY")
                    response = self.io.query("SBY?").strip().upper()
                    if response != "SBY":
                        raise InstrumentError(
                            f"CH{channel} Standby was not verified: {response!r}"
                        )
                except Exception as exc:
                    errors.append(f"CH{channel} standby: {exc}")
        self.io.close()
        self._started = False
        self._configured = False
        self._outputs_may_be_on = False
        if errors:
            raise InstrumentError("ADCMT shutdown incomplete: " + "; ".join(errors))

    def close(self) -> None:
        if self._outputs_may_be_on:
            if self.io.connected:
                self.shutdown()
                return
            self.io.close()
            self._started = False
            self._configured = False
            self._outputs_may_be_on = False
            raise InstrumentError(
                "ADCMT LAN was lost while output may be on; verify Standby at the instrument"
            )
        self.io.close()
        self._started = False
        self._configured = False


class DMM6500:
    """Passive Pt-minus-source DC-voltage reader using FRONT terminals."""

    def __init__(self, host: str, port: int = 5025, timeout_s: float = 5) -> None:
        self.io = _LineSocket(host, port, timeout_s)
        self.identity = ""
        self._configured = False

    def connect(self) -> str:
        self.io.connect()
        try:
            identity = self.io.query("*IDN?")
            if "DMM6500" not in identity.upper():
                raise InstrumentError(f"Expected a Keithley DMM6500, received {identity!r}")
            self.identity = identity
            return identity
        except Exception:
            self.io.close()
            raise

    def _require_scpi_front(self) -> None:
        language = self.io.query("*LANG?").strip().upper()
        if language != "SCPI":
            raise InstrumentError(
                f"DMM6500 command set is {language!r}; set SCPI on the meter and reboot"
            )
        terminals = self.io.query(":ROUT:TERM?").strip().upper()
        if terminals != "FRON":
            raise InstrumentError(
                f"DMM6500 terminal selection is {terminals!r}; "
                "use the front-panel TERMINALS button to select FRONT"
            )

    def _verify_config(self) -> None:
        self._require_scpi_front()
        function = self.io.query(":SENS:FUNC?").strip().strip('"\'').upper()
        if function not in {"VOLT", "VOLT:DC", "VOLTAGE", "VOLTAGE:DC"}:
            raise InstrumentError(f"DMM6500 is not in DC-voltage mode: {function!r}")
        auto_range = self.io.query(":SENS:VOLT:DC:RANG:AUTO?").strip().upper()
        if auto_range not in {"0", "OFF"}:
            raise InstrumentError(f"DMM6500 autorange unexpectedly enabled: {auto_range!r}")
        voltage_range = _first_float(
            self.io.query(":SENS:VOLT:DC:RANG?"), label="DMM6500 voltage range"
        )
        if not math.isclose(voltage_range, 10.0, abs_tol=1e-6):
            raise InstrumentError(f"DMM6500 range is {voltage_range:g} V, expected 10 V")
        impedance = self.io.query(":SENS:VOLT:DC:INP?").strip().strip('"').upper()
        if impedance != "AUTO":
            raise InstrumentError(
                f"DMM6500 input impedance is {impedance!r}, expected AUTO (>10 GΩ)"
            )
        data_format = self.io.query(":FORM:DATA?").strip().strip('"').upper()
        if data_format not in {"ASC", "ASCII"}:
            raise InstrumentError(f"DMM6500 output format is not ASCII: {data_format!r}")
        for command, label in (
            (":SENS:VOLT:DC:AVER?", "averaging"),
            (":SENS:VOLT:DC:REL:STAT?", "relative offset"),
            (":CALC:VOLT:DC:MATH:STAT?", "math operation"),
            (":CALC2:VOLT:DC:LIM1:STAT?", "limit test 1"),
            (":CALC2:VOLT:DC:LIM2:STAT?", "limit test 2"),
        ):
            state = self.io.query(command).strip().upper()
            if state not in {"0", "OFF"}:
                raise InstrumentError(f"DMM6500 {label} is unexpectedly on: {state!r}")

    def configure(self) -> None:
        if not self.io.connected or not self.identity:
            raise InstrumentError("Connect to DMM6500 before configuring")
        self._require_scpi_front()
        # No *RST here: it would temporarily restore 10 MΩ and could load a
        # Pt quasi-reference that is already connected.  Set and verify each
        # relevant function directly, before enabling ADCMT output.
        # Remote DMM operation uses the trigger-model path. Abort a scan or
        # trigger model left by a previous program, then load a one-reading
        # SimpleLoop. This keeps READ? from inheriting an old multi-reading
        # scan or delayed trigger configuration.
        self.io.write(":ABORt")
        self.io.write("*CLS")
        for command in (
            ':SENS:FUNC "VOLT:DC"',
            ":SENS:VOLT:DC:RANG:AUTO OFF",
            ":SENS:VOLT:DC:RANG 10",
            ":SENS:VOLT:DC:INP AUTO",
            ":SENS:VOLT:DC:NPLC 1",
            ":SENS:VOLT:DC:AVER OFF",
            ":SENS:VOLT:DC:REL:STAT OFF",
            ":CALC:VOLT:DC:MATH:STAT OFF",
            ":CALC2:VOLT:DC:LIM1:STAT OFF",
            ":CALC2:VOLT:DC:LIM2:STAT OFF",
            ":FORM:DATA ASC",
            ':TRIG:LOAD "SimpleLoop", 1, 0',
        ):
            self.io.write(command)
        self._verify_config()
        error_reply = self.io.query(":SYST:ERR?")
        error_code = _first_float(error_reply, label="DMM6500 error code")
        if error_code != 0:
            raise InstrumentError(f"DMM6500 rejected a setup command: {error_reply!r}")
        self._configured = True

    def read_vref(self) -> float:
        """Return DMM INPUT HI (Pt) minus INPUT LO (OECT source), in volts."""
        if not self._configured:
            raise InstrumentError("DMM6500 is not configured for reference sensing")
        # A reset, range change, terminal switch, or scan can silently replace
        # the intended path. Recheck the measurement state and reload a
        # one-reading trigger model before every slow OECT reading.
        self._verify_config()
        self.io.write(":ABORt")
        self.io.write(':TRIG:LOAD "SimpleLoop", 1, 0')
        raw = self.io.query(":READ?")
        first_field = raw.split(",", 1)[0].strip()
        try:
            value = float(first_field)
        except ValueError as exc:
            raise InstrumentError(f"Invalid DMM6500 reference reading {raw!r}") from exc
        if not math.isfinite(value) or abs(value) >= 1e35 or abs(value) > 10.1:
            raise InstrumentError(f"DMM6500 reference reading is overrange/invalid: {raw!r}")
        return value

    def close(self) -> None:
        self.io.close()
        self._configured = False


def probe_links(adcmt_host: str, dmm_host: str, port: int = 5025) -> str:
    """Read instrument identities and DMM state without enabling any output."""
    adcmt = ADCMT6541(adcmt_host, port, timeout_s=3)
    dmm = DMM6500(dmm_host, port, timeout_s=3)
    try:
        adcmt_id = adcmt.connect()
        dmm_id = dmm.connect()
        language = dmm.io.query("*LANG?").strip()
        terminals = dmm.io.query(":ROUT:TERM?").strip()
        return (
            f"ADCMT: {adcmt_id}\nDMM6500: {dmm_id}\n"
            f"DMM command set: {language}; terminals: {terminals}"
        )
    finally:
        dmm.close()
        adcmt.close()
