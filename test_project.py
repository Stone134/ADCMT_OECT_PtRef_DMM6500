"""Offline checks for reference loading, feedback, acquisition, and shutdown.

These tests use simulations and in-memory instrument fakes.  They never open a
LAN connection or enable a physical source output.
"""

from __future__ import annotations

import csv
import math
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import instruments
import runner
from models import RunConfig


class _DMMIO:
    """Return readbacks from a meter without opening a socket."""

    def __init__(
        self,
        *,
        impedance: str = "AUTO",
        relative: str = "0",
        math_state: str = "0",
        stuck_relative: bool = False,
        stuck_math: bool = False,
    ) -> None:
        self.connected = True
        self.impedance = impedance
        self.relative = relative
        self.math_state = math_state
        self.stuck_relative = stuck_relative
        self.stuck_math = stuck_math
        self.commands: list[str] = []

    def write(self, command: str) -> None:
        self.commands.append(command)
        if command == ":SENS:VOLT:DC:REL:STAT OFF" and not self.stuck_relative:
            self.relative = "0"
        if command == ":CALC:VOLT:DC:MATH:STAT OFF" and not self.stuck_math:
            self.math_state = "0"

    def connect(self) -> None:
        self.connected = True

    def query(self, command: str) -> str:
        self.commands.append(command)
        replies = {
            "*IDN?": "Keithley Instruments,DMM6500,FAKE,1.0",
            "*LANG?": "SCPI",
            ":ROUT:TERM?": "FRON",
            ":SENS:FUNC?": '"VOLT:DC"',
            ":SENS:VOLT:DC:RANG:AUTO?": "0",
            ":SENS:VOLT:DC:RANG?": "10",
            ":SENS:VOLT:DC:INP?": self.impedance,
            ":SENS:VOLT:DC:AVER?": "0",
            ":SENS:VOLT:DC:REL:STAT?": self.relative,
            ":CALC:VOLT:DC:MATH:STAT?": self.math_state,
            ":CALC2:VOLT:DC:LIM1:STAT?": "0",
            ":CALC2:VOLT:DC:LIM2:STAT?": "0",
            ":FORM:DATA?": "ASC",
            ":SYST:ERR?": '0,"No error"',
            ":READ?": "-0.12345",
        }
        return replies[command]

    def close(self) -> None:
        self.connected = False


class _ADCMTFake:
    def __init__(self, *, shutdown_error: bool = False) -> None:
        self.connected = False
        self.configured = False
        self.started = False
        self.shutdown_calls = 0
        self.shutdown_error = shutdown_error
        self.configure_args: tuple[object, ...] = ()

    def connect(self) -> str:
        self.connected = True
        return "ADCMT 6541 fake"

    def configure(self, *args: object) -> None:
        self.configured = True
        self.configure_args = args

    def start(self) -> None:
        self.started = True

    def read_next(self, timeout_s: float) -> instruments.ADCMTSample:
        return instruments.ADCMTSample(0, -1e-6, 1e-9, 0.1, 0)

    def set_gate(self, voltage_v: float) -> float:
        return voltage_v

    def shutdown(self) -> None:
        self.shutdown_calls += 1
        if self.shutdown_error:
            raise instruments.InstrumentError("standby readback failed")


class _DMMFake:
    def __init__(self, *, fail_read: bool = False) -> None:
        self.fail_read = fail_read
        self.closed = False
        self.read_calls = 0

    def connect(self) -> str:
        return "Keithley DMM6500 fake"

    def configure(self) -> None:
        return None

    def read_vref(self) -> float:
        self.read_calls += 1
        if self.fail_read:
            raise instruments.InstrumentError("DMM reference read failed")
        return -0.1

    def close(self) -> None:
        self.closed = True


