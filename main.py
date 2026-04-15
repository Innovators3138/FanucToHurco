#!/usr/bin/env python3
"""
FanucToHurco  —  Fanuc G-code → Hurco BNC Converter + 3D Backplot
Target hardware: AMTS BX-MPU retrofit on Hurco MB-1

Output format (per AMTS BX-MPU spec / Gcodes.pdf sample):
  %            ← first line, no N-number
  N2G00        ← even N-numbers, N-number glued directly to code
  N4G90
  ...
  E            ← last line, no N-number

Dependencies:
    pip install PyQt6 matplotlib

PyInstaller (single-file, no console window):
    pyinstaller --onefile --windowed --name FanucToHurco \
        --hidden-import mpl_toolkits.mplot3d \
        --hidden-import matplotlib.backends.backend_qtagg \
        main.py
"""

import sys
import re
from typing import Dict, List, Optional, Tuple

# matplotlib backend MUST be set before any other matplotlib imports
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
    QStatusBar, QFileDialog, QLabel,
)
from PyQt6.QtCore import Qt
from PyQt6.QtGui import QFont


# ─────────────────────────────────────────────────────────────────────────────
# Valid Hurco BNC vocabulary  (Gcodes.pdf)
# ─────────────────────────────────────────────────────────────────────────────
VALID_G: set = {
    'G00', 'G01', 'G02', 'G03', 'G04', 'G09',
    'G17', 'G19',
    'G40', 'G41', 'G42',
    'G61', 'G64',
    'G70', 'G71',
    'G80', 'G81', 'G82', 'G83', 'G84', 'G85',
    'G90', 'G91',
}
VALID_M: set = {
    'M00', 'M01', 'M02', 'M03', 'M04', 'M05',
    'M06', 'M07', 'M08', 'M09', 'M25',
}
# G-codes stripped entirely (work offsets, tool-length comp)
STRIP_G: set = {'G54', 'G55', 'G56', 'G57', 'G58', 'G59', 'G43'}


# ─────────────────────────────────────────────────────────────────────────────
# Module-level helpers  (used by both converter and backplot parser)
# ─────────────────────────────────────────────────────────────────────────────
def _word(line: str, letter: str) -> Optional[float]:
    """First numeric value following *letter* in *line*, or None."""
    m = re.search(rf'(?<![A-Za-z]){re.escape(letter)}([+-]?\d*\.?\d+)',
                  line, re.IGNORECASE)
    return float(m.group(1)) if m else None


def _all_z(line: str) -> List[float]:
    """All Z-values in *line*, left-to-right."""
    return [float(m.group(1))
            for m in re.finditer(r'(?<![A-Za-z])Z([+-]?\d*\.?\d+)',
                                 line, re.IGNORECASE)]


def _fmt(v: float) -> str:
    """Float → string with at least one decimal place, no trailing zeros."""
    s = f'{v:.4f}'.rstrip('0')
    if '.' not in s:
        s += '.0'
    elif s.endswith('.'):
        s += '0'
    return s


def _has_g(line: str) -> bool:
    """True if *line* contains at least one G-code word."""
    return bool(re.search(r'(?<![A-Za-z\d])G\d+', line, re.IGNORECASE))


def _strip_n(line: str) -> str:
    """Remove a leading Fanuc N-number (e.g. 'N10 ' or 'N10')."""
    return re.sub(r'^N\d+\s*', '', line, flags=re.IGNORECASE)


