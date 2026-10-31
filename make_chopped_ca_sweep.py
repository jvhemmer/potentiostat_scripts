#!/usr/bin/env python3
"""Create a CA-potential sweep alternating short chopped CA and SUI conditioning."""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from typing import Sequence


DEFAULT_CONDITIONING_EXPERIMENT = "TBAPF6 anodic CV"


def finite_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise argparse.ArgumentTypeError("must be finite")
    return number


def positive_float(value: str) -> float:
    number = finite_float(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be greater than 0")
    return number


def positive_int(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def experiment_name(value: str) -> str:
    """Accept either an SUI experiment name or its .json filename."""
    name = Path(value.strip()).name
    if name.lower().endswith(".json"):
        name = name[:-5]
    if not name:
        raise ValueError("conditioning experiment name cannot be empty")
    return name


def create_chopped_ca_sweep(
    *,
    potentials_v: Sequence[float],
    conditioning_experiment: str = DEFAULT_CONDITIONING_EXPERIMENT,
    conditioning_repeats: int = 1,
    cycle_time_s: float = 60.0,
    sample_rate_hz: float = 10.0,
    potential_vs_ocp: bool = False,
    expected_max_current_a: float | None = None,
    max_absolute_current_a: float | None = None,
    condition_before_first: bool = True,
) -> list[dict[str, object]]:
    """Build CA/conditioning phases without writing or running anything.

    Each CA lasts three cycle periods: blocked, open, blocked. Shutter moves
    open after one period and closed after two periods.
    """
    if not potentials_v:
        raise ValueError("at least one CA potential is required")
    if not math.isfinite(cycle_time_s) or cycle_time_s <= 0:
        raise ValueError("cycle_time_s must be finite and greater than 0")
    if not math.isfinite(sample_rate_hz) or sample_rate_hz <= 0:
        raise ValueError("sample_rate_hz must be finite and greater than 0")
    if conditioning_repeats < 1:
        raise ValueError("conditioning_repeats must be at least 1")

    conditioning_name = experiment_name(conditioning_experiment)
    phases: list[dict[str, object]] = []
    for index, potential in enumerate(potentials_v):
        if not math.isfinite(potential):
            raise ValueError("all CA potentials must be finite")
        if condition_before_first or index > 0:
            phases.append(
                {
                    "type": "sui_experiment",
                    "name": conditioning_name,
                    "repeat": conditioning_repeats,
                }
            )
        ca: dict[str, object] = {
            "type": "chopped_ca",
            "potential_v": float(potential),
            "potential_vs_ocp": potential_vs_ocp,
            "sample_rate_hz": float(sample_rate_hz),
            "cycle_time_s": float(cycle_time_s),
            "loops": 1,
            "ca_duration_s": 3.0 * cycle_time_s,
            "post_close_hold_s": 0.0,
        }
        if expected_max_current_a is not None:
            ca["expected_max_current_a"] = expected_max_current_a
        if max_absolute_current_a is not None:
            ca["max_absolute_current_a"] = max_absolute_current_a
        phases.append(ca)
    return phases


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Create sequence JSON containing short chopped CA measurements "
            "alternating with a named SUI conditioning experiment."
        )
    )
    parser.add_argument("--potentials-v", type=finite_float, nargs="+", required=True)
    parser.add_argument("--cycle-time-s", type=positive_float, default=60.0)
    parser.add_argument("--sample-rate-hz", type=positive_float, default=10.0)
    parser.add_argument(
        "--conditioning-experiment",
        default=DEFAULT_CONDITIONING_EXPERIMENT,
        help="SUI experiment name or .json filename",
    )
    parser.add_argument(
        "--conditioning-repeats",
        type=positive_int,
        default=1,
        help="number of complete CV conditioning runs before each CA",
    )
    parser.add_argument(
        "--skip-initial-conditioning",
        action="store_true",
        help="start with CA; condition only between subsequent CA measurements",
    )
    parser.add_argument("--potential-vs-ocp", action="store_true")
    parser.add_argument("--expected-max-current-a", type=positive_float)
    parser.add_argument("--max-absolute-current-a", type=positive_float)
    parser.add_argument("--custom-experiments-directory", required=True)
    parser.add_argument("--potentiostat-port", default="COM4")
    parser.add_argument("--channel", type=int, default=0)
    parser.add_argument("--arduino-port")
    parser.add_argument("--shutter-speed", type=int, default=2000)
    parser.add_argument("--output-directory", default="../sequence_data")
    parser.add_argument("--run-name", default="short_chopped_ca_sweep")
    parser.add_argument("--output", type=Path, required=True, help="sequence JSON to create")
    parser.add_argument("--overwrite", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not 0 <= args.channel <= 255:
        parser.error("--channel must be between 0 and 255")
    if not 50 <= args.shutter_speed <= 2000:
        parser.error("--shutter-speed must be between 50 and 2000")
    try:
        phases = create_chopped_ca_sweep(
            potentials_v=args.potentials_v,
            conditioning_experiment=args.conditioning_experiment,
            conditioning_repeats=args.conditioning_repeats,
            cycle_time_s=args.cycle_time_s,
            sample_rate_hz=args.sample_rate_hz,
            potential_vs_ocp=args.potential_vs_ocp,
            expected_max_current_a=args.expected_max_current_a,
            max_absolute_current_a=args.max_absolute_current_a,
            condition_before_first=not args.skip_initial_conditioning,
        )
    except ValueError as error:
        parser.error(str(error))

    config = {
        "custom_experiments_directory": args.custom_experiments_directory,
        "device": {"port": args.potentiostat_port, "channel": args.channel},
        "shutter": {
            "port": args.arduino_port,
            "speed": args.shutter_speed,
            "open_angle_deg": 315,
            "close_angle_deg": 45,
        },
        "output_directory": args.output_directory,
        "run_name": args.run_name,
        "sequence": phases,
    }
    output = args.output.expanduser().resolve()
    if output.exists() and not args.overwrite:
        parser.error(f"output already exists; add --overwrite to replace it: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(config, indent=2) + "\n", encoding="utf-8")
    ca_duration = 3 * args.cycle_time_s
    print(
        f"Created {output}: {len(args.potentials_v)} chopped CA measurements, "
        f"{ca_duration:g} s each, conditioning={experiment_name(args.conditioning_experiment)!r}."
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