class OECTProjectTests(unittest.TestCase):
    def test_dmm_opens_vxi11_visa_resource_and_closes_both_handles(self) -> None:
        resource = Mock()
        resource.query.return_value = "Keithley Instruments,DMM6500,FAKE,1.0\n"
        manager = Mock()
        manager.open_resource.return_value = resource
        pyvisa = SimpleNamespace(ResourceManager=Mock(return_value=manager))

        with patch.dict(sys.modules, {"pyvisa": pyvisa}):
            meter = instruments.DMM6500("192.168.1.103", timeout_s=3)
            self.assertEqual(meter.connect(), "Keithley Instruments,DMM6500,FAKE,1.0")
            meter.io.write("*CLS")
            meter.close()

        pyvisa.ResourceManager.assert_called_once_with("@py")
        manager.open_resource.assert_called_once_with(
            "TCPIP0::192.168.1.103::inst0::INSTR"
        )
        self.assertEqual(resource.timeout, 3000)
        self.assertEqual(resource.write_termination, "\n")
        self.assertEqual(resource.read_termination, "\n")
        resource.query.assert_called_once_with("*IDN?")
        resource.write.assert_called_once_with("*CLS")
        resource.close.assert_called_once()
        manager.close.assert_called_once()

    def test_dmm_visa_query_error_discards_session(self) -> None:
        resource = Mock()
        resource.query.side_effect = TimeoutError("VXI-11 read timeout")
        manager = Mock()
        manager.open_resource.return_value = resource
        pyvisa = SimpleNamespace(ResourceManager=Mock(return_value=manager))

        with patch.dict(sys.modules, {"pyvisa": pyvisa}):
            meter = instruments.DMM6500("192.168.1.103")
            with self.assertRaisesRegex(instruments.InstrumentError, "VXI-11 read timeout"):
                meter.connect()

        self.assertFalse(meter.io.connected)
        resource.close.assert_called_once()
        manager.close.assert_called_once()

    def test_connection_probe_uses_adcmt_port_only(self) -> None:
        with patch.object(instruments, "ADCMT6541") as adcmt_factory, patch.object(
            instruments, "DMM6500"
        ) as dmm_factory:
            adcmt_factory.return_value.connect.return_value = "ADCMT fake"
            dmm_factory.return_value.connect.return_value = "DMM6500 fake"
            dmm_factory.return_value.io.query.side_effect = ["SCPI", "FRON"]
            summary = instruments.probe_links("adcmt.local", "dmm.local", port=6000)

        adcmt_factory.assert_called_once_with("adcmt.local", 6000, timeout_s=3)
        dmm_factory.assert_called_once_with("dmm.local", timeout_s=3)
        self.assertIn("DMM6500 fake", summary)

    def test_default_feedback_settings_use_fast_same_sign_correction(self) -> None:
        config = RunConfig()
        self.assertEqual(config.correction_factor, 1.0)
        self.assertEqual(config.feedback_max_step_v, 0.1)
        self.assertEqual(config.feedback_polarity, 1)
        self.assertEqual(config.drain_limit_a, 0.003)
        self.assertEqual(config.gate_limit_a, 0.003)
        self.assertFalse(hasattr(config, "mode"))
        self.assertTrue(config.output_file.endswith(".txt"))
        with self.assertRaisesRegex(ValueError, r"\.txt extension"):
            RunConfig(output_file="data.tsv").validate()
        self.assertLess(runner.next_feedback_gate(0.0, -0.5, 0.0, config), 0.0)

    def test_feedback_acquisition_saves_measured_reference_and_converges_in_both_directions(self) -> None:
        for polarity in (1, -1):
            with self.subTest(polarity=polarity), tempfile.TemporaryDirectory() as temp_dir:
                path = Path(temp_dir) / "short_run.txt"
                config = RunConfig(
                    simulation=True,
                    feedback_polarity=polarity,
                    sweep_start_v=-0.05,
                    sweep_stop_v=-0.05,
                    sweep_step_v=-0.1,
                    hold_s=1.0,
                    delay_s=0,
                    final_keep_s=0,
                    sample_interval_s=0.1,
                    output_file=str(path),
                )
                samples = []
                result = runner.run_experiment(config, on_sample=samples.append)
                with path.open(newline="", encoding="utf-8") as stream:
                    reader = csv.DictReader(stream, delimiter="\t")
                    self.assertEqual(reader.fieldnames, list(runner.FIELDS))
                    self.assertEqual(
                        reader.fieldnames,
                        ["Time", "GateI", "GateV", "DrainI", "DrainV", "Vref"],
                    )
                    rows = list(reader)

                self.assertGreaterEqual(result.samples, 4)
                self.assertEqual(len(rows), result.samples)
                self.assertEqual(len(samples), result.samples)
                self.assertEqual(list(Path(temp_dir).iterdir()), [path])
                self.assertEqual(float(rows[0]["GateV"]), config.gate_initial_v)
                self.assertLess(float(rows[-1]["GateV"]) * polarity, 0)
                self.assertLessEqual(abs(samples[-1].e_reference_v + 0.05), config.feedback_tolerance_v)
                self.assertTrue(samples[-1].within_tolerance)
                self.assertNotEqual(samples[0].vref_v, samples[0].target_v)
                for row, sample in zip(rows, samples):
                    vref = float(row["Vref"])
                    self.assertTrue(math.isfinite(vref))
                    self.assertAlmostEqual(sample.e_reference_v, vref, places=7)
                    self.assertLess(abs(vref - polarity * 0.82 * float(row["GateV"])), 0.01)
                    self.assertTrue(sample.dmm_read_utc)

    def test_feedback_steps_are_bounded_and_converge_in_both_directions(self) -> None:
        for polarity in (1, -1):
            with self.subTest(polarity=polarity):
                config = RunConfig(feedback_polarity=polarity)
                gate = 0.0
                target_e = -0.5
                first_step = runner.next_feedback_gate(gate, target_e, 0.0, config)
                self.assertAlmostEqual(first_step, -polarity * config.feedback_max_step_v)
                for _ in range(80):
                    measured_e = polarity * 0.82 * gate
                    updated = runner.next_feedback_gate(gate, target_e, measured_e, config)
                    self.assertLessEqual(abs(updated - gate), config.feedback_max_step_v + 1e-12)
                    self.assertLessEqual(config.gate_min_v, updated)
                    self.assertGreaterEqual(config.gate_max_v, updated)
                    gate = updated
                self.assertLessEqual(abs(target_e - polarity * 0.82 * gate), config.feedback_tolerance_v)
                limit_gate = config.gate_min_v if polarity == 1 else config.gate_max_v
                self.assertEqual(
                    runner.next_feedback_gate(limit_gate, -2.0, polarity * 0.82 * limit_gate, config),
                    limit_gate,
                )

    def test_acquisition_passes_default_three_milliamp_limits_to_adcmt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adcmt = _ADCMTFake()
            dmm = _DMMFake()
            stop = threading.Event()
            config = RunConfig(simulation=False, output_file=str(Path(temp_dir) / "data.txt"))
            with patch.object(runner, "ADCMT6541", return_value=adcmt), patch.object(
                runner, "DMM6500", return_value=dmm
            ) as dmm_factory:
                result = runner.run_experiment(config, on_sample=lambda _sample: stop.set(), stop_event=stop)
            dmm_factory.assert_called_once_with(config.dmm_ip, timeout_s=5.0)
            self.assertEqual(adcmt.configure_args, (config.vds_v, config.gate_initial_v, 0.003, 0.003, config.sample_interval_s))
            self.assertEqual(result.samples, 1)
            self.assertTrue(result.stopped)
            self.assertEqual(adcmt.shutdown_calls, 1)
            self.assertTrue(dmm.closed)

    def test_dmm_configures_and_rechecks_high_impedance_without_10_megohms(self) -> None:
        meter = instruments.DMM6500("example.invalid")
        io = _DMMIO()
        meter.io = io
        meter.identity = "Keithley DMM6500 fake"
        meter.configure()
        self.assertIn(":SENS:VOLT:DC:INP AUTO", io.commands)
        self.assertIn(":SENS:VOLT:DC:RANG 10", io.commands)
        self.assertIn(':TRIG:LOAD "SimpleLoop", 1, 0', io.commands)
        self.assertNotIn(":SENS:VOLT:DC:RANG:AUTO ON", io.commands)
        self.assertFalse(any("10E6" in command or "10M" in command for command in io.commands))
        self.assertAlmostEqual(meter.read_vref(), -0.12345)
        io.impedance = "MANUAL"
        with self.assertRaisesRegex(instruments.InstrumentError, "impedance"):
            meter.read_vref()
        self.assertEqual(io.commands[-1], ":SENS:VOLT:DC:INP?")

    def test_dmm_disables_saved_relative_and_math_before_reading(self) -> None:
        meter = instruments.DMM6500("example.invalid")
        io = _DMMIO(relative="1", math_state="1")
        meter.io = io
        meter.identity = "Keithley DMM6500 fake"
        meter.configure()
        self.assertEqual((io.relative, io.math_state), ("0", "0"))
        self.assertAlmostEqual(meter.read_vref(), -0.12345)
        for command in (
            ":SENS:VOLT:DC:REL:STAT OFF",
            ":CALC:VOLT:DC:MATH:STAT OFF",
        ):
            self.assertLess(io.commands.index(command), io.commands.index(":READ?"))

    def test_stuck_dmm_relative_or_math_prevents_adcmt_output(self) -> None:
        for fake_kwargs, error in (
            ({"relative": "1", "stuck_relative": True}, "relative offset"),
            ({"math_state": "1", "stuck_math": True}, "math operation"),
        ):
            with self.subTest(error=error), tempfile.TemporaryDirectory() as temp_dir:
                adcmt = _ADCMTFake()
                dmm = instruments.DMM6500("example.invalid")
                dmm.io = _DMMIO(**fake_kwargs)
                config = RunConfig(
                    simulation=False, output_file=str(Path(temp_dir) / "data.txt")
                )
                with patch.object(runner, "ADCMT6541", return_value=adcmt), patch.object(
                    runner, "DMM6500", return_value=dmm
                ):
                    with self.assertRaisesRegex(instruments.InstrumentError, error):
                        runner.run_experiment(config)
                self.assertFalse(adcmt.started)
                self.assertFalse(adcmt.configured)
                self.assertFalse(dmm.io.connected)

    def test_bad_dmm_configuration_prevents_adcmt_output(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adcmt = _ADCMTFake()
            dmm = instruments.DMM6500("example.invalid")
            io = _DMMIO(impedance="MANUAL")
            dmm.io = io
            config = RunConfig(simulation=False, output_file=str(Path(temp_dir) / "data.txt"))
            with patch.object(runner, "ADCMT6541", return_value=adcmt), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                with self.assertRaisesRegex(instruments.InstrumentError, "impedance"):
                    runner.run_experiment(config)
            self.assertFalse(adcmt.connected)
            self.assertFalse(adcmt.configured)
            self.assertFalse(adcmt.started)
            self.assertEqual(adcmt.shutdown_calls, 1)
            self.assertFalse(io.connected)
            self.assertFalse(config.output_path.exists())

    def test_dmm_read_failure_shuts_down_energized_adcmt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adcmt = _ADCMTFake()
            dmm = _DMMFake(fail_read=True)
            config = RunConfig(simulation=False, output_file=str(Path(temp_dir) / "data.txt"))
            with patch.object(runner, "ADCMT6541", return_value=adcmt), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                with self.assertRaisesRegex(instruments.InstrumentError, "reference read failed"):
                    runner.run_experiment(config)
            self.assertTrue(adcmt.started)
            self.assertEqual(dmm.read_calls, 1)
            self.assertEqual(adcmt.shutdown_calls, 1)
            self.assertTrue(dmm.closed)

    def test_adcmt_reports_skipped_hardware_samples(self) -> None:
        adcmt = instruments.ADCMT6541("example.invalid")
        adcmt._started = True
        adcmt.interval_s = 0.1
        counts = iter((5, 5, 8, 8))
        addresses: list[tuple[int, int]] = []

        def current(channel: int, address: int) -> float:
            addresses.append((channel, address))
            return channel * 1e-9 + address * 1e-12

        adcmt._count = lambda _channel: next(counts)
        adcmt._current = current
        first = adcmt.read_next(timeout_s=1)
        second = adcmt.read_next(timeout_s=1)
        self.assertEqual((first.index, first.skipped), (4, 4))
        self.assertEqual((second.index, second.skipped), (7, 2))
        self.assertEqual(addresses, [(1, 4), (2, 4), (1, 7), (2, 7)])
        self.assertAlmostEqual(second.nominal_elapsed_s, 0.8)

    def test_gate_change_keeps_the_configured_sampling_cadence(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "two_levels.txt"
            config = RunConfig(
                simulation=True,
                sweep_start_v=-0.1, sweep_stop_v=-0.2, sweep_step_v=-0.1,
                hold_s=0.35, delay_s=0, final_keep_s=0,
                sample_interval_s=0.1, output_file=str(path),
            )
            samples = []
            runner.run_experiment(config, on_sample=samples.append)
            with path.open(newline="", encoding="utf-8") as stream:
                rows = list(csv.DictReader(stream, delimiter="\t"))

        self.assertEqual({sample.stage_index for sample in samples}, {1, 2})
        self.assertEqual(len(rows), len(samples))
        times = [float(row["Time"]) for row in rows]
        gaps = [later - earlier for earlier, later in zip(times, times[1:])]
        self.assertTrue(gaps)
        self.assertLess(max(gaps), 0.15)
        for row in rows:
            self.assertLess(abs(float(row["Vref"]) - 0.82 * float(row["GateV"])), 0.01)

    def test_unverified_shutdown_is_reported_as_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            adcmt = _ADCMTFake(shutdown_error=True)
            dmm = _DMMFake()
            stop = threading.Event()
            config = RunConfig(
                simulation=False, output_file=str(Path(temp_dir) / "data.txt"),
            )
            with patch.object(runner, "ADCMT6541", return_value=adcmt), patch.object(
                runner, "DMM6500", return_value=dmm
            ):
                with self.assertRaisesRegex(RuntimeError, "ADCMT shutdown could not be verified"):
                    runner.run_experiment(config, on_sample=lambda _sample: stop.set(), stop_event=stop)
            self.assertEqual(adcmt.shutdown_calls, 1)
            self.assertTrue(dmm.closed)


if __name__ == "__main__":
    unittest.main()
