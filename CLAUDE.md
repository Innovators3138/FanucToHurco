# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Desktop GUI (PyQt6 + Matplotlib) that converts Onshape/Fanuc G-code into the Hurco BNC dialect spoken by an AMTS BX-MPU retrofit controller on a Hurco KMB-1 / MB-1 mill, backplots the result in 3D, and sends it over the network to the shop's Micro DNC drip-feed boxes. `Gcodes.pdf` is the authority for the BX-MPU vocabulary and output format; `program_out.nc` is a real Onshape post output used as a reference input sample.

## Commands

```bash
pip install PyQt6 matplotlib          # runtime deps (no requirements.txt)
python3 main.py                       # run the GUI

# Build (macOS .app / local one-file)
pyinstaller FanucToHurco.spec
# or from scratch, matching the CI invocation:
pyinstaller --onefile --windowed --name FanucToHurco \
    --hidden-import mpl_toolkits.mplot3d \
    --hidden-import matplotlib.backends.backend_qtagg \
    main.py
```

Pushing to `main` triggers [.github/workflows/build-windows.yml](.github/workflows/build-windows.yml), which builds `dist/FanucToHurco.exe` on `windows-latest` and uploads it as an artifact.

There is no test suite. `main.py` exposes three import-friendly entry points, so a conversion change can be exercised headlessly:

```python
import main as m
out, warnings = m.convert(open('program_out.nc').read())
problems, stats = m.check(out)          # independent validator, not the converter
bp = m.BackplotParser(); bp.parse(out)  # segment counts for the plot
```

`check()` is the fastest regression signal: it re-reads the output from scratch and should report no problems for any valid input. To drive the GUI itself without a display, set `QT_QPA_PLATFORM=offscreen`, build `MainWindow()`, and `.click()` the buttons.

The DNC protocol client has its own CLI, which is the quickest way to poke a real machine:

```bash
python3 qsdnc.py 192.168.1.50 info
python3 qsdnc.py 192.168.1.50 ls "\\PROGRAMS"
python3 qsdnc.py 192.168.1.50 put out.nc "\\PROGRAMS\\PART1.NC"
python3 qsdnc.py 192.168.1.50 run "\\PROGRAMS\\PART1.NC"
```

To test protocol changes without hardware, stand up a UDP socket that answers the opcodes in §3 of the protocol spec; `qsdnc.QSClient(ip, port=…, bind_port=0)` will talk to it on loopback. `qsdnc.PORT` and `qsdnc.BIND_PORT` are read at connect time by `dncui.ClientThread`, so patching them redirects the whole GUI at a fake device.

## Architecture

Four modules. The converter and the backplot live in [main.py](main.py) together with the main window; the machine-transfer feature is split out so the protocol can be tested and scripted without Qt:

| Module | Role |
|---|---|
| [main.py](main.py) | converter, output checker, backplot, main window |
| [qsdnc.py](qsdnc.py) | Micro DNC / QS Explorer UDP protocol client — Qt-free, blocking, has its own CLI |
| [machines.py](machines.py) | the saved machine list (name + IP), JSON in the per-user config dir |
| [dncui.py](dncui.py) | Qt layer: worker thread, machine manager, remote file browser |

PyInstaller follows the imports from `main.py`, so the build needs no changes for the new modules.

### Converter side

[main.py](main.py) is in five parts:

1. **Machine settings block** (top of the file) — the tuning knobs; see below.
2. **`Converter`** — the Fanuc→BNC state machine. `convert(text)` feeds each source line to `Converter.process()` and returns `(output_text, warnings)`, where warnings is an `OrderedDict` of `{message: [source line numbers]}`.
3. **`check(text)`** — an *independent* re-reader of BNC output returning `(problems, stats)`. It deliberately shares no code with the converter beyond the settings constants, so it catches converter bugs rather than confirming them.
4. **`BackplotParser`** / **`PlotCanvas`** — re-parse the converted output into `rapid` / `feed` / `plunge` segment lists and draw them on embedded 3D Matplotlib axes.
5. **`MainWindow`** — Load / Convert / Verify / Save / Clear, over a three-pane splitter (input, output, backplot) with a Messages pane beneath.

### Invariants worth knowing before editing

**The converter's job is to erase everything modal and relative.** The BX-MPU gets absolute coordinates only, absolute I/J arc centers, one G code per block, and an explicit X, Y, I and J on every arc block even where the Fanuc source omitted them. `Converter` tracks `pos`, `absolute`, `plane`, `motion`, `feed` and `cycle` to do that. A change that emits a word without resolving it through `self.pos`/`self.target()` will produce output that is correct only by accident.

