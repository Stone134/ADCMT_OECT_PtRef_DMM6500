"""Offline checks for independent four-channel OECT acquisition.

These tests use simulated sources and instrument fakes only. They never open a
LAN connection or energize a physical channel.
"""

from __future__ import annotations

import csv
import math
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from dual_instrument import DualADCMT6541
from dual_plan import APlan, BPlan, DualPlan
import dual_runner as runner


def _rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream, delimiter="\t"))


def _first_by_stage(samples: list) -> tuple:
    first = {}
    for sample in samples:
        first.setdefault(sample.stage_index, sample)
    return tuple(first.values())


class _TrackingADCMT(runner.SimulatedDualADCMT):
    def __init__(self, interval_s: float, feedback_polarity: int,
                 samples_a: list, samples_b: list) -> None:
        super().__init__(interval_s, feedback_polarity)
        self.samples_a = samples_a
        self.samples_b = samples_b
        self.zero_events: list[tuple[str, int, int, int]] = []
        self.gate_commands: list[tuple[int, float]] = []

    def set_gate(self, channel: int, voltage_v: float) -> float:
        self.gate_commands.append((channel, voltage_v))
        return super().set_gate(channel, voltage_v)

    def zero_cell(self, cell: str) -> None:
        super().zero_cell(cell)
        self.zero_events.append((cell, len(self.samples_a),
                                 len(self.samples_b), self.last_index))
        if cell == "a":
            assert self.vds_a_v == self.gate_a_v == 0.0
        else:
            assert self.vds_b_v == self.gate_b_v == 0.0


