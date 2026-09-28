#!/usr/bin/env python3
"""Desktop front panel for one ADCMT-driven OECT with a DMM6500 reference input."""

from __future__ import annotations

import argparse
import os
import sys
import threading
from pathlib import Path
from typing import Sequence

from PyQt5 import QtCore, QtGui, QtWidgets
import pyqtgraph as pg

from instruments import probe_links
from models import RunConfig, Sample
from runner import run_experiment


APP_TITLE = "OECT with reference"
PROJECT_DIR = Path(__file__).resolve().parent


class CompactDoubleSpinBox(QtWidgets.QDoubleSpinBox):
    """Display editable precision without trailing decimal zeros."""

    def textFromValue(self, value: float) -> str:  # noqa: N802
        number = f"{value:.{self.decimals()}f}"
        if "." in number:
            number = number.rstrip("0").rstrip(".")
        return "0" if number == "-0" else number


class CompactAxisItem(pg.AxisItem):
    def tickStrings(self, values: Sequence[float], scale: float, spacing: float) -> list[str]:  # noqa: N802
        labels = [f"{value * scale:.8g}" for value in values]
        return ["0" if label == "-0" else label for label in labels]


class AcquisitionWorker(QtCore.QThread):
    sample_ready = QtCore.pyqtSignal(object)
    status_ready = QtCore.pyqtSignal(str)
    run_complete = QtCore.pyqtSignal(bool, str)

    def __init__(self, config: RunConfig, parent: QtCore.QObject | None = None) -> None:
        super().__init__(parent)
        self.config = config
        self.stop_event = threading.Event()

    def request_stop(self) -> None:
        self.stop_event.set()

    def run(self) -> None:
        try:
            result = run_experiment(
                self.config,
                on_sample=self.sample_ready.emit,
                on_status=self.status_ready.emit,
                stop_event=self.stop_event,
            )
        except Exception as exc:
            self.run_complete.emit(False, f"{type(exc).__name__}: {exc}")
        else:
            message = "Stopped safely" if result.stopped else "Measurement completed"
            self.run_complete.emit(True, message)


class ConnectionWorker(QtCore.QThread):
    probe_complete = QtCore.pyqtSignal(bool, str)

    def __init__(
        self, adcmt_host: str, dmm_host: str, port: int, parent: QtCore.QObject | None = None
    ) -> None:
        super().__init__(parent)
        self.adcmt_host = adcmt_host
        self.dmm_host = dmm_host
        self.port = port

    def run(self) -> None:
        try:
            summary = probe_links(self.adcmt_host, self.dmm_host, self.port)
        except Exception as exc:
            self.probe_complete.emit(False, f"{type(exc).__name__}: {exc}")
        else:
            if isinstance(summary, dict):
                message = "  •  ".join(f"{key}: {value}" for key, value in summary.items())
            else:
                message = str(summary)
            self.probe_complete.emit(True, message)


def spin(
    minimum: float,
    maximum: float,
    value: float,
    step: float,
    decimals: int,
    suffix: str,
) -> QtWidgets.QDoubleSpinBox:
    box = CompactDoubleSpinBox()
    box.setRange(minimum, maximum)
    box.setDecimals(decimals)
    box.setSingleStep(step)
    box.setValue(value)
    box.setSuffix(suffix)
    box.setKeyboardTracking(False)
    box.setMinimumWidth(112)
    return box