**`Converter.pos['Z'] is None` means "Z is unknown", not zero.** `emit_m25()` sets it, because M25 retracts to machine home and the program no longer knows where Z is. Code that reads `pos['Z']` must handle `None` (see `target()`, `do_arc()`, `rapid_z()`), or a retract will silently turn into a plunge.

**One G code per block is enforced by `motion_word()`**, which emits the motion code on its own block when the current block already carries one. `check()` fails any block with two G codes, so this is verified, not assumed.

**Helical arcs need the K word.** K is the Z pitch per full 360° turn, *not* the Z travel of the block. `do_arc()` computes it from the whole arc's sweep and then splits the arc into `HELIX_MAX_DEG` pieces that all carry the same K. `check()` re-derives the expected Z travel from K and each piece's sweep and flags disagreement — if you change how arcs are split, that cross-check is what will catch it.

**`BackplotParser` reads the converted output, not the input**, and must keep up with the converter's output shape: absolute I/J arcs (tessellated at `ARC_PLOT_DEG`), the native `G8x X Y Z(R) Z(depth) [Z(Q)]` block form taken from `CYCLE_TEMPLATES`, and modal XY continuation lines. Two deliberate inferences live there because the information is not in the output: M25's target Z is taken as the highest Z the program commands anywhere (pre-scanned in `_scan_z_home`), and a pure downward G01 Z move is coloured as a plunge (`_is_plunge`) so drilled holes stay visible under `CYCLE_MODE='expand'`.

**Word parsing goes through the `WORD` regex.** Output blocks have no spaces (`N30X0.Y1.Z-0.0375I0.J0.K-0.15`), so letter-plus-number tokenising is the only safe way to read them — not ad-hoc per-letter searches. `fnum()` writes a trailing bare decimal point (`X5.`, `Z-0.3`), which is intentional: the decimal point is what keeps the control from reading the value as machine units.

### Settings block

The constants at the top of `main.py` are the machine-specific knobs, and several encode assumptions that are flagged in comments as needing confirmation on the actual machine:

- `CYCLE_MODE` — `'expand'` (default) writes drilling as plain G00/G01 moves, matching the proven Fusion `hurcoBX.cps` post, which never sends G81–G85. `'native'` sends `G8x` blocks built from `CYCLE_TEMPLATES`. G84 tapping is always native.
- `CYCLE_TEMPLATES` — which Z words follow `G8x X.. Y..`. G83 taking three Z words (R plane, depth, peck) is known; **the order and the other cycles' templates are assumptions.**
- `HELIX_K` — `'signed'` (G75 mode, the power-up default) / `'unsigned'` (G74) / `None`. Without K the control gets helical Z motion wrong.
- `HELIX_MAX_DEG`, `ARC_MAX_DEG`, `LINEARIZE_TOL` — arc splitting and G18/G19 linearization tolerance.
- `ALLOWED_G` / `ALLOWED_M` — the BX-MPU vocabulary. Anything else is removed and reported as a warning, never raised.
- `MAX_TOOL`, `RPM_RANGE`, `BUFFER_CHARS` — machine limits that produce warnings only.

### Conversion behaviour

- Header is emitted unconditionally (`G00`, `G90`, `G70`/`G71`, `M25`) and the file is wrapped in `%` … `E` with N2, N4, … numbering, matching the AMTS sample program.
- `G28`/`G30`/`G53` Z retracts → `M25`; XY home moves are dropped with a warning.
- `G91` incremental moves and `G91.1` incremental I/J are resolved to absolute.
- R-format arcs are converted to I/J; arcs in G18/G19 become short G01 moves.
- Fanuc cycles with no Hurco equivalent are remapped via `CYCLE_MAP` (G73→G83, G74→G84, G76/G86–G89→G85) with a warning.
- Unsupported codes (G43, G49, G54, G94, G98, H and D words …) are stripped and reported.
- `M30`/`M02` triggers the end sequence; if the source has neither, one is appended with a warning.
- Saving from the GUI writes ASCII with CRLF line endings, which is what the control expects over the DNC link.

## Machine transfer (DNC)

The drip-feed boxes speak a protocol reverse-engineered from QS Explorer 4.06. It is TFTP-*shaped* but is not TFTP, and the differences are the part that bites:

