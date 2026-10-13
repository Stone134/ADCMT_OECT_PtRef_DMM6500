#!/usr/bin/env python3
"""Front panel for two independently scheduled OECTs on ADCMT CH1–CH4.

OECT A uses CH1/CH2 and the DMM6500 reference input. OECT B uses CH3/CH4
and a programmed gate voltage. Both share one ADCMT acquisition clock.
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import threading
from pathlib import Path
from typing import Sequence

from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from gui import CompactAxisItem, spin
from instruments import probe_links
from dual_plan import APlan, BPlan, BSample, DualPlan
from models import Sample
from saved_defaults import load_defaults, save_defaults


APP_TITLE = "Dual OECT • A with reference / B direct gate"
PROJECT_DIR = Path(__file__).resolve().parent
ASSETS_DIR = PROJECT_DIR / "assets"


class AcquisitionWorker(QtCore.QThread):
    sample_a_ready = QtCore.pyqtSignal(object)
    sample_b_ready = QtCore.pyqtSignal(object)
    status_ready = QtCore.pyqtSignal(str)
    run_complete = QtCore.pyqtSignal(bool, str)

    def __init__(self, plan: DualPlan, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.stop_event = threading.Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            from dual_runner import run_experiment

            result = run_experiment(
                self.plan,
                on_sample_a=self.sample_a_ready.emit,
                on_sample_b=self.sample_b_ready.emit,
                on_status=self.status_ready.emit,
                stop_event=self.stop_event,
            )
        except Exception as exc:
            self.run_complete.emit(False, f"{type(exc).__name__}: {exc}")
        else:
            self.run_complete.emit(
                True,
                "Stopped safely" if result.stopped else "Both measurements completed",
            )


class ConnectionWorker(QtCore.QThread):
    probe_complete = QtCore.pyqtSignal(bool, str)

    def __init__(self, adcmt_ip: str, dmm_ip: str, port: int,
                 parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.adcmt_ip = adcmt_ip
        self.dmm_ip = dmm_ip
        self.port = port

    def run(self) -> None:
        try:
            summary = probe_links(self.adcmt_ip, self.dmm_ip, self.port)
        except Exception as exc:
            self.probe_complete.emit(False, f"{type(exc).__name__}: {exc}")
        else:
            message = (
                "  •  ".join(f"{key}: {value}" for key, value in summary.items())
                if isinstance(summary, dict) else str(summary)
            )
            self.probe_complete.emit(True, message)


def _combo(items: tuple[tuple[str, str | int], ...], value: str | int) -> QtWidgets.QComboBox:
    box = QtWidgets.QComboBox()
    for title, data in items:
        box.addItem(title, data)
    index = box.findData(value)
    box.setCurrentIndex(max(index, 0))
    return box


class DualOECTWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1610, 960)
        self.setMinimumSize(1190, 800)
        self.worker: AcquisitionWorker | None = None
        self.connection_worker: ConnectionWorker | None = None
        self._last_result: tuple[bool, str] | None = None
        self._pending_close = False
        self._durations = {"a": 1.0, "b": 1.0}
        self._counts = {"a": 0, "b": 0}
        self._completed = {"a": False, "b": False}
        self._plot_data: dict[str, dict[str, list[float]]] = {
            "a": {"time": [], "id": [], "vg": [], "vref": [], "target": []},
            "b": {"time": [], "id": [], "vg": []},
        }

        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(18, 16, 18, 14)
        outer.setSpacing(12)
        outer.addWidget(self._header())
        body = QtWidgets.QHBoxLayout()
        body.setSpacing(12)
        body.addWidget(self._control_panel())
        body.addWidget(self._plot_panel(), 1)
        outer.addLayout(body, 1)
        outer.addWidget(self._footer())
        self._apply_style()
        defaults_warning = self._restore_saved_defaults()
        self._mode_changed()
        if defaults_warning:
            self.status_label.setText(defaults_warning)

    @staticmethod
    def _group(title: str) -> tuple[QtWidgets.QGroupBox, QtWidgets.QGridLayout]:
        group = QtWidgets.QGroupBox(title)
        grid = QtWidgets.QGridLayout(group)
        grid.setContentsMargins(14, 16, 14, 11)
        grid.setHorizontalSpacing(9)
        grid.setVerticalSpacing(7)
        return group, grid

    @staticmethod
    def _field(title: str, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        container = QtWidgets.QWidget()
        row = QtWidgets.QHBoxLayout(container)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(6)
        label = QtWidgets.QLabel(title)
        label.setMinimumWidth(92)
        row.addWidget(label)
        row.addWidget(widget, 1)
        return container

    @staticmethod
    def _hint(text: str) -> QtWidgets.QLabel:
        label = QtWidgets.QLabel(text)
        label.setProperty("muted", True)
        label.setWordWrap(True)
        return label

    def _header(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("headerCard")
        row = QtWidgets.QHBoxLayout(card)
        row.setContentsMargins(18, 13, 18, 13)
        title_box = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(APP_TITLE)
        title.setObjectName("title")
        title_box.addWidget(title)
        title_box.addWidget(self._hint(
            "ADCMT 6541 • CH1/CH2 OECT A + CH3/CH4 OECT B  |  DMM6500 • A reference only"
        ))
        row.addLayout(title_box, 1)
        self.mode_badge = QtWidgets.QLabel("SIMULATION")
        self.mode_badge.setObjectName("modeBadge")
        row.addWidget(self.mode_badge)
        return card

    def _control_panel(self) -> QtWidgets.QWidget:
        controls = QtWidgets.QWidget()
        controls.setMinimumWidth(590)
        controls.setMaximumWidth(680)
        layout = QtWidgets.QVBoxLayout(controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(10)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        panel = QtWidgets.QWidget()
        stack = QtWidgets.QVBoxLayout(panel)
        stack.setContentsMargins(0, 0, 4, 0)
        stack.setSpacing(10)
        stack.addWidget(self._connection_card())
        stack.addWidget(self._output_card())
        stack.addWidget(self._a_card())
        stack.addWidget(self._a_feedback_card())
        stack.addWidget(self._b_card())
        stack.addStretch(1)
        scroll.setWidget(panel)
        layout.addWidget(scroll, 1)
        layout.addWidget(self._run_controls())
        return controls

    def _connection_card(self) -> QtWidgets.QWidget:
        group, grid = self._group("Connection")
        self.simulation = QtWidgets.QCheckBox("Simulation • no instrument commands")
        self.simulation.setChecked(True)
        self.simulation.toggled.connect(self._refresh_controls)
        grid.addWidget(self.simulation, 0, 0, 1, 4)
        self.adcmt_ip = QtWidgets.QLineEdit(APlan.adcmt_ip)
        self.dmm_ip = QtWidgets.QLineEdit(APlan.dmm_ip)
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(APlan.port)
        self.port.setMinimumWidth(112)
        grid.addWidget(QtWidgets.QLabel("ADCMT IP"), 1, 0)
        grid.addWidget(self.adcmt_ip, 1, 1)
        grid.addWidget(QtWidgets.QLabel("DMM6500 IP"), 1, 2)
        grid.addWidget(self.dmm_ip, 1, 3)
        grid.addWidget(QtWidgets.QLabel("ADCMT port"), 2, 0)
        grid.addWidget(self.port, 2, 1)
        self.test_button = QtWidgets.QPushButton("Test connections")
        self.test_button.setProperty("secondary", True)
        self.test_button.clicked.connect(self._test_connections)
        grid.addWidget(self.test_button, 2, 2, 1, 2)
        grid.addWidget(self._hint(
            "DMM front INPUT HI → reference; INPUT LO → OECT A source. "
            "OECT B has no reference connection."
        ), 3, 0, 1, 4)
        return group

    def _output_card(self) -> QtWidgets.QWidget:
        group, grid = self._group("Raw data • separate files")
        self.a_output = QtWidgets.QLineEdit(APlan.output_file)
        self.b_output = QtWidgets.QLineEdit(BPlan.output_file)
        self.a_browse = QtWidgets.QPushButton("Browse")
        self.b_browse = QtWidgets.QPushButton("Browse")
        for button in (self.a_browse, self.b_browse):
            button.setProperty("secondary", True)
        self.a_browse.clicked.connect(lambda: self._browse_output(self.a_output))
        self.b_browse.clicked.connect(lambda: self._browse_output(self.b_output))
        grid.addWidget(QtWidgets.QLabel("OECT A"), 0, 0)
        grid.addWidget(self.a_output, 0, 1)
        grid.addWidget(self.a_browse, 0, 2)
        grid.addWidget(QtWidgets.QLabel("OECT B"), 1, 0)
        grid.addWidget(self.b_output, 1, 1)
        grid.addWidget(self.b_browse, 1, 2)
        self.progress: dict[str, QtWidgets.QProgressBar] = {}
        self.progress_detail: dict[str, QtWidgets.QLabel] = {}
        for row, lane in ((2, "a"), (3, "b")):
            bar = QtWidgets.QProgressBar()
            bar.setRange(0, 100)
            bar.setValue(0)
            detail = self._hint("Ready")
            self.progress[lane] = bar
            self.progress_detail[lane] = detail
            grid.addWidget(QtWidgets.QLabel(f"{lane.upper()} progress"), row, 0)
            grid.addWidget(bar, row, 1)
            grid.addWidget(detail, row, 2)
        grid.addWidget(self._hint(
            "A: Time, GateI, GateV, DrainI, DrainV, Vref. "
            "B: Time, GateI, GateV, DrainI, DrainV. No rows are recorded after a lane finishes."
        ), 4, 0, 1, 3)
        return group

    def _a_card(self) -> QtWidgets.QWidget:
        a = APlan()
        self.a_group, grid = self._group("OECT A • CH1 drain / CH2 gate / DMM reference")
        self.a_mode = _combo((
            ("Reference feedback • sweep Eref target", "feedback"),
            ("Vref monitor • sweep Vg directly", "monitor"),
        ), a.mode)
        self.a_mode.currentIndexChanged.connect(self._mode_changed)
        grid.addWidget(self._field("Mode", self.a_mode), 0, 0, 1, 2)
        self.a_mode_note = self._hint("")
        grid.addWidget(self.a_mode_note, 1, 0, 1, 2)
        self.a_vds = spin(-3, 3, a.vds_v, 0.01, 4, " V")
        self.a_start = spin(-3, 3, a.sweep_start_v, 0.1, 4, " V")
        self.a_stop = spin(-3, 3, a.sweep_stop_v, 0.1, 4, " V")
        self.a_step = spin(-3, 3, a.sweep_step_v, 0.1, 4, " V")
        self.a_pattern = _combo((("One way", "one_way"),
                                 ("Back and forth", "back_and_forth")), a.sweep_pattern)
        self.a_cycles = QtWidgets.QSpinBox()
        self.a_cycles.setRange(1, 1000)
        self.a_cycles.setValue(a.sweep_cycles)
        self.a_cycles.setMinimumWidth(112)
        self.a_pattern.currentIndexChanged.connect(self._refresh_controls)
        self.a_hold = spin(0.1, 1_000_000, a.hold_s, 10, 3, " s")
        self.a_delay = spin(0, 999_999, a.delay_s, 0.1, 3, " s")
        self.a_final = spin(0, 1_000_000, a.final_keep_s, 60, 3, " s")
        self.a_interval = spin(0.1, 60, a.sample_interval_s, 0.1, 3, " s")
        self.a_start_label = "Eref initial"
        self.a_stop_label = "Eref final"
        self.a_step_label = "Eref step"
        self.a_sweep_fields: list[tuple[QtWidgets.QWidget, QtWidgets.QWidget, QtWidgets.QWidget]] = []
        pairs = (
            (("Vds", self.a_vds), ("Period", self.a_hold)),
            ((self.a_start_label, self.a_start), ("Delay", self.a_delay)),
            ((self.a_stop_label, self.a_stop), ("Keep at end", self.a_final)),
            ((self.a_step_label, self.a_step), ("Read interval", self.a_interval)),
            (("Sweep", self.a_pattern), ("Cycles", self.a_cycles)),
        )
        for row, pair in enumerate(pairs, 2):
            for col, (name, control) in enumerate(pair):
                field = self._field(name, control)
                grid.addWidget(field, row, col)
                if control in (self.a_start, self.a_stop, self.a_step):
                    self.a_sweep_fields.append((field, field.findChild(QtWidgets.QLabel), control))
        grid.addWidget(self._hint(
            "Back and forth returns to the initial value on each cycle. "
            "Delay is within each period. Drain and gate limits: 0.003 A."
        ), 7, 0, 1, 2)
        return self.a_group

    def _a_feedback_card(self) -> QtWidgets.QWidget:
        a = APlan()
        self.a_feedback_group, grid = self._group("OECT A • gate bounds and feedback")
        self.a_gate_min = spin(-3, 3, a.gate_min_v, 0.1, 4, " V")
        self.a_gate_max = spin(-3, 3, a.gate_max_v, 0.1, 4, " V")
        self.a_gate_initial = spin(-3, 3, a.gate_initial_v, 0.1, 4, " V")
        self.a_gain = spin(0.001, 10, a.correction_factor, 0.1, 4, "")
        self.a_max_change = spin(0.0001, 3, a.feedback_max_step_v, 0.01, 4, " V")
        self.a_tolerance = spin(0, 3, a.feedback_tolerance_v, 0.001, 4, " V")
        self.a_polarity = _combo((
            ("Decrease Vg lowers Eref (same direction)", +1),
            ("Increase Vg lowers Eref (opposite direction)", -1),
        ), a.feedback_polarity)
        pairs = (
            (("Gate min", self.a_gate_min), ("Gate max", self.a_gate_max)),
            (("Initial gate", self.a_gate_initial), ("Correction factor", self.a_gain)),
            (("Max change", self.a_max_change), ("Tolerance", self.a_tolerance)),
        )
        for row, pair in enumerate(pairs):
            for col, (name, widget) in enumerate(pair):
                grid.addWidget(self._field(name, widget), row, col)
        grid.addWidget(self._field("Direction", self.a_polarity), 3, 0, 1, 2)
        self.a_feedback_note = self._hint("")
        grid.addWidget(self.a_feedback_note, 4, 0, 1, 2)
        self.a_feedback_controls = (
            self.a_gate_initial, self.a_gain, self.a_max_change,
            self.a_tolerance, self.a_polarity,
        )
        return self.a_feedback_group

    def _b_card(self) -> QtWidgets.QWidget:
        b = BPlan()
        group, grid = self._group("OECT B • CH3 drain / CH4 gate • direct Vg")
        self.b_vds = spin(-3, 3, b.vds_v, 0.01, 4, " V")
        self.b_start = spin(-3, 3, b.sweep_start_v, 0.1, 4, " V")
        self.b_stop = spin(-3, 3, b.sweep_stop_v, 0.1, 4, " V")
        self.b_step = spin(-3, 3, b.sweep_step_v, 0.1, 4, " V")
        self.b_pattern = _combo((("One way", "one_way"),
                                 ("Back and forth", "back_and_forth")), b.sweep_pattern)
        self.b_cycles = QtWidgets.QSpinBox()
        self.b_cycles.setRange(1, 1000)
        self.b_cycles.setValue(b.sweep_cycles)
        self.b_cycles.setMinimumWidth(112)
        self.b_pattern.currentIndexChanged.connect(self._refresh_controls)
        self.b_hold = spin(0.1, 1_000_000, b.hold_s, 10, 3, " s")
        self.b_delay = spin(0, 999_999, b.delay_s, 0.1, 3, " s")
        self.b_final = spin(0, 1_000_000, b.final_keep_s, 60, 3, " s")
        self.b_interval = spin(0.1, 60, b.sample_interval_s, 0.1, 3, " s")
        self.b_gate_min = spin(-3, 3, b.gate_min_v, 0.1, 4, " V")
        self.b_gate_max = spin(-3, 3, b.gate_max_v, 0.1, 4, " V")
        pairs = (
            (("Vds", self.b_vds), ("Period", self.b_hold)),
            (("Vg initial", self.b_start), ("Delay", self.b_delay)),
            (("Vg final", self.b_stop), ("Keep at end", self.b_final)),
            (("Vg step", self.b_step), ("Read interval", self.b_interval)),
            (("Sweep", self.b_pattern), ("Cycles", self.b_cycles)),
            (("Gate min", self.b_gate_min), ("Gate max", self.b_gate_max)),
        )
        for row, pair in enumerate(pairs):
            for col, (name, widget) in enumerate(pair):
                grid.addWidget(self._field(name, widget), row, col)
        grid.addWidget(self._hint(
            "B uses no DMM or feedback. Once either OECT finishes, its Vds and Vg "
            "are set to 0 V while the other continues. No waiting data is recorded. "
            "Drain and gate limits: 0.003 A."
        ), len(pairs), 0, 1, 2)
        return group

    def _plot_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.curves: dict[str, pg.PlotDataItem] = {}
        self.plots: dict[str, pg.PlotWidget] = {}
        specs = (
            ("a_id", "A • drain current (CH1)", "Id A", "A", "#078A84", 0, 0),
            ("b_id", "B • drain current (CH3)", "Id B", "A", "#B27B33", 0, 1),
            ("a_vg", "A • gate voltage (CH2)", "Vg A", "V", "#1CA89B", 1, 0),
            ("b_vg", "B • gate voltage (CH4)", "Vg B", "V", "#D1994E", 1, 1),
            ("a_vref", "A • measured reference (DMM6500)", "Vref A", "V", "#8260BE", 2, 0),
        )
        for key, title, ylabel, units, color, row, col in specs:
            plot = pg.PlotWidget(
                background="#FFFCF6",
                axisItems={
                    "left": CompactAxisItem(orientation="left"),
                    "bottom": CompactAxisItem(orientation="bottom"),
                },
            )
            plot.setTitle(title, color="#273449", size="10pt")
            plot.setLabel("bottom", "Elapsed time", units="s", color="#526378")
            plot.setLabel("left", ylabel, units=units, color="#526378")
            plot.getViewBox().invertY(True)
            plot.showGrid(x=True, y=True, alpha=0.14)
            for axis in ("left", "bottom"):
                plot.getAxis(axis).setPen("#CAD5E3")
                plot.getAxis(axis).setTextPen("#526378")
            plot.setDownsampling(auto=True, mode="peak")
            plot.setClipToView(True)
            self.plots[key] = plot
            if key == "a_vref":
                self.a_ref_legend = plot.addLegend(offset=(12, 10))
            self.curves[key] = plot.plot(
                pen=pg.mkPen(color, width=2), connect="finite",
                name="Vref measured" if key == "a_vref" else None,
            )
            grid.addWidget(plot, row, col)
        self.curves["a_target"] = self.plots["a_vref"].plot(
            pen=pg.mkPen("#A76647", width=1.5, style=QtCore.Qt.DashLine),
            connect="finite", name="Eref target",
        )
        self.b_note = QtWidgets.QFrame()
        self.b_note.setObjectName("noteCard")
        note_layout = QtWidgets.QVBoxLayout(self.b_note)
        note_layout.addWidget(self._hint("OECT B  •  direct gate drive"))
        self.b_live = QtWidgets.QLabel("Ready")
        self.b_live.setObjectName("plotStatus")
        self.b_live.setWordWrap(True)
        note_layout.addWidget(self.b_live)
        note_layout.addWidget(self._hint(
            "B has no reference electrode. Its plot and data end when its schedule ends; "
            "CH3 and CH4 then hold at 0 V until A also finishes."
        ))
        note_layout.addStretch(1)
        grid.addWidget(self.b_note, 2, 1)
        for row in range(3):
            grid.setRowStretch(row, 1)
        grid.setColumnStretch(0, 1)
        grid.setColumnStretch(1, 1)
        return panel

    def _run_controls(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("controlActions")
        row = QtWidgets.QHBoxLayout(card)
        row.setContentsMargins(12, 9, 12, 9)
        row.setSpacing(10)
        self.start_button = QtWidgets.QPushButton("Start both OECTs")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QtWidgets.QPushButton("Stop safely")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        self.defaults_button = QtWidgets.QPushButton("Set current as default")
        self.defaults_button.setProperty("secondary", True)
        self.defaults_button.setToolTip("Save all current settings for the next launch")
        self.defaults_button.clicked.connect(self._save_current_defaults)
        row.addWidget(self.start_button, 1)
        row.addWidget(self.stop_button, 1)
        row.addWidget(self.defaults_button, 1)
        return card

    def _footer(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("footerCard")
        row = QtWidgets.QHBoxLayout(card)
        row.setContentsMargins(14, 9, 14, 9)
        self.status_dot = QtWidgets.QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = self._hint("Ready • simulation mode")
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding,
                                        QtWidgets.QSizePolicy.Preferred)
        self.sample_label = self._hint("A 0 samples  •  B 0 samples")
        self.sample_label.setWordWrap(False)
        row.addWidget(self.status_dot)
        row.addWidget(self.status_label, 1)
        row.addWidget(self.sample_label)
        return card

    def _apply_style(self) -> None:
        up = (ASSETS_DIR / "spin_up.svg").as_posix()
        down = (ASSETS_DIR / "spin_down.svg").as_posix()
        self.setStyleSheet(
            """
            QMainWindow { background: #F6F2EA; }
            QWidget { color: #273449; font-family: 'Helvetica Neue'; font-size: 13px; }
            QScrollArea#controlScroll { background: transparent; border: 0; }
            QFrame#headerCard, QFrame#footerCard, QFrame#controlActions,
            QFrame#noteCard, QGroupBox { background: #FFFCF6; border: 1px solid #E2DACE; border-radius: 10px; }
            QFrame#headerCard { border-left: 4px solid #0D9D93; }
            QLabel#title { font-size: 23px; font-weight: 700; color: #1C3045; }
            QLabel#plotStatus { font-size: 17px; font-weight: 700; color: #865E30; }
            QLabel[muted="true"] { color: #63758B; }
            QLabel#modeBadge { color: #075E57; background: #DDF6F0; border-radius: 10px; padding: 5px 12px; font-size: 11px; font-weight: 800; }
            QLabel#statusDot { color: #0D9D93; font-size: 16px; }
            QGroupBox { margin-top: 12px; padding: 7px 0 0 0; font-weight: 700; }
            QGroupBox::title { subcontrol-origin: margin; left: 14px; padding: 0 6px; color: #30435B; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox { background: #FFFDF9; border: 1px solid #D9D1C5; border-radius: 6px; padding: 3px 7px; color: #24364A; min-height: 20px; selection-background-color: #AFDAD6; }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border: 1px solid #0D9D93; }
            QLineEdit:disabled, QSpinBox:disabled, QDoubleSpinBox:disabled, QComboBox:disabled { background: #F2EEE7; color: #8796A6; }
            QSpinBox, QDoubleSpinBox { padding-right: 22px; }
            QSpinBox::up-button, QDoubleSpinBox::up-button { subcontrol-origin: border; subcontrol-position: top right; width: 19px; background: #F3EFE7; border-left: 1px solid #D9D1C5; border-top-right-radius: 5px; }
            QSpinBox::down-button, QDoubleSpinBox::down-button { subcontrol-origin: border; subcontrol-position: bottom right; width: 19px; background: #F3EFE7; border-left: 1px solid #D9D1C5; border-bottom-right-radius: 5px; }
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__UP__"); width: 10px; height: 6px; }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__DOWN__"); width: 10px; height: 6px; }
            QPushButton { border: 1px solid #D9D1C5; border-radius: 7px; padding: 6px 11px; font-weight: 700; background: #F3EFE7; color: #30435B; }
            QPushButton:hover { background: #EAE3D8; }
            QPushButton:disabled { background: #F1EEE8; color: #9CACBB; }
            QPushButton#startButton { background: #0D9D93; border-color: #0D9D93; color: white; min-width: 145px; }
            QPushButton#startButton:hover { background: #0B827A; }
            QPushButton#stopButton { background: #C7586A; border-color: #C7586A; color: white; min-width: 105px; }
            QPushButton#stopButton:hover { background: #AC4557; }
            QPushButton[secondary="true"] { background: #FAF7F1; border: 1px solid #D9D1C5; }
            QProgressBar { background: #F1ECE3; border: 1px solid #DDD5CA; border-radius: 6px; text-align: center; color: #30435B; min-height: 16px; }
            QProgressBar::chunk { background: #49BAAF; border-radius: 5px; }
            QCheckBox { spacing: 8px; }
            """.replace("__UP__", up).replace("__DOWN__", down)
        )

    def _mode_changed(self, _index: int | None = None) -> None:
        feedback = self.a_mode.currentData() == "feedback"
        self.a_group.setTitle(
            "OECT A • reference feedback / target Eref" if feedback else
            "OECT A • Vref monitor / programmed Vg"
        )
        names = ("Eref initial", "Eref final", "Eref step") if feedback else (
            "Vg initial", "Vg final", "Vg step"
        )
        for (_, label, _), name in zip(self.a_sweep_fields, names):
            label.setText(name)
        self.a_mode_note.setText(
            "DMM Vref is used to correct Vg toward each target Eref." if feedback else
            "Vg follows the programmed sweep. DMM Vref is recorded but does not change Vg."
        )
        self.a_feedback_group.setTitle(
            "OECT A • gate bounds and feedback" if feedback else "OECT A • gate bounds"
        )
        self.a_feedback_note.setText(
            "Eref = Vref = V(reference) − V(source). Confirm the actual gate response direction."
            if feedback else
            "All Vg sweep levels must fit inside the gate bounds. Reference feedback controls are inactive."
        )
        self.curves["a_target"].setVisible(feedback)
        self.a_ref_legend.setVisible(feedback)
        self._refresh_controls()

    def _refresh_controls(self, _unused: object = None) -> None:
        running = self.worker is not None
        probing = self.connection_worker is not None
        self.start_button.setEnabled(not running and not probing)
        self.stop_button.setEnabled(running)
        self.defaults_button.setEnabled(not running and not probing)
        self.simulation.setEnabled(not running and not probing)
        self.mode_badge.setText("SIMULATION" if self.simulation.isChecked() else "LIVE INSTRUMENT")
        self.mode_badge.setStyleSheet(
            "background: #DDF6F0; color: #075E57;" if self.simulation.isChecked()
            else "background: #FFF0D9; color: #785318;"
        )
        live_editable = not running and not probing and not self.simulation.isChecked()
        for widget in (self.adcmt_ip, self.dmm_ip, self.port, self.test_button):
            widget.setEnabled(live_editable)
        for widget in (
            self.a_output, self.b_output, self.a_browse, self.b_browse,
            self.a_mode, self.a_vds, self.a_start, self.a_stop, self.a_step,
            self.a_pattern, self.a_hold, self.a_delay, self.a_final, self.a_interval,
            self.a_gate_min, self.a_gate_max,
            self.b_vds, self.b_start, self.b_stop, self.b_step, self.b_pattern,
            self.b_hold, self.b_delay, self.b_final, self.b_interval,
            self.b_gate_min, self.b_gate_max,
        ):
            widget.setEnabled(not running and not probing)
        self.a_cycles.setEnabled(not running and not probing and
                                 self.a_pattern.currentData() == "back_and_forth")
        self.b_cycles.setEnabled(not running and not probing and
                                 self.b_pattern.currentData() == "back_and_forth")
        feedback_editable = not running and not probing and self.a_mode.currentData() == "feedback"
        for widget in self.a_feedback_controls:
            widget.setEnabled(feedback_editable)
        if not running and not probing:
            self.status_label.setText(
                "Ready • simulation mode" if self.simulation.isChecked()
                else "Ready • verify all four channel connections and A reference wiring"
            )

    def _browse_output(self, field: QtWidgets.QLineEdit) -> None:
        initial = Path(field.text().strip()).expanduser()
        if not initial.is_absolute():
            initial = PROJECT_DIR / initial
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Choose raw-data file", str(initial), "Tab-delimited text (*.txt)"
        )
        if selected:
            field.setText(selected)

    def _default_widgets(self) -> dict[str, QtWidgets.QWidget]:
        """Every editable value that should survive restarting this front panel."""
        return {
            "simulation": self.simulation,
            "adcmt_ip": self.adcmt_ip,
            "dmm_ip": self.dmm_ip,
            "port": self.port,
            "a_output": self.a_output,
            "b_output": self.b_output,
            "a_mode": self.a_mode,
            "a_vds": self.a_vds,
            "a_start": self.a_start,
            "a_stop": self.a_stop,
            "a_step": self.a_step,
            "a_pattern": self.a_pattern,
            "a_cycles": self.a_cycles,
            "a_hold": self.a_hold,
            "a_delay": self.a_delay,
            "a_final": self.a_final,
            "a_interval": self.a_interval,
            "a_gate_min": self.a_gate_min,
            "a_gate_max": self.a_gate_max,
            "a_gate_initial": self.a_gate_initial,
            "a_gain": self.a_gain,
            "a_max_change": self.a_max_change,
            "a_tolerance": self.a_tolerance,
            "a_polarity": self.a_polarity,
            "b_vds": self.b_vds,
            "b_start": self.b_start,
            "b_stop": self.b_stop,
            "b_step": self.b_step,
            "b_pattern": self.b_pattern,
            "b_cycles": self.b_cycles,
            "b_hold": self.b_hold,
            "b_delay": self.b_delay,
            "b_final": self.b_final,
            "b_interval": self.b_interval,
            "b_gate_min": self.b_gate_min,
            "b_gate_max": self.b_gate_max,
        }

    @staticmethod
    def _widget_value(widget: QtWidgets.QWidget) -> bool | str | int | float:
        if isinstance(widget, QtWidgets.QCheckBox):
            return widget.isChecked()
        if isinstance(widget, QtWidgets.QComboBox):
            return widget.currentData()
        if isinstance(widget, QtWidgets.QLineEdit):
            return widget.text()
        if isinstance(widget, (QtWidgets.QSpinBox, QtWidgets.QDoubleSpinBox)):
            return widget.value()
        raise TypeError(f"Unsupported settings control: {type(widget).__name__}")

    @staticmethod
    def _set_widget_value(widget: QtWidgets.QWidget, value: object) -> None:
        if isinstance(widget, QtWidgets.QCheckBox) and type(value) is bool:
            widget.setChecked(value)
        elif isinstance(widget, QtWidgets.QComboBox) and type(value) in (str, int):
            index = widget.findData(value)
            if index < 0:
                raise ValueError(f"Unrecognized option {value!r}")
            widget.setCurrentIndex(index)
        elif isinstance(widget, QtWidgets.QLineEdit) and isinstance(value, str):
            widget.setText(value)
        elif isinstance(widget, QtWidgets.QSpinBox) and type(value) is int:
            if not widget.minimum() <= value <= widget.maximum():
                raise ValueError(f"Saved integer {value!r} is outside its control range")
            widget.setValue(value)
        elif isinstance(widget, QtWidgets.QDoubleSpinBox) and type(value) in (int, float):
            if not widget.minimum() <= value <= widget.maximum() or not math.isfinite(value):
                raise ValueError(f"Saved number {value!r} is outside its control range")
            widget.setValue(value)
        else:
            raise ValueError(f"Saved value {value!r} has the wrong type")

    def _current_defaults(self) -> dict[str, bool | str | int | float]:
        return {name: self._widget_value(widget)
                for name, widget in self._default_widgets().items()}

    def _restore_saved_defaults(self) -> str | None:
        try:
            saved = load_defaults("dual")
        except ValueError as exc:
            return f"Ready • saved defaults could not be loaded: {exc}"
        if not saved:
            return None
        widgets = self._default_widgets()
        factory = self._current_defaults()
        try:
            for name, value in saved.items():
                if name in widgets:
                    self._set_widget_value(widgets[name], value)
            self._plan()
        except (TypeError, ValueError) as exc:
            for name, value in factory.items():
                self._set_widget_value(widgets[name], value)
            return f"Ready • saved defaults were invalid; factory values restored: {exc}"
        return None

    def _save_current_defaults(self) -> None:
        if self.worker is not None or self.connection_worker is not None:
            return
        try:
            self._plan()
            path = save_defaults("dual", self._current_defaults())
        except (OSError, TypeError, ValueError) as exc:
            QtWidgets.QMessageBox.warning(self, "Could not set defaults", str(exc))
        else:
            self.status_label.setText(f"Current settings saved as defaults • {path.name}")

    def _plan(self) -> DualPlan:
        a = APlan(
            simulation=self.simulation.isChecked(),
            mode=self.a_mode.currentData(),
            adcmt_ip=self.adcmt_ip.text().strip(),
            dmm_ip=self.dmm_ip.text().strip(),
            port=self.port.value(),
            vds_v=self.a_vds.value(),
            sweep_start_v=self.a_start.value(),
            sweep_stop_v=self.a_stop.value(),
            sweep_step_v=self.a_step.value(),
            sweep_pattern=self.a_pattern.currentData(),
            sweep_cycles=self.a_cycles.value(),
            hold_s=self.a_hold.value(),
            delay_s=self.a_delay.value(),
            final_keep_s=self.a_final.value(),
            sample_interval_s=self.a_interval.value(),
            gate_initial_v=self.a_gate_initial.value(),
            gate_min_v=self.a_gate_min.value(),
            gate_max_v=self.a_gate_max.value(),
            correction_factor=self.a_gain.value(),
            feedback_polarity=self.a_polarity.currentData(),
            feedback_max_step_v=self.a_max_change.value(),
            feedback_tolerance_v=self.a_tolerance.value(),
            drain_limit_a=0.003,
            gate_limit_a=0.003,
            output_file=self.a_output.text().strip(),
        )
        b = BPlan(
            vds_v=self.b_vds.value(),
            sweep_start_v=self.b_start.value(),
            sweep_stop_v=self.b_stop.value(),
            sweep_step_v=self.b_step.value(),
            sweep_pattern=self.b_pattern.currentData(),
            sweep_cycles=self.b_cycles.value(),
            hold_s=self.b_hold.value(),
            delay_s=self.b_delay.value(),
            final_keep_s=self.b_final.value(),
            sample_interval_s=self.b_interval.value(),
            gate_min_v=self.b_gate_min.value(),
            gate_max_v=self.b_gate_max.value(),
            drain_limit_a=0.003,
            gate_limit_a=0.003,
            output_file=self.b_output.text().strip(),
        )
        plan = DualPlan(a, b)
        plan.validate()
        return plan

    def _clear_plots(self) -> None:
        for lane in self._plot_data.values():
            for values in lane.values():
                values.clear()
        for curve in self.curves.values():
            curve.setData([], [])
        for lane in ("a", "b"):
            self._counts[lane] = 0
            self._completed[lane] = False
            self.progress[lane].setValue(0)
            self.progress_detail[lane].setText("Starting")
        self.sample_label.setText("A 0 samples  •  B 0 samples")
        self.b_live.setText("Starting")

    def _start(self) -> None:
        if self.worker is not None or self.connection_worker is not None:
            return
        try:
            plan = self._plan()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        existing = [path for path in (plan.a.output_path, plan.b.output_path) if path.exists()]
        if existing:
            names = "\n".join(str(path) for path in existing)
            answer = QtWidgets.QMessageBox.question(
                self, "Replace existing output?",
                f"These raw-data files already exist and will be replaced:\n\n{names}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        self._clear_plots()
        self._durations = {"a": plan.a.duration_s, "b": plan.b.duration_s}
        self._last_result = None
        self.worker = AcquisitionWorker(plan, self)
        self.worker.sample_a_ready.connect(self._on_sample_a)
        self.worker.sample_b_ready.connect(self._on_sample_b)
        self.worker.status_ready.connect(self._on_status)
        self.worker.run_complete.connect(self._on_run_complete)
        self.worker.finished.connect(self._on_worker_finished)
        self._refresh_controls()
        self.status_dot.setStyleSheet("color: #0D9D93;")
        self.status_label.setText(
            f"Starting both • A {len(plan.a.levels)} levels / {plan.a.duration_s:g} s  •  "
            f"B {len(plan.b.levels)} levels / {plan.b.duration_s:g} s"
        )
        self.worker.start()

    def _stop(self) -> None:
        if self.worker is not None:
            self.stop_button.setEnabled(False)
            self.status_label.setText("Stopping safely after the current instrument transaction…")
            self.worker.request_stop()

    def _mark_complete(self, lane: str) -> None:
        if self._completed[lane]:
            return
        self._completed[lane] = True
        self.progress[lane].setValue(100)
        self.progress_detail[lane].setText(f"Complete • {self._counts[lane]} samples")
        if lane == "b":
            self.b_live.setText("Complete • CH3 and CH4 at 0 V")

    @QtCore.pyqtSlot(object)
    def _on_sample_a(self, sample: Sample) -> None:
        data = self._plot_data["a"]
        data["time"].append(sample.elapsed_s)
        data["id"].append(sample.drain_i_a)
        data["vg"].append(sample.gate_v)
        data["vref"].append(sample.vref_v)
        self.curves["a_id"].setData(data["time"], data["id"])
        self.curves["a_vg"].setData(data["time"], data["vg"], stepMode="right")
        self.curves["a_vref"].setData(data["time"], data["vref"])
        if self.worker is not None and self.worker.plan.a.mode == "feedback":
            data["target"].append(sample.target_v)
            self.curves["a_target"].setData(data["time"], data["target"], stepMode="right")
        self._counts["a"] += 1
        self.progress["a"].setValue(min(99, max(0, int(100 * sample.elapsed_s / self._durations["a"]))))
        self.progress_detail["a"].setText(f"Level {sample.stage_index} • {sample.phase}")
        self.sample_label.setText(f"A {self._counts['a']} samples  •  B {self._counts['b']} samples")

    @QtCore.pyqtSlot(object)
    def _on_sample_b(self, sample: BSample) -> None:
        data = self._plot_data["b"]
        data["time"].append(sample.elapsed_s)
        data["id"].append(sample.drain_i_a)
        data["vg"].append(sample.gate_v)
        self.curves["b_id"].setData(data["time"], data["id"])
        self.curves["b_vg"].setData(data["time"], data["vg"], stepMode="right")
        self._counts["b"] += 1
        self.progress["b"].setValue(min(99, max(0, int(100 * sample.elapsed_s / self._durations["b"]))))
        self.progress_detail["b"].setText(f"Level {sample.stage_index} • {sample.phase}")
        self.b_live.setText(f"Programmed Vg {sample.target_v:+.4g} V • sample {self._counts['b']}")
        self.sample_label.setText(f"A {self._counts['a']} samples  •  B {self._counts['b']} samples")

    @QtCore.pyqtSlot(str)
    def _on_status(self, message: str) -> None:
        self.status_label.setText(message)
        lower = message.lower()
        if lower.startswith("oect a complete") or lower.startswith("oect a finished"):
            self._mark_complete("a")
        if lower.startswith("oect b complete") or lower.startswith("oect b finished"):
            self._mark_complete("b")

    @QtCore.pyqtSlot(bool, str)
    def _on_run_complete(self, success: bool, message: str) -> None:
        self._last_result = success, message

    @QtCore.pyqtSlot()
    def _on_worker_finished(self) -> None:
        self.worker = None
        self._refresh_controls()
        success, message = self._last_result or (False, "Measurement ended unexpectedly")
        self.status_label.setText(message)
        self.status_dot.setStyleSheet("color: #0D9D93;" if success else "color: #C7586A;")
        if success and message == "Both measurements completed":
            self._mark_complete("a")
            self._mark_complete("b")
        elif success:
            for lane in ("a", "b"):
                if not self._completed[lane]:
                    self.progress_detail[lane].setText(f"Stopped • {self._counts[lane]} samples")
        if not success and not self._pending_close:
            QtWidgets.QMessageBox.critical(self, "Measurement ended", message)
        if self._pending_close and self.connection_worker is None:
            QtCore.QTimer.singleShot(0, self.close)

    def _test_connections(self) -> None:
        if self.worker is not None or self.connection_worker is not None:
            return
        adcmt_ip = self.adcmt_ip.text().strip()
        dmm_ip = self.dmm_ip.text().strip()
        if not adcmt_ip or not dmm_ip:
            QtWidgets.QMessageBox.warning(self, "Connection test", "Enter both instrument IP addresses")
            return
        self.connection_worker = ConnectionWorker(adcmt_ip, dmm_ip, self.port.value(), self)
        self.connection_worker.probe_complete.connect(self._on_probe_complete)
        self.connection_worker.finished.connect(self._on_probe_finished)
        self._refresh_controls()
        self.status_label.setText("Testing read-only ADCMT and DMM6500 connections…")
        self.connection_worker.start()

    @QtCore.pyqtSlot(bool, str)
    def _on_probe_complete(self, success: bool, message: str) -> None:
        self.status_label.setText(("Connected • " if success else "Connection failed • ") + message)
        self.status_dot.setStyleSheet("color: #0D9D93;" if success else "color: #C7586A;")
        if not success and not self._pending_close:
            QtWidgets.QMessageBox.warning(self, "Connection test", message)

    @QtCore.pyqtSlot()
    def _on_probe_finished(self) -> None:
        self.connection_worker = None
        self._refresh_controls()
        if self._pending_close and self.worker is None:
            QtCore.QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            if not self._pending_close:
                answer = QtWidgets.QMessageBox.question(
                    self, "Measurement is running", "Stop both OECTs safely and close?",
                    QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                    QtWidgets.QMessageBox.No,
                )
                if answer != QtWidgets.QMessageBox.Yes:
                    event.ignore()
                    return
                self._pending_close = True
                self._stop()
            event.ignore()
            return
        if self.connection_worker is not None and self.connection_worker.isRunning():
            self._pending_close = True
            self.status_label.setText("Closing after the connection test…")
            event.ignore()
            return
        event.accept()


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--screenshot", type=Path, help="save a preview image and exit")
    args = parser.parse_args(argv)
    if args.screenshot:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    pg.setConfigOptions(antialias=True)
    app = QtWidgets.QApplication(sys.argv[:1])
    app.setApplicationName(APP_TITLE)
    window = DualOECTWindow()
    window.show()
    if args.screenshot:
        app.processEvents()
        args.screenshot.parent.mkdir(parents=True, exist_ok=True)
        if not window.grab().save(str(args.screenshot)):
            raise RuntimeError(f"Could not save screenshot to {args.screenshot}")
        return 0
    return app.exec_()


if __name__ == "__main__":
    raise SystemExit(main())
