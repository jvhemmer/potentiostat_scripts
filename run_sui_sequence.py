#!/usr/bin/env python3
"""Run SUI custom experiments and synchronized chopped-CA phases in sequence."""

from __future__ import annotations

import argparse
import csv
import importlib
import json
import math
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from chopped_illumination import (
    LivePlotWindow,
    Settings as ChoppedSettings,
    build_experiment as build_chopped_experiment,
    connect_auto_shutter,
    require_success,
    run_shutter_schedule,
)
from squidstat_sui import SuiExperimentError, SuiExperimentLibrary


# Edit these two defaults, or override them in sequence JSON / command line.
CUSTOM_EXPERIMENTS_DIRECTORY = Path.home() / "Documents" / "Admiral Instruments" / "Custom Experiments"
EXPERIMENT_NAME = "ORR DME (rAu) 10mVps"
POTENTIOSTAT_PORT = "COM4"
CHANNEL = 0


class SequenceError(ValueError):
    pass


def _finite(value: Any, location: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SequenceError(f"{location} must be a number")
    result = float(value)
    if not math.isfinite(result):
        raise SequenceError(f"{location} must be finite")
    if positive and result <= 0:
        raise SequenceError(f"{location} must be greater than 0")
    if nonnegative and result < 0:
        raise SequenceError(f"{location} must be at least 0")
    return result


def _positive_int(value: Any, location: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise SequenceError(f"{location} must be an integer of at least 1")
    return value


def load_sequence(path: Path) -> dict[str, Any]:
    path = path.expanduser().resolve()
    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SequenceError(f"cannot read sequence {path}: {error}") from error
    if not isinstance(config, dict):
        raise SequenceError("sequence file must contain a JSON object")
    device = config.get("device")
    shutter = config.get("shutter", {})
    phases = config.get("sequence")
    if not isinstance(device, dict) or not isinstance(device.get("port"), str):
        raise SequenceError("device.port must be a COM-port string")
    channel = device.get("channel", 0)
    if isinstance(channel, bool) or not isinstance(channel, int) or not 0 <= channel <= 255:
        raise SequenceError("device.channel must be an integer between 0 and 255")
    if not isinstance(shutter, dict):
        raise SequenceError("shutter must be a JSON object")
    speed = shutter.get("speed", 2000)
    if isinstance(speed, bool) or not isinstance(speed, int) or not 50 <= speed <= 2000:
        raise SequenceError("shutter.speed must be an integer between 50 and 2000")
    if not isinstance(phases, list) or not phases:
        raise SequenceError("sequence must be a non-empty array")
    normalized: list[dict[str, Any]] = []
    for index, raw in enumerate(phases):
        location = f"sequence[{index}]"
        if not isinstance(raw, dict):
            raise SequenceError(f"{location} must be an object")
        phase = dict(raw)
        kind = phase.get("type")
        if kind == "sui_experiment":
            if not isinstance(phase.get("name"), str) or not phase["name"].strip():
                raise SequenceError(f"{location}.name must be a non-empty string")
            phase["repeat"] = _positive_int(phase.get("repeat", 1), f"{location}.repeat")
        elif kind == "chopped_ca":
            for key in ("potential_v",):
                phase[key] = _finite(phase.get(key), f"{location}.{key}")
            phase["cycle_time_s"] = _finite(phase.get("cycle_time_s", 60), f"{location}.cycle_time_s", positive=True)
            phase["loops"] = _positive_int(phase.get("loops", 3), f"{location}.loops")
            phase["post_close_hold_s"] = _finite(phase.get("post_close_hold_s", 2), f"{location}.post_close_hold_s", nonnegative=True)
            sampling = {key for key in ("sample_rate_hz", "sample_interval_s") if key in phase}
            if len(sampling) != 1:
                raise SequenceError(f"{location} needs exactly one of sample_rate_hz or sample_interval_s")
            key = sampling.pop()
            phase[key] = _finite(phase[key], f"{location}.{key}", positive=True)
            if "ca_duration_s" in phase:
                phase["ca_duration_s"] = _finite(phase["ca_duration_s"], f"{location}.ca_duration_s", positive=True)
            for key in ("expected_max_current_a", "max_absolute_current_a"):
                if key in phase:
                    phase[key] = _finite(phase[key], f"{location}.{key}", positive=True)
            phase.setdefault("potential_vs_ocp", False)
            if not isinstance(phase["potential_vs_ocp"], bool):
                raise SequenceError(f"{location}.potential_vs_ocp must be true or false")
        else:
            raise SequenceError(f"{location}.type must be sui_experiment or chopped_ca")
        normalized.append(phase)
    config["sequence"] = normalized
    config["device"]["channel"] = channel
    config["shutter"] = shutter
    config["shutter"]["speed"] = speed
    config["_source_path"] = str(path)
    return config


def chopped_settings(config: dict[str, Any], phase: dict[str, Any]) -> ChoppedSettings:
    interval = phase.get("sample_interval_s")
    if interval is None:
        interval = 1.0 / phase["sample_rate_hz"]
    minimum_duration = 2 * phase["cycle_time_s"] * phase["loops"] + phase["post_close_hold_s"]
    duration = phase.get("ca_duration_s", minimum_duration)
    if duration < minimum_duration:
        raise SequenceError(
            f"chopped CA at {phase['potential_v']:g} V needs ca_duration_s >= {minimum_duration:g}"
        )
    return ChoppedSettings(
        potentiostat_port=config["device"]["port"],
        channel=config["device"]["channel"],
        arduino_port=config["shutter"].get("port"),
        potential_v=phase["potential_v"],
        potential_vs_ocp=phase["potential_vs_ocp"],
        sample_interval_s=interval,
        ca_duration_s=duration,
        expected_max_current_a=phase.get("expected_max_current_a"),
        max_absolute_current_a=phase.get("max_absolute_current_a"),
        cycle_time_s=phase["cycle_time_s"],
        loops=phase["loops"],
        speed=config["shutter"]["speed"],
        open_angle_deg=_finite(config["shutter"].get("open_angle_deg", 315), "shutter.open_angle_deg"),
        close_angle_deg=_finite(config["shutter"].get("close_angle_deg", 45), "shutter.close_angle_deg"),
        post_close_hold_s=phase["post_close_hold_s"],
        save_path=None,
        prompt_for_save_folder=False,
        overwrite=False,
        buffered_save=False,
        print_data=False,
        show_ui=True,
        plot_refresh_ms=250,
    )


def build_phase_experiment(api: Any, library: SuiExperimentLibrary, config: dict[str, Any], phase: dict[str, Any]) -> Any:
    if phase["type"] == "chopped_ca":
        return build_chopped_experiment(api, chopped_settings(config, phase))
    experiment = library.build(api, phase["name"])
    if phase["repeat"] == 1:
        return experiment
    wrapper = api.AisExperiment()
    wrapper.setExperimentName(f"{phase['name']} x{phase['repeat']}")
    if not wrapper.appendSubExperiment(experiment, phase["repeat"]):
        raise SequenceError(f"API rejected repeat count for {phase['name']!r}")
    return wrapper


def phase_label(phase: dict[str, Any]) -> str:
    if phase["type"] == "sui_experiment":
        return str(phase["name"])
    return f"Chopped CA {phase['potential_v']:g} V"


class SequenceRecorder:
    DC_FIELDS = ("workflow_elapsed_s", "phase_number", "phase_name", "phase_type", "instrument_timestamp_s", "illumination_state", "working_electrode_v", "counter_electrode_v", "current_a", "temperature_c")
    AC_FIELDS = ("workflow_elapsed_s", "phase_number", "phase_name", "frequency_hz", "impedance_magnitude_ohm", "impedance_real_ohm", "impedance_imag_ohm", "phase_angle_deg", "working_electrode_dc_v", "dc_current_a")
    EVENT_FIELDS = ("phase_number", "phase_name", "scheduled_from_ca_start_s", "actual_from_ca_start_s", "workflow_elapsed_s", "angle_deg", "target_state", "result")

    def __init__(self, output: Path):
        output.mkdir(parents=True, exist_ok=False)
        self.output = output
        self._lock = threading.RLock()
        self._workflow_start: float | None = None
        self._phase_start: float | None = None
        self.phase_number = 0
        self.phase_name = ""
        self.phase_type = ""
        self.state = "blocked"
        self._elapsed_history: list[float] = []
        self._voltage_history: list[float] = []
        self._current_history: list[float] = []
        self._state_history: list[tuple[float, str]] = [(0.0, "blocked")]
        self._dc_file = (output / "dc.csv").open("w", newline="", encoding="utf-8")
        self._ac_file = (output / "eis.csv").open("w", newline="", encoding="utf-8")
        self._event_file = (output / "shutter_events.csv").open("w", newline="", encoding="utf-8")
        self._dc = csv.DictWriter(self._dc_file, fieldnames=self.DC_FIELDS)
        self._ac = csv.DictWriter(self._ac_file, fieldnames=self.AC_FIELDS)
        self._events = csv.DictWriter(self._event_file, fieldnames=self.EVENT_FIELDS)
        self._dc.writeheader(); self._ac.writeheader(); self._events.writeheader()

    def begin_phase(self, number: int, phase: dict[str, Any], start: float) -> None:
        with self._lock:
            if self._workflow_start is None:
                self._workflow_start = start
            self._phase_start = start
            self.phase_number = number
            self.phase_type = phase["type"]
            self.phase_name = phase_label(phase)

    def start(self, start_time: float) -> None:
        with self._lock:
            self._phase_start = start_time

    def elapsed(self) -> float:
        with self._lock:
            start = self._workflow_start
        return 0.0 if start is None else time.monotonic() - start

    def phase_elapsed(self) -> float:
        with self._lock:
            start = self._phase_start
        return 0.0 if start is None else time.monotonic() - start

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state
            self._state_history.append((self.elapsed(), state))

    def snapshot(self):
        with self._lock:
            return (list(self._elapsed_history), list(self._voltage_history), list(self._current_history), list(self._state_history), self.state)

    def record_event(self, action: Any, result: str) -> None:
        row = {"phase_number": self.phase_number, "phase_name": self.phase_name, "scheduled_from_ca_start_s": action.scheduled_s, "actual_from_ca_start_s": self.phase_elapsed(), "workflow_elapsed_s": self.elapsed(), "angle_deg": action.angle_deg, "target_state": action.target_state, "result": result}
        self._events.writerow(row); self._event_file.flush()
        print(f"Phase {self.phase_number}, t={row['actual_from_ca_start_s']:.3f} s: MOVE {action.angle_deg:g}; {result}")

    def dc_data(self, _channel: int, data: Any) -> None:
        elapsed = self.elapsed()
        with self._lock:
            state = self.state
            self._elapsed_history.append(elapsed)
            self._voltage_history.append(float(data.workingElectrodeVoltage))
            self._current_history.append(float(data.current))
        self._dc.writerow({"workflow_elapsed_s": elapsed, "phase_number": self.phase_number, "phase_name": self.phase_name, "phase_type": self.phase_type, "instrument_timestamp_s": data.timestamp, "illumination_state": state, "working_electrode_v": data.workingElectrodeVoltage, "counter_electrode_v": data.counterElectrodeVoltage, "current_a": data.current, "temperature_c": data.temperature})
        self._dc_file.flush()

    def ac_data(self, _channel: int, data: Any) -> None:
        self._ac.writerow({"workflow_elapsed_s": self.elapsed(), "phase_number": self.phase_number, "phase_name": self.phase_name, "frequency_hz": data.frequency, "impedance_magnitude_ohm": data.absoluteImpedance, "impedance_real_ohm": data.realImpedance, "impedance_imag_ohm": data.imagImpedance, "phase_angle_deg": data.phaseAngle, "working_electrode_dc_v": data.workingElectrodeDCVoltage, "dc_current_a": data.DCCurrent})
        self._ac_file.flush()

    def close(self) -> None:
        self._dc_file.close(); self._ac_file.close(); self._event_file.close()


def output_directory(config: dict[str, Any], override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    source = Path(config["_source_path"])
    parent = Path(config.get("output_directory", "sequence_runs"))
    if not parent.is_absolute():
        parent = source.parent / parent
    name = config.get("run_name", source.stem) + datetime.now().strftime("_%Y%m%d_%H%M%S")
    return (parent / name).resolve()


def run(config: dict[str, Any], library: SuiExperimentLibrary, destination: Path, *, show_ui: bool) -> int:
    try:
        qt_core = importlib.import_module("PySide6.QtCore")
        qt_gui = importlib.import_module("PySide6.QtGui")
        qt_widgets = importlib.import_module("PySide6.QtWidgets")
        qt_charts = importlib.import_module("PySide6.QtCharts") if show_ui else None
        api = importlib.import_module("SquidstatPyLibrary")
    except ImportError as error:
        print(f"Missing runtime dependency: {error}", file=sys.stderr)
        return 2
    shutter = None
    try:
        experiments = [build_phase_experiment(api, library, config, phase) for phase in config["sequence"]]
        if any(phase["type"] == "chopped_ca" for phase in config["sequence"]):
            shutter = connect_auto_shutter(config["shutter"].get("port"))
            shutter.set_speed(config["shutter"]["speed"])
        recorder = SequenceRecorder(destination)
        used = {key: value for key, value in config.items() if not key.startswith("_")}
        used["custom_experiments_directory"] = str(library.directory)
        (destination / "sequence_used.json").write_text(json.dumps(used, indent=2) + "\n", encoding="utf-8")
    except (OSError, RuntimeError, SequenceError, SuiExperimentError) as error:
        if shutter is not None:
            shutter.close()
        print(error, file=sys.stderr)
        return 2

    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    app.setQuitOnLastWindowClosed(False)
    tracker = api.AisDeviceTracker.Instance()
    phases = config["sequence"]
    channel = config["device"]["channel"]
    phase_index = -1
    handler = None
    exit_code = 0
    user_stopped = False
    phase_abort: threading.Event | None = None
    schedule_completed: threading.Event | None = None
    schedule_thread: threading.Thread | None = None
    failures: queue.Queue[str] = queue.Queue()
    plot = None

    def fail(message: str) -> None:
        nonlocal exit_code
        exit_code = 1
        print(message, file=sys.stderr)
        if phase_abort is not None:
            phase_abort.set()
        app.quit()

    def start_next_phase() -> None:
        nonlocal phase_index, phase_abort, schedule_completed, schedule_thread
        phase_index += 1
        schedule_thread = None
        phase_abort = threading.Event()
        schedule_completed = threading.Event()
        if phase_index >= len(phases):
            print("Sequence completed.")
            app.quit()
            return
        phase = phases[phase_index]
        label = phase_label(phase)
        print(f"Starting phase {phase_index + 1}/{len(phases)}: {label}")
        try:
            require_success(handler.uploadExperimentToChannel(channel, experiments[phase_index]), api, f"upload phase {phase_index + 1}")
            require_success(handler.startUploadedExperiment(channel), api, f"start phase {phase_index + 1}")
        except RuntimeError as error:
            fail(str(error))

    def on_start(active_channel: int) -> None:
        nonlocal schedule_thread
        if active_channel != channel or phase_index < 0:
            return
        phase = phases[phase_index]
        now = time.monotonic()
        recorder.begin_phase(phase_index + 1, phase, now)
        if plot is not None:
            plot.measurement_started()
        if phase["type"] == "chopped_ca":
            settings = chopped_settings(config, phase)
            schedule_thread = threading.Thread(target=run_shutter_schedule, args=(shutter, settings, recorder, now, phase_abort, failures, schedule_completed), daemon=True)
            schedule_thread.start()

    def on_stopped(active_channel: int, reason: str) -> None:
        if active_channel != channel:
            return
        print(f"Phase {phase_index + 1} stopped: {reason}")
        if user_stopped:
            app.quit()
            return
        phase = phases[phase_index]
        if phase["type"] == "chopped_ca" and not schedule_completed.is_set():
            fail("Chopped CA ended before shutter schedule completed; verify laser is blocked.")
            return
        qt_core.QTimer.singleShot(100, start_next_phase)

    def request_stop() -> None:
        nonlocal user_stopped, exit_code
        if user_stopped:
            return
        user_stopped = True
        exit_code = 130
        if phase_abort is not None:
            phase_abort.set()
        error = handler.stopExperiment(channel)
        if error.value() != api.AisErrorCode.Success:
            print(f"Could not stop experiment: {error.message()}", file=sys.stderr)
            app.quit()
        qt_core.QTimer.singleShot(3000, app.quit)

    def poll_failures() -> None:
        try:
            message = failures.get_nowait()
        except queue.Empty:
            return
        if handler is not None:
            handler.stopExperiment(channel)
        fail(f"Shutter failure: {message}")

    def connected(device_name: str) -> None:
        nonlocal handler, plot
        handler = tracker.getInstrumentHandler(device_name)
        handler.activeDCDataReady.connect(recorder.dc_data)
        handler.activeACDataReady.connect(recorder.ac_data)
        handler.experimentStarting.connect(on_start)
        handler.experimentStopped.connect(on_stopped)
        handler.deviceError.connect(lambda active_channel, error: fail(f"Squidstat error on channel {active_channel}: {error}"))
        if show_ui:
            plot = LivePlotWindow(recorder, 250, qt_core, qt_gui, qt_widgets, qt_charts)
            plot.window.setWindowTitle("SquidStat Experiment Sequence")
            plot.set_stop_callback(request_stop)
            plot.show()
        start_next_phase()

    tracker.newDeviceConnected.connect(connected)
    tracker.deviceDisconnected.connect(lambda name: fail(f"Squidstat disconnected: {name}"))
    timer = qt_core.QTimer(); timer.timeout.connect(poll_failures); timer.start(100)
    try:
        require_success(tracker.connectToDeviceOnComPort(config["device"]["port"]), api, "connect Squidstat")
        app.exec()
    except (RuntimeError, KeyboardInterrupt) as error:
        exit_code = 130 if isinstance(error, KeyboardInterrupt) else 1
        print(error, file=sys.stderr)
    finally:
        if phase_abort is not None:
            phase_abort.set()
        if schedule_thread is not None:
            schedule_thread.join(timeout=4)
        if shutter is not None:
            shutter.stop(); shutter.close()
        recorder.close()
        if recorder.state != "blocked":
            print(f"Final shutter state is {recorder.state!r}; verify laser is blocked.", file=sys.stderr)
    return exit_code


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run SUI experiments and chopped CA phases in order.")
    parser.add_argument(
        "sequence_file",
        type=Path,
        nargs="?",
        help="sequence JSON; omitted runs EXPERIMENT_NAME configured in this script",
    )
    parser.add_argument("--custom-experiments-directory", type=Path)
    parser.add_argument("--port")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--no-ui", action="store_true")
    parser.add_argument("--validate-only", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser(); args = parser.parse_args(argv)
    try:
        if args.sequence_file is None:
            config = {
                "device": {"port": POTENTIOSTAT_PORT, "channel": CHANNEL},
                "shutter": {"port": None, "speed": 2000},
                "output_directory": "sequence_runs",
                "run_name": EXPERIMENT_NAME,
                "sequence": [{"type": "sui_experiment", "name": EXPERIMENT_NAME, "repeat": 1}],
                "_source_path": str(Path(__file__).resolve()),
            }
        else:
            config = load_sequence(args.sequence_file)
        if args.port:
            config["device"]["port"] = args.port
        directory = args.custom_experiments_directory or Path(config.get("custom_experiments_directory", CUSTOM_EXPERIMENTS_DIRECTORY))
        library = SuiExperimentLibrary(directory)
        for phase in config["sequence"]:
            if phase["type"] == "sui_experiment":
                library.source_path(phase["name"])
            else:
                chopped_settings(config, phase)
        destination = output_directory(config, args.output_dir)
    except (SequenceError, SuiExperimentError) as error:
        parser.error(str(error))
    print(f"Validated {len(config['sequence'])} phases; output: {destination}")
    if args.validate_only:
        print("No hardware connection made.")
        return 0
    return run(config, library, destination, show_ui=not args.no_ui)


if __name__ == "__main__":
    sys.exit(main())