# ─────────────────────────────────────────────────────────────────────────────
# Converter Engine
# ─────────────────────────────────────────────────────────────────────────────
class FanucConverter:
    """
    Converts Fanuc / Onshape G-code to Hurco BNC for the AMTS BX-MPU retrofit.

    Processing order per line:
      1.  Strip bare % and O-number (program ID) lines
      2.  Strip input N-numbers and (parenthesis) / ; comments
      3.  G28 Z0  →  M25
      4.  G80     →  clear modal canned-cycle state
      5.  G20/G21 →  G70/G71
      6.  Strip G54-G59, G43, H-words
      7.  Modal canned-cycle continuation (position-only line while cycle active)
      8.  Canned-cycle 3-Z rewrite for G81-G85 (sets modal cycle state)
      9.  Decimal enforcement on bare-integer X / Y / Z
      10. Split into one-G-code-per-line segments
      11. Vocabulary validation

    Output format:
      %           first line
      N2…         even N-numbers glued to content
      E           last line
    """

    def __init__(self) -> None:
        self.errors: List[str] = []
        self.info:   List[str] = []
        self._modal_cycle: Optional[Dict] = None  # active canned-cycle params

    # ── Public ───────────────────────────────────────────────────────────────
    def convert(self, fanuc: str) -> str:
        self.errors, self.info = [], []
        self._modal_cycle = None

        content: List[str] = []  # raw content lines before N-numbering

        for n, raw in enumerate(fanuc.splitlines(), 1):
            content.extend(self._process_line(raw, n))

        # Trim trailing blanks; guarantee file ends with E
        while content and not content[-1].strip():
            content.pop()
        if not content or content[-1].strip() != 'E':
            content.append('E')

        # Build final output: % header → N-numbered lines → E
        out = ['%']
        n_num = 2
        for line in content:
            stripped = line.strip()
            if not stripped:
                continue
            if stripped == 'E':
                out.append('E')
            else:
                out.append(f'N{n_num}{line}')
                n_num += 2

        return '\n'.join(out)

    # ── Per-line processing (returns 0-N output lines) ───────────────────────
    def _process_line(self, raw: str, n: int) -> List[str]:

        # ── 1. Drop bare program delimiters and O-number headers ─────────
        stripped_raw = raw.strip()
        if stripped_raw in ('%', '%%'):
            return []
        # O-number lines (Fanuc program ID): O1234, O(NAME)
        if re.match(r'^O[\d(]', stripped_raw, re.IGNORECASE):
            return []

        # ── 2. Strip input N-number; remove comments ──────────────────────
        line = _strip_n(stripped_raw)
        line = re.sub(r'\([^)]*\)', '', line)   # (parenthesis comments)
        line = re.sub(r';.*$', '', line).strip()
        if not line:
            return []

        # ── 3. G28 Z0  →  M25 ────────────────────────────────────────────
        if re.match(r'G28\s*Z0\.?0*\s*$', line, re.IGNORECASE):
            self.info.append(f'L{n}: G28 Z0 → M25')
            self._modal_cycle = None
            return ['M25']

        # ── 4. G80 clears modal canned cycle ─────────────────────────────
        if re.search(r'(?<![A-Za-z\d])G80(?!\d)', line, re.IGNORECASE):
            self._modal_cycle = None
            # G80 still gets emitted as a normal line below

        # ── 5. Unit-mode swaps ────────────────────────────────────────────
        line = re.sub(r'(?<![A-Za-z\d])G20(?!\d)', 'G70', line, flags=re.IGNORECASE)
        line = re.sub(r'(?<![A-Za-z\d])G21(?!\d)', 'G71', line, flags=re.IGNORECASE)

        # ── 6. Strip work offsets, tool-length comp, H-words ─────────────
        for code in STRIP_G:
            if re.search(rf'(?<![A-Za-z\d]){code}(?!\d)', line, re.IGNORECASE):
                line = re.sub(rf'(?<![A-Za-z\d]){code}(?!\d)', '',
                              line, flags=re.IGNORECASE)
                self.info.append(f'L{n}: stripped {code}')

        if re.search(r'(?<![A-Za-z])H\d+', line, re.IGNORECASE):
            line = re.sub(r'(?<![A-Za-z])H\d+', '', line, flags=re.IGNORECASE)
            self.info.append(f'L{n}: stripped H-word')

        line = line.strip()
        if not line:
            return []

        # ── 7. Modal canned-cycle continuation ────────────────────────────
        # A position-only line (no G-code) while a canned cycle is modal.
        # The Hurco carries Z/F forward modally, so only the new XY position
        # is needed.  The backplot parser's modal_cycle tracker draws the plunge.
        if (not _has_g(line)
                and self._modal_cycle is not None
                and (_word(line, 'X') is not None or _word(line, 'Y') is not None)):
            return [self._enforce_decimals(line)]

        # ── 8. Canned-cycle 3-Z rewrite ───────────────────────────────────
        converted = self._convert_canned(line, n)
        if converted is not None:
            return [converted]  # canned-cycle output is always valid vocabulary

        # ── 9. Decimal enforcement ────────────────────────────────────────
        line = self._enforce_decimals(line)

        # ── 10. Split multi-G-code lines → one G per line ─────────────────
        segments = self._split_gcodes(line)

        # ── 11. Sanitize: strip any G/M codes not in the allowed vocabulary ─
        sanitized = [self._sanitize(s, n) for s in segments]

        return [s for s in sanitized if s.strip()]

    # ── Canned-cycle rewrite ─────────────────────────────────────────────────
    def _convert_canned(self, line: str, n: int) -> Optional[str]:
        """
        Rewrite a Fanuc canned-cycle line to Hurco 3-Z format and save modal state.
        Returns None if the line is not a canned cycle.

        Fanuc:  G83 X Y Z[depth]  R[start]  Q[peck]  F
        Hurco:  G83 X Y Z[start]  Z[depth]  Z[peck]  F
        """
        f_val = _word(line, 'F')
        fstr  = f' F{_fmt(f_val)}' if f_val is not None else ''
        xy    = self._xy_str(line)

        def _g(code: str) -> bool:
            return bool(re.search(
                rf'(?<![A-Za-z\d]){code}(?!\d)', line, re.IGNORECASE))

        # G83 — peck drill
        if _g('G83'):
            z, r, q = _word(line,'Z'), _word(line,'R'), _word(line,'Q')
            if None in (z, r, q):
                self.errors.append(f'L{n}: G83 missing Z/R/Q')
                return self._enforce_decimals(line)
            self._modal_cycle = {'code':'G83','r':r,'depth':z,'q':q,'f':f_val}
            result = f'G83{xy} Z{_fmt(r)} Z{_fmt(z)} Z{_fmt(q)}{fstr}'
            self.info.append(f'L{n}: G83 3-Z → Z{_fmt(r)} / Z{_fmt(z)} / Z{_fmt(q)}')
            return result

        # G81 — drill, no dwell
        if _g('G81'):
            z, r = _word(line,'Z'), _word(line,'R')
            if None in (z, r):
                self.errors.append(f'L{n}: G81 missing Z/R')
                return self._enforce_decimals(line)
            self._modal_cycle = {'code':'G81','r':r,'depth':z,'f':f_val}
            result = f'G81{xy} Z{_fmt(r)} Z{_fmt(z)}{fstr}'
            self.info.append(f'L{n}: G81 3-Z → Z{_fmt(r)} / Z{_fmt(z)}')
            return result

        # G82 — drill with dwell
        if _g('G82'):
            z, r = _word(line,'Z'), _word(line,'R')
            if None in (z, r):
                self.errors.append(f'L{n}: G82 missing Z/R')
                return self._enforce_decimals(line)
            p = _word(line,'P')
            self._modal_cycle = {'code':'G82','r':r,'depth':z,'p':p,'f':f_val}
            pstr = f' P{_fmt(p)}' if p is not None else ''
            result = f'G82{xy} Z{_fmt(r)} Z{_fmt(z)}{pstr}{fstr}'
            self.info.append(f'L{n}: G82 3-Z → Z{_fmt(r)} / Z{_fmt(z)}')
            return result

        # G84 — tapping
        if _g('G84'):
            z, r = _word(line,'Z'), _word(line,'R')
            if None in (z, r):
                self.errors.append(f'L{n}: G84 missing Z/R')
                return self._enforce_decimals(line)
            self._modal_cycle = {'code':'G84','r':r,'depth':z,'f':f_val}
            result = f'G84{xy} Z{_fmt(r)} Z{_fmt(z)}{fstr}'
            self.info.append(f'L{n}: G84 3-Z → Z{_fmt(r)} / Z{_fmt(z)}')
            return result

        # G85 — boring
        if _g('G85'):
            z, r = _word(line,'Z'), _word(line,'R')
            if None in (z, r):
                self.errors.append(f'L{n}: G85 missing Z/R')
                return self._enforce_decimals(line)
            self._modal_cycle = {'code':'G85','r':r,'depth':z,'f':f_val}
            result = f'G85{xy} Z{_fmt(r)} Z{_fmt(z)}{fstr}'
            self.info.append(f'L{n}: G85 3-Z → Z{_fmt(r)} / Z{_fmt(z)}')
            return result

        return None  # not a canned cycle

    # ── Static helpers ────────────────────────────────────────────────────────
    @staticmethod
    def _enforce_decimals(line: str) -> str:
        """Append .0 to bare-integer X/Y/Z values (e.g. X5 → X5.0)."""
        return re.sub(
            r'(?<![A-Za-z])([XYZ])([+-]?\d+)(?![\d.])',
            lambda m: f'{m.group(1)}{m.group(2)}.0',
            line,
            flags=re.IGNORECASE,
        )

    @staticmethod
    def _xy_str(line: str) -> str:
        """Extract X and Y words, formatting values with _fmt for decimal safety."""
        parts: List[str] = []
        for letter in ('X', 'Y'):
            m = re.search(rf'(?<![A-Za-z]){letter}([+-]?\d*\.?\d+)',
                          line, re.IGNORECASE)
            if m:
                parts.append(f'{letter}{_fmt(float(m.group(1)))}')
        return (' ' + ' '.join(parts)) if parts else ''

    @staticmethod
    def _split_gcodes(line: str) -> List[str]:
        """
        If *line* contains more than one G-code, split it so each output line
        has exactly one G-code.  Non-G parameters (X,Y,Z,F,S,T,I,J,R,Q,P,M)
        stay with the G-code they immediately follow.
        """
        g_count = len(re.findall(r'(?<![A-Za-z\d])G\d+', line, re.IGNORECASE))
        if g_count <= 1:
            return [line.strip()] if line.strip() else []

        # Split at every G-code boundary using a zero-width lookahead
        parts = re.split(r'(?=(?<![A-Za-z\d])G\d)', line, flags=re.IGNORECASE)
        return [p.strip() for p in parts if p.strip()]

    def _sanitize(self, line: str, n: int) -> str:
        """
        Remove any G/M codes not in the allowed Hurco vocabulary and log them.
        Returns the cleaned line (may be empty if the whole line was invalid).
        """
        def drop_g(m: re.Match) -> str:
            code = f'G{int(m.group(1)):02d}'
            if code not in VALID_G:
                self.errors.append(f'L{n}: removed unsupported {code}')
                return ''
            return m.group(0)

        def drop_m(m: re.Match) -> str:
            code = f'M{int(m.group(1)):02d}'
            if code not in VALID_M:
                self.errors.append(f'L{n}: removed unsupported {code}')
                return ''
            return m.group(0)

        line = re.sub(r'(?<![A-Za-z\d])G(\d+)', drop_g, line, flags=re.IGNORECASE)
        line = re.sub(r'(?<![A-Za-z\d])M(\d+)', drop_m, line, flags=re.IGNORECASE)
        return ' '.join(line.split())  # collapse any leftover whitespace


