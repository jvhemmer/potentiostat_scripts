#!/usr/bin/env python3
"""Run repeated chronoamperometry followed by potentiostatic EIS.

One CA/EIS pair is created for every value passed to
``--eis-dc-potentials-v``.  CA settings stay constant; EIS DC bias changes.
DC and AC data are streamed to separate CSV files while the experiment runs.
"""

from __future__ import annotations

import argparse
import csv
import importlib
import math
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence


@dataclass(frozen=True)
class Settings:
    com_port: str
    channel: int
    ca_potential_v: float
    ca_sample_interval_s: float
    ca_duration_s: float
    eis_dc_potentials_v: tuple[float, ...]
    eis_start_hz: float
    eis_end_hz: float
    eis_steps_per_decade: float
    eis_amplitude_v: float
    eis_minimum_cycles: int
    eis_bias_vs_ocp: bool
    eis_quiet_time_s: float
    eis_quiet_sample_interval_s: float
    output_dir: Path


def positive_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return number


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be a finite number")
    return number


def channel_number(value: str) -> int:
    number = int(value)
    if not 0 <= number <= 255:
        raise argparse.ArgumentTypeError("must be between 0 and 255")
    return number


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run one fixed CA step followed by potentiostatic EIS at each "
            "listed DC potential."
        )
    )
    parser.add_argument("--com-port", required=True, help="Squidstat port, e.g. COM16")
    parser.add_argument("--channel", type=channel_number, default=0)
    parser.add_argument("--ca-potential-v", type=finite_float, required=True)

    sampling = parser.add_mutually_exclusive_group(required=True)
    sampling.add_argument(
        "--ca-sample-rate-hz",
        type=positive_float,
        help="CA samples per second (converted to API sampling interval)",
    )
    sampling.add_argument(
        "--ca-sample-interval-s",
        type=positive_float,
        help="CA time between samples in seconds",
    )
    parser.add_argument("--ca-duration-s", type=positive_float, required=True)

    parser.add_argument(
        "--eis-dc-potentials-v",
        type=finite_float,
        nargs="+",
        required=True,
        metavar="V",
        help="ordered EIS DC biases; one CA/EIS pair runs per value",
    )
    parser.add_argument("--eis-start-hz", type=positive_float, required=True)
    parser.add_argument("--eis-end-hz", type=positive_float, required=True)
    parser.add_argument("--eis-steps-per-decade", type=positive_float, required=True)
    parser.add_argument("--eis-amplitude-v", type=positive_float, required=True)
    parser.add_argument("--eis-minimum-cycles", type=int, default=1)
    parser.add_argument(
        "--eis-bias-vs-ocp",
        action="store_true",
        help="reference every EIS DC bias to OCP instead of reference terminal",
    )
    parser.add_argument("--eis-quiet-time-s", type=float, default=0.0)
    parser.add_argument("--eis-quiet-sample-interval-s", type=positive_float, default=1.0)
    parser.add_argument(
        "--output-dir",
        type=Path,
        help="CSV destination (default: ca_eis_YYYYmmdd_HHMMSS)",
    )
    return parser


def settings_from_args(args: argparse.Namespace, parser: argparse.ArgumentParser) -> Settings:
    if args.eis_minimum_cycles < 1:
        parser.error("--eis-minimum-cycles must be at least 1")
    if not math.isfinite(args.eis_quiet_time_s) or args.eis_quiet_time_s < 0:
        parser.error("--eis-quiet-time-s must be finite and at least 0")

    interval = (
        args.ca_sample_interval_s
        if args.ca_sample_interval_s is not None
        else 1.0 / args.ca_sample_rate_hz
    )
    output_dir = args.output_dir or Path(datetime.now().strftime("ca_eis_%Y%m%d_%H%M%S"))
    return Settings(
        com_port=args.com_port,
        channel=args.channel,
        ca_potential_v=args.ca_potential_v,
        ca_sample_interval_s=interval,
        ca_duration_s=args.ca_duration_s,
        eis_dc_potentials_v=tuple(args.eis_dc_potentials_v),
        eis_start_hz=args.eis_start_hz,
        eis_end_hz=args.eis_end_hz,
        eis_steps_per_decade=args.eis_steps_per_decade,
        eis_amplitude_v=args.eis_amplitude_v,
        eis_minimum_cycles=args.eis_minimum_cycles,
        eis_bias_vs_ocp=args.eis_bias_vs_ocp,
        eis_quiet_time_s=args.eis_quiet_time_s,
        eis_quiet_sample_interval_s=args.eis_quiet_sample_interval_s,
        output_dir=output_dir,
    )


def build_experiment(api: Any, settings: Settings) -> Any:
    experiment = api.AisExperiment()
    experiment.setExperimentName("Repeated CA then potentiostatic EIS")

    for bias_v in settings.eis_dc_potentials_v:
        ca = api.AisConstantPotElement(
            settings.ca_potential_v,
            settings.ca_sample_interval_s,
            settings.ca_duration_s,
        )
        eis = api.AisEISPotentiostaticElement(
            settings.eis_start_hz,
            settings.eis_end_hz,
            settings.eis_steps_per_decade,
            bias_v,
            settings.eis_amplitude_v,
        )
        eis.setBiasVoltageVsOCP(settings.eis_bias_vs_ocp)
        eis.setMinimumCycles(settings.eis_minimum_cycles)
        eis.setQuietTime(settings.eis_quiet_time_s)
        eis.setQuietTimeSamplingInterval(settings.eis_quiet_sample_interval_s)

        if not experiment.appendElement(ca, 1):
            raise RuntimeError("Squidstat API rejected a CA element")
        if not experiment.appendElement(eis, 1):
            raise RuntimeError(f"Squidstat API rejected EIS element at {bias_v:g} V")

    return experiment


