#!/usr/bin/env python3
"""
FanucToHurco  —  Fanuc G-code → Hurco BNC Converter + 3D Backplot
Target hardware: AMTS BX-MPU retrofit on Hurco KMB-1 / MB-1

Converts "Generic 3-axis Fanuc" G-code (e.g. from Onshape CAM Studio) into the
legacy Hurco KMB-1 / AMTS BX-MPU NC format described in the AMTS G-code sheet
(Gcodes.pdf), then backplots the converted program in 3D.

What the converter does
  * Arc centers (I/J) converted to ABSOLUTE coordinates, and every arc block
    carries X, Y, I and J even when the Fanuc file omitted them.
  * Helical arcs get the K word (Z pitch per 360 degrees) the control needs.
  * R-format arcs converted to I/J.  Helical arcs split into small pieces.
  * Arcs in G18/G19 planes converted to short G01 moves.
  * Incremental (G91) moves converted to absolute.
  * G28/G53 Z retracts -> M25.  XY home moves dropped.
  * Canned cycles expanded into plain moves (default, like the Fusion
    hurcoBX.cps post) or rewritten using the Z-word templates below.
  * Unsupported codes (G43 G49 G54 G94 G98 H D ...) removed and reported.
  * Header/footer and N2, N4, ... numbering match the AMTS sample program.

Output format (per AMTS BX-MPU spec / Gcodes.pdf sample):
  %            <- first line, no N-number
  N2G00        <- even N-numbers, N-number glued directly to code, no spaces
  N4G90
  ...
  E            <- last line, no N-number

Dependencies:
    pip install PyQt6 matplotlib

PyInstaller (single-file, no console window):
    pyinstaller --onefile --windowed --name FanucToHurco \
        --hidden-import mpl_toolkits.mplot3d \
        --hidden-import matplotlib.backends.backend_qtagg \
        main.py
"""

import math
import re
import sys
from collections import OrderedDict
from typing import Dict, List, Optional, Tuple

# matplotlib backend MUST be set before any other matplotlib imports
import numpy as np
import matplotlib
matplotlib.use('QtAgg')

from matplotlib.backends.backend_qtagg import FigureCanvasQTAgg as FigureCanvas
from matplotlib.backends.backend_qtagg import NavigationToolbar2QT as NavigationToolbar
from matplotlib.figure import Figure
from matplotlib.lines import Line2D
from mpl_toolkits.mplot3d import Axes3D  # noqa: F401 — registers '3d' projection

from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget,
    QVBoxLayout, QHBoxLayout,
    QPushButton, QTextEdit, QSplitter,
    QStatusBar, QFileDialog, QLabel, QDialog, QMessageBox, QSizePolicy,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont, QAction

import dncui
import machines as mach


# ═════════════════════════ MACHINE SETTINGS ═════════════════════════════════
DEC = 4                 # decimals for X Y Z I J (inch)
FEED_DEC = 1            # decimals for F
N_START, N_STEP = 2, 2
HELIX_MAX_DEG = 90.0    # helical arcs are split into pieces no bigger than this
ARC_MAX_DEG = 360.0     # flat arcs: AMTS sample shows full circles are accepted
LINEARIZE_TOL = 0.0005  # chord error when converting G18/G19 arcs to lines

# Helical K word.  The Hurco BX/KMBX-1 control needs K on every G17 helical
# arc: the Z travel per full 360-degree turn (the pitch).
#   'signed'   -> negative when going down  (G75 mode, the power-up default)
#   'unsigned' -> always positive           (G74 mode)
#   None       -> no K (NOT recommended; Z motion goes wrong without it)
HELIX_K = 'signed'

# Canned cycles: which Z words follow "G8x X.. Y..".
#   'R' = retract/rapid plane (absolute)   'Z' = final depth (absolute)
#   'Q' = peck increment (positive number, times PECK_SIGN)
# >>> CONFIRM ON THE MACHINE.  G83 taking three Z words is known; the ORDER
# >>> below and the use of Z words for the other cycles are assumptions.
CYCLE_TEMPLATES = {
    81: ['R', 'Z'],
    82: ['R', 'Z'],
    83: ['R', 'Z', 'Q'],
    84: ['R', 'Z'],
    85: ['R', 'Z'],
}
PECK_SIGN = +1

# How canned cycles are sent.
#   'expand' -> write drilling as plain G00/G01 moves (what the proven Fusion
#               hurcoBX.cps post does; it never sends G81-G85 to the control)
#   'native' -> send G8x blocks using CYCLE_TEMPLATES above
# G84 tapping is always sent native (it cannot be safely expanded).
CYCLE_MODE = 'expand'
PECK_CLEARANCE = 0.01   # expanded G83: rapid back down to this far above last peck

KEEP_D_WORD = False     # hurcoBX.cps writes D on G41/G42; the AMTS sheet does not
EMIT_G75 = False        # G75 is the power-up default per AMTS; not on its valid-code list
MAX_TOOL = 24           # Hurco BX tool changer limit (hurcoBX.cps)
RPM_RANGE = (60, 4000)  # Hurco BX spindle range (hurcoBX.cps)

# Fanuc cycles with no Hurco equivalent are mapped to the nearest one (warned)
CYCLE_MAP = {73: 83, 74: 84, 76: 85, 86: 85, 87: 85, 88: 85, 89: 85}

ALLOWED_G = {0, 1, 2, 3, 4, 9, 17, 40, 41, 42, 61, 64, 70, 71, 75,
             80, 81, 82, 83, 84, 85, 90}
ALLOWED_M = {0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 25}

BUFFER_CHARS = 20000    # control's program buffer; above this it must drip-feed
# ════════════════════════════════════════════════════════════════════════════

WORD = re.compile(r'([A-Z])\s*([-+]?(?:\d+\.?\d*|\.\d+))')
EPS = 1e-6


def fnum(v, dec=DEC):
    s = f"{v:.{dec}f}".rstrip('0')
    return '0.' if s == '-0.' else s


def strip_comments(s):
    s = re.sub(r'\(.*?\)', '', s)
    return s.split(';', 1)[0]