class DualOECTTests(unittest.TestCase):
    def _plans(self, folder: Path, *, a_mode: str = "monitor",
               a_pattern: str = "one_way", b_pattern: str = "one_way",
               a_start: float = -0.4, a_stop: float = -0.4,
               b_start: float = -0.4, b_stop: float = -0.4) -> DualPlan:
        a = APlan(
            simulation=True, mode=a_mode, sweep_start_v=a_start,
            sweep_stop_v=a_stop, sweep_step_v=-0.1,
            sweep_pattern=a_pattern, sweep_cycles=3,
            hold_s=0.1, delay_s=0.0, final_keep_s=0.0,
            sample_interval_s=0.1, output_file=str(folder / "a.txt"),
        )
        b = BPlan(
            sweep_start_v=b_start, sweep_stop_v=b_stop,
            sweep_step_v=-0.1, sweep_pattern=b_pattern, sweep_cycles=3,
            hold_s=0.1, delay_s=0.0, final_keep_s=0.0,
            sample_interval_s=0.1, output_file=str(folder / "b.txt"),
        )
        return DualPlan(a, b)

    def _run_tracked(self, plan: DualPlan):
        samples_a: list = []
        samples_b: list = []
        devices: list[_TrackingADCMT] = []

        def make_adcmt(interval_s: float, feedback_polarity: int) -> _TrackingADCMT:
            device = _TrackingADCMT(interval_s, feedback_polarity, samples_a, samples_b)
            devices.append(device)
            return device

        with patch.object(runner, "SimulatedDualADCMT", side_effect=make_adcmt):
            result = runner.run_experiment(plan, on_sample_a=samples_a.append,
                                           on_sample_b=samples_b.append)
        return result, samples_a, samples_b, devices[0]

    def test_b_sweeps_both_patterns_and_a_keeps_feedback_or_monitor_levels(self) -> None:
        expected = (-0.4, -0.5, -0.6, -0.5, -0.4,
                    -0.5, -0.6, -0.5, -0.4,
                    -0.5, -0.6, -0.5, -0.4)
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            for mode in ("feedback", "monitor"):
                with self.subTest(mode=mode):
                    plan = self._plans(root, a_mode=mode, a_pattern="back_and_forth",
                                       b_pattern="back_and_forth", a_stop=-0.6,
                                       b_stop=-0.6)
                    plan.validate()
                    self.assertEqual(plan.a.levels, expected)
                    self.assertEqual(plan.b.levels, expected)
            one_way = self._plans(root, a_stop=-0.6, b_stop=-0.6)
            self.assertEqual(one_way.a.levels, expected[:3])
            self.assertEqual(one_way.b.levels, expected[:3])

    def test_monitor_a_completes_first_and_b_continues_without_a_rows(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plan = self._plans(Path(folder), a_mode="monitor",
                               b_pattern="back_and_forth", b_stop=-0.6)
            with patch.object(runner, "next_feedback_gate",
                              side_effect=AssertionError("monitor called feedback")):
                result, samples_a, samples_b, adcmt = self._run_tracked(plan)
            rows_a, rows_b = _rows(plan.a.output_path), _rows(plan.b.output_path)

        self.assertFalse(result.stopped)
        self.assertEqual([event[0] for event in adcmt.zero_events], ["a", "b"])
        self.assertEqual(len(samples_a), adcmt.zero_events[0][1])
        self.assertGreater(len(samples_b), adcmt.zero_events[0][2])
        self.assertEqual((result.samples_a, result.samples_b),
                         (len(rows_a), len(rows_b)))
        self.assertEqual(len(rows_a), len(samples_a))
        self.assertEqual(list(rows_a[0]), ["Time", "GateI", "GateV",
                                            "DrainI", "DrainV", "Vref"])
        self.assertEqual(list(rows_b[0]), ["Time", "GateI", "GateV",
                                            "DrainI", "DrainV"])
        self.assertEqual(tuple(s.target_v for s in _first_by_stage(samples_b)),
                         plan.b.levels)
        self.assertEqual(tuple(s.gate_v for s in _first_by_stage(samples_b)),
                         plan.b.levels)
        self.assertEqual({channel for channel, _ in adcmt.gate_commands}, {4})
        self.assertTrue(all(math.isfinite(float(row["Vref"])) for row in rows_a))
        self.assertTrue(all(s.within_tolerance is None for s in samples_a))
        self.assertTrue(adcmt.closed)
        self.assertEqual((adcmt.vds_a_v, adcmt.gate_a_v,
                          adcmt.vds_b_v, adcmt.gate_b_v), (0.0,) * 4)

    def test_b_completes_first_while_a_feedback_continues(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plan = self._plans(Path(folder), a_mode="feedback",
                               a_pattern="back_and_forth", a_stop=-0.6,
                               b_stop=-0.6)
            result, samples_a, samples_b, adcmt = self._run_tracked(plan)
            rows_a, rows_b = _rows(plan.a.output_path), _rows(plan.b.output_path)

        self.assertEqual([event[0] for event in adcmt.zero_events], ["b", "a"])
        self.assertEqual(len(samples_b), adcmt.zero_events[0][2])
        self.assertGreater(len(samples_a), adcmt.zero_events[0][1])
        self.assertEqual((len(rows_a), len(rows_b)),
                         (result.samples_a, result.samples_b))
        self.assertEqual(tuple(s.target_v for s in _first_by_stage(samples_a)),
                         plan.a.levels)
        self.assertTrue(all(s.within_tolerance is not None for s in samples_a))
        self.assertTrue(all(math.isfinite(float(row["Vref"])) for row in rows_a))
        self.assertTrue(any(channel == 2 for channel, _ in adcmt.gate_commands))
        self.assertEqual({channel for channel, _ in adcmt.gate_commands}, {2, 4})
        self.assertEqual(tuple(s.target_v for s in _first_by_stage(samples_b)),
                         plan.b.levels)
        self.assertEqual(tuple(s.gate_v for s in _first_by_stage(samples_b)),
                         plan.b.levels)
        self.assertEqual(list(rows_b[0]), ["Time", "GateI", "GateV",
                                            "DrainI", "DrainV"])
        self.assertEqual(adcmt.vds_a_v, 0.0)
        self.assertEqual(adcmt.gate_a_v, 0.0)
        self.assertEqual(adcmt.vds_b_v, 0.0)
        self.assertEqual(adcmt.gate_b_v, 0.0)

    def test_delayed_dmm_backlog_does_not_relabel_old_feedback_pair(self) -> None:
        # A slow DMM leaves several ADCMT pairs in memory. After A advances
        # from target 1 to target 2, those old pairs must not be written under
        # target 2 even though they are fetched after the transition.
        with tempfile.TemporaryDirectory() as folder:
            base = self._plans(Path(folder), a_mode="feedback", a_stop=-0.5)
            plan = DualPlan(base.a, replace(base.b, hold_s=1.0))
            stop = threading.Event()
            samples_a: list = []
            devices: list = []
            transitions: list[float] = []
            original_adcmt = runner.SimulatedDualADCMT
            original_dmm = runner.SimulatedDMM

            class BacklogADCMT(original_adcmt):
                def __init__(self, interval_s: float, feedback_polarity: int):
                    super().__init__(interval_s, feedback_polarity)
                    self.batches: list[tuple[float, tuple]] = []

                def read_available(self, timeout_s: float = 0.05,
                                   max_batch: int = 512):
                    pairs = super().read_available(timeout_s, max_batch)
                    self.batches.append((time.monotonic() - self.clock_started_monotonic,
                                         tuple(pairs)))
                    return pairs

            class DelayedDMM(original_dmm):
                def __init__(self, adcmt):
                    super().__init__(adcmt)
                    self.reads = 0

                def read_vref(self) -> float:
                    self.reads += 1
                    if self.reads == 1:
                        time.sleep(0.35)
                    return super().read_vref()

            def make_adcmt(interval_s: float, feedback_polarity: int):
                device = BacklogADCMT(interval_s, feedback_polarity)
                devices.append(device)
                return device

            def on_status(message: str) -> None:
                if message.startswith("OECT A level 2/"):
                    transitions.append(time.monotonic() - devices[0].clock_started_monotonic)

            def on_sample(sample) -> None:
                samples_a.append(sample)
                if sample.stage_index == 2:
                    stop.set()

            with patch.object(runner, "SimulatedDualADCMT", side_effect=make_adcmt), \
                 patch.object(runner, "SimulatedDMM", side_effect=DelayedDMM):
                result = runner.run_experiment(plan, on_sample_a=on_sample,
                                               on_status=on_status, stop_event=stop)
            rows_a = _rows(plan.a.output_path)

        self.assertTrue(result.stopped)
        self.assertEqual(len(transitions), 1)
        transition = transitions[0]
        backlog = [pair for fetched_at, pairs in devices[0].batches
                   if fetched_at > transition for pair in pairs
                   if pair.nominal_elapsed_s <= transition]
        self.assertTrue(backlog, "the delayed DMM should create old ADCMT pairs")
        first_at_new_target = next(sample for sample in samples_a
                                   if sample.stage_index == 2)
        self.assertEqual(first_at_new_target.target_v, -0.5)
        self.assertGreater(first_at_new_target.adcmt_nominal_elapsed_s, transition)
        self.assertNotIn(first_at_new_target.adcmt_index,
                         {pair.index for pair in backlog})
        self.assertEqual(len(rows_a), len(samples_a))
        self.assertTrue(devices[0].closed)

    def test_distinct_paths_and_compatible_hardware_clock_are_required(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            plan = self._plans(root)
            self.assertAlmostEqual(plan.hardware_interval_s, 0.1)
            same_path = DualPlan(plan.a, BPlan(
                output_file=str(plan.a.output_path), hold_s=0.1,
                delay_s=0.0, sample_interval_s=0.1,
            ))
            with self.assertRaisesRegex(ValueError, "different raw-data files"):
                same_path.validate()
            incompatible = DualPlan(
                APlan(sample_interval_s=0.2, hold_s=0.4, delay_s=0.0,
                      output_file=str(root / "a.txt")),
                BPlan(sample_interval_s=0.35, hold_s=0.4, delay_s=0.0,
                      output_file=str(root / "b.txt")),
            )
            with self.assertRaisesRegex(ValueError, "common ADCMT clock"):
                incompatible.validate()
            self.assertFalse((root / "a.txt").exists())
            self.assertFalse((root / "b.txt").exists())

    def test_stop_zeroes_all_four_sources(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            plan = self._plans(Path(folder), a_stop=-0.6, b_stop=-0.6)
            stop = threading.Event()
            devices: list[runner.SimulatedDualADCMT] = []
            original = runner.SimulatedDualADCMT

            def make_adcmt(interval_s: float, feedback_polarity: int):
                device = original(interval_s, feedback_polarity)
                devices.append(device)
                return device

            with patch.object(runner, "SimulatedDualADCMT", side_effect=make_adcmt):
                result = runner.run_experiment(
                    plan, on_sample_a=lambda _sample: stop.set(), stop_event=stop,
                )
            self.assertTrue(result.stopped)
            self.assertEqual(result.samples_a, 1)
            self.assertTrue(devices[0].closed)
            self.assertEqual((devices[0].vds_a_v, devices[0].gate_a_v,
                              devices[0].vds_b_v, devices[0].gate_b_v), (0.0,) * 4)

    def test_dmm_configuration_or_read_failure_shuts_down_both_cells(self) -> None:
        for fail_at in ("configure", "read_vref"):
            with self.subTest(fail_at=fail_at), tempfile.TemporaryDirectory() as folder:
                plan = self._plans(Path(folder))
                devices: list[runner.SimulatedDualADCMT] = []
                original_adcmt = runner.SimulatedDualADCMT
                original_dmm = runner.SimulatedDMM

                def make_adcmt(interval_s: float, feedback_polarity: int):
                    device = original_adcmt(interval_s, feedback_polarity)
                    devices.append(device)
                    return device

                def make_dmm(device):
                    meter = original_dmm(device)
                    setattr(meter, fail_at, lambda: (_ for _ in ()).throw(
                        RuntimeError("DMM failure")))
                    return meter

                with patch.object(runner, "SimulatedDualADCMT", side_effect=make_adcmt), \
                     patch.object(runner, "SimulatedDMM", side_effect=make_dmm):
                    with self.assertRaisesRegex(RuntimeError, "DMM failure"):
                        runner.run_experiment(plan)
                adcmt = devices[0]
                self.assertTrue(adcmt.closed)
                self.assertEqual((adcmt.vds_a_v, adcmt.gate_a_v,
                                  adcmt.vds_b_v, adcmt.gate_b_v), (0.0,) * 4)
                if fail_at == "configure":
                    self.assertIsNone(adcmt.started)
                    self.assertFalse(plan.a.output_path.exists())
                    self.assertFalse(plan.b.output_path.exists())
                else:
                    self.assertIsNotNone(adcmt.started)

    def test_completed_cell_zero_hold_keeps_channels_in_operate(self) -> None:
        driver = DualADCMT6541("example.invalid")
        driver._configured = True
        commands: list[tuple[int, float]] = []
        selected: list[int] = []
        driver._set_channel_voltage = lambda channel, voltage: (
            commands.append((channel, voltage)) or voltage
        )
        driver._select = selected.append
        driver.io.query = lambda command: "OPR" if command == "OPR?" else ""
        driver.zero_cell("a")
        driver.zero_cell("b")
        self.assertEqual(commands, [(2, 0.0), (1, 0.0),
                                    (4, 0.0), (3, 0.0)])
        self.assertEqual(selected, [2, 1, 4, 3])


if __name__ == "__main__":
    unittest.main()
