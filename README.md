# Repeated CA + EIS for Admiral Squidstat

`ca_eis_sequence.py` builds one Squidstat custom experiment containing this order:

1. chronoamperometry (constant potential) with fixed potential, sampling interval, and duration;
2. potentiostatic EIS at first requested DC bias;
3. the same CA followed by EIS at each remaining DC bias.

It writes live DC data to `dc.csv` and impedance data to `eis.csv`.

## Setup with uv

- Python 3.9 through 3.12
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Admiral `SquidstatPyLibrary` wheel matching the operating system
- Squidstat connected by USB and not open in another control program

Create the project environment and install the public dependencies:

```powershell
uv sync --python 3.12
```

Install Admiral's downloaded wheel into the same environment:

```powershell
uv pip install 'C:\path\to\SquidstatPyLibrary-VERSION-py3-none-win_amd64.whl'
```

`SquidstatPyLibrary` is distributed as a platform-specific wheel rather than a
public package, so it cannot be placed in the shared `uv.lock`. Use
`uv run --no-sync` after installing the wheel; a normal exact sync could remove
this manually installed package.

Run all tests:

```powershell
uv run --no-sync python -m unittest -v
```

Follow Admiral's [Python installation guide](https://admiral-instruments.github.io/AdmiralSquidstatAPI/md__markdown_files_2_setup_python.html). On Linux, review their dependency installer before running it because the documented script updates installed system packages.

## Example

```bash
uv run --no-sync python ca_eis_sequence.py \
  --com-port COM16 \
  --channel 0 \
  --ca-potential-v 0.20 \
  --ca-sample-rate-hz 10 \
  --ca-duration-s 60 \
  --eis-dc-potentials-v 0.00 0.10 0.20 0.30 \
  --eis-start-hz 100000 \
  --eis-end-hz 0.1 \
  --eis-steps-per-decade 10 \
  --eis-amplitude-v 0.010 \
  --eis-minimum-cycles 2
```

`--ca-sample-rate-hz 10` becomes the `0.1 s` sampling interval expected by `AisConstantPotElement`. Use `--ca-sample-interval-s` instead when interval is already known.

EIS potentials reference the instrument's reference terminal by default. Add `--eis-bias-vs-ocp` to make every listed DC bias relative to OCP. Optional EIS quiet-time controls are available through `--eis-quiet-time-s` and `--eis-quiet-sample-interval-s`.

Run `python ca_eis_sequence.py --help` for all options. Voltage, current, frequency, and time values use V, A, Hz, and s.

## Safety

Validate all potentials, frequency limits, amplitude, channel, cell connections, and instrument limits before running on a real cell. Command-line electrochemical parameters are required so hardware-affecting defaults are not silently chosen.

## Chopped illumination CA

`chopped_illumination.py` connects to an `AUTO_SHUTTER_V1` Arduino first, sets
`SPEED 2000`, and leaves it blocked on standby. It then connects to the
Squidstat. The instrument's `experimentStarting` signal releases this default
schedule:

```text
t=0 s       CA starts; shutter blocked
t=60 s      MOVE +315 degrees; illumination open
t=120 s     MOVE +45 degrees; illumination blocked
t=180 s     MOVE +315 degrees; illumination open
t=240 s     MOVE +45 degrees; illumination blocked
t=300 s     MOVE +315 degrees; illumination open
t=360 s     MOVE +45 degrees; illumination blocked
t=362 s     CA ends after the default 2 s closing hold
```

Example for Windows PowerShell:

```powershell
uv run --no-sync python .\chopped_illumination.py `
  --potentiostat-port COM16 `
  --arduino-port COM3 `
  --channel 0 `
  --potential-v 0.20 `
  --sample-rate-hz 10 `
  --save-path 'C:\Users\Wilson Lab\Documents\chopped_ca.csv'
```

Omit `--arduino-port` to find the controller through its firmware `ID`
handshake. If `--save-path` is omitted, a folder-selection dialog appears
after both devices are ready. Selecting a folder starts the experiment and
creates a timestamped CSV there. Cancelling the dialog exits without starting
the CA. Main CSV includes CA data and illumination state. A second
`*_shutter_events.csv` file records scheduled and actual movement times.

A live dashboard opens before acquisition starts. It plots working-electrode
voltage, current, and shutter state against elapsed time using Squidstat's
real-time `activeDCDataReady` signal. The shutter axis uses `0 = blocked`,
`0.5 = moving`, and `1 = open`. Closing the plot window does not stop the
experiment. A red **Stop measurement** button asks for confirmation, aborts
the shutter schedule, stops the Squidstat experiment, and exits the script.
If stopped while illuminated or moving, verify the laser path is blocked.
Use `--no-ui` for headless operation or `--plot-refresh-ms` to change the
display refresh interval; acquisition rate is unchanged.

Useful options include `--potential-vs-ocp`, `--expected-max-current-a`,
`--max-absolute-current-a`, `--cycle-time-s`, `--loops`, `--speed`,
`--ca-duration-s`, `--overwrite`, `--buffered-save`, `--no-save`, and
`--print-data`. Run
`uv run --no-sync python .\chopped_illumination.py --help` for details.

The default CA duration is 362 s: 360 s for three complete chopped loops plus
2 s so the final closing movement finishes before the potentiostat stops.