# ─────────────────────────────────────────────────────────────────────────────
# Converter Engine
# ─────────────────────────────────────────────────────────────────────────────
class Converter:
    """
    Fanuc -> Hurco BNC state machine.

    Tracks absolute position, modal motion/plane/units and canned-cycle state
    so the output can be emitted in the one-G-code-per-block, absolute-only,
    absolute-I/J form the BX-MPU expects.
    """

    def __init__(self, helix_max=HELIX_MAX_DEG, metric=False):
        self.helix_max = helix_max
        self.body = []
        self.n = N_START
        self.warnings = OrderedDict()
        self.pos = {'X': 0.0, 'Y': 0.0, 'Z': None}
        self.absolute = True
        self.ij_absolute_in = False
        self.motion = 0
        self.plane = 17
        self.last_motion_out = None
        self.last_feed_out = None
        self.feed = None
        self.cycle = None
        self.cycle_return = 'init'
        self.spindle_on = False
        self.coolant_on = False
        self.metric = metric
        self.ended = False
        self.last_block = None
        self.lineno = 0
        self.raw = {}
        # header, as in the AMTS sample
        self.emit(['G00'])
        self.emit(['G90'])
        if EMIT_G75:
            self.emit(['G75'])   # multi-quadrant arcs; K is signed in this mode
        self.emit(['G71' if metric else 'G70'])
        self.emit_m25()

    # ---------------------------------------------------------------- output
    def warn(self, msg):
        self.warnings.setdefault(msg, []).append(self.lineno)

    def emit(self, words):
        block = ''.join(words)
        if not block:
            return
        self.body.append(f"N{self.n}{block}")
        self.n += N_STEP
        self.last_block = block

    def emit_m25(self):
        if self.last_block != 'M25':
            self.emit(['M25'])
        self.pos['Z'] = None
        self.last_motion_out = None

    def motion_word(self, code, words):
        """Add the motion G code if it changed. Keeps one G code per block."""
        if self.last_motion_out != code:
            if any(w.startswith('G') for w in words):
                self.emit([f"G{code:02d}"])
            else:
                words.insert(0, f"G{code:02d}")
            self.last_motion_out = code

    def feed_word(self, words):
        if self.feed is not None and self.feed != self.last_feed_out:
            words.append('F' + fnum(self.feed, FEED_DEC))
            self.last_feed_out = self.feed

    # ----------------------------------------------------------------- input
    def process(self, lineno, raw):
        self.lineno = lineno
        s = strip_comments(raw.strip().upper()).strip()
        if not s or s == '%' or self.ended:
            return
        if s.startswith('/'):
            self.warn("block-delete '/' ignored; block was kept")
            s = s[1:]
        if s.startswith('O') or s.startswith(':'):
            return
        leftover = WORD.sub('', s).strip()
        if leftover:
            self.warn(f"unreadable text dropped: {leftover!r}")
        raw_words = WORD.findall(s)
        self.raw = {L: v for L, v in raw_words}
        words = [(L, float(v)) for L, v in raw_words if L != 'N']
        gs = [round(v, 1) for L, v in words if L == 'G']
        ms = [int(v) for L, v in words if L == 'M']
        vals = {L: v for L, v in words if L not in 'GMN'}

        if 10 in gs or 65 in gs:
            self.warn("G10/G65 block dropped")
            return

        # ---- home / machine-coordinate retracts ----
        if any(g in gs for g in (28, 30, 53)):
            if 'Z' in vals or not ('X' in vals or 'Y' in vals):
                self.emit_m25()
            if 'X' in vals or 'Y' in vals:
                self.warn("XY home/machine move dropped (no Hurco equivalent)")
            self.do_m_codes(ms, vals, before=True)
            self.do_m_codes(ms, vals, before=False)
            return

        new_motion = None
        cycle_code = None
        comp = None
        solo = []
        for g in gs:
            gi = int(g) if g == int(g) else g
            if gi in (0, 1, 2, 3):
                new_motion = gi
            elif gi == 17:
                if self.plane != 17:
                    self.plane = 17
                solo.append('G17')
            elif gi in (18, 19):
                self.plane = gi
            elif gi in (20, 21):
                if (gi == 21) != self.metric:
                    self.metric = gi == 21
                    solo.append('G71' if self.metric else 'G70')
            elif gi == 90:
                self.absolute = True
            elif gi == 91:
                self.absolute = False
                self.warn("G91 incremental moves converted to absolute")
            elif gi == 90.1:
                self.ij_absolute_in = True
            elif gi == 91.1:
                self.ij_absolute_in = False
            elif gi in (40, 41, 42):
                comp = f"G{gi}"
            elif gi == 4:
                sec = self.dwell_seconds(vals)
                if sec is not None:
                    self.emit(['G04', 'P' + fnum(sec, 3)])
                return
            elif gi in (9, 61, 64, 75):
                solo.append(f"G{gi:02d}")
            elif gi == 80:
                self.cancel_cycle()
            elif gi in CYCLE_TEMPLATES:
                cycle_code = gi
            elif gi in CYCLE_MAP:
                self.warn(f"G{gi} not supported; mapped to G{CYCLE_MAP[gi]} - verify")
                cycle_code = CYCLE_MAP[gi]
            elif gi == 98:
                self.cycle_return = 'init'
            elif gi == 99:
                self.cycle_return = 'R'
            elif gi in (94,):
                pass
            elif gi == 95:
                self.warn("G95 feed-per-rev found: F values will be WRONG on the Hurco")
            else:
                self.warn(f"G{gi} not supported; removed")

        for w in solo:
            if self.last_block != w:
                self.emit([w])
        if 'H' in vals:
            vals.pop('H')
        dword = None
        if 'D' in vals:
            d = vals.pop('D')
            if KEEP_D_WORD and comp in ('G41', 'G42'):
                dword = f"D{int(d)}"
            elif not KEEP_D_WORD:
                self.warn("D (comp offset) word removed")
        if dword:
            comp = comp + '|' + dword
        if 'F' in vals:
            self.feed = vals.pop('F')

        self.do_m_codes(ms, vals, before=True)

        axes = any(a in vals for a in 'XYZ')
        arcw = any(a in vals for a in 'IJKR')
        if cycle_code is not None:
            self.do_cycle(cycle_code, vals, new_line=True)
        elif self.cycle and new_motion is None and (axes or 'R' in vals or 'Q' in vals):
            self.do_cycle(self.cycle['code'], vals, new_line=False)
        else:
            if new_motion is not None:
                if self.cycle:
                    self.cancel_cycle()
                self.motion = new_motion
            if axes or (self.motion in (2, 3) and arcw):
                if self.motion in (2, 3):
                    self.do_arc(vals, comp)
                else:
                    self.do_linear(vals, comp)
                comp = None
            if comp:
                self.emit([w for w in comp.split('|') if w])

        self.do_m_codes(ms, vals, before=False)

    # ------------------------------------------------------------- M, S, T
    def do_m_codes(self, ms, vals, before):
        if before:
            if 6 in ms:
                if 'T' not in vals:
                    self.warn("M06 without T word")
                t = int(vals.get('T', 0))
                if t > MAX_TOOL:
                    self.warn(f"tool T{t} is above the BX limit of {MAX_TOOL}")
                self.emit_m25()
                self.emit([f"T{t}", "M06"])
                self.emit_m25()
            elif 'T' in vals:
                self.warn("tool pre-select (T without M06) dropped")
            spin = [m for m in ms if m in (3, 4)]
            if 'S' in vals or spin:
                w = []
                if 'S' in vals:
                    rpm = int(round(vals['S']))
                    if rpm and not (RPM_RANGE[0] <= rpm <= RPM_RANGE[1]):
                        self.warn(f"S{rpm} outside BX spindle range {RPM_RANGE[0]}-{RPM_RANGE[1]}")
                    w.append(f"S{rpm}")
                if spin:
                    w.append(f"M{spin[0]:02d}")
                    self.spindle_on = True
                self.emit(w)
            for m in ms:
                if m in (7, 8):
                    self.emit([f"M{m:02d}"])
                    self.coolant_on = True
            return
        for m in ms:
            if m in (3, 4, 6, 7, 8):
                continue
            if m in (30, 2):
                self.finish()
                return
            if m == 5:
                self.emit(['M05']); self.spindle_on = False
            elif m == 9:
                self.emit(['M09']); self.coolant_on = False
            elif m == 25:
                self.emit_m25()
            elif m in ALLOWED_M:
                self.emit([f"M{m:02d}"])
            else:
                self.warn(f"M{m} not supported; removed")

    def finish(self):
        if self.cycle:
            self.cancel_cycle()
        self.emit_m25()
        self.emit(['G00'])
        self.last_motion_out = 0
        if self.coolant_on:
            self.emit(['M09']); self.coolant_on = False
        self.emit_m25()
        if self.spindle_on:
            self.emit(['M05']); self.spindle_on = False
        self.emit(['M02'])
        self.ended = True

    # --------------------------------------------------------------- motion
    def target(self, vals):
        t = dict(self.pos)
        for a in 'XYZ':
            if a in vals:
                if self.absolute:
                    t[a] = vals[a]
                else:
                    if self.pos[a] is None:
                        self.warn("incremental move from unknown Z (after M25) - check")
                        t[a] = vals[a]
                    else:
                        t[a] = self.pos[a] + vals[a]
        return t

    def do_linear(self, vals, comp):
        t = self.target(vals)
        words = []
        dword = None
        if comp:
            comp, _, dword = comp.partition('|')
            words.append(comp)
        for a in 'XYZ':
            if a in vals:
                words.append(a + fnum(t[a]))
        if dword:
            words.append(dword)
        self.motion_word(self.motion, words)
        if self.motion == 1:
            self.feed_word(words)
        self.emit(words)
        self.pos = t

    def do_arc(self, vals, comp):
        cw = self.motion == 2
        t = self.target(vals)
        # plane mapping: (u, v) arc axes, w linear axis, offset letters
        u, v, w, iu, iv = {17: ('X', 'Y', 'Z', 'I', 'J'),
                           18: ('Z', 'X', 'Y', 'K', 'I'),
                           19: ('Y', 'Z', 'X', 'J', 'K')}[self.plane]
        if self.pos[u] is None or self.pos[v] is None:
            self.warn("arc started from unknown position (after M25); skipped")
            return
        su, sv = self.pos[u], self.pos[v]
        eu, ev = t[u], t[v]
        if 'R' in vals:
            c = self.center_from_r(su, sv, eu, ev, vals['R'], cw)
            if c is None:
                return
            cu, cv = c
        elif iu in vals or iv in vals:
            ou, ov = vals.get(iu, 0.0), vals.get(iv, 0.0)
            if self.ij_absolute_in:
                cu, cv = ou, ov
            else:
                cu, cv = su + ou, sv + ov
        else:
            self.warn("arc with no I/J/R treated as a straight move")
            self.do_linear(vals, comp)
            return
        r0 = math.hypot(su - cu, sv - cv)
        r1 = math.hypot(eu - cu, ev - cv)
        if abs(r0 - r1) > 0.001:
            self.warn(f"arc radius mismatch {abs(r0-r1):.4f} in source")
        a0 = math.atan2(sv - cv, su - cu)
        a1 = math.atan2(ev - cv, eu - cu)
        sweep = (a0 - a1) if cw else (a1 - a0)
        sweep %= 2 * math.pi
        if sweep < 1e-9:
            sweep = 2 * math.pi
        sw0 = self.pos[w]
        sw1 = t[w]
        helical = sw0 is not None and sw1 is not None and abs(sw1 - sw0) > EPS
        sign = -1 if cw else 1

        if self.plane != 17:
            if sw0 is None and helical:
                self.warn("helix in G18/G19 from unknown position; skipped")
                return
            step = 2 * math.acos(max(-1.0, 1 - LINEARIZE_TOL / max(r0, 1e-9)))
            nseg = max(1, math.ceil(sweep / max(step, 1e-3)))
            self.warn(f"G{self.plane} arc converted to straight moves")
            for k in range(1, nseg + 1):
                p = dict(self.pos)
                if k == nseg:
                    p = dict(t)
                else:
                    ang = a0 + sign * sweep * k / nseg
                    p[u] = cu + r0 * math.cos(ang)
                    p[v] = cv + r0 * math.sin(ang)
                    if helical:
                        p[w] = sw0 + (sw1 - sw0) * k / nseg
                words = [a + fnum(p[a]) for a in 'XYZ' if p[a] is not None]
                self.motion_word(1, words)
                self.feed_word(words)
                self.emit(words)
                self.pos = p
            return

        limit = self.helix_max if helical else ARC_MAX_DEG
        nseg = max(1, math.ceil(math.degrees(sweep) / limit - 1e-9))
        kword = None
        if helical and HELIX_K:
            pitch = (sw1 - sw0) * 2 * math.pi / sweep
            if HELIX_K == 'unsigned':
                pitch = abs(pitch)
            kword = 'K' + fnum(pitch)
        for k in range(1, nseg + 1):
            if k == nseg:
                px, py, pz = eu, ev, sw1
            else:
                ang = a0 + sign * sweep * k / nseg
                px = cu + r0 * math.cos(ang)
                py = cv + r0 * math.sin(ang)
                pz = sw0 + (sw1 - sw0) * k / nseg if helical else sw1
            words = []
            if comp and k == 1:
                self.warn("cutter comp change on an arc; comp code put on its own block")
                self.emit([comp.split('|')[0]])
            words += ['X' + fnum(px), 'Y' + fnum(py)]
            if helical or ('Z' in vals and pz is not None):
                words.append('Z' + fnum(pz))
            words += ['I' + fnum(cu), 'J' + fnum(cv)]
            if kword:
                words.append(kword)
            self.motion_word(self.motion, words)
            self.feed_word(words)
            self.emit(words)
            self.pos = {'X': px, 'Y': py, 'Z': pz}

    def center_from_r(self, sx, sy, ex, ey, r, cw):
        dx, dy = ex - sx, ey - sy
        d = math.hypot(dx, dy)
        if d < EPS:
            self.warn("R-format full circle is impossible; block skipped")
            return None
        h2 = r * r - (d / 2) ** 2
        if h2 < -1e-6:
            self.warn("R too small for arc endpoints; block skipped")
            return None
        h = math.sqrt(max(h2, 0.0))
        nx, ny = -dy / d, dx / d          # left-hand normal of the chord
        side = 1 if not cw else -1        # small CCW arc: center on the left
        if r < 0:
            side = -side
        mx, my = (sx + ex) / 2, (sy + ey) / 2
        return mx + side * h * nx, my + side * h * ny

    def dwell_seconds(self, vals):
        """Fanuc: X = seconds, P = milliseconds unless written with a decimal."""
        if 'X' in vals:
            return vals['X']
        if 'P' in vals:
            raw = self.raw.get('P', '')
            return vals['P'] if '.' in raw else vals['P'] / 1000.0
        self.warn("dwell with no time dropped")
        return None

    # --------------------------------------------------------------- cycles
    def rapid_z(self, z):
        if self.pos['Z'] is None or abs(self.pos['Z'] - z) > EPS:
            w = ['Z' + fnum(z)]
            self.motion_word(0, w)
            self.emit(w)
            self.pos['Z'] = z

    def feed_z(self, z):
        w = ['Z' + fnum(z)]
        self.motion_word(1, w)
        self.feed_word(w)
        self.emit(w)
        self.pos['Z'] = z

    def expand_cycle(self, c, moved=True):
        code, r, depth = c['code'], c['R'], c['Z']
        if r is None or depth is None:
            self.warn(f"G{code} missing R or Z; hole skipped")
            return
        ret = c['init_z'] if (self.cycle_return == 'init' and c['init_z'] is not None) else r
        if moved:
            w = ['X' + fnum(self.pos['X']), 'Y' + fnum(self.pos['Y'])]
            self.motion_word(0, w)
            self.emit(w)
        self.rapid_z(r)
        if code == 83:
            q = c['Q'] or abs(r - depth)
            if not c['Q']:
                self.warn("G83 without Q: drilled in one peck")
            cur = r
            while cur > depth + EPS:
                nxt = max(depth, cur - q)
                if cur < r - EPS:
                    self.rapid_z(min(r, cur + PECK_CLEARANCE))
                self.feed_z(nxt)
                cur = nxt
                if cur > depth + EPS:
                    self.rapid_z(r)
        else:
            self.feed_z(depth)
            if code == 82 and c.get('P'):
                self.emit(['G04', 'P' + fnum(c['P'], 3)])
                self.last_motion_out = None
            if code == 85:
                self.feed_z(r)
        self.rapid_z(ret)

    def cancel_cycle(self):
        if not self.cycle:
            return
        if self.cycle.get('native'):
            self.emit(['G80'])
        c = self.cycle
        self.pos['Z'] = c['init_z'] if self.cycle_return == 'init' else c['R']
        self.cycle = None
        self.last_motion_out = None

    def do_cycle(self, code, vals, new_line):
        if self.cycle is None:
            self.cycle = {'code': code, 'init_z': self.pos['Z'],
                          'R': None, 'Z': None, 'Q': None, 'P': None, 'sent': None}
        c = self.cycle
        c['code'] = code
        if 'R' in vals:
            if self.absolute:
                c['R'] = vals['R']
            else:
                c['R'] = (c['init_z'] or 0.0) + vals['R']
        if 'Z' in vals:
            c['Z'] = vals['Z'] if self.absolute else (c['R'] or 0.0) + vals['Z']
        if 'Q' in vals:
            c['Q'] = abs(vals['Q'])
        if 'P' in vals:
            c['P'] = self.dwell_seconds(vals)
        native = CYCLE_MODE != 'expand' or code == 84
        c['native'] = native
        if not native:
            moved = False
            for a in 'XY':
                if a in vals:
                    new = vals[a] if self.absolute else self.pos[a] + vals[a]
                    moved |= self.pos[a] is None or abs(new - self.pos[a]) > EPS
                    self.pos[a] = new
            self.expand_cycle(c, moved)
            return
        if code == 84:
            self.warn("G84 tapping sent as a native cycle - verify format on the machine")
        if c.get('P'):
            self.warn(f"G{code} dwell dropped in native mode - Hurco cycle dwell word unknown")
        for a in 'XY':
            if a in vals:
                self.pos[a] = vals[a] if self.absolute else self.pos[a] + vals[a]

        tmpl = CYCLE_TEMPLATES[code]
        params = (code, c['R'], c['Z'], c['Q'], self.feed)
        words = ['X' + fnum(self.pos['X']), 'Y' + fnum(self.pos['Y'])]
        if params != c['sent']:
            words.insert(0, f"G{code}")
            for key in tmpl:
                val = c[key]
                if key == 'Q':
                    if val is None:
                        val = abs((c['R'] or 0) - (c['Z'] or 0))
                        self.warn(f"G{code} without Q: peck set to full depth")
                    val *= PECK_SIGN
                if val is None:
                    self.warn(f"G{code} missing {key} value")
                    continue
                words.append('Z' + fnum(val))
            if self.feed is not None:
                words.append('F' + fnum(self.feed, FEED_DEC))
                self.last_feed_out = self.feed
            c['sent'] = params
        self.emit(words)
        self.last_motion_out = None
        self.pos['Z'] = c['init_z'] if self.cycle_return == 'init' else c['R']

    # --------------------------------------------------------------- result
    def result(self):
        if not self.ended:
            self.lineno = 0
            self.warn("no M30/M02 in source; end sequence added")
            self.finish()
        return '\n'.join(['%'] + self.body + ['E']) + '\n'


