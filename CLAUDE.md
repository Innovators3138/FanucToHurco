# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Desktop GUI (PyQt6 + Matplotlib) that converts Onshape/Fanuc G-code into the Hurco BNC dialect spoken by an AMTS BX-MPU retrofit controller on a Hurco KMB-1 / MB-1 mill, and backplots the result in 3D. `Gcodes.pdf` is the authority for the BX-MPU vocabulary and output format; `program_out.nc` is a real Onshape post output used as a reference input sample.

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

## Architecture

Everything lives in [main.py](main.py), in five parts:

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

### Backplot colors

G00 rapid = red dashed, G01/G02/G03 feed = blue solid, plunge = green solid, origin = yellow dot. Axes are forced to equal scale by `_equalize_axes()` so geometry isn't distorted.
