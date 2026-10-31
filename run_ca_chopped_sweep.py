#!/usr/bin/env python3
"""Edit settings below, then run this file to create, validate, and run sweep."""

from __future__ import annotations

import json
import sys
from pathlib import Path

from make_chopped_ca_sweep import create_chopped_ca_sweep, experiment_name
from run_sui_sequence import main as run_sequence

# =============================================================================
# USER SETTINGS -- edit this section only
# =============================================================================

# SquidStat and Arduino connections
POTENTIOSTAT_PORT = "COM4"
POTENTIOSTAT_CHANNEL = 0
ARDUINO_PORT: str | None = None  # Example: "COM3"; None enables auto-detection.

# Folder containing SquidStat SUI custom-experiment JSON files
CUSTOM_EXPERIMENTS_DIRECTORY = Path(
    r"C:\Users\Wilson Lab\Documents\Admiral Instruments\Custom Experiments"
)

# Chopped CA potential sweep
POTENTIALS_V = [-0.9, -0.95, -1, -1.05, -1.1, -1.15, -1.2, -1.25, -1.3, -1.35, -1.4]
SAMPLE_RATE_HZ = 10.0
CYCLE_TIME_S = 60.0
POTENTIAL_VS_OCP = False

# Optional current-ranging hint and safety cutoff. Use None to disable.
EXPECTED_MAX_CURRENT_A: float | None = None
MAX_ABSOLUTE_CURRENT_A: float | None = None

# SUI conditioning experiment run before each CA
CONDITIONING_EXPERIMENT = "TBAPF6 anodic CV.json"
CONDITIONING_REPEATS = 1
CONDITION_BEFORE_FIRST_CA = True

# Shutter settings
SHUTTER_SPEED = 2000
SHUTTER_OPEN_ANGLE_DEG = 315.0
SHUTTER_CLOSE_ANGLE_DEG = 45.0

# Data destination and display
DATA_OUTPUT_DIRECTORY = Path(
    r"C:\Users\Wilson Lab\Desktop\Data\JVH\ORR\Potentiostat\20261031\DMF THAPF6 smooth Au\Chopped CA sweep"
)
RUN_NAME = "THAPF6_short_chopped_CA_sweep"
SHOW_LIVE_UI = True

# Generated sequence is kept here as a record and replaced on each run.
GENERATED_SEQUENCE_FILE = (
    Path(__file__).resolve().parent / "sequences" / "generated_ca_chopped_sweep.json"
)

# =============================================================================
# END USER SETTINGS
# =============================================================================


def create_sequence_document() -> dict[str, object]:
    phases = create_chopped_ca_sweep(
        potentials_v=POTENTIALS_V,
        conditioning_experiment=CONDITIONING_EXPERIMENT,
        conditioning_repeats=CONDITIONING_REPEATS,
        cycle_time_s=CYCLE_TIME_S,
        sample_rate_hz=SAMPLE_RATE_HZ,
        potential_vs_ocp=POTENTIAL_VS_OCP,
        expected_max_current_a=EXPECTED_MAX_CURRENT_A,
        max_absolute_current_a=MAX_ABSOLUTE_CURRENT_A,
        condition_before_first=CONDITION_BEFORE_FIRST_CA,
    )
    return {
        "custom_experiments_directory": str(CUSTOM_EXPERIMENTS_DIRECTORY),
        "device": {
            "port": POTENTIOSTAT_PORT,
            "channel": POTENTIOSTAT_CHANNEL,
        },
        "shutter": {
            "port": ARDUINO_PORT,
            "speed": SHUTTER_SPEED,
            "open_angle_deg": SHUTTER_OPEN_ANGLE_DEG,
            "close_angle_deg": SHUTTER_CLOSE_ANGLE_DEG,
        },
        "output_directory": str(DATA_OUTPUT_DIRECTORY),
        "run_name": RUN_NAME,
        "sequence": phases,
    }


def main() -> int:
    document = create_sequence_document()
    GENERATED_SEQUENCE_FILE.parent.mkdir(parents=True, exist_ok=True)
    GENERATED_SEQUENCE_FILE.write_text(
        json.dumps(document, indent=2) + "\n",
        encoding="utf-8",
    )

    ca_duration_s = 3.0 * CYCLE_TIME_S
    print(f"Created sequence: {GENERATED_SEQUENCE_FILE}")
    print(
        f"Plan: {len(POTENTIALS_V)} CA measurements, {ca_duration_s:g} s each; "
        f"conditioning={experiment_name(CONDITIONING_EXPERIMENT)!r}."
    )

    print("Validating generated sequence...")
    validation_result = run_sequence([str(GENERATED_SEQUENCE_FILE), "--validate-only"])
    if validation_result != 0:
        print("Validation failed. Hardware was not started.", file=sys.stderr)
        return validation_result

    print("Validation passed. Starting sequence...")
    arguments = [str(GENERATED_SEQUENCE_FILE)]
    if not SHOW_LIVE_UI:
        arguments.append("--no-ui")
    return run_sequence(arguments)


if __name__ == "__main__":
    sys.exit(main())