def convert(text, helix_max=HELIX_MAX_DEG):
    """Fanuc text -> (hurco text, warnings OrderedDict{message: [source lines]})."""
    metric = bool(re.search(r'\bG21\b', strip_comments(text.upper())))
    conv = Converter(helix_max=helix_max, metric=metric)
    for i, line in enumerate(text.splitlines(), 1):
        conv.process(i, line)
    return conv.result(), conv.warnings


# ─────────────────────────────────────────────────────────────────────────────
# Independent output checker
# ─────────────────────────────────────────────────────────────────────────────
def check(text):
    """Independent check of a Hurco-format file. Returns (problems, stats)."""
    probs = []
    pos = {'X': None, 'Y': None, 'Z': None}
    motion = None
    worst = 0.0
    arcs = 0
    lines = text.splitlines()
    if not lines or lines[0].strip() != '%':
        probs.append("first line is not %")
    if not lines or lines[-1].strip() != 'E':
        probs.append("last line is not E")
    expect = N_START
    arc_zdir = 0          # Z direction within the current run of arc blocks
    for ln in lines[1:-1]:
        if not ln.strip():
            continue
        m = re.fullmatch(r'N(\d+)((?:[A-Z][-]?\d*\.?\d*)+)', ln.strip())
        if not m:
            probs.append(f"bad block: {ln}")
            continue
        if int(m.group(1)) != expect:
            probs.append(f"N sequence break at {ln}")
        expect = int(m.group(1)) + N_STEP
        w = [(L, float(v))
             for L, v in re.findall(r'([A-Z])(-?\d*\.?\d*)', m.group(2))
             if re.fullmatch(r'-?(?:\d+\.?\d*|\.\d+)', v)]
        gs = [int(v) for L, v in w if L == 'G']
        if re.search(r'G9[01]\.', ln):
            probs.append(f"G90.x/G91.x code (control may read it as G91): {ln}")
        if len(gs) > 1:
            probs.append(f"more than one G code: {ln}")
        for g in gs:
            if g not in ALLOWED_G:
                probs.append(f"unsupported G{g}: {ln}")
        for L, v in w:
            if L == 'M' and int(v) not in ALLOWED_M:
                probs.append(f"unsupported M{int(v)}: {ln}")
            if L not in 'NGMXYZIJKFSTPD':
                probs.append(f"unexpected word {L}: {ln}")
        vals = dict((L, v) for L, v in w if L not in 'GM')
        for g in gs:
            if g in (0, 1, 2, 3):
                motion = g
            if g in range(81, 86):
                motion = 'cycle'
            if g == 80:
                motion = None
        if any(v == 25 for L, v in w if L == 'M'):
            pos['Z'] = None
        if motion in (2, 3) and 'Z' in vals and pos['Z'] is not None:
            dz = vals['Z'] - pos['Z']
            if abs(dz) > 1e-9:
                d = 1 if dz > 0 else -1
                if arc_zdir and d != arc_zdir:
                    probs.append(f"Z reverses direction inside a helix: {ln}")
                arc_zdir = d
        elif motion not in (2, 3):
            arc_zdir = 0
        if motion in (2, 3) and ('I' in vals or 'J' in vals or 'X' in vals):
            arcs += 1
            miss = [a for a in 'XYIJ' if a not in vals]
            if miss:
                probs.append(f"arc missing {''.join(miss)}: {ln}")
                continue
            if pos['X'] is None:
                probs.append(f"arc from unknown position: {ln}")
            else:
                r0 = math.hypot(pos['X'] - vals['I'], pos['Y'] - vals['J'])
                r1 = math.hypot(vals['X'] - vals['I'], vals['Y'] - vals['J'])
                worst = max(worst, abs(r0 - r1))
                if abs(r0 - r1) > 0.0005:
                    probs.append(f"arc radius error {abs(r0-r1):.4f}: {ln}")
                dz = (vals['Z'] - pos['Z']) if ('Z' in vals and pos['Z'] is not None) else 0.0
                if abs(dz) > 1e-9 and HELIX_K:
                    if 'K' not in vals:
                        probs.append(f"helical arc without K: {ln}")
                    else:
                        a0 = math.atan2(pos['Y'] - vals['J'], pos['X'] - vals['I'])
                        a1 = math.atan2(vals['Y'] - vals['J'], vals['X'] - vals['I'])
                        sw = ((a0 - a1) if motion == 2 else (a1 - a0)) % (2 * math.pi) or 2 * math.pi
                        k = vals['K']
                        if HELIX_K == 'unsigned':
                            k = math.copysign(k, dz)
                        expect_dz = k * sw / (2 * math.pi)
                        if abs(expect_dz - dz) > 0.0005:
                            probs.append(f"K pitch disagrees with Z travel "
                                         f"({expect_dz:.4f} vs {dz:.4f}): {ln}")
        for a in 'XYZ':
            if a in vals and not (motion == 'cycle' and a == 'Z'):
                pos[a] = vals[a]
    return probs, {'arcs': arcs, 'worst_radius_error': round(worst, 5),
                   'characters': len(text), 'blocks': max(len(lines) - 2, 0)}


