#!/usr/bin/env python3
"""Run chopped illumination during a Squidstat chronoamperogram.

The Arduino is connected and placed on standby before the potentiostat starts.
The shutter schedule is released by Squidstat's ``experimentStarting`` signal,
so its time origin follows the actual CA start rather than the upload command.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import os
import queue
import sys
import threading
import time
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


ARDUINO_BAUD_RATE = 115200
ARDUINO_DEVICE_ID = "AUTO_SHUTTER_V1"
STEPS_PER_REVOLUTION = 1600.0


@dataclass(frozen=True)
class Settings:
    potentiostat_port: str
    channel: int
    arduino_port: str | None
    potential_v: float
    potential_vs_ocp: bool
    sample_interval_s: float
    ca_duration_s: float
    expected_max_current_a: float | None
    max_absolute_current_a: float | None
    cycle_time_s: float
    loops: int
    speed: int
    open_angle_deg: float
    close_angle_deg: float
    post_close_hold_s: float
    save_path: Path | None
    prompt_for_save_folder: bool
    overwrite: bool
    buffered_save: bool
    print_data: bool


@dataclass(frozen=True)
class ShutterAction:
    scheduled_s: float
    angle_deg: float
    target_state: str


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be a finite number")
    return number


def positive_float(value: str) -> float:
    number = finite_float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def nonnegative_float(value: str) -> float:
    number = finite_float(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run synchronized chopped illumination and constant-potential CA."
    )
    parser.add_argument("--potentiostat-port", required=True, help="e.g. COM16")
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument(
        "--arduino-port",
        help="e.g. COM3; omitted means identify AUTO_SHUTTER_V1 automatically",
    )
    parser.add_argument("--potential-v", type=finite_float, required=True)
    parser.add_argument(
        "--potential-vs-ocp",
        action="store_true",
        help="reference CA potential to OCP instead of reference terminal",
    )

    sampling = parser.add_mutually_exclusive_group(required=True)
    sampling.add_argument("--sample-rate-hz", type=positive_float)
    sampling.add_argument("--sample-interval-s", type=positive_float)

    parser.add_argument(
        "--ca-duration-s",
        type=positive_float,
        help="default: full shutter sequence plus post-close hold",
    )
    parser.add_argument("--expected-max-current-a", type=positive_float)
    parser.add_argument(
        "--max-absolute-current-a",
        type=positive_float,
        help="optional safety cutoff that stops CA",
    )

    parser.add_argument("--cycle-time-s", type=positive_float, default=60.0)
    parser.add_argument("--loops", type=positive_int, default=3)
    parser.add_argument("--speed", type=int, default=2000)
    parser.add_argument("--open-angle-deg", type=finite_float, default=315.0)
    parser.add_argument("--close-angle-deg", type=finite_float, default=45.0)
    parser.add_argument("--post-close-hold-s", type=nonnegative_float, default=2.0)

    parser.add_argument(
        "--save-path",
        type=Path,
        help="CA CSV path (omitted: show a folder-selection dialog)",
    )
    parser.add_argument("--no-save", action="store_true")
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument(
        "--buffered-save",
        action="store_true",
        help="do not flush CSV files after every row",
    )
    parser.add_argument("--print-data", action="store_true")
    return parser


def settings_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Settings:
    if not 0 <= args.channel <= 255:
        parser.error("--channel must be between 0 and 255")
    if not 50 <= args.speed <= 2000:
        parser.error("--speed must be between 50 and 2000")
    if args.no_save and args.save_path is not None:
        parser.error("--no-save and --save-path cannot be used together")

    sample_interval_s = (
        args.sample_interval_s
        if args.sample_interval_s is not None
        else 1.0 / args.sample_rate_hz
    )
    sequence_duration_s = 2.0 * args.cycle_time_s * args.loops
    minimum_duration_s = sequence_duration_s + args.post_close_hold_s
    ca_duration_s = args.ca_duration_s or minimum_duration_s
    if ca_duration_s < minimum_duration_s:
        parser.error(
            "--ca-duration-s must be at least "
            f"{minimum_duration_s:g} s so final closing move can finish"
        )

    if args.no_save:
        save_path = None
        prompt_for_save_folder = False
    elif args.save_path is not None:
        save_path = args.save_path
        prompt_for_save_folder = False
    else:
        save_path = None
        prompt_for_save_folder = True

    return Settings(
        potentiostat_port=args.potentiostat_port,
        channel=args.channel,
        arduino_port=args.arduino_port,
        potential_v=args.potential_v,
        potential_vs_ocp=args.potential_vs_ocp,
        sample_interval_s=sample_interval_s,
        ca_duration_s=ca_duration_s,
        expected_max_current_a=args.expected_max_current_a,
        max_absolute_current_a=args.max_absolute_current_a,
        cycle_time_s=args.cycle_time_s,
        loops=args.loops,
        speed=args.speed,
        open_angle_deg=args.open_angle_deg,
        close_angle_deg=args.close_angle_deg,
        post_close_hold_s=args.post_close_hold_s,
        save_path=save_path,
        prompt_for_save_folder=prompt_for_save_folder,
        overwrite=args.overwrite,
        buffered_save=args.buffered_save,
        print_data=args.print_data,
    )


def build_shutter_plan(settings: Settings) -> list[ShutterAction]:
    actions: list[ShutterAction] = []
    for loop_index in range(settings.loops):
        actions.append(
            ShutterAction(
                scheduled_s=(2 * loop_index + 1) * settings.cycle_time_s,
                angle_deg=settings.open_angle_deg,
                target_state="open",
            )
        )
        actions.append(
            ShutterAction(
                scheduled_s=(2 * loop_index + 2) * settings.cycle_time_s,
                angle_deg=settings.close_angle_deg,
                target_state="blocked",
            )
        )
    return actions


def build_experiment(api: Any, settings: Settings) -> Any:
    ca = api.AisConstantPotElement(
        settings.potential_v,
        settings.sample_interval_s,
        settings.ca_duration_s,
    )
    ca.setVoltageVsOCP(settings.potential_vs_ocp)
    if settings.expected_max_current_a is not None:
        ca.setApproxMaxCurrent(settings.expected_max_current_a)
    if settings.max_absolute_current_a is not None:
        ca.setMaxAbsoluteCurrent(settings.max_absolute_current_a)

    experiment = api.AisExperiment()
    experiment.setExperimentName("Chopped illumination chronoamperometry")
    if not experiment.appendElement(ca, 1):
        raise RuntimeError("Squidstat API rejected the CA element")
    return experiment


class AutoShutterClient:
    """Serial client compatible with jvhemmer/auto-shutter firmware V1."""

    def __init__(self, connection: Any):
        self.serial = connection
        self.speed = 500

    @property
    def port(self) -> str:
        return str(self.serial.port)

    def _send_and_expect(self, command: str, expected: str, timeout_s: float) -> str:
        self.serial.write((command + "\n").encode("ascii"))
        self.serial.flush()
        deadline = time.monotonic() + timeout_s
        seen: list[str] = []
        while time.monotonic() < deadline:
            response = self.serial.readline().decode("ascii", errors="replace").strip()
            if not response:
                continue
            seen.append(response)
            if response == expected:
                return response
            if response.startswith("INVALID"):
                raise RuntimeError(f"Arduino rejected {command!r}: {response}")
        detail = f"; received {seen!r}" if seen else ""
        raise TimeoutError(f"Arduino did not acknowledge {command!r}{detail}")

    def set_speed(self, speed: int) -> None:
        last_error: Exception | None = None
        for attempt in range(3):
            try:
                self._send_and_expect(f"SPEED {speed}", "SPEED SET", 1.2)
                self.speed = speed
                return
            except TimeoutError as error:
                last_error = error
                if attempt < 2:
                    time.sleep(0.1)
        raise last_error or TimeoutError("Arduino did not acknowledge SPEED")

    def move_and_wait(self, angle_deg: float, abort: threading.Event) -> None:
        self._send_and_expect(f"MOVE {angle_deg:g}", "MOVING", 1.2)
        expected_motion_s = (
            abs(angle_deg) * STEPS_PER_REVOLUTION / 360.0 / self.speed
        )
        deadline = time.monotonic() + expected_motion_s + 3.0
        while time.monotonic() < deadline:
            if abort.is_set():
                self.stop()
                raise InterruptedError("shutter schedule aborted during movement")
            response = self.serial.readline().decode("ascii", errors="replace").strip()
            if response == "DONE":
                return
            if response:
                raise RuntimeError(f"unexpected Arduino response during movement: {response}")
        self.stop()
        raise TimeoutError(f"Arduino did not finish MOVE {angle_deg:g}")

    def stop(self) -> None:
        try:
            self._send_and_expect("STOP", "STOPPED", 1.2)
        except (OSError, RuntimeError, TimeoutError):
            pass

    def close(self) -> None:
        self.serial.close()


def connect_auto_shutter(port: str | None) -> AutoShutterClient:
    try:
        serial = importlib.import_module("serial")
        list_ports = importlib.import_module("serial.tools.list_ports")
    except ImportError as error:
        raise RuntimeError("pyserial is required: pip install pyserial") from error

    selected = port or os.environ.get("AUTO_SHUTTER_PORT")
    candidates = [selected] if selected else [item.device for item in list_ports.comports()]
    errors: list[str] = []
    for candidate in candidates:
        connection = None
        try:
            connection = serial.Serial(
                candidate,
                ARDUINO_BAUD_RATE,
                timeout=0.2,
                write_timeout=1,
            )
            time.sleep(2.0)  # Uno resets when its serial port opens.
            connection.reset_input_buffer()
            client = AutoShutterClient(connection)
            client._send_and_expect("ID", ARDUINO_DEVICE_ID, 1.2)
            return client
        except (OSError, RuntimeError, TimeoutError, serial.SerialException) as error:
            errors.append(f"{candidate}: {error}")
            if connection is not None:
                connection.close()

    checked = ", ".join(str(item) for item in candidates) or "no serial ports"
    detail = "; ".join(errors)
    raise RuntimeError(
        f"No {ARDUINO_DEVICE_ID} found. Checked {checked}. {detail}".strip()
    )


class DataRecorder:
    DATA_FIELDS = (
        "elapsed_s",
        "instrument_timestamp_s",
        "illumination_state",
        "working_electrode_v",
        "counter_electrode_v",
        "current_a",
        "temperature_c",
    )
    EVENT_FIELDS = (
        "scheduled_s",
        "actual_s",
        "angle_deg",
        "target_state",
        "result",
    )

    def __init__(self, settings: Settings):
        self._lock = threading.Lock()
        self._start_time: float | None = None
        self.state = "blocked"
        self.print_data = settings.print_data
        self.flush_rows = not settings.buffered_save
        self._data_file = None
        self._event_file = None
        self._data_writer = None
        self._event_writer = None

        if settings.save_path is not None:
            path = settings.save_path.expanduser().resolve()
            path.parent.mkdir(parents=True, exist_ok=True)
            event_path = path.with_name(f"{path.stem}_shutter_events{path.suffix}")
            if not settings.overwrite:
                existing = [item for item in (path, event_path) if item.exists()]
                if existing:
                    raise FileExistsError(
                        "output exists; choose another --save-path or add --overwrite: "
                        + ", ".join(str(item) for item in existing)
                    )
            self._data_file = path.open("w", newline="", encoding="utf-8")
            self._event_file = event_path.open("w", newline="", encoding="utf-8")
            self._data_writer = csv.DictWriter(self._data_file, fieldnames=self.DATA_FIELDS)
            self._event_writer = csv.DictWriter(self._event_file, fieldnames=self.EVENT_FIELDS)
            self._data_writer.writeheader()
            self._event_writer.writeheader()
            print(f"Saving CA data to {path}")
            print(f"Saving shutter events to {event_path}")

    def start(self, start_time: float) -> None:
        with self._lock:
            self._start_time = start_time

    def elapsed(self) -> float:
        with self._lock:
            start_time = self._start_time
        return 0.0 if start_time is None else time.monotonic() - start_time

    def set_state(self, state: str) -> None:
        with self._lock:
            self.state = state

    def record_event(self, action: ShutterAction, result: str) -> None:
        row = {
            "scheduled_s": action.scheduled_s,
            "actual_s": self.elapsed(),
            "angle_deg": action.angle_deg,
            "target_state": action.target_state,
            "result": result,
        }
        print(
            f"t={row['actual_s']:.3f} s: MOVE {action.angle_deg:g}; "
            f"state={action.target_state}; {result}"
        )
        if self._event_writer is not None:
            self._event_writer.writerow(row)
            if self.flush_rows:
                self._event_file.flush()

    def dc_data(self, _channel: int, data: Any) -> None:
        with self._lock:
            state = self.state
            start_time = self._start_time
        elapsed = 0.0 if start_time is None else time.monotonic() - start_time
        row = {
            "elapsed_s": elapsed,
            "instrument_timestamp_s": data.timestamp,
            "illumination_state": state,
            "working_electrode_v": data.workingElectrodeVoltage,
            "counter_electrode_v": data.counterElectrodeVoltage,
            "current_a": data.current,
            "temperature_c": data.temperature,
        }
        if self._data_writer is not None:
            self._data_writer.writerow(row)
            if self.flush_rows:
                self._data_file.flush()
        if self.print_data:
            print(
                f"t={elapsed:.3f} s, {state}, E={data.workingElectrodeVoltage:g} V, "
                f"I={data.current:g} A"
            )

    def close(self) -> None:
        if self._data_file is not None:
            self._data_file.close()
        if self._event_file is not None:
            self._event_file.close()


def wait_until(deadline: float, abort: threading.Event) -> None:
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        if abort.wait(remaining):
            raise InterruptedError("shutter schedule aborted")


def run_shutter_schedule(
    shutter: AutoShutterClient,
    settings: Settings,
    recorder: DataRecorder,
    start_time: float,
    abort: threading.Event,
    failures: queue.Queue[str],
    completed: threading.Event,
) -> None:
    try:
        for action in build_shutter_plan(settings):
            wait_until(start_time + action.scheduled_s, abort)
            recorder.set_state(f"moving_to_{action.target_state}")
            recorder.record_event(action, "started")
            shutter.move_and_wait(action.angle_deg, abort)
            recorder.set_state(action.target_state)
            recorder.record_event(action, "done")
        completed.set()
    except InterruptedError:
        return
    except Exception as error:  # Hardware faults must reach Qt's main thread.
        failures.put(str(error))
        abort.set()


def require_success(error: Any, api: Any, action: str) -> None:
    if error.value() != api.AisErrorCode.Success:
        raise RuntimeError(f"{action}: {error.message()}")


def timestamped_save_path(folder: Path) -> Path:
    return folder / datetime.now().strftime("chopped_illumination_%Y%m%d_%H%M%S.csv")


def run(settings: Settings) -> int:
    try:
        qt_core = importlib.import_module("PySide6.QtCore")
        qt_widgets = importlib.import_module("PySide6.QtWidgets")
        api = importlib.import_module("SquidstatPyLibrary")
    except ImportError as error:
        print(f"Missing Squidstat runtime dependency: {error}", file=sys.stderr)
        return 2

    app = qt_widgets.QApplication.instance() or qt_widgets.QApplication([])
    recorder: DataRecorder | None = None
    shutter: AutoShutterClient | None = None
    try:
        shutter = connect_auto_shutter(settings.arduino_port)
        shutter.set_speed(settings.speed)
        experiment = build_experiment(api, settings)
    except (OSError, RuntimeError, ValueError) as error:
        if shutter is not None:
            shutter.close()
        print(error, file=sys.stderr)
        return 2

    print(f"Auto Shutter connected on {shutter.port}; SPEED {settings.speed}; standby")
    tracker = api.AisDeviceTracker.Instance()
    abort = threading.Event()
    schedule_completed = threading.Event()
    schedule_failures: queue.Queue[str] = queue.Queue()
    schedule_thread: threading.Thread | None = None
    poll_timer = None
    exit_code = 0
    started = False
    cancelled = False

    def fail(message: str) -> None:
        nonlocal exit_code
        exit_code = 1
        print(message, file=sys.stderr)
        abort.set()
        app.quit()

    def on_connected(device_name: str) -> None:
        nonlocal started, schedule_thread, poll_timer, exit_code, recorder, cancelled
        if started:
            return
        started = True
        handler = tracker.getInstrumentHandler(device_name)
        print(f"Squidstat connected to {device_name} on {settings.potentiostat_port}")

        active_settings = settings
        if settings.prompt_for_save_folder:
            folder_name = qt_widgets.QFileDialog.getExistingDirectory(
                None,
                "Choose folder for chopped illumination data",
                str(Path.home()),
            )
            if not folder_name:
                cancelled = True
                abort.set()
                print("Save cancelled; experiment was not started.")
                app.quit()
                return
            active_settings = replace(
                settings,
                save_path=timestamped_save_path(Path(folder_name)),
                prompt_for_save_folder=False,
            )

        try:
            recorder = DataRecorder(active_settings)
        except (OSError, RuntimeError, ValueError) as error:
            fail(str(error))
            return
        active_recorder = recorder

        def on_experiment_starting(channel: int) -> None:
            nonlocal schedule_thread
            if channel != settings.channel or schedule_thread is not None:
                return
            start_time = time.monotonic()
            active_recorder.start(start_time)
            print("t=0: CA started; shutter blocked; illumination schedule released")
            schedule_thread = threading.Thread(
                target=run_shutter_schedule,
                args=(
                    shutter,
                    settings,
                    active_recorder,
                    start_time,
                    abort,
                    schedule_failures,
                    schedule_completed,
                ),
                name="auto-shutter-schedule",
                daemon=True,
            )
            schedule_thread.start()

        def on_experiment_stopped(channel: int, reason: str) -> None:
            nonlocal exit_code
            if channel != settings.channel:
                return
            if schedule_thread is not None and not schedule_completed.is_set():
                exit_code = 1
                print(
                    "CA stopped before shutter schedule completed; verify laser is blocked.",
                    file=sys.stderr,
                )
            print(f"Experiment stopped on channel {channel}: {reason}")
            abort.set()
            app.quit()

        def poll_schedule() -> None:
            nonlocal exit_code
            try:
                message = schedule_failures.get_nowait()
            except queue.Empty:
                return
            exit_code = 1
            print(f"Shutter failure: {message}", file=sys.stderr)
            stop_error = handler.stopExperiment(settings.channel)
            if stop_error.value() != api.AisErrorCode.Success:
                print(f"Could not stop CA: {stop_error.message()}", file=sys.stderr)
            app.quit()

        handler.activeDCDataReady.connect(active_recorder.dc_data)
        handler.experimentStarting.connect(on_experiment_starting)
        handler.experimentStopped.connect(on_experiment_stopped)
        handler.deviceError.connect(
            lambda channel, error: fail(f"Squidstat error on channel {channel}: {error}")
        )
        poll_timer = qt_core.QTimer()
        poll_timer.timeout.connect(poll_schedule)
        poll_timer.start(100)

        try:
            require_success(
                handler.uploadExperimentToChannel(settings.channel, experiment),
                api,
                "upload experiment",
            )
            require_success(
                handler.startUploadedExperiment(settings.channel),
                api,
                "start experiment",
            )
        except RuntimeError as error:
            fail(str(error))

    tracker.newDeviceConnected.connect(on_connected)
    tracker.deviceDisconnected.connect(lambda name: fail(f"Squidstat disconnected: {name}"))

    try:
        require_success(
            tracker.connectToDeviceOnComPort(settings.potentiostat_port),
            api,
            f"connect to {settings.potentiostat_port}",
        )
        if exit_code == 0 and not cancelled:
            app.exec()
    except RuntimeError as error:
        exit_code = 1
        print(error, file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 130
        print("Interrupted; verify laser is blocked and stop CA if needed.", file=sys.stderr)
    finally:
        abort.set()
        if schedule_thread is not None:
            schedule_thread.join(timeout=4.0)
        if recorder is not None and recorder.state != "blocked":
            print(
                f"Final shutter state is {recorder.state!r}; verify laser is blocked.",
                file=sys.stderr,
            )
        shutter.stop()
        shutter.close()
        if recorder is not None:
            recorder.close()

    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    settings = settings_from_args(parser.parse_args(argv), parser)
    print(
        f"Plan: {settings.loops} loops, {settings.cycle_time_s:g} s phases, "
        f"CA duration {settings.ca_duration_s:g} s, "
        f"sample interval {settings.sample_interval_s:g} s."
    )
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
