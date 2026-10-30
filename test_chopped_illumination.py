import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

import chopped_illumination as chopped


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
        self.elements = []

    def setExperimentName(self, name):
        self.name = name

    def appendElement(self, element, repeat):
        self.elements.append((element, repeat))
        return True


class FakeSerial:
    def __init__(self, responses):
        self.port = "COM3"
        self.responses = iter(responses)
        self.writes = []

    def write(self, data):
        self.writes.append(data)

    def flush(self):
        pass

    def readline(self):
        return next(self.responses, b"")

    def close(self):
        pass


class ChoppedIlluminationTests(unittest.TestCase):
    def parse(self, *extra):
        parser = chopped.build_parser()
        args = parser.parse_args(
            [
                "--potentiostat-port", "COM16",
                "--arduino-port", "COM3",
                "--potential-v", "0.2",
                "--sample-rate-hz", "10",
                "--no-save",
                *extra,
            ]
        )
        return chopped.settings_from_args(args, parser)

    def test_default_plan_matches_requested_three_loops(self):
        plan = chopped.build_shutter_plan(self.parse())
        self.assertEqual(
            [(item.scheduled_s, item.angle_deg, item.target_state) for item in plan],
            [
                (60.0, 315.0, "open"),
                (120.0, 45.0, "blocked"),
                (180.0, 315.0, "open"),
                (240.0, 45.0, "blocked"),
                (300.0, 315.0, "open"),
                (360.0, 45.0, "blocked"),
            ],
        )

    def test_default_ca_duration_includes_final_settle_time(self):
        settings = self.parse()
        self.assertEqual(settings.ca_duration_s, 362.0)
        self.assertAlmostEqual(settings.sample_interval_s, 0.1)

    def test_omitted_save_path_requests_folder_dialog(self):
        parser = chopped.build_parser()
        args = parser.parse_args(
            [
                "--potentiostat-port", "COM16",
                "--potential-v", "0.2",
                "--sample-rate-hz", "10",
            ]
        )
        settings = chopped.settings_from_args(args, parser)
        self.assertTrue(settings.prompt_for_save_folder)
        self.assertIsNone(settings.save_path)

    def test_ca_element_uses_requested_settings(self):
        api = SimpleNamespace(
            AisConstantPotElement=FakeElement,
            AisExperiment=FakeExperiment,
        )
        settings = self.parse(
            "--potential-vs-ocp",
            "--expected-max-current-a", "0.01",
            "--max-absolute-current-a", "0.02",
        )
        experiment = chopped.build_experiment(api, settings)
        element = experiment.elements[0][0]
        self.assertEqual(element.args, (0.2, 0.1, 362.0))
        self.assertIn(("setVoltageVsOCP", True), element.calls)
        self.assertIn(("setApproxMaxCurrent", 0.01), element.calls)
        self.assertIn(("setMaxAbsoluteCurrent", 0.02), element.calls)

    def test_recorder_creates_data_and_event_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            parser = chopped.build_parser()
            args = parser.parse_args(
                [
                    "--potentiostat-port", "COM16",
                    "--potential-v", "0.2",
                    "--sample-interval-s", "1",
                    "--save-path", str(Path(temp_dir) / "run.csv"),
                ]
            )
            recorder = chopped.DataRecorder(chopped.settings_from_args(args, parser))
            recorder.close()
            self.assertTrue((Path(temp_dir) / "run.csv").exists())
            self.assertTrue((Path(temp_dir) / "run_shutter_events.csv").exists())

    def test_recorder_exposes_live_plot_history(self):
        settings = self.parse()
        recorder = chopped.DataRecorder(settings)
        recorder.start(0.0)
        recorder.dc_data(
            0,
            SimpleNamespace(
                timestamp=1.0,
                workingElectrodeVoltage=0.2,
                counterElectrodeVoltage=0.3,
                current=0.004,
                temperature=25.0,
            ),
        )
        recorder.set_state("open")
        times, voltages, currents, states, current_state = recorder.snapshot()
        self.assertEqual(voltages, [0.2])
        self.assertEqual(currents, [0.004])
        self.assertEqual(current_state, "open")
        self.assertEqual(states[-1][1], "open")
        self.assertEqual(len(times), 1)
        recorder.close()

    def test_auto_shutter_commands_match_firmware_protocol(self):
        serial = FakeSerial([b"SPEED SET\n", b"MOVING\n", b"DONE\n"])
        shutter = chopped.AutoShutterClient(serial)
        shutter.set_speed(2000)
        shutter.move_and_wait(315, threading.Event())
        self.assertEqual(
            serial.writes,
            [b"SPEED 2000\n", b"MOVE 315\n"],
        )


if __name__ == "__main__":
    unittest.main()
