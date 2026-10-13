# OECT with reference

This is a standalone, single-OECT Python project. It uses ADCMT 6541 CH1 for
drain bias/current, CH2 for gate bias/current, and a Keithley DMM6500 for the
voltage of a **separate reference electrode through a high-impedance input**. The GUI follows the
cream-card/teal style of the earlier OECT program. The colleague's project was
used as a workflow example; its 10 MΩ DMM setup is **not** used here.
The DAQ6510 is not used in this version; the DMM6500 is the dedicated Vref
measurement instrument.

## Instrument connections

| Connection | Where it goes |
| --- | --- |
| ADCMT CH1 HI OUTPUT / HI SENSE | OECT drain |
| ADCMT CH2 HI OUTPUT / HI SENSE | Current-carrying gate electrode |
| ADCMT CH1/CH2 LO OUTPUT / LO SENSE | OECT source (shared ADCMT LO) |
| DMM6500 **INPUT HI** | Separate immersed reference electrode |
| DMM6500 **INPUT LO** | OECT source / ADCMT common LO |

The DMM reads `Vref = V(reference) - V(source)`. The controlled quantity uses
the **same sign**: `Eref = Vref`; the program does not negate the DMM reading.
Negative target values therefore request a reference potential below the
source potential. The default feedback direction assumes that decreasing gate
voltage also decreases Eref. Electrode offsets can affect the absolute values,
so this direction describes how the voltages change, not a requirement that
Vg and Eref always have the same sign.
Do not use the DMM SENSE terminals or connect the reference to an ADCMT
output. The reference must be separate from the gate electrode that carries
current.
The ADCMT 6541 uses four-wire OUTPUT/SENSE connections: connect each HI SENSE
to the same device terminal as its HI OUTPUT, and each LO SENSE to the same
source terminal as its LO OUTPUT. Do not leave SENSE open or connect it to the reference.

The DMM6500 must be physically switched to **FRONT** terminals. The program
checks this with `ROUTe:TERMINals?`, checks that its remote language is SCPI,
sets DC voltage / fixed 10 V range / **Input Impedance AUTO**, and reads these
settings back before ADCMT output is enabled. On this meter AUTO impedance on
the 10 V range gives >10 GΩ; the 10 MΩ setting can load and polarize a small
reference electrode.
It aborts any old scan/trigger model and loads a one-reading `SimpleLoop` before
each `READ?`, so a previous DMM program cannot turn one Vref sample into a
multi-reading scan.
The program checks the meter settings again during acquisition and stops the
ADCMT outputs if verification or a reading fails. A meter reset may revert to
10 MΩ, so do not reset it while the reference is connected. The program also disables and
checks saved Relative, Math, averaging, and limit functions so `Vref` is the
unmodified DC-voltage reading.

The DMM6500 uses a VXI-11 VISA instrument session at
`TCPIP0::<DMM6500 IP>::inst0::INSTR`. Enter its IP address in the GUI; the
program builds this resource name automatically. The DMM6500 does **not** use
the GUI's TCP port. That port is for the ADCMT raw socket only, with default
`5025`. The ADCMT address defaults to `192.168.1.204` from the earlier OECT
project; change it in the GUI if your instrument uses another address. Both
instruments and the computer must be reachable on the same LAN. On the DMM6500,
check the assigned IP under **Menu → Communication → LAN**. The photograph
does not establish the computer's network settings.

