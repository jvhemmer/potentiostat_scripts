#!/usr/bin/env python3
"""Run a validated JSON Squidstat experiment recipe."""

from __future__ import annotations

import argparse
import importlib
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from ca_eis_sequence import CsvRecorder, require_success
from squidstat_recipe import Recipe, RecipeError, build_experiment, load_recipe, write_resolved_recipe


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Validate and run a reusable Squidstat JSON recipe.")
    parser.add_argument("recipe", type=Path, help="path to the JSON recipe")
    parser.add_argument("--port", help="override device.port, e.g. COM4")
    parser.add_argument("--channel", type=int, help="override device.channel")
    parser.add_argument("--output-dir", type=Path, help="use this exact run output folder")
    parser.add_argument("--validate-only", action="store_true", help="validate and show the experiment without connecting")
    return parser


def output_directory(recipe: Recipe, override: Path | None) -> Path:
    if override is not None:
        return override.expanduser().resolve()
    config = recipe.data["output"]
    parent = Path(config.get("directory", "runs"))
    if not parent.is_absolute():
        parent = recipe.source_path.parent / parent
    name = config.get("run_name", recipe.source_path.stem)
    if config.get("timestamp", True):
        name += datetime.now().strftime("_%Y%m%d_%H%M%S")
    return (parent / name).resolve()


def describe(recipe: Recipe, port: str, channel: int, output_dir: Path) -> None:
    print(f"Recipe: {recipe.name}")
    print(f"Device: {port}, channel {channel}")
    for index, step in enumerate(recipe.steps, 1):
        label = f" ({step['label']})" if "label" in step else ""
        print(f"  {index}. {step['type']}{label}, repeat {step['repeat']}")
    print(f"Output: {output_dir}")


def run(recipe: Recipe, port: str, channel: int, output_dir: Path) -> int:
    try:
        qt_core = importlib.import_module("PySide6.QtCore")
        api = importlib.import_module("SquidstatPyLibrary")
    except ImportError as error:
        print(f"Missing Squidstat runtime dependency: {error}", file=sys.stderr)
        return 2
    app = qt_core.QCoreApplication.instance() or qt_core.QCoreApplication([])
    tracker = api.AisDeviceTracker.Instance()
    try:
        experiment = build_experiment(api, recipe)
        recorder = CsvRecorder(output_dir)
        write_resolved_recipe(recipe, output_dir / "recipe_used.json", port, channel)
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

    def connected(device_name: str) -> None:
        nonlocal started
        if started:
            return
        started = True
        print(f"Connected to {device_name} on {port}")
        handler = tracker.getInstrumentHandler(device_name)
        handler.activeDCDataReady.connect(recorder.dc_data)
        handler.activeACDataReady.connect(recorder.ac_data)
        handler.experimentNewElementStarting.connect(recorder.new_element)
        handler.deviceError.connect(lambda active_channel, error: fail(f"Device error on channel {active_channel}: {error}"))
        handler.experimentStopped.connect(lambda active_channel, reason: (print(f"Experiment stopped on channel {active_channel}: {reason}"), app.quit()))
        try:
            require_success(handler.uploadExperimentToChannel(channel, experiment), api, "upload experiment")
            require_success(handler.startUploadedExperiment(channel), api, "start experiment")
        except RuntimeError as error:
            fail(str(error))

    tracker.newDeviceConnected.connect(connected)
    tracker.deviceDisconnected.connect(lambda name: fail(f"Device disconnected: {name}"))
    try:
        require_success(tracker.connectToDeviceOnComPort(port), api, f"connect to {port}")
        if exit_code == 0:
            app.exec()
    except RuntimeError as error:
        exit_code = 1
        print(error, file=sys.stderr)
    except KeyboardInterrupt:
        exit_code = 130
        print("Interrupted; verify the instrument is stopped.", file=sys.stderr)
    finally:
        recorder.close()
    return exit_code


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        recipe = load_recipe(args.recipe)
        port = args.port or recipe.port
        channel = recipe.channel if args.channel is None else args.channel
        if not 0 <= channel <= 255:
            parser.error("--channel must be between 0 and 255")
        destination = output_directory(recipe, args.output_dir)
    except RecipeError as error:
        parser.error(str(error))
    describe(recipe, port, channel, destination)
    if args.validate_only:
        print("Recipe is valid; no hardware connection was made.")
        return 0
    return run(recipe, port, channel, destination)


if __name__ == "__main__":
    sys.exit(main())
