# G- and M-codes in the 55-80B3 executive (HURCO KMB-1 "BX" G-code)

Sources: the parser's G-code group table (`0x16CF`), its address-letter table
(`0x7D40`), the block checker (`0x1733`–`0x2170`), the M-code executor
(`0x1972`, `0x21BD`) and the built-in help screens. Error names in **CAPS** are
the exact messages the control shows. Items marked *(inferred)* are my reading
of the code and are not stated in the help text.

---

## 1. Address words

| Word | Digits / format | Meaning | Limits enforced by the code |
|---|---|---|---|
| N | 5 digits | Sequence number | Optional. Help: 1–19999. |
| G | 2 digits | Preparatory code | Up to 7 per block, but only one from each group (**EXTRA G CODE**) |
| X Y | signed, 4 decimals (inch) / 3 (mm) | Axis target | Absolute (G90) or incremental (G91) |
| Z | signed | Axis target, **or** canned-cycle depth | Up to 3 Z words per block. The 2nd and 3rd are only legal in G83/G87 (**EXTRA Z COMMAND**). |
| A | signed, 3 decimals, degrees | Rotary table | −360 < A < 360 (**BAD RO-TB COMMAND**). Rejected if the ROTARY AXIS parameter is 0 (**RO-TB DISABLED**). |
| I J K | signed | Arc centre / helical pitch / G86 back-off | See G2/G3 and G86 |
| F | 5 digits, 1 decimal | Feed | ≤ 250.0 ipm, or the metric equivalent (**BAD FEED RATE**). Modal. |
| S | 4 digits | Spindle rpm | The code accepts 40–3680. The help says 40–4000. (**BAD RPM**) |
| T | 2 digits | Tool number | 0–24 (**BAD TOOL #**). M06 accepts only 1–12 in this build. |
| L | signed | Tool length (stored for tool T) | Requires T in the same block (**TOOL SIZE & NO TOOL #**) |
| D | signed | Tool diameter (stored for tool T) | Requires T in the same block (**TOOL SIZE & NO TOOL #**) |
| M | 2 digits | Miscellaneous | One per block |
| / | 4 digits | **Dwell time** when it appears inside a block, used with G4. A `/` at the **start** of a block is block delete. | 0–9999 (**BAD DWELL TIME**) |
| R | signed | Listed in the parser table | **No code reads it.** It is accepted and ignored. |

Modal values: F, S, all G groups, and the canned-cycle depths Z1/Z2/Z3 (see
section 3).

---

## 2. G-codes and their arguments

### Motion group (only one per block)

**G0: rapid positioning**
- Words: any of X Y Z A.
- Z is sequenced automatically *(inferred)*. If Z is moving up, Z moves first
  and then X/Y. If Z is moving down, X/Y move first and then Z.
- I J K are not allowed (**IJK & NON-CIRCULAR MOTION**). A Z2 word gives
  **EXTRA Z COMMAND**.
- Programming G0 (or G1) without a plane code resets the plane to G17.

**G1: linear feed move**
- Words: any of X Y Z A, plus **F**. F is required once (**NO FEED RATE**)
  and is modal after that.
- I J K are not allowed (**IJK & NON-CIRCULAR MOTION**).

**G2 / G3: arc CW / CCW**
- Words: the two in-plane end coordinates, the centre words for the plane, and **F** (required, modal).

  | Plane | End point | Centre | Helical axis + pitch |
  |---|---|---|---|
  | G17 XY (default) | X Y | I J | Z + K |
  | G18 XZ | X Z | I K | Y + J |
  | G19 YZ | Y Z | J K | X + I |

- **How the centre words are read** (from the code at `0x1CF9`; this
  corrects the built-in help, which says "incremental" for every case):
  - **G75 + G90** (the power-up default): I/J/K are **absolute centre
    coordinates**.
  - **G75 + G91**: I/J/K are signed distances from the start point to the
    centre.
  - **G74**: I/J/K are unsigned distances from the start point to the centre.
  - The AMTS sample program agrees with the first case. For example, its
    block `G02 X-2.25 Y-1.75 I-1.75 J-1.75` places the centre at (−1.75, −1.75).
- For a helix, also give the third axis, plus its centre letter as the
  **pitch (travel per 360°)**.
- **G75 (multi-quadrant):** I/J/K are signed. The block must contain **both**
  in-plane end coordinates **and both** centre words (zeros included), or
  **BAD ARC DB**.
- **G74 (single quadrant):** the control works out the centre signs itself
  *(inferred)*. Words that are left out keep their current values.
- A is not allowed in an arc block (**BAD ARC DB**).

**G81–G87, G89: canned cycles** (section 3)

**G92: set position (preload registers)**
- Words: any of X Y Z A. These are the coordinates the current position becomes.
- Not allowed while G91 or cutter comp is active (**G92 WITH INCR OR CUTTER COMP**).
- Z2 is not allowed (**EXTRA Z COMMAND**). For A: 0 ≤ A < 360 (**BAD RO-TB COMMAND**).
- Does not change the active motion mode.

### Other groups

| Code | Words | Notes |
|---|---|---|
| G4 dwell | **`/` time required** (`G4 /150`) | Missing or out of range gives **BAD DWELL TIME**. Units disagree in the help: "G4 … /TIME .1SEC" versus "DWELL TIME (SECONDS x 100)". The value is counted down by the control's periodic interrupt, whose rate I couldn't determine. **Caution:** I found this value used only by G82 and G89. I found no code that dwells for a G4 on its own. |
| G17 / G18 / G19 | none | Arc plane. Reset to G17 by any block that programs G0/G1 without a plane code. |
| G40 / G41 / G42 | none | Cutter comp. G41/G42 need a diameter (D) stored for the current tool (**NO TOOL DIAMETER**). |
| G70 / G71 | none | Inch / metric. Stored canned depths are converted when you switch. |
| G74 / G75 | none | Single- or multi-quadrant arcs (see G2/G3) |
| G80 | none | Cancels a canned cycle. Error if none is active (**G80 DOES NOT FOLLOW G81-G89**). |
| G90 / G91 | none | Absolute / incremental for X Y Z A. In incremental mode the axis must have a known position (**AXES NOT CALIBRATED**). |
| G14 | n/a | Always **UNSUPPORTED FEATURE** |
| G16 | none | Accepted; no effect found |
| G99 | none | Accepted and ignored |

---

## 3. Canned cycles: arguments and behaviour

The block that **enters** canned mode (from G0–G3) clears the stored Z1, Z2,
Z3 and canned feed. They then stay modal for later hole blocks until the cycle
is cancelled.

| Word | Meaning |
|---|---|
| **Z** (Z1) | **Total depth**, unsigned and incremental **down from the Z height when the cycle starts**. A sign is ignored. |
| **Z** second word (Z2) | G83/G87 only: **first peck** size. Must be smaller than Z1 (**BAD Z IN CANNED CYCLE**). |
| **Z** third word (Z3) | G83/G87 only: **later peck** size. Optional; defaults to Z2. |
| **F** | **Canned-cycle feed**, kept separately from the G1 feed. Required (**NO FEED RATE**). |
| X Y A | Hole position. Rapid move to it, then the cycle runs there. |
| **`G4 /t`** | Dwell at the bottom for G82/G89. G4 can sit in the same block as G82/G89. |

Rules (all errors below are raised when a hole is actually drilled):
- A cycle block without X, Y or A only sets the parameters. No hole is drilled.
- Z must already be a known position (**CANNED Z FROM UNKNOWN POSITION**).
  After M06 or M25, make a Z move first.
- A depth must be defined (**MISSING Z IN CANNED CYCLE**). G83/G87 also need
  Z2.
- I J K are not allowed, except I/J in G86 (**IJK & NON-CIRCULAR MOTION**).
- Retract is always back to the start Z, the height Z was at when the cycle
  was first called. **There is no R plane.**

| Code | Sequence (from the code) | Required | Optional |
|---|---|---|---|
| **G81** drill | Feed to depth, rapid out | Z1, F | X Y A |
| **G82** drill + dwell | Feed to depth, dwell, rapid out | Z1, F, dwell (`G4 /t`) | X Y A |
| **G83** deep-hole peck | Feed Z2, **rapid back to start Z**, rapid back down, feed Z3 … to Z1, rapid out | Z1, Z2, F | Z3, X Y A |
| **G84** tap | Feed in, spindle reverses, feed out *(inferred)* | Z1, F, spindle running | X Y A. **TAP STROKE TOO SMALL** if the depth is too short for the reversal. |
| **G85** bore | Feed in, **feed out** | Z1, F | X Y A |
| **G86** bore | Feed in, spindle stop/orient, move X/Y by the back-off, rapid out *(inferred)* | Z1, F | **I J** = X/Y back-off at the bottom. Overrides the "X/Y BACKOFF (G86 I/J)" parameters and is stored back into them. K is not allowed. |
| **G87** chip-break | Like G83, but after each peck it retracts only **0.050 in (1.27 mm)** | Z1, Z2, F | Z3, X Y A |
| **G89** bore + dwell | Feed in, dwell, feed out | Z1, F, dwell (`G4 /t`) | X Y A |

---

## 4. M-codes and their arguments

| Code | Words | Behaviour / notes |
|---|---|---|
| M00 | none | Program stop |
| M01 | none | Stop only if the OPTIONAL STOP parameter = 1 |
| M02 | none | End of program |
| M03 / M04 | S (or a previous S) | Spindle CW/CCW at the current rpm (**NO RPM** if none is set) |
| M05 | none | Spindle off |
| M06 | **T required** (**NO TOOL #**) | Tool change. T must be 1–12 here. It also cancels a canned cycle, and Z becomes unknown afterwards. |
| M07 / M08 | none | Mist / flood coolant on |
| M09 | none | Coolant off |
| M19 | none | Oriented spindle stop (with a fixed settle delay) |
| M20 | none | Pulse the rotary indexer once (waits for it to finish) |
| M25 | none | Z to home. Any Z word in the block is ignored, and Z becomes unknown. |
| M30 | none | No help and no handler. Look-ahead treats it as a stop point. |

## 5. Tool data blocks

A block with **T** but no M06 doesn't change the tool. It stores **L**
(length), **D** (diameter) and **S** (rpm) for tool T in the tool table. A
block with S and no T sets the current rpm.

Example: `T5 L3.2500 D.5000 S1200`

## 6. Example program (from these rules, not tested on a machine)

```
N1 G70 G90 G17 G40 G80
N2 T1 M06
N3 S1500 M03 M08
N4 G0 X1.0 Y1.0 Z0.1
N5 G81 Z0.6 F8.0 X1.0 Y1.0     (drill 0.6 deep from Z0.1)
N6 X2.0                        (second hole)
N7 G83 Z1.5 Z0.3 Z0.2 F6.0 X3.0 Y1.0   (peck: total, first, then 0.2)
N8 G80
N9 G1 X4.0 F20.0
N10 G2 X5.0 Y2.0 I4.0 J2.0     (default G75+G90: absolute centre; give all of X Y I J)
N11 M05                        (only one M per block)
N12 M09
N13 M02
```

## 7. Comparison with the AMTS documents (Gcodes.pdf, atc_codes.pdf, 1996)

**These agree with the firmware:**
- G00–G04, G17, G40–G42, G70/71, G80–G85 and G90/91 exist.
- G04 is "one-shot; affects fixed cycles". The code uses the dwell value only
  in G82/G89.
- The M codes listed (M00–M09, M25 Z-retract) all exist.
- The power-up defaults G00, G17, G40, G75, G80, G90, M05 and M09 match. The
  init code sets G75 at `0x23EC`.
- The sample program follows the firmware's rules. Every G75 arc block gives
  all of X, Y, I and J, the centres are absolute, there is a T with M06, and
  the tape starts with `%` and ends with `E`.
- The DNC serial settings (7 data bits, even parity, 2 stop bits, XON/XOFF)
  fit the firmware's ISO/RS-358 handling and its PARITY ERROR / PROTOCOL
  VIOLATED messages.

**These don't match. The document seems to cover a different BX software
version:**

| In the document | In 55-80B3 |
|---|---|
| G09, G61, G64 precision cornering (G64 is a default) | Not in the G table. They would give **BAD G CODE**. |
| "G19 XZ plane", and no G18 | G18 = XZ and G19 = YZ (the document is probably a typo) |
| Not listed: G18, G74/G75 (G75 appears only as a default), G86, G87, G89, G92 | All present |
| Not listed: M19, M20 | Present |

**Tool-changer errors (atc_codes.pdf).** The firmware shows these as two
ASCII digits.

| Code | Document | Firmware check found |
|---|---|---|
| 60 | Tool number 0 but a tool is in the spindle | `0x7052`: tool = 0 and E502 bit 4 |
| 61 | Magazine pot already holds a tool | `0x706C`: E501 bit 7 |
| 62 | Spindle not unclamped while Z moves during a change | `0x36C7`: E502 bit 6, checked during Z moves |
| 63 | Shot-pin-in switch lost | `0x70E4`: E502 bit 5, with timeout |
| 64 | Magazine-in-position switch not seen | `0x70FC`: E504 bit 0, with timeout (the C=1 build reads E500) |
| 65 | **MB-1H only:** orientation speed / spindle reference | `0x7145`/`0x7170`. **Only in 55-80B3 (C=9)**, so 55-80B3 is the MB-1H build. |
| 66 | Drive monitor (DM) not present | `0x7026` |
| 67 | Arm-in switch lost during Z move | `0x36DB`: E500 bit 6 |
| 68 / 69 | Magazine didn't rotate / non-sequential pot | `0x71DD` / `0x71D8` |
| 84 | Arm-back / arm-front switch fault | `0x728B` |
| 06, 07, 25, 26, 27 | Limit switch missing for a step | Not found as fixed numbers. They are probably computed elsewhere. |

*Caveats:* the dwell units and tick rate, the exact G84/G86 sequences, and the
meaning of G16 and R are inferred and are not confirmed on the machine.