# ─────────────────────────────────────────────────────────────────────────────
# Backplot Parser
# ─────────────────────────────────────────────────────────────────────────────
Segment = Tuple[float, float, float, float, float, float]  # x0 y0 z0 x1 y1 z1


class BackplotParser:
    """
    Parses converted Hurco BNC output (with N-numbers) and builds segment lists:
      rapid  — G00        red dashed
      feed   — G01/02/03  blue solid
      plunge — G81-G85    green solid (vertical plunge to depth)
    """

    def __init__(self) -> None:
        self.rapid:  List[Segment] = []
        self.feed:   List[Segment] = []
        self.plunge: List[Segment] = []

    def parse(self, bnc: str) -> None:
        self.rapid, self.feed, self.plunge = [], [], []

        x = y = z = 0.0
        modal_motion = 'G00'
        modal_cycle: Optional[Dict] = None   # mirrors converter's _modal_cycle

        for raw in bnc.splitlines():
            # Strip leading N-number prefix (e.g. "N10G01..." → "G01...")
            line = re.sub(r'^N\d+', '', raw.strip())
            line = line.strip()

            if not line or line == 'E' or line == '%':
                continue

            # ── Update linear/arc modal ──────────────────────────────────
            for pat, code in (
                (r'(?<![A-Za-z\d])G00(?!\d)', 'G00'),
                (r'(?<![A-Za-z\d])G01(?!\d)', 'G01'),
                (r'(?<![A-Za-z\d])G02(?!\d)', 'G02'),
                (r'(?<![A-Za-z\d])G03(?!\d)', 'G03'),
            ):
                if re.search(pat, line, re.IGNORECASE):
                    modal_motion = code
                    break

            # ── G80 cancels canned cycle ─────────────────────────────────
            if re.search(r'(?<![A-Za-z\d])G80(?!\d)', line, re.IGNORECASE):
                modal_cycle = None

            # ── Canned cycles (3-Z BNC format) ───────────────────────────
            cm = re.search(r'(?<![A-Za-z\d])G8([1-5])(?!\d)', line, re.IGNORECASE)
            if cm:
                nx = _word(line, 'X')
                ny = _word(line, 'Y')
                zs = _all_z(line)   # [Z_start(R-plane), Z_depth, Z_peck?]

                nx = nx if nx is not None else x
                ny = ny if ny is not None else y

                # Store for any subsequent modal continuation lines
                if len(zs) >= 2:
                    modal_cycle = {'r': zs[0], 'depth': zs[1]}

                # Rapid move to XY position
                if (nx, ny) != (x, y):
                    self.rapid.append((x, y, z, nx, ny, z))

                if len(zs) >= 2:
                    z_r = zs[0]  # R-plane
                    z_d = zs[1]  # final depth
                    self.rapid.append((nx, ny, z,   nx, ny, z_r))  # to R-plane
                    self.plunge.append((nx, ny, z_r, nx, ny, z_d)) # plunge
                    self.rapid.append((nx, ny, z_d, nx, ny, z_r))  # retract
                    z = z_r
                elif len(zs) == 1:
                    self.plunge.append((nx, ny, z, nx, ny, zs[0]))
                    z = zs[0]

                x, y = nx, ny
                continue

            # ── Modal canned cycle continuation (position-only line) ──────
            # Handles any lines the converter may have expanded, and provides
            # a safety net if the user feeds partially-converted code.
            if (modal_cycle is not None
                    and not _has_g(line)
                    and (_word(line, 'X') is not None or _word(line, 'Y') is not None)):
                nx = _word(line, 'X')
                ny = _word(line, 'Y')
                nx = nx if nx is not None else x
                ny = ny if ny is not None else y

                if (nx, ny) != (x, y):
                    self.rapid.append((x, y, z, nx, ny, z))

                z_r = modal_cycle['r']
                z_d = modal_cycle['depth']
                self.rapid.append((nx, ny, z,   nx, ny, z_r))
                self.plunge.append((nx, ny, z_r, nx, ny, z_d))
                self.rapid.append((nx, ny, z_d, nx, ny, z_r))
                z = z_r
                x, y = nx, ny
                continue

            # ── Regular motion ────────────────────────────────────────────
            nx = _word(line, 'X')
            ny = _word(line, 'Y')
            nz = _word(line, 'Z')

            if nx is None and ny is None and nz is None:
                continue

            nx = nx if nx is not None else x
            ny = ny if ny is not None else y
            nz = nz if nz is not None else z

            seg: Segment = (x, y, z, nx, ny, nz)
            if modal_motion == 'G00':
                self.rapid.append(seg)
            else:
                self.feed.append(seg)

            x, y, z = nx, ny, nz