# ─────────────────────────────────────────────────────────────────────────────
# Backplot Parser
# ─────────────────────────────────────────────────────────────────────────────
Segment = Tuple[float, float, float, float, float, float]  # x0 y0 z0 x1 y1 z1

ARC_PLOT_DEG = 4.0   # chord resolution when drawing G02/G03 blocks


class BackplotParser:
    """
    Parses converted Hurco BNC output and builds segment lists:
      rapid  — G00                    red dashed
      feed   — G01 / G02 / G03        blue solid
      plunge — drilling Z-down moves  green solid

    Reads the same format the Converter writes: one G code per block, N-numbers
    glued to the content, absolute coordinates, absolute I/J arc centers.

    M25 has no coordinate in the output, so the Z it retracts to is inferred as
    the highest Z the program ever commands (pre-scanned) — enough to keep the
    plotted path connected and correctly ordered.
    """

    def __init__(self) -> None:
        self.rapid:  List[Segment] = []
        self.feed:   List[Segment] = []
        self.plunge: List[Segment] = []

    # ── Public ───────────────────────────────────────────────────────────────
    def parse(self, bnc: str) -> None:
        self.rapid, self.feed, self.plunge = [], [], []

        z_home = self._scan_z_home(bnc)
        x = y = 0.0
        z = z_home
        motion = 0
        cycle: Optional[Dict] = None   # native G8x state: {'code', 'r', 'depth'}

        for raw in bnc.splitlines():
            block = raw.strip()
            if not block or block in ('%', 'E'):
                continue

            words = self._words(block)
            if words is None:
                continue
            gs, ms, vals, zs = words

            # ── M25: retract to machine home ─────────────────────────────
            if 25 in ms:
                if abs(z - z_home) > EPS:
                    self.rapid.append((x, y, z, x, y, z_home))
                z = z_home
                cycle = None
                continue

            if 80 in gs:
                cycle = None

            for g in gs:
                if g in (0, 1, 2, 3):
                    motion = g

            # ── Native canned cycle: G8x X Y Z(R) Z(depth) [Z(Q)] ────────
            cyc = next((g for g in gs if g in CYCLE_TEMPLATES), None)
            if cyc is not None:
                tmpl = CYCLE_TEMPLATES[cyc]
                named = dict(zip(tmpl, zs))
                cycle = {'code': cyc, 'r': named.get('R'), 'depth': named.get('Z')}
                x, y, z = self._drill(x, y, z, vals, cycle)
                continue

            # ── Modal continuation of a native cycle (XY only, no G) ─────
            if cycle and not gs and ('X' in vals or 'Y' in vals):
                x, y, z = self._drill(x, y, z, vals, cycle)
                continue

            # ── Arcs ─────────────────────────────────────────────────────
            if motion in (2, 3) and 'I' in vals and 'J' in vals:
                nx = vals.get('X', x)
                ny = vals.get('Y', y)
                nz = vals.get('Z', z)
                for px, py, pz in self._arc_points(x, y, z, nx, ny, nz,
                                                   vals['I'], vals['J'],
                                                   cw=(motion == 2)):
                    self.feed.append((x, y, z, px, py, pz))
                    x, y, z = px, py, pz
                continue

            # ── Straight moves ───────────────────────────────────────────
            if not any(a in vals for a in 'XYZ'):
                continue
            nx = vals.get('X', x)
            ny = vals.get('Y', y)
            nz = vals.get('Z', z)
            seg: Segment = (x, y, z, nx, ny, nz)
            if motion == 0:
                self.rapid.append(seg)
            elif self._is_plunge(seg):
                # CYCLE_MODE='expand' turns drilling into plain G01 Z moves;
                # colour a pure downward feed as a plunge so holes stay visible.
                self.plunge.append(seg)
            else:
                self.feed.append(seg)
            x, y, z = nx, ny, nz

    # ── Internal ─────────────────────────────────────────────────────────────
    @staticmethod
    def _words(block: str):
        """Block -> (g codes, m codes, {letter: value}, [Z values in order])."""
        found = WORD.findall(block)
        if not found:
            return None
        gs = [int(round(float(v))) for L, v in found if L == 'G']
        ms = [int(round(float(v))) for L, v in found if L == 'M']
        vals = {L: float(v) for L, v in found if L not in 'GMN'}
        zs = [float(v) for L, v in found if L == 'Z']
        return gs, ms, vals, zs

    @staticmethod
    def _scan_z_home(bnc: str) -> float:
        zs = [float(m.group(1))
              for m in re.finditer(r'(?<![A-Z])Z([-+]?(?:\d+\.?\d*|\.\d+))', bnc)]
        return max(zs + [0.0])

    @staticmethod
    def _is_plunge(seg: Segment) -> bool:
        x0, y0, z0, x1, y1, z1 = seg
        return abs(x1 - x0) < EPS and abs(y1 - y0) < EPS and z1 < z0 - EPS

    def _drill(self, x, y, z, vals, cycle):
        """Draw one hole of a native canned cycle; returns the new position."""
        nx = vals.get('X', x)
        ny = vals.get('Y', y)
        if (nx, ny) != (x, y):
            self.rapid.append((x, y, z, nx, ny, z))
        r, depth = cycle.get('r'), cycle.get('depth')
        if r is None or depth is None:
            return nx, ny, z
        if abs(z - r) > EPS:
            self.rapid.append((nx, ny, z, nx, ny, r))
        self.plunge.append((nx, ny, r, nx, ny, depth))
        self.rapid.append((nx, ny, depth, nx, ny, r))
        return nx, ny, r

    @staticmethod
    def _arc_points(x0, y0, z0, x1, y1, z1, cx, cy, cw):
        """Tessellate one arc block into points, ending exactly on the endpoint."""
        a0 = math.atan2(y0 - cy, x0 - cx)
        a1 = math.atan2(y1 - cy, x1 - cx)
        sweep = (a0 - a1) if cw else (a1 - a0)
        sweep %= 2 * math.pi
        if sweep < 1e-9:
            sweep = 2 * math.pi          # start == end means a full circle
        r = math.hypot(x0 - cx, y0 - cy)
        n = max(2, math.ceil(math.degrees(sweep) / ARC_PLOT_DEG))
        sign = -1 if cw else 1
        pts = []
        for k in range(1, n + 1):
            if k == n:
                pts.append((x1, y1, z1))
            else:
                ang = a0 + sign * sweep * k / n
                pts.append((cx + r * math.cos(ang),
                            cy + r * math.sin(ang),
                            z0 + (z1 - z0) * k / n))
        return pts