class CsvRecorder:
    DC_FIELDS = (
        "timestamp_s",
        "step_number",
        "step_name",
        "working_electrode_v",
        "counter_electrode_v",
        "current_a",
        "temperature_c",
    )
    AC_FIELDS = (
        "timestamp_s",
        "step_number",
        "step_name",
        "frequency_hz",
        "impedance_magnitude_ohm",
        "impedance_real_ohm",
        "impedance_imag_ohm",
        "phase_angle_deg",
        "total_harmonic_distortion_percent",
        "number_of_cycles",
        "working_electrode_dc_v",
        "dc_current_a",
        "current_amplitude_a",
        "voltage_amplitude_v",
    )

    def __init__(self, output_dir: Path) -> None:
        output_dir.mkdir(parents=True, exist_ok=False)
        self._dc_file = (output_dir / "dc.csv").open("w", newline="", encoding="utf-8")
        self._ac_file = (output_dir / "eis.csv").open("w", newline="", encoding="utf-8")
        self._dc = csv.DictWriter(self._dc_file, fieldnames=self.DC_FIELDS)
        self._ac = csv.DictWriter(self._ac_file, fieldnames=self.AC_FIELDS)
        self._dc.writeheader()
        self._ac.writeheader()
        self.step_number: Any = ""
        self.step_name = ""

    def new_element(self, _channel: int, info: Any) -> None:
        self.step_number = info.stepNumber
        self.step_name = str(info.stepName)
        print(f"Starting step {self.step_number}: {self.step_name}")

    def dc_data(self, _channel: int, data: Any) -> None:
        self._dc.writerow(
            {
                "timestamp_s": data.timestamp,
                "step_number": self.step_number,
                "step_name": self.step_name,
                "working_electrode_v": data.workingElectrodeVoltage,
                "counter_electrode_v": data.counterElectrodeVoltage,
                "current_a": data.current,
                "temperature_c": data.temperature,
            }
        )
        self._dc_file.flush()

    def ac_data(self, _channel: int, data: Any) -> None:
        self._ac.writerow(
            {
                "timestamp_s": data.timestamp,
                "step_number": self.step_number,
                "step_name": self.step_name,
                "frequency_hz": data.frequency,
                "impedance_magnitude_ohm": data.absoluteImpedance,
                "impedance_real_ohm": data.realImpedance,
                "impedance_imag_ohm": data.imagImpedance,
                "phase_angle_deg": data.phaseAngle,
                "total_harmonic_distortion_percent": data.totalHarmonicDistortion,
                "number_of_cycles": data.numberOfCycles,
                "working_electrode_dc_v": data.workingElectrodeDCVoltage,
                "dc_current_a": data.DCCurrent,
                "current_amplitude_a": data.currentAmplitude,
                "voltage_amplitude_v": data.voltageAmplitude,
            }
        )
        self._ac_file.flush()

    def close(self) -> None:
        self._dc_file.close()
        self._ac_file.close()


def require_success(error: Any, api: Any, action: str) -> None:
    if error.value() != api.AisErrorCode.Success:
        raise RuntimeError(f"{action}: {error.message()}")


def run(settings: Settings) -> int:
    try:
        qt_core = importlib.import_module("PySide6.QtCore")
        api = importlib.import_module("SquidstatPyLibrary")
    except ImportError as error:
        print(f"Missing Squidstat runtime dependency: {error}", file=sys.stderr)
        return 2

    app = qt_core.QCoreApplication.instance() or qt_core.QCoreApplication([])
    tracker = api.AisDeviceTracker.Instance()

    try:
        experiment = build_experiment(api, settings)
        recorder = CsvRecorder(settings.output_dir)
    except (OSError, RuntimeError) as error:
        print(error, file=sys.stderr)
        return 2

    exit_code = 0
    started = False

    def fail(message: str) -> None:
        nonlocal exit_code
        exit_code = 1
        print(message, file=sys.stderr)
        app.quit()

    def on_connected(device_name: str) -> None:
        nonlocal started
        if started:
            return
        started = True
        print(f"Connected to {device_name} on {settings.com_port}")
        handler = tracker.getInstrumentHandler(device_name)
        handler.activeDCDataReady.connect(recorder.dc_data)
        handler.activeACDataReady.connect(recorder.ac_data)
        handler.experimentNewElementStarting.connect(recorder.new_element)
        handler.deviceError.connect(lambda channel, error: fail(f"Device error on channel {channel}: {error}"))
        handler.experimentStopped.connect(
            lambda channel, reason: (print(f"Experiment stopped on channel {channel}: {reason}"), app.quit())
        )
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
    tracker.deviceDisconnected.connect(lambda name: fail(f"Device disconnected: {name}"))

    try:
        require_success(
            tracker.connectToDeviceOnComPort(settings.com_port),
            api,
            f"connect to {settings.com_port}",
        )
        if exit_code == 0:
            app.exec()
    except RuntimeError as error:
        exit_code = 1
        print(error, file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 130
        print("Interrupted; stop experiment from Squidstat software if still active.", file=sys.stderr)
    finally:
        recorder.close()

    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    settings = settings_from_args(parser.parse_args(argv), parser)
    print(
        f"Prepared {len(settings.eis_dc_potentials_v)} CA/EIS pairs; "
        f"CA interval {settings.ca_sample_interval_s:g} s."
    )
    return run(settings)


if __name__ == "__main__":
    sys.exit(main())
