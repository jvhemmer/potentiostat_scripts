"""Load, validate, and build reusable Squidstat experiment recipes."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping


class RecipeError(ValueError):
    """A recipe is invalid or cannot be read."""


@dataclass(frozen=True)
class Recipe:
    source_path: Path
    data: dict[str, Any]

    @property
    def name(self) -> str:
        return self.data["name"]

    @property
    def port(self) -> str:
        return self.data["device"]["port"]

    @property
    def channel(self) -> int:
        return self.data["device"]["channel"]

    @property
    def steps(self) -> list[dict[str, Any]]:
        return self.data["steps"]


def _object(value: Any, location: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise RecipeError(f"{location} must be a JSON object")
    return value


def _keys(value: Mapping[str, Any], allowed: set[str], location: str) -> None:
    unknown = sorted(set(value) - allowed)
    if unknown:
        raise RecipeError(f"{location} has unknown field(s): {', '.join(unknown)}")


def _string(value: Any, location: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RecipeError(f"{location} must be a non-empty string")
    return value.strip()


def _number(value: Any, location: str, *, positive: bool = False, nonnegative: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise RecipeError(f"{location} must be a number")
    number = float(value)
    if not math.isfinite(number):
        raise RecipeError(f"{location} must be finite")
    if positive and number <= 0:
        raise RecipeError(f"{location} must be greater than 0")
    if nonnegative and number < 0:
        raise RecipeError(f"{location} must be at least 0")
    return number


def _integer(value: Any, location: str, *, minimum: int = 0, maximum: int | None = None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise RecipeError(f"{location} must be an integer")
    if value < minimum or (maximum is not None and value > maximum):
        limit = f" between {minimum} and {maximum}" if maximum is not None else f" at least {minimum}"
        raise RecipeError(f"{location} must be{limit}")
    return value


def _boolean(value: Any, location: str) -> bool:
    if not isinstance(value, bool):
        raise RecipeError(f"{location} must be true or false")
    return value


def _required(step: Mapping[str, Any], fields: set[str], location: str) -> None:
    missing = sorted(fields - set(step))
    if missing:
        raise RecipeError(f"{location} is missing field(s): {', '.join(missing)}")


COMMON = {"type", "label", "repeat"}
STEP_FIELDS = {
    "ocp": COMMON | {"duration_s", "sample_interval_s", "max_voltage_v", "min_voltage_v", "min_dvdt_v_per_s", "approx_max_voltage_v"},
    "ca": COMMON | {"potential_v", "duration_s", "sample_interval_s", "sample_rate_hz", "potential_vs_ocp", "max_absolute_current_a", "min_absolute_current_a", "max_capacity_c", "min_didt_a_per_s", "approx_max_current_a"},
    "cv": COMMON | {"start_v", "first_limit_v", "second_limit_v", "end_v", "scan_rate_v_per_s", "sample_interval_s", "quiet_time_s", "quiet_sample_interval_s", "start_vs_ocp", "first_limit_vs_ocp", "second_limit_vs_ocp", "end_vs_ocp", "cycles", "approx_max_current_a", "alpha_factor"},
    "eis_potentiostatic": COMMON | {"start_frequency_hz", "end_frequency_hz", "steps_per_decade", "bias_v", "amplitude_v", "bias_vs_ocp", "quiet_time_s", "quiet_sample_interval_s", "minimum_cycles"},
}
REQUIRED = {
    "ocp": {"duration_s", "sample_interval_s"},
    "ca": {"potential_v", "duration_s"},
    "cv": {"start_v", "first_limit_v", "second_limit_v", "end_v", "scan_rate_v_per_s", "sample_interval_s"},
    "eis_potentiostatic": {"start_frequency_hz", "end_frequency_hz", "steps_per_decade", "bias_v", "amplitude_v"},
}
POSITIVE = {"duration_s", "sample_interval_s", "sample_rate_hz", "scan_rate_v_per_s", "quiet_sample_interval_s", "start_frequency_hz", "end_frequency_hz", "steps_per_decade", "amplitude_v", "max_absolute_current_a", "min_absolute_current_a", "max_capacity_c", "approx_max_current_a", "approx_max_voltage_v"}
NONNEGATIVE = {"quiet_time_s", "min_dvdt_v_per_s", "min_didt_a_per_s", "alpha_factor"}
BOOLEANS = {"potential_vs_ocp", "start_vs_ocp", "first_limit_vs_ocp", "second_limit_vs_ocp", "end_vs_ocp", "bias_vs_ocp"}
INTEGERS = {"repeat", "cycles", "minimum_cycles"}


def _validate_step(raw: Any, index: int) -> dict[str, Any]:
    location = f"steps[{index}]"
    step = dict(_object(raw, location))
    kind = _string(step.get("type"), f"{location}.type").lower()
    if kind not in STEP_FIELDS:
        raise RecipeError(f"{location}.type must be one of: {', '.join(STEP_FIELDS)}")
    step["type"] = kind
    _keys(step, STEP_FIELDS[kind], location)
    _required(step, REQUIRED[kind], location)
    if kind == "ca":
        sampling = {"sample_interval_s", "sample_rate_hz"} & set(step)
        if len(sampling) != 1:
            raise RecipeError(f"{location} must contain exactly one of sample_interval_s or sample_rate_hz")
        if "sample_rate_hz" in step:
            step["sample_interval_s"] = 1.0 / _number(step.pop("sample_rate_hz"), f"{location}.sample_rate_hz", positive=True)
    for key, value in list(step.items()):
        field = f"{location}.{key}"
        if key == "label":
            step[key] = _string(value, field)
        elif key in INTEGERS:
            step[key] = _integer(value, field, minimum=1)
        elif key in BOOLEANS:
            step[key] = _boolean(value, field)
        elif key in POSITIVE:
            step[key] = _number(value, field, positive=True)
        elif key in NONNEGATIVE:
            step[key] = _number(value, field, nonnegative=True)
        elif key != "type":
            step[key] = _number(value, field)
    step.setdefault("repeat", 1)
    return step


def load_recipe(path: Path) -> Recipe:
    path = path.expanduser().resolve()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except OSError as error:
        raise RecipeError(f"cannot read recipe {path}: {error}") from error
    except json.JSONDecodeError as error:
        raise RecipeError(f"invalid JSON in {path} at line {error.lineno}, column {error.colno}: {error.msg}") from error

    root = _object(raw, "recipe")
    _keys(root, {"schema_version", "name", "device", "output", "steps"}, "recipe")
    if root.get("schema_version") != 1:
        raise RecipeError("schema_version must be 1")
    name = _string(root.get("name"), "name")
    device = _object(root.get("device"), "device")
    _keys(device, {"port", "channel"}, "device")
    port = _string(device.get("port"), "device.port")
    channel = _integer(device.get("channel", 0), "device.channel", maximum=255)
    output = _object(root.get("output", {}), "output")
    _keys(output, {"directory", "run_name", "timestamp"}, "output")
    if "directory" in output:
        output["directory"] = _string(output["directory"], "output.directory")
    if "run_name" in output:
        output["run_name"] = _string(output["run_name"], "output.run_name")
    if "timestamp" in output:
        output["timestamp"] = _boolean(output["timestamp"], "output.timestamp")
    steps = root.get("steps")
    if not isinstance(steps, list) or not steps:
        raise RecipeError("steps must be a non-empty JSON array")
    normalized = {
        "schema_version": 1,
        "name": name,
        "device": {"port": port, "channel": channel},
        "output": output,
        "steps": [_validate_step(step, index) for index, step in enumerate(steps)],
    }
    return Recipe(path, normalized)


def build_experiment(api: Any, recipe: Recipe) -> Any:
    experiment = api.AisExperiment()
    experiment.setExperimentName(recipe.name)
    for index, step in enumerate(recipe.steps):
        kind = step["type"]
        if kind == "ocp":
            element = api.AisOpenCircuitElement(step["duration_s"], step["sample_interval_s"])
            setters = {"max_voltage_v": "setMaxVoltage", "min_voltage_v": "setMinVoltage", "min_dvdt_v_per_s": "setMindVdt", "approx_max_voltage_v": "setApproxMaxVoltage"}
        elif kind == "ca":
            element = api.AisConstantPotElement(step["potential_v"], step["sample_interval_s"], step["duration_s"])
            setters = {"potential_vs_ocp": "setVoltageVsOCP", "max_absolute_current_a": "setMaxAbsoluteCurrent", "min_absolute_current_a": "setMinAbsoluteCurrent", "max_capacity_c": "setMaxCapacity", "min_didt_a_per_s": "setMindIdt", "approx_max_current_a": "setApproxMaxCurrent"}
        elif kind == "cv":
            element = api.AisCyclicVoltammetryElement(step["start_v"], step["first_limit_v"], step["second_limit_v"], step["end_v"], step["scan_rate_v_per_s"], step["sample_interval_s"])
            setters = {"quiet_time_s": "setQuietTime", "quiet_sample_interval_s": "setQuietTimeSamplingInterval", "start_vs_ocp": "setStartVoltageVsOCP", "first_limit_vs_ocp": "setFirstVoltageLimitVsOCP", "second_limit_vs_ocp": "setSecondVoltageLimitVsOCP", "end_vs_ocp": "setEndVoltageVsOCP", "cycles": "setNumberOfCycles", "approx_max_current_a": "setApproxMaxCurrent", "alpha_factor": "setAlphaFactor"}
        else:
            element = api.AisEISPotentiostaticElement(step["start_frequency_hz"], step["end_frequency_hz"], step["steps_per_decade"], step["bias_v"], step["amplitude_v"])
            setters = {"bias_vs_ocp": "setBiasVoltageVsOCP", "quiet_time_s": "setQuietTime", "quiet_sample_interval_s": "setQuietTimeSamplingInterval", "minimum_cycles": "setMinimumCycles"}
        for field, setter in setters.items():
            if field in step:
                getattr(element, setter)(step[field])
        if not experiment.appendElement(element, step["repeat"]):
            raise RuntimeError(f"Squidstat rejected steps[{index}] ({kind})")
    return experiment


def write_resolved_recipe(recipe: Recipe, destination: Path, port: str, channel: int) -> None:
    data = dict(recipe.data)
    data["device"] = {"port": port, "channel": channel}
    destination.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