## Run the program

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python gui.py
```

For an existing `.venv`, run `.venv/bin/python -m pip install -r requirements.txt`
again to add PyVISA and its Python VXI-11 backend. This backend opens a known
instrument address directly; VISA device discovery is not required.

Or double-click `launch_gui.command` on macOS. **Simulation is selected when
the GUI opens**; it uses no network connection and cannot energize an output.
Use **Test connections** to query instrument identities before a live run.

Select one of two measurement modes in the GUI:

| Mode | Sweep values mean | What happens to CH2 gate voltage |
| --- | --- | --- |
| Reference feedback | Desired `Eref = Vref` | The program uses each DMM reading to correct Vg toward the target, within the configured bounds and maximum change per reading. |
| Gate sweep + Vref monitor | Programmed `Vg` | The program sets each gate voltage directly and records Vref. Vref is **never used to correct Vg**. |

Both modes retain the six raw-data columns described below. Drain and gate current
compliance are each **0.003 A (3 mA)** by default; the GUI fixes these values
and has no current-limit controls. Compliance limits the current the ADCMT can
deliver, which can prevent the requested voltage from being reached.

The sweep can be **one way** (initial to final, inclusive) or **back and forth**.
For back-and-forth sweeps, `Cycles` is the number of complete trips from the
initial value to the final value and back. The turnarounds are recorded once,
without repeating a voltage at the same endpoint. For example, initial −0.4 V,
final −0.6 V, step −0.1 V, and 3 cycles produce:

```text
−0.4, −0.5, −0.6, −0.5, −0.4, −0.5, −0.6, −0.5, −0.4, −0.5, −0.6, −0.5, −0.4 V
```

In reference feedback mode these are Eref targets. In Vref monitor mode they
are programmed Vg values. `Period` applies to every value in this sequence,
and `Keep at end` adds time after the last value (the initial value when a
back-and-forth sweep ends).

All controls are on the left, with **Connection** at the top and **Raw data**
directly below it, followed by sweep and gate settings. The plots are on the
right. The default raw file is
`outputs/oect_reference.txt` inside the project folder.

### Feedback direction

This section applies to **Reference feedback** mode. The monitor mode does
not use this setting.

**Direction** tells the controller which gate-voltage change lowers the measured
Eref in your cell. The minus or plus describes a change in Vg, not whether the
gate voltage itself is negative or positive.

| Direction | Assumed response | Example of the gate change |
| --- | --- | --- |
| Decrease Vg lowers Eref (same direction) — default, `feedback_polarity = +1` | Vg and Eref change in the same direction | Vg from −0.30 V to −0.32 V lowers Eref |
| Increase Vg lowers Eref (opposite direction) — `feedback_polarity = −1` | Vg and Eref change in opposite directions | Vg from −0.30 V to −0.28 V lowers Eref |

Vref does **not** need to move in the opposite direction to Vg. The default
assumes that both rise together and both fall together. The alternate setting
is only for a cell whose measured response is opposite.

The setting does not invert the DMM reading or the target. Choose it from the
observed response of your wiring and cell to a small gate-voltage change;
electrode material alone does not select the direction. Choosing a direction
that disagrees with the measured response makes corrections move Eref away
from the target.

The familiar source potential relative to the reference would instead be
`V(source) − V(reference) = −Vref`. This program controls the reference
potential relative to the source, `Eref = Vref`, so that opposite sign
convention is not applied to either the readings or targets.
Simulation models the selected response direction so either option can be
demonstrated; it does not determine the response direction of your real cell.

The **Correction factor** defaults to `1.0`. On each reading outside the
configured tolerance, the controller calculates:

```text
error = target Eref − measured Eref
gate change = feedback_polarity × Correction factor × error
```

It then limits the gate change to **Max change**, which defaults to `0.1 V`,
and keeps the resulting gate voltage within the configured bounds. For example,
if measured Eref is −0.30 V and the target is −0.45 V, the error is −0.15 V.
With Correction factor `1.0`, the default direction requests a −0.15 V gate
change, but Max change limits that reading's correction to **−0.1 V**. The
opposite direction would apply **+0.1 V**. Subsequent corrections use fresh
DMM readings.

Correction factor `1.0` does not guarantee reaching the target in one reading:
the step limit, read interval, electrode response, and cell settling all affect
the speed. This is a software feedback loop whose updates require instrument
communication and measurements.

The **Read interval** is the ADCMT sampling period. Changing Vg does not add a
second interval: after a gate command, the program only skips a buffered pair
whose ADCMT nominal time is before that command. The next sample is recorded,
so a 1 s setting remains approximately 1 s even while feedback is changing Vg.

CH1 stays at the configured `Vds`. A delay is included within
each staircase hold. Unlike the previous program, **raw rows during the delay
are retained** so the potential response is visible.
The last level gets the configured additional keep time. Stop, a meter
failure, or a control error leads to a 0 V/standby cleanup attempt on the
ADCMT.

## Raw data

The raw output is a tab-delimited text file with exactly these six columns:

```text
Time  GateI  GateV  DrainI  DrainV  Vref
```

`Time` is elapsed time in seconds. `GateI` and `DrainI` are synchronized ADCMT
current measurements in amperes. `GateV` and `DrainV` are verified ADCMT source
setpoints in volts, not independent measured voltages. `Vref` is the DMM6500
measurement in volts, with the sign `V(reference) − V(source)`.

CH1/CH2 current readings are hardware synchronized by the ADCMT. The DMM
reading follows those two currents over LAN; it is not hardware simultaneous.
If computer/network work misses an ADCMT sample, the program records the next
available current pair and its corresponding DMM reading. After a gate-voltage
change, it discards any buffered current pair that predates the new command.
It does not intentionally discard the entire following sampling interval.

## Before connecting a device

1. In simulation, confirm both modes, the sweep direction and cycle count,
   graph labels, and Vref column. For live feedback, determine the response
   direction with a small, reversible gate perturbation on a dummy cell or
   robust sample.
2. Confirm the two instrument IPs, common-source wiring, DMM FRONT selection,
   and that the reference is separate from the gate electrode. Check the
   displayed gate bounds and ensure that the fixed 3 mA drain and gate current
   compliance values suit your device.
3. Make a short live dummy-load run. Verify that `Vref` has the expected sign,
   that the DMM stays in AUTO impedance, and that Stop returns both outputs to
   0 V and standby. The program has not been tested against the attached live
   instruments in this workspace.

A quasi-reference electrode can drift with electrolyte, surface state, and
dissolved species. Holding a voltage relative to the reference cannot remove
the reference's own drift. When using a quasi-reference for quantitative
potential comparisons, check its potential against a suitable stable reference
before and after a run.

## Manuals

- [DMM6500 reference manual](https://download.tek.com/manual/DMM6500-901-01B_Sept_2019_Ref.pdf)
- [DMM6500 specifications](https://download.tek.com/document/SPEC-DMM6500A_April_2018.pdf)
- Local ADCMT 6540/6541 operation manual:
  `../6540-41-ope-foe-00000147b01-e.pdf`
