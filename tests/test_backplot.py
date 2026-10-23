"""BackplotParser reads converted output into rapid / feed / plunge segments."""

import math

import numpy as np
import pytest

import main as m


def parse(src: str) -> m.BackplotParser:
    out, _ = m.convert(src)
    bp = m.BackplotParser()
    bp.parse(out)
    return bp


def test_sample_program_plots(sample_text):
    bp = parse(sample_text)
    assert bp.rapid and (bp.feed or bp.plunge)


def test_segments_join_end_to_start(sample_text):
    bp = parse(sample_text + '\nG2 X1 Y0 I-1 J0 F10')
    for layer in (bp.rapid, bp.feed, bp.plunge):
        for seg in layer:
            assert len(seg) == 6 and all(math.isfinite(v) for v in seg)


def test_helix_is_tessellated_on_the_circle():
    bp = parse('G0 X1 Y0 Z0\nG3 X1 Y0 Z-0.2 I-1 J0 F10\nM30')
    arc = [s for s in bp.feed]
    assert len(arc) >= 360 / m.ARC_PLOT_DEG
    for x0, y0, z0, x1, y1, z1 in arc:
        assert math.hypot(x1, y1) == pytest.approx(1, abs=1e-6)
        assert z1 <= z0 + 1e-12                      # always descending
    assert arc[-1][3:] == pytest.approx((1, 0, -0.2))


def test_expanded_drilling_is_drawn_as_plunges():
    bp = parse('G0 X1 Y1 Z0.5\nG81 X1 Y1 Z-0.25 R0.1 F5\nX2\nG80\nM30')
    assert [s[:2] for s in bp.plunge] == [(1, 1), (2, 1)]
    assert all(s[2] == pytest.approx(0.1) and s[5] == pytest.approx(-0.25)
               for s in bp.plunge)


def test_native_drilling_is_drawn_as_plunges(monkeypatch):
    monkeypatch.setattr(m, 'CYCLE_MODE', 'native')
    bp = parse('G0 X1 Y1 Z0.5\nG81 X1 Y1 Z-0.25 R0.1 F5\nX2\nG80\nM30')
    assert [s[:2] for s in bp.plunge] == [(1, 1), (2, 1)]


def test_m25_retracts_to_highest_commanded_z():
    bp = parse('G0 X0 Y0 Z2\nG1 Z-0.1 F5\nG28 G91 Z0\nG90 G0 X3 Y0\nM30')
    up = [s for s in bp.rapid if s[2] == pytest.approx(-0.1)][0]
    assert up[5] == pytest.approx(2)


def test_runs_and_flatten_make_one_array_per_layer():
    segs = [(0, 0, 0, 1, 0, 0), (1, 0, 0, 1, 1, 0), (5, 5, 0, 6, 5, 0)]
    runs = m.PlotCanvas._runs(segs)
    assert len(runs) == 2                           # the gap starts a new run
    xs, ys, zs = m.PlotCanvas._flatten(runs)
    assert isinstance(xs, np.ndarray)               # set_data_3d needs arrays
    assert np.isnan(xs).sum() == 1                  # one break between runs
