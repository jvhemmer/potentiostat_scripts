"""Read SquidStat SUI custom-experiment JSON and build API experiments."""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any


class SuiExperimentError(ValueError):
    """SUI experiment file is missing, invalid, or unsupported."""


UNIT_FACTORS = {
    "V": 1.0,
    "mV": 1e-3,
    "uV": 1e-6,
    "A": 1.0,
    "mA": 1e-3,
    "uA": 1e-6,
    "nA": 1e-9,
    "s": 1.0,
    "ms": 1e-3,
    "min": 60.0,
    "h": 3600.0,
    "Hz": 1.0,
    "kHz": 1e3,
    "MHz": 1e6,
    "mHz": 1e-3,
    "V/s": 1.0,
    "mV/s": 1e-3,
    "uV/s": 1e-6,
}


def _number(inputs: dict[str, Any], key: str, *, optional: bool = False) -> float | None:
    raw = inputs.get(key, "")
    if raw in ("", None):
        if optional:
            return None
        raise SuiExperimentError(f"missing required SUI parameter {key!r}")
    try:
        value = float(raw)
    except (TypeError, ValueError) as error:
        raise SuiExperimentError(f"invalid number for SUI parameter {key!r}: {raw!r}") from error
    if not math.isfinite(value):
        raise SuiExperimentError(f"SUI parameter {key!r} must be finite")
    return value


def _quantity(inputs: dict[str, Any], key: str, *, optional: bool = False) -> float | None:
    value = _number(inputs, key, optional=optional)
    if value is None:
        return None
    units = inputs.get(f"{key}-units", "")
    try:
        factor = UNIT_FACTORS[units]
    except KeyError as error:
        raise SuiExperimentError(f"unsupported units for {key!r}: {units!r}") from error
    return value * factor


def _vs_ocp(value: Any) -> bool:
    return str(value).strip().lower() in {"ocp", "open circuit", "open-circuit"}