# ─────────────────────────────────────────────────────────────────────────────
# 3-D Matplotlib Canvas Widget
# ─────────────────────────────────────────────────────────────────────────────
class PlotCanvas(FigureCanvas):
    """Embeddable Matplotlib 3-D axes for CNC toolpath visualisation."""

    _COL_RAPID  = '#ff4444'   # red   — G00 rapid
    _COL_FEED   = '#4488ff'   # blue  — G01/02/03 feed
    _COL_PLUNGE = '#44cc88'   # green — canned-cycle plunge

    def __init__(self, parent=None) -> None:
        self._fig = Figure(facecolor='#1e1e1e')
        super().__init__(self._fig)
        self.setParent(parent)
        self.ax = self._fig.add_subplot(111, projection='3d')
        self._reset_axes()
        self.draw()

    # ── Public ───────────────────────────────────────────────────────────────
    def update_toolpath(self, parser: BackplotParser) -> None:
        self.ax.cla()
        self._reset_axes()

        def _draw(segs: List[Segment], color: str, ls: str, lw: float) -> None:
            for x0, y0, z0, x1, y1, z1 in segs:
                self.ax.plot([x0, x1], [y0, y1], [z0, z1],
                             color=color, linestyle=ls, linewidth=lw)

        _draw(parser.rapid,  self._COL_RAPID,  '--', 0.9)
        _draw(parser.feed,   self._COL_FEED,   '-',  1.2)
        _draw(parser.plunge, self._COL_PLUNGE, '-',  1.8)

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
                                  lw=1.8, label='Canned Plunge'))
        if handles:
            self.ax.legend(handles=handles, loc='upper left', fontsize=7,
                           facecolor='#2b2b2b', labelcolor='#dddddd',
                           framealpha=0.85)

        self._equalize_axes(parser)
        self._fig.tight_layout(pad=0.4)
        self.draw()

    def clear_plot(self) -> None:
        self.ax.cla()
        self._reset_axes()
        self.draw()

    # ── Internal ─────────────────────────────────────────────────────────────
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
        self.ax.set_box_aspect([1, 1, 1])   # equal physical box (matplotlib ≥ 3.3)

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
"""


# ─────────────────────────────────────────────────────────────────────────────
# Main Window
# ─────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle('FanucToHurco  —  Hurco BNC Converter + 3D Backplot')
        self.resize(1440, 820)
        self.setStyleSheet(_DARK_STYLE)

        self._conv   = FanucConverter()
        self._parser = BackplotParser()

        self._build_ui()

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
        self.btn_save    = QPushButton('Save BNC')
        self.btn_clear   = QPushButton('Clear')
        for btn in (self.btn_load, self.btn_convert, self.btn_save, self.btn_clear):
            btn_row.addWidget(btn)
        btn_row.addStretch()
        vlay.addLayout(btn_row)

        # ── Three-panel splitter ──────────────────────────────────────────
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
        cv_lay.addWidget(self.canvas)
        split.addWidget(cv_wrap)

        split.setSizes([370, 370, 600])
        vlay.addWidget(split, stretch=1)

        # ── Status bar ────────────────────────────────────────────────────
        self._status = QStatusBar()
        self.setStatusBar(self._status)
        self._msg('Ready — load a Fanuc G-code file or paste code to begin.')

        self.btn_load.clicked.connect(self._on_load)
        self.btn_convert.clicked.connect(self._on_convert)
        self.btn_save.clicked.connect(self._on_save)
        self.btn_clear.clicked.connect(self._on_clear)

    # ── UI helpers ────────────────────────────────────────────────────────────
    @staticmethod
    def _make_editor(placeholder: str = '', readonly: bool = False) -> QTextEdit:
        ed = QTextEdit()
        ed.setFont(QFont('Courier New', 10))
        if placeholder:
            ed.setPlaceholderText(placeholder)
        ed.setReadOnly(readonly)
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

        result = self._conv.convert(src)
        self.output_edit.setPlainText(result)

        self._parser.parse(result)
        self.canvas.update_toolpath(self._parser)

        n_info  = len(self._conv.info)
        n_err   = len(self._conv.errors)
        n_segs  = (len(self._parser.rapid) +
                   len(self._parser.feed)  +
                   len(self._parser.plunge))

        parts = [f'{n_info} conversion(s)', f'{n_segs} plot segment(s)']
        if n_err:
            parts.append(f'{n_err} ERROR(S)')

        preview = (self._conv.errors or self._conv.info)[:3]
        summary = '  |  '.join(parts)
        if preview:
            summary += f'   [{" · ".join(preview)}]'
        self._msg(summary)

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
        try:
            with open(path, 'w', encoding='utf-8') as fh:
                fh.write(text)
            self._msg(f'Saved: {path}')
        except OSError as exc:
            self._msg(f'Error saving: {exc}')

    def _on_clear(self) -> None:
        self.input_edit.clear()
        self.output_edit.clear()
        self.canvas.clear_plot()
        self._msg('Cleared.')


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────
def main() -> None:
    app = QApplication(sys.argv)
    app.setStyle('Fusion')
    win = MainWindow()
    win.show()
    sys.exit(app.exec())


if __name__ == '__main__':
    main()
