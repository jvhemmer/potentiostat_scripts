import json
import tempfile
import unittest
from pathlib import Path

from make_chopped_ca_sweep import create_chopped_ca_sweep, experiment_name, main


class ChoppedCaSweepTests(unittest.TestCase):
    def test_short_ca_is_three_cycle_periods(self):
        phases = create_chopped_ca_sweep(
            potentials_v=[-0.9, -1.0, -1.1],
            cycle_time_s=60,
            conditioning_experiment="TBAPF6 anodic CV.json",
        )
        self.assertEqual(len(phases), 6)
        for phase in phases[0::2]:
            self.assertEqual(phase["type"], "chopped_ca")
            self.assertEqual(phase["loops"], 1)
            self.assertEqual(phase["ca_duration_s"], 180)
            self.assertEqual(phase["post_close_hold_s"], 0)
        self.assertEqual([phase["potential_v"] for phase in phases[0::2]], [-0.9, -1.0, -1.1])
        self.assertTrue(all(phase["name"] == "TBAPF6 anodic CV" for phase in phases[1::2]))

    def test_can_condition_only_between_measurements(self):
        phases = create_chopped_ca_sweep(
            potentials_v=[-0.9, -1.0], condition_after_last=False
        )
        self.assertEqual([phase["type"] for phase in phases], ["chopped_ca", "sui_experiment", "chopped_ca"])

    def test_filename_is_normalized_to_sui_experiment_name(self):
        self.assertEqual(experiment_name("TBAPF6 anodic CV.json"), "TBAPF6 anodic CV")

    def test_cli_writes_sequence_json(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / "sweep.json"
            result = main([
                "--potentials-v", "-0.9", "-1.0",
                "--custom-experiments-directory", folder,
                "--output", str(output),
            ])
            self.assertEqual(result, 0)
            document = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(document["sequence"][0]["ca_duration_s"], 180)
            self.assertEqual(document["sequence"][1]["name"], "TBAPF6 anodic CV")


if __name__ == "__main__":
    unittest.main()