# ─────────────────────────────────────────────────────────────────────────────
# 3-D Matplotlib Canvas Widget
# ─────────────────────────────────────────────────────────────────────────────
class PlotCanvas(FigureCanvas):
    """Embeddable Matplotlib 3-D axes for CNC toolpath visualisation."""

    _COL_RAPID  = '#ff4444'   # red   — G00 rapid
    _COL_FEED   = '#4488ff'   # blue  — G01/02/03 feed
    _COL_PLUNGE = '#44cc88'   # green — drilling plunge

    # Above this many plotted points, rotating swaps in a thinned copy of the
    # path for the duration of the drag; full detail comes back on release.
    _DRAG_MAX_POINTS = 30000

    # How much of the (always square) 3-D axes rectangle the cube fills.  Much
    # past this and the X tick labels run off the bottom of the canvas.
    _BOX_ZOOM = 1.05

    # Room for the tick labels, in pixels: (side, top, bottom).
    _MARGIN_PX = (22.0, 10.0, 30.0)

    def __init__(self, parent=None) -> None:
        self._fig = Figure(facecolor='#1e1e1e')
        super().__init__(self._fig)
        self.setParent(parent)
        # Matplotlib does not set a size policy on the canvas, so it defaults
        # to Preferred and the widget sits at the figure's 640x480 size hint
        # however much room the pane has.
        self.setSizePolicy(QSizePolicy.Policy.Expanding,
                           QSizePolicy.Policy.Expanding)
        self.setMinimumSize(240, 200)
        self.ax = self._fig.add_subplot(111, projection='3d')
        # One entry per drawn layer: [line, full points, thinned points|None].
        self._layers: List[list] = []
        self._coarse = False
        self.mpl_connect('button_press_event', self._on_press)
        self.mpl_connect('button_release_event', self._on_release)
        self._reset_axes()
        self.draw()

    # ── Public ───────────────────────────────────────────────────────────────
    def update_toolpath(self, parser: BackplotParser) -> None:
        self.ax.cla()
        self._reset_axes()
        self._layers = []
        self._coarse = False

        built = []      # (layer, runs) until we know how hard to thin

        def _draw(segs: List[Segment], color: str, ls: str, lw: float) -> None:
            # One artist per layer, not per segment: mplot3d re-projects and
            # re-draws every artist on every mouse-move frame, so a 20k-segment
            # program drawn segment by segment rotates at under a frame a
            # second.  The runs are chained into a single point sequence with
            # NaN breaks, which Matplotlib draws as gaps.
            runs = self._runs(segs)
            if not runs:
                return
            full = self._flatten(runs)
            line, = self.ax.plot(*full, color=color, linestyle=ls, linewidth=lw)
            layer = [line, full, None]
            self._layers.append(layer)
            built.append((layer, runs))

        _draw(parser.rapid,  self._COL_RAPID,  '--', 0.9)
        _draw(parser.feed,   self._COL_FEED,   '-',  1.2)
        _draw(parser.plunge, self._COL_PLUNGE, '-',  1.8)

        # Thin once, here, so a drag only has to swap in arrays that already
        # exist.  Beyond this size the redraw is bound by how many pixels the
        # path covers rather than by its point count, so thinning harder than
        # this buys nothing.
        total = sum(len(layer[1][0]) for layer in self._layers)
        if total > self._DRAG_MAX_POINTS:
            step = math.ceil(total / self._DRAG_MAX_POINTS)
            for layer, runs in built:
                layer[2] = self._flatten(runs, step)

        self.ax.scatter([0], [0], [0], color='#ffff44', s=30, zorder=5)

        handles: List[Line2D] = []
        if parser.rapid:
            handles.append(Line2D([0],[0], color=self._COL_RAPID,  ls='--',
                                  lw=0.9, label='G00  Rapid'))
        if parser.feed:
            handles.append(Line2D([0],[0], color=self._COL_FEED,   ls='-',
                                  lw=1.2, label='G01/02/03  Feed'))
        if parser.plunge:
            handles.append(Line2D([0],[0], color=self._COL_PLUNGE, ls='-',
                                  lw=1.8, label='Plunge'))
        if handles:
            self.ax.legend(handles=handles, loc='upper left', fontsize=7,
                           facecolor='#2b2b2b', labelcolor='#dddddd',
                           framealpha=0.85)

        self._equalize_axes(parser)
        self._fit_figure()
        self.draw()

    def resizeEvent(self, event) -> None:
        # Pixel margins have to be recomputed against the new canvas size.
        super().resizeEvent(event)
        self._fit_figure()

    def clear_plot(self) -> None:
        self.ax.cla()
        self._reset_axes()
        self._fit_figure()
        self._layers = []
        self._coarse = False
        self.draw()

    # ── drag handling ────────────────────────────────────────────────────────
    def _on_press(self, event) -> None:
        """Swap in the thinned path so the rotate stays responsive."""
        if self._coarse or event.inaxes is not self.ax:
            return
        if not any(layer[2] for layer in self._layers):
            return                      # small enough to rotate at full detail
        for line, _full, coarse in self._layers:
            if coarse:
                line.set_data_3d(*coarse)
        self._coarse = True              # Matplotlib's own rotate draws it

    def _on_release(self, _event) -> None:
        if not self._coarse:
            return
        for line, full, _coarse in self._layers:
            line.set_data_3d(*full)
        self._coarse = False
        self.draw_idle()

    # ── Internal ─────────────────────────────────────────────────────────────
    @staticmethod
    def _runs(segs: List[Segment]) -> List[Tuple[List, List, List]]:
        """
        Segments -> contiguous runs, each `(xs, ys, zs)`.

        Consecutive segments almost always share an endpoint, so a whole
        contour chains into one run; a segment that starts somewhere else
        begins a new one.
        """
        runs: List[Tuple[List, List, List]] = []
        xs = ys = zs = None
        px = py = pz = None
        for x0, y0, z0, x1, y1, z1 in segs:
            if (px is None or abs(x0 - px) > EPS or abs(y0 - py) > EPS
                    or abs(z0 - pz) > EPS):
                xs, ys, zs = [x0], [y0], [z0]
                runs.append((xs, ys, zs))
            xs.append(x1), ys.append(y1), zs.append(z1)
            px, py, pz = x1, y1, z1
        return runs

    @staticmethod
    def _flatten(runs: List[Tuple[List, List, List]], step: int = 1):
        """
        Runs -> one `(xs, ys, zs)` point sequence, NaN between runs.

        `step` keeps every nth point of each run, always keeping both of its
        ends so contours stay closed and the breaks stay in the same places.
        """
        nan = float('nan')
        out: Tuple[List, List, List] = ([], [], [])
        for run in runs:
            if step > 1 and len(run[0]) > 2:
                kept = [axis[::step] for axis in run]
                if (len(run[0]) - 1) % step:
                    for axis, keep in zip(run, kept):
                        keep.append(axis[-1])
            else:
                kept = list(run)
            if out[0]:
                for axis in out:
                    axis.append(nan)
            for axis, keep in zip(out, kept):
                axis += keep
        # Arrays, not lists: Line3D.set_data_3d stores what it is given and
        # the 3-D draw indexes it as an array.
        return tuple(np.asarray(axis, dtype=float) for axis in out)

    def _equalize_axes(self, parser: BackplotParser) -> None:
        """Force X, Y, Z to the same scale so geometry isn't distorted."""
        all_segs = parser.rapid + parser.feed + parser.plunge
        if not all_segs:
            return

        xs = [v for s in all_segs for v in (s[0], s[3])] + [0.0]
        ys = [v for s in all_segs for v in (s[1], s[4])] + [0.0]
        zs = [v for s in all_segs for v in (s[2], s[5])] + [0.0]

        half = max(
            max(xs) - min(xs),
            max(ys) - min(ys),
            max(zs) - min(zs),
            1e-6,           # guard against zero-range (single-point programs)
        ) / 2.0

        xm = (max(xs) + min(xs)) / 2
        ym = (max(ys) + min(ys)) / 2
        zm = (max(zs) + min(zs)) / 2

        self.ax.set_xlim3d(xm - half, xm + half)
        self.ax.set_ylim3d(ym - half, ym + half)
        self.ax.set_zlim3d(zm - half, zm + half)
        # Equal physical box (Matplotlib ≥ 3.3); `zoom` enlarges the cube
        # inside the axes rectangle and arrived in Matplotlib 3.6.
        try:
            self.ax.set_box_aspect([1, 1, 1], zoom=self._BOX_ZOOM)
        except TypeError:
            self.ax.set_box_aspect([1, 1, 1])

    def _fit_figure(self) -> None:
        """
        Give the axes the whole canvas bar a margin for the tick labels.

        `tight_layout()` cannot lay out 3-D axes — it warns that the margins
        cannot be made large enough and leaves the default 12% borders in
        place, which is most of the empty space around a backplot.

        mplot3d's `apply_aspect()` then forces the axes rectangle square
        whatever we ask for, so on a pane wider than it is tall the plot is
        sized by the canvas *height*; the side margins only decide where the
        square sits, and the default anchor centres it.

        The margins are in pixels, not fractions, because a fraction that
        clears the tick labels on a tall pane is a few pixels on a short one.
        """
        w = max(float(self._fig.bbox.width), 1.0)
        h = max(float(self._fig.bbox.height), 1.0)
        side, top, bottom = self._MARGIN_PX
        self._fig.subplots_adjust(
            left=min(side / w, 0.25), right=1.0 - min(side / w, 0.25),
            bottom=min(bottom / h, 0.25), top=1.0 - min(top / h, 0.25))

    def _reset_axes(self) -> None:
        self.ax.set_facecolor('#2b2b2b')
        self.ax.tick_params(colors='#888888', labelsize=6)
        self.ax.set_xlabel('X', color='#aaaaaa', fontsize=8, labelpad=3)
        self.ax.set_ylabel('Y', color='#aaaaaa', fontsize=8, labelpad=3)
        self.ax.set_zlabel('Z', color='#aaaaaa', fontsize=8, labelpad=3)
        for axis in (self.ax.xaxis, self.ax.yaxis, self.ax.zaxis):
            axis.pane.fill = False
            axis.pane.set_edgecolor('#404040')
        self.ax.grid(True, color='#404040', linewidth=0.4)


