import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from run_recipe import main, output_directory
from squidstat_recipe import RecipeError, build_experiment, load_recipe, write_resolved_recipe


class FakeElement:
    def __init__(self, *args):
        self.args = args
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda value: self.calls.append((name, value))
        raise AttributeError(name)


class FakeExperiment:
    def __init__(self):
        self.name = ""
        self.elements = []

    def setExperimentName(self, name):
        self.name = name

    def appendElement(self, element, repeat):
        self.elements.append((element, repeat))
        return True


def fake_api():
    return SimpleNamespace(
        AisExperiment=FakeExperiment,
        AisOpenCircuitElement=FakeElement,
        AisConstantPotElement=FakeElement,
        AisCyclicVoltammetryElement=FakeElement,
        AisEISPotentiostaticElement=FakeElement,
    )


class RecipeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def save(self, data):
        path = self.root / "recipe.json"
        path.write_text(json.dumps(data), encoding="utf-8")
        return path

    def base(self, steps):
        return {
            "schema_version": 1,
            "name": "test sequence",
            "device": {"port": "COM4", "channel": 0},
            "steps": steps,
        }

    def test_loads_and_builds_all_supported_elements(self):
        recipe = load_recipe(
            self.save(
                self.base(
                    [
                        {"type": "ocp", "duration_s": 5, "sample_interval_s": 1},
                        {"type": "cv", "start_v": 0, "first_limit_v": 1, "second_limit_v": -1, "end_v": 0, "scan_rate_v_per_s": 0.1, "sample_interval_s": 0.01, "cycles": 2},
                        {"type": "ca", "potential_v": 0.2, "duration_s": 10, "sample_rate_hz": 20, "potential_vs_ocp": True},
                        {"type": "eis_potentiostatic", "start_frequency_hz": 10000, "end_frequency_hz": 1, "steps_per_decade": 10, "bias_v": 0.2, "amplitude_v": 0.01, "minimum_cycles": 3, "repeat": 2},
                    ]
                )
            )
        )
        experiment = build_experiment(fake_api(), recipe)
        self.assertEqual(experiment.name, "test sequence")
        self.assertEqual(len(experiment.elements), 4)
        self.assertEqual(experiment.elements[2][0].args, (0.2, 0.05, 10.0))
        self.assertIn(("setVoltageVsOCP", True), experiment.elements[2][0].calls)
        self.assertEqual(experiment.elements[3][1], 2)
        self.assertIn(("setMinimumCycles", 3), experiment.elements[3][0].calls)

    def test_rejects_ca_with_two_sampling_modes(self):
        path = self.save(
            self.base(
                [{"type": "ca", "potential_v": 0.2, "duration_s": 10, "sample_rate_hz": 10, "sample_interval_s": 0.1}]
            )
        )
        with self.assertRaisesRegex(RecipeError, "exactly one"):
            load_recipe(path)

    def test_rejects_unknown_field(self):
        path = self.save(self.base([{"type": "ocp", "duration_s": 5, "sample_interval_s": 1, "typo": 2}]))
        with self.assertRaisesRegex(RecipeError, "unknown field.*typo"):
            load_recipe(path)

    def test_writes_resolved_device_settings(self):
        recipe = load_recipe(self.save(self.base([{"type": "ocp", "duration_s": 5, "sample_interval_s": 1}])))
        destination = self.root / "used.json"
        write_resolved_recipe(recipe, destination, "COM9", 2)
        saved = json.loads(destination.read_text(encoding="utf-8"))
        self.assertEqual(saved["device"], {"port": "COM9", "channel": 2})

    def test_output_defaults_next_to_recipe(self):
        recipe = load_recipe(self.save(self.base([{"type": "ocp", "duration_s": 5, "sample_interval_s": 1}])))
        destination = output_directory(recipe, None)
        self.assertEqual(destination.parent, self.root / "runs")
        self.assertTrue(destination.name.startswith("recipe_"))

    def test_validate_only_does_not_import_hardware_library(self):
        path = self.save(self.base([{"type": "ocp", "duration_s": 5, "sample_interval_s": 1}]))
        self.assertEqual(main([str(path), "--validate-only"]), 0)


if __name__ == "__main__":
    unittest.main()