- **Block numbers are 32-bit**, not TFTP's 16-bit. A stock TFTP library cannot talk to these devices.
- **Both ends use port 69** — there is no ephemeral TID negotiation. Binding source port 69 needs root on macOS and Linux, so `QSClient.open()` tries it `BIND_RETRIES` times and then falls back to an ephemeral port, reporting what it actually got in `local_port`. The browser surfaces that in its status line, because if the device turns out to insist on source port 69 that message is the only clue.
- **One client at a time.** Opcode `0x63` means another PC holds the device; it is raised as `DncBusy` from any call. `MainWindow._open_browser` keeps one browser per IP for the same reason.
- **Downloads need the file size up front.** There is no EOF marker, so the block count comes from the directory listing — `download_path()` does the lookup, `download()` takes the size.
- **`0x12 WaitACK` can precede any filesystem ACK.** `_command()` keeps waiting after one *without resending*, since re-sending a delete or rename could run it twice.
- **The device pushes unsolicited packets** (`0x1B` entered-DNC-mode, `0x18` startup-copy) that can land mid-exchange. Every wait loop steps over them and records them in `QSClient.notices`; the browser shows them in its message line.
- Strings are one byte per character, so **filenames must stay ASCII**. Paths use `\`, have no drive letter, and the root is the empty string — the `0:` in the UI is display only.

**No protocol call ever runs on the GUI thread.** Timeouts are 800 ms with up to 10 retries, so a call can block for seconds. `dncui.ClientThread` owns the socket and runs every call, taking work as `(tag, fn)` and emitting the tag back with the result; `MachineBrowserDialog` dispatches on the tag to a `_done_<tag>` method. Adding an operation means adding a `self._submit()` call and the matching `_done_` handler — never calling `QSClient` directly from a slot; `_submit()` refuses while the link is down, so a `_done_` handler only runs for a call that actually went out.

**A browser stays open whether or not the device answers.** The status line at the top of the dialog is the connection state — green "Connected to <name> at <ip>", red "Not connected to <name>" with the reason — and a failed connection greys out the controls and offers Reconnect instead of popping an error and closing the window. Three consecutive failed status polls (the 1 Hz tick, one in flight at a time) count as the device going away. Because a silent device costs retries × timeout seconds per call, `QSClient` takes a `should_stop` callback checked between attempts and raises `DncAborted`; that is what lets `ClientThread.shutdown()` return promptly when the dialog is closed mid-connect. A worker that still will not stop is cut loose with `setParent(None)` and deleted on `finished` — destroying a running `QThread` aborts the process.

The machine list is JSON in the per-user config directory (`machines.config_path()`), not in the repo. `machines.import_device_dat()` reads QS Explorer's own `Device.dat` for migration.

### Backplot colors

G00 rapid = red dashed, G01/G02/G03 feed = blue solid, plunge = green solid, origin = yellow dot. Axes are forced to equal scale by `_equalize_axes()` so geometry isn't distorted.

### Backplot performance

**Three artists, not one per segment.** mplot3d re-projects and re-draws every artist on every mouse-move frame, so plotting each segment with its own `ax.plot()` made rotation unusable — 0.8 fps at 18k segments, 0.12 fps at 112k. `_runs()` chains segments that share an endpoint into contiguous runs and `_flatten()` joins the runs into one point sequence per layer with NaN between them, which Matplotlib draws as a break. Keep it to one artist per layer; adding a per-segment or per-move artist is what regresses this.

`_flatten()` returns numpy arrays because `Line3D.set_data_3d` stores what it is handed and the 3-D draw indexes it as an array — lists raise `AttributeError: 'list' object has no attribute 'shape'` at draw time, not at call time.

**The canvas has to be told it may grow.** Matplotlib sets no size policy on `FigureCanvasQTAgg`, so it defaults to `Preferred` and the widget sits at the figure's 640x480 size hint however large the pane is — leaving the leftover height to whatever else is in the layout (it went to the `3D Backplot` label). `PlotCanvas.__init__` sets `Expanding` and the layout gives the canvas `stretch=1`.

**`tight_layout()` does not work on 3-D axes.** It warns that the margins cannot be made large enough and leaves the default 12% borders, which was most of the empty space around the plot. `_fit_figure()` sets the margins directly, in pixels rather than fractions (a fraction that clears the tick labels on a tall pane is a few pixels on a short one), and `resizeEvent` re-applies it. mplot3d's `apply_aspect()` forces the axes rectangle square whatever aspect is asked for, so on a pane wider than it is tall the plot is sized by the canvas *height* and the side margins only decide where the square sits. `_BOX_ZOOM` then enlarges the cube inside that square; past about 1.05 with these margins the X tick labels run off the bottom on a short wide pane.

Past `_DRAG_MAX_POINTS` the canvas swaps in a thinned copy of the path on `button_press_event` and restores full detail on release (`_on_press` / `_on_release`); both arrays are built once in `update_toolpath()`, so a drag only swaps references. Beyond roughly 30k points the frame time is bound by how many pixels the path covers rather than by its point count, so thinning harder than that buys nothing — measured, not assumed.