class SuiExperimentLibrary:
    """Name-indexed collection of SUI JSON experiments from one directory."""

    BUILT_INS = {
        "Open Circuit Potential",
        "Constant Potential",
        "Cyclic Voltammetry",
        "Potentiostatic EIS",
    }

    def __init__(self, directory: Path):
        self.directory = directory.expanduser().resolve()
        if not self.directory.is_dir():
            raise SuiExperimentError(f"custom experiment directory does not exist: {self.directory}")
        self.documents: dict[str, list[tuple[Path, dict[str, Any]]]] = {}
        for path in sorted(self.directory.rglob("*.json")):
            try:
                document = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                raise SuiExperimentError(f"cannot read SUI experiment {path}: {error}") from error
            name = document.get("name")
            if not isinstance(name, str) or not name.strip():
                continue
            self.documents.setdefault(name, []).append((path, document))

    def names(self) -> list[str]:
        return sorted(self.documents)

    def source_path(self, name: str) -> Path:
        return self._document(name)[0]

    def _document(self, name: str) -> tuple[Path, dict[str, Any]]:
        matches = self.documents.get(name, [])
        if not matches:
            available = ", ".join(self.names()) or "none"
            raise SuiExperimentError(
                f"SUI experiment {name!r} not found; available: {available}"
            )
        if len(matches) > 1:
            paths = " and ".join(str(item[0]) for item in matches)
            raise SuiExperimentError(
                f"SUI experiment name {name!r} is ambiguous; found in {paths}"
            )
        return matches[0]

    def build(self, api: Any, name: str) -> Any:
        return self._build_named(api, name, ())

    def _build_named(self, api: Any, name: str, stack: tuple[str, ...]) -> Any:
        if name in stack:
            raise SuiExperimentError("recursive SUI experiment reference: " + " -> ".join((*stack, name)))
        try:
            _path, document = self._document(name)
        except SuiExperimentError as error:
            if not self.documents.get(name):
                raise SuiExperimentError(
                    f"referenced custom experiment {name!r} was not found in {self.directory}"
                ) from error
            raise
        root = document.get("elements")
        experiment = self._build_set(api, root, (*stack, name))
        root_repeats = root.get("repeats", 1) if isinstance(root, dict) else 1
        if root_repeats != 1:
            wrapper = api.AisExperiment()
            if not wrapper.appendSubExperiment(experiment, root_repeats):
                raise SuiExperimentError(f"API rejected root repeat count {root_repeats} in {name!r}")
            experiment = wrapper
        experiment.setExperimentName(name)
        description = document.get("description")
        if isinstance(description, str) and description and hasattr(experiment, "setDescription"):
            experiment.setDescription(description)
        return experiment

    def _build_set(self, api: Any, node: Any, stack: tuple[str, ...]) -> Any:
        if not isinstance(node, dict) or node.get("type") != "set":
            raise SuiExperimentError(f"{stack[-1]!r} contains an invalid experiment set")
        experiment = api.AisExperiment()
        children = node.get("elements")
        if not isinstance(children, list) or not children:
            raise SuiExperimentError(f"{stack[-1]!r} contains an empty experiment set")
        for child in children:
            if not isinstance(child, dict):
                raise SuiExperimentError(f"{stack[-1]!r} contains an invalid child")
            repeats = child.get("repeats", 1)
            if isinstance(repeats, bool) or not isinstance(repeats, int) or repeats < 1:
                raise SuiExperimentError(f"invalid repeat count in {stack[-1]!r}: {repeats!r}")
            if child.get("type") == "set":
                subexperiment = self._build_set(api, child, stack)
                if not experiment.appendSubExperiment(subexperiment, repeats):
                    raise SuiExperimentError(f"API rejected nested set in {stack[-1]!r}")
                continue
            plugin = child.get("plugin-name")
            if not isinstance(plugin, str) or not plugin:
                raise SuiExperimentError(f"missing plugin-name in {stack[-1]!r}")
            if plugin in self.BUILT_INS:
                element = self._build_element(api, plugin, child.get("user-input", {}))
                if not experiment.appendElement(element, repeats):
                    raise SuiExperimentError(f"API rejected {plugin!r} in {stack[-1]!r}")
            else:
                subexperiment = self._build_named(api, plugin, stack)
                if not experiment.appendSubExperiment(subexperiment, repeats):
                    raise SuiExperimentError(f"API rejected custom experiment {plugin!r} in {stack[-1]!r}")
        return experiment

    def _build_element(self, api: Any, plugin: str, raw_inputs: Any) -> Any:
        if not isinstance(raw_inputs, dict):
            raise SuiExperimentError(f"invalid user-input for {plugin!r}")
        values = raw_inputs
        if plugin == "Constant Potential":
            element = api.AisConstantPotElement(
                _quantity(values, "constant-potential"),
                _quantity(values, "sampling-interval"),
                _quantity(values, "duration"),
            )
            element.setVoltageVsOCP(_vs_ocp(values.get("potential-vs-ocp")))
            return element
        if plugin == "Open Circuit Potential":
            element = api.AisOpenCircuitElement(
                _quantity(values, "experiment-duration"),
                _quantity(values, "sampling-interval"),
            )
            optional = {
                "maximum-voltage": "setMaxVoltage",
                "minimum-voltage": "setMinVoltage",
                "dvdt-minimum": "setMindVdt",
            }
            for key, setter in optional.items():
                value = _quantity(values, key, optional=True)
                if value is not None:
                    getattr(element, setter)(value)
            return element
        if plugin == "Cyclic Voltammetry":
            scan_rate = _quantity(values, "slewrate")
            sampling_value = _quantity(values, "sampling-interval")
            sampling_units = values.get("sampling-interval-units")
            sampling_interval = sampling_value / scan_rate if sampling_units in {"V", "mV", "uV"} else sampling_value
            element = api.AisCyclicVoltammetryElement(
                _quantity(values, "Starting-voltage"),
                _quantity(values, "Potential-Limit-1"),
                _quantity(values, "Potential-Limit-2"),
                _quantity(values, "Ending-voltage"),
                scan_rate,
                sampling_interval,
            )
            element.setStartVoltageVsOCP(_vs_ocp(values.get("start-voltage-vs-ocp")))
            element.setFirstVoltageLimitVsOCP(_vs_ocp(values.get("first-voltage-vs-ocp")))
            element.setSecondVoltageLimitVsOCP(_vs_ocp(values.get("second-voltage-vs-ocp")))
            element.setEndVoltageVsOCP(_vs_ocp(values.get("end-voltage-vs-ocp")))
            element.setNumberOfCycles(int(_number(values, "general-cycles")))
            element.setQuietTime(_quantity(values, "quiet-time"))
            element.setQuietTimeSamplingInterval(_quantity(values, "quiet-sampling-interval"))
            alpha = _number(values, "alpha-factor", optional=True)
            if alpha is not None:
                element.setAlphaFactor(alpha)
            expected_current = _number(values, "current-ranging-approx", optional=True)
            if expected_current is not None:
                current_units = values.get("current-ranging-units", "")
                try:
                    expected_current *= UNIT_FACTORS[current_units]
                except KeyError as error:
                    raise SuiExperimentError(
                        f"unsupported current-ranging-units: {current_units!r}"
                    ) from error
            if values.get("current-ranging-on") and expected_current is not None:
                element.setApproxMaxCurrent(expected_current)
            return element
        if plugin == "Potentiostatic EIS":
            element = api.AisEISPotentiostaticElement(
                _quantity(values, "start-frequency"),
                _quantity(values, "lower-frequency"),
                _number(values, "steps-per-decade"),
                _quantity(values, "dc-bias-current"),
                _quantity(values, "ac-amplitude"),
            )
            element.setBiasVoltageVsOCP(_vs_ocp(values.get("dc-bias-vs-ocp")))
            element.setMinimumCycles(int(_number(values, "min-cycles-sampled")))
            element.setQuietTime(_quantity(values, "quiet-time"))
            element.setQuietTimeSamplingInterval(_quantity(values, "quiet-sampling-interval"))
            return element
        raise SuiExperimentError(f"unsupported SUI experiment plugin: {plugin!r}")