# ─────────────────────────────────────────────────────────────────────────────
# Dark style-sheet
# ─────────────────────────────────────────────────────────────────────────────
_DARK_STYLE = """
QMainWindow, QWidget {
    background: #2b2b2b;
    color: #d4d4d4;
}
QTextEdit {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid #444;
    selection-background-color: #264f78;
}
QLabel {
    color: #aaaaaa;
    font-size: 11px;
    font-weight: bold;
    padding: 2px 0px;
}
QPushButton {
    background: #3c3f41;
    color: #cccccc;
    border: 1px solid #555;
    border-radius: 4px;
    padding: 4px 18px;
    font-size: 12px;
    min-height: 28px;
}
QPushButton:hover   { background: #4c5052; color: #ffffff; }
QPushButton:pressed { background: #2d5a8e; }
QStatusBar {
    background: #1a1a1a;
    color: #88aacc;
    font-size: 11px;
}
QStatusBar::item { border: none; }
QSplitter::handle            { background: #444; }
QSplitter::handle:horizontal { width: 3px; }
QSplitter::handle:vertical   { height: 3px; }
QMenuBar { background: #2b2b2b; color: #d4d4d4; }
QMenuBar::item:selected { background: #3c3f41; }
QMenu { background: #2b2b2b; color: #d4d4d4; border: 1px solid #555; }
QMenu::item:selected   { background: #2d5a8e; }
QMenu::item:disabled   { color: #777777; }
QDialog { background: #2b2b2b; }
QLineEdit {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid #444;
    padding: 4px;
}
QListWidget, QTableWidget {
    background: #1e1e1e;
    color: #d4d4d4;
    border: 1px solid #444;
    gridline-color: #333;
}
QListWidget::item:selected, QTableWidget::item:selected { background: #2d5a8e; }
QHeaderView::section {
    background: #3c3f41;
    color: #cccccc;
    border: none;
    border-right: 1px solid #2b2b2b;
    padding: 3px 6px;
}
QProgressBar {
    background: #1e1e1e;
    border: 1px solid #444;
    text-align: center;
    color: #d4d4d4;
    max-height: 14px;
}
QProgressBar::chunk { background: #2d5a8e; }
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle('FanucToHurco  —  Hurco BNC Converter + 3D Backplot')
        self.resize(1440, 900)
        self.setStyleSheet(_DARK_STYLE)

        self._parser = BackplotParser()
        self._machines: List[mach.Machine] = mach.load()
        self._browsers: Dict[str, dncui.MachineBrowserDialog] = {}

        self._build_ui()
        self._build_menu()

    # ── Layout ───────────────────────────────────────────────────────────────
    def _build_ui(self) -> None:
        root = QWidget()
        self.setCentralWidget(root)
        vlay = QVBoxLayout(root)
        vlay.setContentsMargins(8, 8, 8, 4)
        vlay.setSpacing(6)

        # ── Top button row ────────────────────────────────────────────────
        btn_row = QHBoxLayout()
        self.btn_load    = QPushButton('Load G-Code')
        self.btn_convert = QPushButton('Convert  →')
        self.btn_verify  = QPushButton('Verify Output')
        self.btn_save    = QPushButton('Save BNC')
        self.btn_clear   = QPushButton('Clear')
        for btn in (self.btn_load, self.btn_convert, self.btn_verify,
                    self.btn_save, self.btn_clear):
            btn_row.addWidget(btn)
        btn_row.addStretch()
        vlay.addLayout(btn_row)

        # ── Three-panel splitter over a messages pane ─────────────────────
        split = QSplitter(Qt.Orientation.Horizontal)

        self.input_edit = self._make_editor(
            placeholder='Paste or load Fanuc G-code here…'
        )
        split.addWidget(self._panel('Input  (Fanuc G-Code)', self.input_edit))

        self.output_edit = self._make_editor()
        split.addWidget(self._panel('Output  (Hurco BNC)', self.output_edit))

        self.canvas = PlotCanvas()
        nav_toolbar = NavigationToolbar(self.canvas, self)
        cv_wrap = QWidget()
        cv_lay  = QVBoxLayout(cv_wrap)
        cv_lay.setContentsMargins(0, 0, 0, 0)
        cv_lay.setSpacing(2)
        cv_lay.addWidget(QLabel('3D Backplot'))
        cv_lay.addWidget(nav_toolbar)
        cv_lay.addWidget(self.canvas, stretch=1)   # all the leftover height
        split.addWidget(cv_wrap)

        split.setSizes([370, 370, 600])

        self.log_edit = self._make_editor(readonly=True)
        vsplit = QSplitter(Qt.Orientation.Vertical)
        vsplit.addWidget(split)
        vsplit.addWidget(self._panel('Messages', self.log_edit))
        vsplit.setSizes([620, 180])
        vlay.addWidget(vsplit, stretch=1)

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._msg('Ready — load a Fanuc G-code file or paste code to begin.')

        self.btn_load.clicked.connect(self._on_load)
        self.btn_convert.clicked.connect(self._on_convert)
        self.btn_verify.clicked.connect(self._on_verify)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_clear.clicked.connect(self._on_clear)

    # ── Machines menu ─────────────────────────────────────────────────────────
    def _build_menu(self) -> None:
        self._machine_menu = self.menuBar().addMenu('&Machines')
        self._refresh_machine_menu()

    def _refresh_machine_menu(self) -> None:
        """Rebuilt whenever the saved machine list changes."""
        menu = self._machine_menu
        menu.clear()

        manage = QAction('Manage Machines…', self)
        manage.triggered.connect(self._on_manage_machines)
        menu.addAction(manage)
        menu.addSeparator()

        if not self._machines:
            empty = QAction('No machines configured', self)
            empty.setEnabled(False)
            menu.addAction(empty)
        for machine in self._machines:
            act = QAction(f'Browse {machine.label()}…', self)
            act.triggered.connect(
                lambda _checked=False, m=machine: self._open_browser(m))
            menu.addAction(act)

        menu.addSeparator()
        send = QAction('Send Output to Machine…', self)
        send.setEnabled(bool(self._machines))
        send.triggered.connect(self._on_send_output)
        menu.addAction(send)

    def _on_manage_machines(self) -> None:
        dlg = dncui.MachineManagerDialog(self, self._machines)
        if dlg.exec() != QDialog.DialogCode.Accepted:
            return
        self._machines = dlg.machines()
        try:
            mach.save(self._machines)
        except OSError as exc:
            QMessageBox.warning(self, 'Could not save machines',
                                f'{mach.config_path()}\n\n{exc}')
        self._refresh_machine_menu()
        self._msg(f'{len(self._machines)} machine(s) saved to '
                  f'{mach.config_path()}')

    def _open_browser(self, machine: mach.Machine,
                      pending_upload=None) -> None:
        """One browser per machine — the device only accepts one client."""
        existing = self._browsers.get(machine.ip)
        if existing is not None and existing.isVisible():
            if pending_upload is not None:
                existing.queue_upload(*pending_upload)
            else:
                existing.raise_()
                existing.activateWindow()
                self._msg(f'{machine.label()} is already open.')
            return
        dlg = dncui.MachineBrowserDialog(machine, self, pending_upload)
        self._browsers[machine.ip] = dlg
        dlg.destroyed.connect(
            lambda _o=None, ip=machine.ip: self._browsers.pop(ip, None))
        dlg.show()

    def _on_send_output(self) -> None:
        text = self.output_edit.toPlainText()
        if not text.strip():
            self._msg('Nothing to send — run Convert first.')
            return
        machine = dncui.choose_machine(self, self._machines)
        if machine is None:
            return
        if not text.endswith('\n'):
            text += '\n'
        # Same bytes the Save button writes: ASCII with CRLF for the DNC link.
        data = text.replace('\n', '\r\n').encode('ascii', errors='replace')
        self._open_browser(machine, pending_upload=(data, 'PROGRAM.NC'))

    # ── UI helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _make_editor(placeholder: str = '', readonly: bool = False) -> QTextEdit:
        ed = QTextEdit()
        ed.setFont(QFont('Courier New', 10))
        if placeholder:
            ed.setPlaceholderText(placeholder)
        ed.setReadOnly(readonly)
        ed.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        return ed

    @staticmethod
    def _panel(title: str, widget: QWidget) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        lay.addWidget(QLabel(title))
        lay.addWidget(widget)
        return w

    def _msg(self, text: str) -> None:
        self._status.showMessage(text)

    def _log(self, lines: List[str]) -> None:
        self.log_edit.setPlainText('\n'.join(lines))

    @staticmethod
    def _stats_line(stats: Dict) -> str:
        return (f"{stats['blocks']} blocks, {stats['characters']} characters, "
                f"{stats['arcs']} arc blocks, worst arc radius error "
                f"{stats['worst_radius_error']}")

    def _report(self, warns, probs, stats) -> List[str]:
        out: List[str] = []
        for msg, where in (warns or {}).items():
            src = ', '.join(str(x) for x in where[:8] if x)
            more = f" (+{len(where)-8} more)" if len(where) > 8 else ''
            out.append(f"NOTE: {msg}" + (f"   [source lines {src}{more}]" if src else ''))
        if out:
            out.append('')
        out.append(self._stats_line(stats))
        if stats['characters'] > BUFFER_CHARS:
            out.append(f"Larger than the ~{BUFFER_CHARS:,} character buffer: the control "
                       "will drip-feed it, so keep the computer link running.")
        for p in probs:
            out.append(f"PROBLEM: {p}")
        if not probs:
            out.append('Check passed.')
        return out

    # ── Slots ─────────────────────────────────────────────────────────────────
    def _on_load(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self, 'Open Fanuc G-Code', '',
            'G-Code files (*.nc *.cnc *.gcode *.gc *.txt);;All files (*)'
        )
        if not path:
            return
        try:
            with open(path, encoding='utf-8', errors='replace') as fh:
                self.input_edit.setPlainText(fh.read())
            self._msg(f'Loaded: {path}')
        except OSError as exc:
            self._msg(f'Error loading: {exc}')

    def _on_convert(self) -> None:
        src = self.input_edit.toPlainText()
        if not src.strip():
            self._msg('Nothing to convert — paste or load G-code first.')
            return

        try:
            bnc, warns = convert(src, helix_max=HELIX_MAX_DEG)
            probs, stats = check(bnc)
        except Exception as exc:                     # keep the GUI alive
            self._log([f'CONVERSION FAILED: {exc!r}'])
            self._msg(f'Conversion failed: {exc}')
            return

        self.output_edit.setPlainText(bnc)
        self._parser.parse(bnc)
        self.canvas.update_toolpath(self._parser)

        self._log(self._report(warns, probs, stats))

        n_segs = (len(self._parser.rapid) +
                  len(self._parser.feed) +
                  len(self._parser.plunge))
        parts = [f"{stats['blocks']} blocks",
                 f'{len(warns)} note(s)',
                 f'{n_segs} plot segment(s)']
        parts.append('check passed' if not probs else f'{len(probs)} PROBLEM(S)')
        self._msg('  |  '.join(parts))

    def _on_verify(self) -> None:
        text = self.output_edit.toPlainText()
        if not text.strip():
            self._msg('Nothing to verify — convert or paste a Hurco file first.')
            return
        try:
            probs, stats = check(text)
            self._parser.parse(text)
        except Exception as exc:
            self._log([f'CHECK FAILED: {exc!r}'])
            self._msg(f'Check failed: {exc}')
            return
        self.canvas.update_toolpath(self._parser)
        self._log(self._report(None, probs, stats))
        self._msg(f"{stats['blocks']} blocks  |  " +
                  ('check passed' if not probs else f'{len(probs)} PROBLEM(S)'))

    def _on_save(self) -> None:
        text = self.output_edit.toPlainText()
        if not text.strip():
            self._msg('Nothing to save — run Convert first.')
            return
        path, _ = QFileDialog.getSaveFileName(
            self, 'Save Hurco BNC', '',
            'BNC / NC files (*.bnc *.nc *.txt);;All files (*)'
        )
        if not path:
            return
        if not text.endswith('\n'):
            text += '\n'
        try:
            # CRLF: what the control expects over the serial/DNC link
            with open(path, 'w', encoding='ascii', errors='replace',
                      newline='\r\n') as fh:
                fh.write(text)
            self._msg(f'Saved: {path}')
        except OSError as exc:
            self._msg(f'Error saving: {exc}')

    def _on_clear(self) -> None:
        self.input_edit.clear()
        self.output_edit.clear()
        self.log_edit.clear()
        self.canvas.clear_plot()
        self._msg('Cleared.')


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    app.setApplicationName('FanucToHurco')
    app.setOrganizationName('FanucToHurco')
    app.setStyleSheet(_DARK_STYLE)   # on the app, so dialogs are styled too
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
