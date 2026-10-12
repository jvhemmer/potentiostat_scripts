import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

import ca_eis_sequence as sequence


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

    def appendElement(self, element, repeat=1):
        self.elements.append((element, repeat))
        return True


class SequenceTests(unittest.TestCase):
    def parse(self, *extra):
        parser = sequence.build_parser()
        args = parser.parse_args(
            [
                "--com-port", "COM16",
                "--ca-potential-v", "0.2",
                "--ca-sample-rate-hz", "10",
                "--ca-duration-s", "60",
                "--eis-dc-potentials-v", "0", "0.1", "0.2",
                "--eis-start-hz", "100000",
                "--eis-end-hz", "0.1",
                "--eis-steps-per-decade", "10",
                "--eis-amplitude-v", "0.01",
                *extra,
            ]
        )
        return sequence.settings_from_args(args, parser)

    def test_sample_rate_is_converted_to_interval(self):
        self.assertAlmostEqual(self.parse().ca_sample_interval_s, 0.1)

    def test_builds_one_ca_eis_pair_per_bias(self):
        api = SimpleNamespace(
            AisExperiment=FakeExperiment,
            AisConstantPotElement=FakeElement,
            AisEISPotentiostaticElement=FakeElement,
        )
        experiment = sequence.build_experiment(api, self.parse())
        self.assertEqual(len(experiment.elements), 6)
        self.assertEqual(experiment.elements[0][0].args, (0.2, 0.1, 60.0))
        self.assertEqual(experiment.elements[1][0].args, (100000.0, 0.1, 10.0, 0.0, 0.01))
        self.assertEqual(experiment.elements[5][0].args[3], 0.2)

    def test_csv_recorder_writes_headers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "run"
            recorder = sequence.CsvRecorder(output)
            recorder.close()
            self.assertTrue((output / "dc.csv").read_text().startswith("timestamp_s,"))
            self.assertTrue((output / "eis.csv").read_text().startswith("timestamp_s,"))


if __name__ == "__main__":
    unittest.main()