class OECTWindow(QtWidgets.QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.resize(1450, 930)
        self.setMinimumSize(1110, 820)
        self.worker: AcquisitionWorker | None = None
        self.connection_worker: ConnectionWorker | None = None
        self._last_result: tuple[bool, str] | None = None
        self._pending_close = False
        self._sample_count = 0
        self._duration_s = 1.0
        self._plot_data: dict[str, list[float]] = {
            "time": [], "id": [], "vg": [], "vref": [], "target_ref": [],
        }

        root = QtWidgets.QWidget()
        self.setCentralWidget(root)
        outer = QtWidgets.QVBoxLayout(root)
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
        self._simulation_changed(True)

    def _header(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("headerCard")
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(18, 13, 18, 13)
        title_box = QtWidgets.QVBoxLayout()
        title = QtWidgets.QLabel(APP_TITLE)
        title.setObjectName("title")
        subtitle = QtWidgets.QLabel("ADCMT 6541 • CH1 drain / CH2 gate  |  DMM6500 • Reference voltage")
        subtitle.setProperty("muted", True)
        title_box.addWidget(title)
        title_box.addWidget(subtitle)
        layout.addLayout(title_box, 1)
        self.mode_badge = QtWidgets.QLabel("SIMULATION")
        self.mode_badge.setObjectName("modeBadge")
        layout.addWidget(self.mode_badge)
        return card

    def _control_panel(self) -> QtWidgets.QWidget:
        controls = QtWidgets.QWidget()
        controls.setMinimumWidth(560)
        controls.setMaximumWidth(620)
        controls_layout = QtWidgets.QVBoxLayout(controls)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(10)
        scroll = QtWidgets.QScrollArea()
        scroll.setObjectName("controlScroll")
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QtWidgets.QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(QtCore.Qt.ScrollBarAlwaysOff)
        panel = QtWidgets.QWidget()
        layout = QtWidgets.QVBoxLayout(panel)
        layout.setContentsMargins(0, 0, 4, 0)
        layout.setSpacing(10)
        layout.addWidget(self._connection_card())
        layout.addWidget(self._output_card())
        layout.addWidget(self._sweep_card())
        layout.addWidget(self._feedback_card())
        layout.addStretch(1)
        scroll.setWidget(panel)
        controls_layout.addWidget(scroll, 1)
        controls_layout.addWidget(self._run_controls())
        return controls

    @staticmethod
    def _group_grid(title: str) -> tuple[QtWidgets.QGroupBox, QtWidgets.QGridLayout]:
        group = QtWidgets.QGroupBox(title)
        grid = QtWidgets.QGridLayout(group)
        grid.setContentsMargins(14, 16, 14, 10)
        grid.setHorizontalSpacing(10)
        grid.setVerticalSpacing(7)
        return group, grid

    @staticmethod
    def _row_field(label: QtWidgets.QLabel, widget: QtWidgets.QWidget) -> QtWidgets.QWidget:
        field = QtWidgets.QWidget()
        layout = QtWidgets.QHBoxLayout(field)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(7)
        label.setMinimumWidth(85)
        layout.addWidget(label)
        layout.addWidget(widget, 1)
        return field

    def _connection_card(self) -> QtWidgets.QWidget:
        group, grid = self._group_grid("Connection")
        self.connection_group = group
        self.simulation = QtWidgets.QCheckBox("Simulation • no instrument commands")
        self.simulation.setChecked(True)
        self.simulation.toggled.connect(self._simulation_changed)
        grid.addWidget(self.simulation, 0, 0, 1, 4)

        self.adcmt_ip = QtWidgets.QLineEdit(RunConfig.adcmt_ip)
        self.dmm_ip = QtWidgets.QLineEdit(RunConfig.dmm_ip)
        self.port = QtWidgets.QSpinBox()
        self.port.setRange(1, 65535)
        self.port.setValue(RunConfig.port)
        self.port.setFixedWidth(88)
        grid.addWidget(QtWidgets.QLabel("ADCMT IP"), 1, 0)
        grid.addWidget(self.adcmt_ip, 1, 1)
        grid.addWidget(QtWidgets.QLabel("DMM6500 IP"), 1, 2)
        grid.addWidget(self.dmm_ip, 1, 3)
        grid.addWidget(QtWidgets.QLabel("TCP port"), 2, 0)
        grid.addWidget(self.port, 2, 1)
        self.test_button = QtWidgets.QPushButton("Test both connections")
        self.test_button.setProperty("secondary", True)
        self.test_button.clicked.connect(self._test_connections)
        grid.addWidget(self.test_button, 2, 2, 1, 2)
        note = QtWidgets.QLabel("DMM front INPUT HI → Reference  •  INPUT LO → OECT source")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        grid.addWidget(note, 3, 0, 1, 4)
        return group

    def _sweep_card(self) -> QtWidgets.QWidget:
        self.sweep_group, grid = self._group_grid("Target Eref sweep")
        defaults = RunConfig()
        self.vds = spin(-3, 3, defaults.vds_v, 0.01, 4, " V")
        self.sweep_start = spin(-3, 3, defaults.sweep_start_v, 0.1, 4, " V")
        self.sweep_stop = spin(-3, 3, defaults.sweep_stop_v, 0.1, 4, " V")
        self.sweep_step = spin(-3, 3, defaults.sweep_step_v, 0.1, 4, " V")
        self.hold = spin(0.1, 1_000_000, defaults.hold_s, 10, 3, " s")
        self.delay = spin(0, 999_999, defaults.delay_s, 0.1, 3, " s")
        self.final_keep = spin(0, 1_000_000, defaults.final_keep_s, 60, 3, " s")
        self.interval = spin(0.1, 60, defaults.sample_interval_s, 0.1, 3, " s")
        self.start_label = QtWidgets.QLabel("Eref initial")
        self.stop_label = QtWidgets.QLabel("Eref final")
        self.step_label = QtWidgets.QLabel("Eref step")
        fields = (
            ((QtWidgets.QLabel("Vds"), self.vds), (QtWidgets.QLabel("Period"), self.hold)),
            ((self.start_label, self.sweep_start), (QtWidgets.QLabel("Delay"), self.delay)),
            ((self.stop_label, self.sweep_stop), (QtWidgets.QLabel("Keep at final"), self.final_keep)),
            ((self.step_label, self.sweep_step), (QtWidgets.QLabel("Read interval"), self.interval)),
        )
        for row, pair in enumerate(fields):
            for column, (caption, control) in enumerate(pair):
                grid.addWidget(self._row_field(caption, control), row, column)
        note = QtWidgets.QLabel("Delay is inside each period. Drain and gate current limits: 0.003 A each.")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        grid.addWidget(note, len(fields), 0, 1, 2)
        return self.sweep_group

    def _feedback_card(self) -> QtWidgets.QWidget:
        group, grid = self._group_grid("Gate bounds and reference feedback")
        defaults = RunConfig()
        self.gate_min = spin(-3, 3, defaults.gate_min_v, 0.1, 4, " V")
        self.gate_max = spin(-3, 3, defaults.gate_max_v, 0.1, 4, " V")
        self.gate_initial = spin(-3, 3, defaults.gate_initial_v, 0.1, 4, " V")
        self.correction_factor = spin(0.001, 10, defaults.correction_factor, 0.1, 4, "")
        self.max_step = spin(0.0001, 3, defaults.feedback_max_step_v, 0.01, 4, " V")
        self.tolerance = spin(0, 3, defaults.feedback_tolerance_v, 0.001, 4, " V")
        self.polarity = QtWidgets.QComboBox()
        self.polarity.addItem("Decrease Vg lowers Eref (same direction)", +1)
        self.polarity.addItem("Increase Vg lowers Eref (opposite direction)", -1)
        self.polarity.setCurrentIndex(0)
        self.polarity.setToolTip(
            "Choose how your cell responds to a gate-voltage change.\n"
            "Decrease Vg: e.g. −0.30 → −0.32 V. Increase Vg: −0.30 → −0.28 V.\n"
            "This selects the feedback direction; the DMM reading keeps its measured sign."
        )
        rows = (
            (("Gate min", self.gate_min), ("Gate max", self.gate_max)),
            (("Initial gate", self.gate_initial), ("Correction factor", self.correction_factor)),
            (("Max change", self.max_step), ("Tolerance", self.tolerance)),
        )
        for row, pair in enumerate(rows):
            for column, (caption, control) in enumerate(pair):
                grid.addWidget(self._row_field(QtWidgets.QLabel(caption), control), row, column)
        grid.addWidget(self._row_field(QtWidgets.QLabel("Direction"), self.polarity), 3, 0, 1, 2)
        note = QtWidgets.QLabel(
            "Eref = Vref = V(reference) − V(source). The gate adjusts to follow the Eref target."
        )
        note.setProperty("muted", True)
        note.setWordWrap(True)
        grid.addWidget(note, 4, 0, 1, 2)
        self.feedback_controls = (self.gate_initial, self.correction_factor, self.max_step, self.tolerance, self.polarity)
        return group

    def _output_card(self) -> QtWidgets.QWidget:
        group, grid = self._group_grid("Raw data")
        self.output_group = group
        self.output = QtWidgets.QLineEdit(RunConfig.output_file)
        self.output.setToolTip("Relative filenames are saved inside this project folder")
        self.browse_button = QtWidgets.QPushButton("Browse")
        self.browse_button.setProperty("secondary", True)
        self.browse_button.clicked.connect(self._browse_output)
        grid.addWidget(self.output, 0, 0)
        grid.addWidget(self.browse_button, 0, 1)
        self.progress = QtWidgets.QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(125)
        self.progress_detail = QtWidgets.QLabel("Ready")
        self.progress_detail.setProperty("muted", True)
        progress_row = QtWidgets.QHBoxLayout()
        progress_row.addWidget(self.progress)
        progress_row.addStretch(1)
        progress_row.addWidget(self.progress_detail)
        grid.addLayout(progress_row, 1, 0, 1, 2)
        note = QtWidgets.QLabel("Every row includes measured Vref and Eref (reference − source).")
        note.setProperty("muted", True)
        note.setWordWrap(True)
        grid.addWidget(note, 2, 0, 1, 2)
        return group

    def _plot_panel(self) -> QtWidgets.QWidget:
        panel = QtWidgets.QWidget()
        grid = QtWidgets.QGridLayout(panel)
        grid.setContentsMargins(0, 0, 0, 0)
        grid.setSpacing(10)
        self.plots: dict[str, pg.PlotWidget] = {}
        self.curves: dict[str, pg.PlotDataItem] = {}
        specs = (
            ("id", "OECT • Drain current (CH1)", "Id", "A", "#078A84"),
            ("vg", "OECT • Gate voltage command (CH2)", "Vg", "V", "#1CA89B"),
            ("vref", "Reference • DMM6500 measured", "Vref / Eref", "V", "#8260BE"),
        )
        for row, (key, title, ylabel, units, color) in enumerate(specs):
            plot = pg.PlotWidget(
                background="#FFFCF6",
                axisItems={
                    "left": CompactAxisItem(orientation="left"),
                    "bottom": CompactAxisItem(orientation="bottom"),
                },
            )
            plot.setTitle(title, color="#273449", size="11pt")
            plot.setLabel("bottom", "Elapsed time", units="s", color="#526378")
            plot.setLabel("left", ylabel, units=units, color="#526378")
            plot.getViewBox().invertY(True)
            plot.showGrid(x=True, y=True, alpha=0.14)
            plot.getAxis("left").setPen("#CAD5E3")
            plot.getAxis("bottom").setPen("#CAD5E3")
            plot.getAxis("left").setTextPen("#526378")
            plot.getAxis("bottom").setTextPen("#526378")
            plot.setDownsampling(auto=True, mode="peak")
            plot.setClipToView(True)
            self.plots[key] = plot
            if key == "vref":
                plot.addLegend(offset=(12, 10))
            self.curves[key] = plot.plot(
                pen=pg.mkPen(color, width=2),
                connect="finite",
                name="Vref measured" if key == "vref" else None,
            )
            grid.addWidget(plot, row, 0)
            grid.setRowStretch(row, 1)
        self.curves["target_ref"] = self.plots["vref"].plot(
            pen=pg.mkPen("#A76647", width=1.5, style=QtCore.Qt.DashLine),
            connect="finite", name="Eref target"
        )
        return panel

    def _run_controls(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("controlActions")
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(12, 9, 12, 9)
        layout.setSpacing(10)
        self.start_button = QtWidgets.QPushButton("Start measurement")
        self.start_button.setObjectName("startButton")
        self.start_button.clicked.connect(self._start)
        self.stop_button = QtWidgets.QPushButton("Stop safely")
        self.stop_button.setObjectName("stopButton")
        self.stop_button.setEnabled(False)
        self.stop_button.clicked.connect(self._stop)
        layout.addWidget(self.start_button, 1)
        layout.addWidget(self.stop_button, 1)
        return card

    def _footer(self) -> QtWidgets.QWidget:
        card = QtWidgets.QFrame()
        card.setObjectName("footerCard")
        layout = QtWidgets.QHBoxLayout(card)
        layout.setContentsMargins(14, 9, 14, 9)
        self.status_dot = QtWidgets.QLabel("●")
        self.status_dot.setObjectName("statusDot")
        self.status_label = QtWidgets.QLabel("Ready • simulation mode")
        self.status_label.setProperty("muted", True)
        self.status_label.setMinimumWidth(250)
        self.status_label.setSizePolicy(QtWidgets.QSizePolicy.Expanding, QtWidgets.QSizePolicy.Preferred)
        self.sample_label = QtWidgets.QLabel("0 samples")
        self.sample_label.setProperty("muted", True)
        layout.addWidget(self.status_dot)
        layout.addWidget(self.status_label, 1)
        layout.addWidget(self.sample_label)
        return card

    def _apply_style(self) -> None:
        arrow_up = (PROJECT_DIR / "assets" / "spin_up.svg").as_posix()
        arrow_down = (PROJECT_DIR / "assets" / "spin_down.svg").as_posix()
        self.setStyleSheet(
            """
            QMainWindow { background: #F6F2EA; }
            QWidget { color: #273449; font-family: 'Helvetica Neue'; font-size: 13px; }
            QScrollArea#controlScroll { background: transparent; border: 0; }
            QFrame#headerCard, QFrame#footerCard, QFrame#controlActions, QGroupBox { background: #FFFCF6; border: 1px solid #E2DACE; border-radius: 10px; }
            QFrame#headerCard { border-left: 4px solid #0D9D93; }
            QLabel#title { font-size: 23px; font-weight: 700; color: #1C3045; }
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
            QSpinBox::up-arrow, QDoubleSpinBox::up-arrow { image: url("__ARROW_UP__"); width: 10px; height: 6px; }
            QSpinBox::down-arrow, QDoubleSpinBox::down-arrow { image: url("__ARROW_DOWN__"); width: 10px; height: 6px; }
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
            """.replace("__ARROW_UP__", arrow_up).replace("__ARROW_DOWN__", arrow_down)
        )

    def _simulation_changed(self, checked: bool) -> None:
        for widget in (self.adcmt_ip, self.dmm_ip, self.port, self.test_button):
            widget.setEnabled(not checked and self.worker is None)
        self.mode_badge.setText("SIMULATION" if checked else "LIVE INSTRUMENT")
        self.mode_badge.setStyleSheet(
            "background: #DDF6F0; color: #075E57;" if checked else
            "background: #FFF0D9; color: #785318;"
        )
        self.status_label.setText(
            "Ready • simulation mode" if checked else "Ready • verify reference wiring and gate direction"
        )

    def _browse_output(self) -> None:
        value = self.output.text().strip()
        initial = Path(value).expanduser() if value else Path(RunConfig.output_file)
        if not initial.is_absolute():
            initial = PROJECT_DIR / initial
        selected, _ = QtWidgets.QFileDialog.getSaveFileName(
            self, "Choose raw-data file", str(initial), "Tab-delimited data (*.tsv);;All files (*)"
        )
        if selected:
            self.output.setText(selected)

    def _config(self) -> RunConfig:
        config = RunConfig(
            simulation=self.simulation.isChecked(),
            adcmt_ip=self.adcmt_ip.text().strip(),
            dmm_ip=self.dmm_ip.text().strip(),
            port=self.port.value(),
            vds_v=self.vds.value(),
            sweep_start_v=self.sweep_start.value(),
            sweep_stop_v=self.sweep_stop.value(),
            sweep_step_v=self.sweep_step.value(),
            hold_s=self.hold.value(),
            delay_s=self.delay.value(),
            final_keep_s=self.final_keep.value(),
            sample_interval_s=self.interval.value(),
            gate_initial_v=self.gate_initial.value(),
            gate_min_v=self.gate_min.value(),
            gate_max_v=self.gate_max.value(),
            correction_factor=self.correction_factor.value(),
            feedback_polarity=self.polarity.currentData(),
            feedback_max_step_v=self.max_step.value(),
            feedback_tolerance_v=self.tolerance.value(),
            output_file=self.output.text().strip(),
        )
        config.validate()
        return config

    def _clear_plots(self) -> None:
        for values in self._plot_data.values():
            values.clear()
        for curve in self.curves.values():
            curve.setData([], [])
        self._sample_count = 0
        self.sample_label.setText("0 samples")
        self.progress.setValue(0)
        self.progress_detail.setText("Starting")

    def _start(self) -> None:
        if self.worker is not None or self.connection_worker is not None:
            return
        try:
            config = self._config()
        except Exception as exc:
            QtWidgets.QMessageBox.warning(self, "Invalid settings", str(exc))
            return
        if config.output_path.exists():
            answer = QtWidgets.QMessageBox.question(
                self, "Replace existing output?",
                f"This raw-data file already exists and will be replaced:\n\n{config.output_path}",
                QtWidgets.QMessageBox.Yes | QtWidgets.QMessageBox.No,
                QtWidgets.QMessageBox.No,
            )
            if answer != QtWidgets.QMessageBox.Yes:
                return
        self._clear_plots()
        self._duration_s = config.duration_s
        self._last_result = None
        self.worker = AcquisitionWorker(config, self)
        self.worker.sample_ready.connect(self._on_sample)
        self.worker.status_ready.connect(self._on_status)
        self.worker.run_complete.connect(self._on_run_complete)
        self.worker.finished.connect(self._on_worker_finished)
        self._set_running(True)
        self.status_dot.setStyleSheet("color: #0D9D93;")
        self.status_label.setText(
            f"Starting • {len(config.levels)} levels • estimated {config.duration_s:g} s"
        )
        self.worker.start()

    def _stop(self) -> None:
        if self.worker is not None:
            self.stop_button.setEnabled(False)
            self.status_label.setText("Stopping safely after the current instrument transaction…")
            self.worker.request_stop()

    @QtCore.pyqtSlot(object)
    def _on_sample(self, sample: Sample) -> None:
        data = self._plot_data
        data["time"].append(sample.elapsed_s)
        data["id"].append(sample.drain_i_a)
        data["vg"].append(sample.gate_v)
        data["vref"].append(sample.vref_v)
        data["target_ref"].append(sample.target_v)
        self.curves["id"].setData(data["time"], data["id"])
        self.curves["vg"].setData(data["time"], data["vg"], stepMode="right")
        self.curves["vref"].setData(data["time"], data["vref"])
        self.curves["target_ref"].setData(data["time"], data["target_ref"], stepMode="right")
        self._sample_count += 1
        self.sample_label.setText(f"{self._sample_count} samples")
        self.progress.setValue(min(99, max(0, int(100 * sample.elapsed_s / self._duration_s))))
        self.progress_detail.setText(f"Level {sample.stage_index} • {sample.phase}")
        self.status_label.setText(
            f"Vref / Eref {sample.vref_v:+.4g} V  •  target {sample.target_v:+.4g} V"
        )

    @QtCore.pyqtSlot(str)
    def _on_status(self, message: str) -> None:
        self.status_label.setText(message)

    @QtCore.pyqtSlot(bool, str)
    def _on_run_complete(self, success: bool, message: str) -> None:
        self._last_result = success, message

    @QtCore.pyqtSlot()
    def _on_worker_finished(self) -> None:
        self.worker = None
        self._set_running(False)
        success, message = self._last_result or (False, "Measurement ended unexpectedly")
        self.status_label.setText(message)
        self.status_dot.setStyleSheet("color: #0D9D93;" if success else "color: #C7586A;")
        if success and message == "Measurement completed":
            self.progress.setValue(100)
            self.progress_detail.setText(f"Complete • {self._sample_count} samples")
        elif success:
            self.progress_detail.setText(f"Stopped • {self._sample_count} samples")
        if not success and not self._pending_close:
            QtWidgets.QMessageBox.critical(self, "Measurement ended", message)
        if self._pending_close and self.connection_worker is None:
            QtCore.QTimer.singleShot(0, self.close)

    def _set_running(self, running: bool) -> None:
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(running)
        self.simulation.setEnabled(not running)
        for widget in (
            self.vds, self.sweep_start, self.sweep_stop, self.sweep_step,
            self.hold, self.delay, self.final_keep, self.interval,
            self.gate_min, self.gate_max,
            self.output, self.browse_button,
        ):
            widget.setEnabled(not running)
        for widget in self.feedback_controls:
            widget.setEnabled(not running)
        live_editable = not running and not self.simulation.isChecked()
        for widget in (self.adcmt_ip, self.dmm_ip, self.port, self.test_button):
            widget.setEnabled(live_editable)

    def _test_connections(self) -> None:
        if self.connection_worker is not None or self.worker is not None:
            return
        adcmt_host = self.adcmt_ip.text().strip()
        dmm_host = self.dmm_ip.text().strip()
        if not adcmt_host or not dmm_host:
            QtWidgets.QMessageBox.warning(self, "Connection test", "Enter both instrument IP addresses")
            return
        self.test_button.setEnabled(False)
        self.start_button.setEnabled(False)
        self.status_label.setText("Testing read-only connections to ADCMT and DMM6500…")
        self.connection_worker = ConnectionWorker(adcmt_host, dmm_host, self.port.value(), self)
        self.connection_worker.probe_complete.connect(self._on_probe_complete)
        self.connection_worker.finished.connect(self._on_probe_finished)
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
        self.start_button.setEnabled(self.worker is None)
        self.test_button.setEnabled(self.worker is None and not self.simulation.isChecked())
        if self._pending_close and self.worker is None:
            QtCore.QTimer.singleShot(0, self.close)

    def closeEvent(self, event: QtGui.QCloseEvent) -> None:  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            if not self._pending_close:
                answer = QtWidgets.QMessageBox.question(
                    self, "Measurement is running", "Stop safely and close?",
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
    window = OECTWindow()
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
