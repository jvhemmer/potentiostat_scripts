import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from run_sui_sequence import SequenceRecorder, chopped_settings, load_sequence, phase_label
from squidstat_sui import SuiExperimentError, SuiExperimentLibrary


class FakeElement:
    def __init__(self, *args):
        self.args = args
        self.calls = []

    def __getattr__(self, name):
        if name.startswith("set"):
            return lambda *args: self.calls.append((name, args))
        raise AttributeError(name)


class FakeExperiment:
    def __init__(self):
        self.items = []

    def setExperimentName(self, name):
        self.name = name

    def setDescription(self, description):
        self.description = description

    def appendElement(self, element, repeat=1):
        self.items.append(("element", element, repeat))
        return True

    def appendSubExperiment(self, experiment, repeat=1):
        self.items.append(("experiment", experiment, repeat))
        return True


API = SimpleNamespace(
    AisExperiment=FakeExperiment,
    AisConstantPotElement=FakeElement,
    AisOpenCircuitElement=FakeElement,
    AisCyclicVoltammetryElement=FakeElement,
    AisEISPotentiostaticElement=FakeElement,
)


class SuiExperimentTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def write(self, name, elements):
        document = {"name": name, "description": "test", "elements": elements}
        (self.root / f"{name}.json").write_text(json.dumps(document), encoding="utf-8")

    def write_file(self, filename, name, elements):
        document = {"name": name, "description": "test", "elements": elements}
        (self.root / filename).write_text(json.dumps(document), encoding="utf-8")

    def cv(self):
        return {
            "type": "element",
            "plugin-name": "Cyclic Voltammetry",
            "repeats": 1,
            "user-input": {
                "Starting-voltage": "0", "Starting-voltage-units": "V",
                "Potential-Limit-1": "1", "Potential-Limit-1-units": "V",
                "Potential-Limit-2": "-1", "Potential-Limit-2-units": "V",
                "Ending-voltage": "0", "Ending-voltage-units": "V",
                "slewrate": "10", "slewrate-units": "mV/s",
                "sampling-interval": "1", "sampling-interval-units": "mV",
                "general-cycles": "2", "quiet-time": "5", "quiet-time-units": "s",
                "quiet-sampling-interval": "0.1", "quiet-sampling-interval-units": "s",
                "alpha-factor": "75", "current-ranging-approx": "10",
                "current-ranging-units": "uA", "current-ranging-on": True,
                "start-voltage-vs-ocp": "reference", "first-voltage-vs-ocp": "reference",
                "second-voltage-vs-ocp": "reference", "end-voltage-vs-ocp": "reference"
            },
        }

    def test_builds_cv_and_converts_sui_voltage_sampling_increment(self):
        self.write("CV", {"type": "set", "repeats": 1, "elements": [self.cv()]})
        experiment = SuiExperimentLibrary(self.root).build(API, "CV")
        element = experiment.items[0][1]
        self.assertAlmostEqual(element.args[4], 0.01)
        self.assertAlmostEqual(element.args[5], 0.1)
        expected_current = dict(element.calls)["setApproxMaxCurrent"][0]
        self.assertAlmostEqual(expected_current, 10e-6)

    def test_resolves_custom_experiment_and_nested_set_repeats(self):
        self.write("CV", {"type": "set", "repeats": 1, "elements": [self.cv()]})
        ca = {"type": "element", "plugin-name": "Constant Potential", "repeats": 1, "user-input": {"constant-potential": "0.2", "constant-potential-units": "V", "sampling-interval": "0.1", "sampling-interval-units": "s", "duration": "1", "duration-units": "min", "potential-vs-ocp": "reference"}}
        self.write("Parent", {"type": "set", "repeats": 1, "elements": [{"type": "element", "plugin-name": "CV", "repeats": 2, "user-input": {}}, {"type": "set", "repeats": 3, "elements": [ca]}]})
        experiment = SuiExperimentLibrary(self.root).build(API, "Parent")
        self.assertEqual(experiment.items[0][0::2], ("experiment", 2))
        self.assertEqual(experiment.items[1][0::2], ("experiment", 3))

    def test_reports_missing_custom_experiment(self):
        self.write("Parent", {"type": "set", "repeats": 1, "elements": [{"type": "element", "plugin-name": "Missing CV", "repeats": 1, "user-input": {}}]})
        with self.assertRaisesRegex(SuiExperimentError, "Missing CV"):
            SuiExperimentLibrary(self.root).build(API, "Parent")

    def test_unrelated_duplicate_names_do_not_block_requested_experiment(self):
        elements = {"type": "set", "repeats": 1, "elements": [self.cv()]}
        self.write("Requested CV", elements)
        self.write_file("old copy.json", "Unrelated CV", elements)
        self.write_file("new copy.json", "Unrelated CV", elements)
        experiment = SuiExperimentLibrary(self.root).build(API, "Requested CV")
        self.assertEqual(experiment.name, "Requested CV")

    def test_requested_duplicate_name_is_reported_as_ambiguous(self):
        elements = {"type": "set", "repeats": 1, "elements": [self.cv()]}
        self.write_file("first.json", "Duplicate CV", elements)
        self.write_file("second.json", "Duplicate CV", elements)
        with self.assertRaisesRegex(SuiExperimentError, "ambiguous"):
            SuiExperimentLibrary(self.root).build(API, "Duplicate CV")

    def test_loads_cv_chopped_sequence(self):
        path = self.root / "sequence.json"
        path.write_text(json.dumps({"custom_experiments_directory": str(self.root), "device": {"port": "COM4", "channel": 0}, "shutter": {"speed": 2000}, "sequence": [{"type": "sui_experiment", "name": "CV"}, {"type": "chopped_ca", "potential_v": -1.2, "sample_rate_hz": 10, "loops": 3}]}), encoding="utf-8")
        config = load_sequence(path)
        settings = chopped_settings(config, config["sequence"][1])
        self.assertEqual(settings.ca_duration_s, 362)
        self.assertEqual(settings.sample_interval_s, 0.1)

    def test_phase_labels_do_not_read_ca_fields_from_sui_phase(self):
        self.assertEqual(
            phase_label({"type": "sui_experiment", "name": "TBAPF6 anodic CV"}),
            "TBAPF6 anodic CV",
        )
        self.assertEqual(
            phase_label({"type": "chopped_ca", "potential_v": -0.9}),
            "Chopped CA -0.9 V",
        )

    def test_recorder_creates_one_dc_file_per_phase(self):
        output = self.root / "run"
        recorder = SequenceRecorder(output)
        data = SimpleNamespace(
            timestamp=0.1,
            workingElectrodeVoltage=0.2,
            counterElectrodeVoltage=0.0,
            current=1e-6,
            temperature=25.0,
        )
        recorder.begin_phase(
            1,
            {"type": "sui_experiment", "name": "TBAPF6 anodic CV"},
            1.0,
        )
        recorder.dc_data(0, data)
        recorder.begin_phase(
            2,
            {"type": "chopped_ca", "potential_v": -0.9},
            2.0,
        )
        recorder.dc_data(0, data)
        recorder.close()

        self.assertTrue((output / "01_TBAPF6_anodic_CV.csv").is_file())
        self.assertTrue((output / "02_Chopped_CA_-0.9_V.csv").is_file())
        self.assertFalse((output / "dc.csv").exists())


if __name__ == "__main__":
    unittest.main()
