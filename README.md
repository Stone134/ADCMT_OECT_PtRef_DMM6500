# OECT measurement with a separate Pt quasi-reference

This is a standalone, single-OECT Python project. It uses ADCMT 6541 CH1 for
drain bias/current, CH2 for gate bias/current, and a Keithley DMM6500 for the
voltage of a **separate, high-impedance Pt sense electrode**. The GUI follows the
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
| DMM6500 **INPUT HI** | Separate immersed Pt quasi-reference |
| DMM6500 **INPUT LO** | OECT source / ADCMT common LO |

The DMM reads `Vref = V(Pt) - V(source)`. For this project, the controlled
quantity uses the **same sign**: `ERef = Vref`. Therefore a negative gate
voltage, negative Vref, and negative target ERef point in the same direction.
Do not use the DMM SENSE terminals or connect the Pt reference to an ADCMT
output. The Pt reference must be separate from the gate electrode that carries
current.
The ADCMT 6541 uses four-wire OUTPUT/SENSE connections: connect each HI SENSE
to the same device terminal as its HI OUTPUT, and each LO SENSE to the same
source terminal as its LO OUTPUT. Do not leave SENSE open or connect it to Pt.

The DMM6500 must be physically switched to **FRONT** terminals. The program
checks this with `ROUTe:TERMINals?`, checks that its remote language is SCPI,
sets DC voltage / fixed 10 V range / **Input Impedance AUTO**, and reads these
settings back before ADCMT output is enabled. On this meter AUTO impedance on
the 10 V range gives >10 GΩ; the 10 MΩ setting can load and polarize a small Pt
quasi-reference.
It aborts any old scan/trigger model and loads a one-reading `SimpleLoop` before
each `READ?`, so a previous DMM program cannot turn one Vref sample into a
multi-reading scan.
The program checks the meter settings again during acquisition and stops the
ADCMT outputs if verification or a reading fails. A meter reset may revert to
10 MΩ, so do not reset it while Pt is connected. The program also disables and
checks saved Relative, Math, averaging, and limit functions so `Vref` is the
unmodified DC-voltage reading.

The provided photograph shows DMM6500 `192.168.1.103` and raw socket port
`5025`. The ADCMT address defaults to `192.168.1.204` from the earlier OECT
project; change it in the GUI if your instrument uses another address. Both
instruments and the computer must be reachable on the same LAN. The photograph
does not establish the computer's network settings.

## Run the program

```bash
python3 -m pip install -r requirements.txt
python3 gui.py
```

Or double-click `launch_gui.command` on macOS. **Simulation is selected when
the GUI opens**; it uses no network connection and cannot energize an output.
Use **Test connections** to query instrument identities before a live run.

Two run modes are available:

- **Gate sweep + Pt monitor:** The staircase values are programmed electrical
  `Vgate - Vsource`; the DMM voltage is measured and logged at each sample.
  This reproduces the earlier OECT gate-sweep behavior while adding Vref.
- **Pt-referenced feedback:** The staircase values are targets for
  `ERef = Vref`. After each DMM reading, a bounded proportional update changes
  CH2 gate voltage. The default assumes Vg and ERef have the same sign, so
  decreasing Vg lowers ERef; the GUI exposes the alternate direction if your
  cell responds oppositely. The controller also enforces adjustable gate-voltage
  bounds, maximum change per reading, and current-compliance settings. This is
  a slow software feedback loop, not a fast analog potentiostat.

The feedback **Correction factor** defaults to `1.0` for a faster response. It
is multiplied by the voltage error, then limited by **Max change** on every
reading. Increase it only with a suitable per-step limit and while watching for
oscillation.

Both modes maintain CH1 at the configured `Vds`. A delay is included within
each staircase hold. Unlike the previous program, **raw rows during the delay
are retained** and marked `settling` so the potential response is visible.
The last level gets the configured additional final keep time. Stop, a meter
failure, or a control error leads to a 0 V/standby cleanup attempt on the
ADCMT.

## Raw data

The tab-delimited output begins with the five historical columns and adds the
actual DMM reading:

```text
Time  GateI  GateV  DrainI  DrainV  Vref  ERef  TargetV  Stage  Phase  UTC  DMMReadUTC  ADCMTIndex  ADCMTSampleTime  ADCMTSkipped  WithinTolerance
```

`GateI` and `DrainI` are ADCMT current measurements. `GateV` and `DrainV`
are verified source setpoints, not independent measured voltages. `Vref` is
the **measured** DMM voltage, and `ERef` is the same signed quantity. The
output metadata file documents this polarity and all run settings. `TargetV` is
the programmed gate voltage in monitor mode and the desired ERef in feedback
mode. An additional `.meta.json` file sits beside the raw file.

CH1/CH2 current readings are hardware synchronized by the ADCMT. The DMM
reading follows those two currents over LAN; it is not hardware simultaneous.
The file therefore keeps both the ADCMT sample index/time and DMM read time.
If computer/network work misses an ADCMT sample, `ADCMTSkipped` reports the
gap rather than fabricating a Vref value for it. After a gate-voltage change,
the program also discards buffered current pairs that may predate the new
command; these count toward `ADCMTSkipped` in the next recorded row.

## Before connecting a device

1. In simulation, confirm the sweep, graph labels, Vref/ERef columns, and
   feedback sign. For live feedback, determine the response direction with a
   small, reversible gate perturbation on a dummy cell or robust sample.
2. Confirm the two instrument IPs, common-source wiring, DMM FRONT selection,
   and that the Pt wire is a separate reference electrode. Check the displayed
   gate bounds and current limits for your device.
3. Make a short live dummy-load run. Verify that `Vref` has the expected sign,
   that the DMM stays in AUTO impedance, and that Stop returns both outputs to
   0 V and standby. The program has not been tested against the attached live
   instruments in this workspace.

Bare Pt is a quasi-reference: its potential can drift with electrolyte,
surface state, and dissolved species. Holding a voltage relative to Pt cannot
remove Pt's own drift. For quantitative potential comparisons, check the Pt
potential against a suitable stable reference before and after a run.

## Manuals

- [DMM6500 reference manual](https://download.tek.com/manual/DMM6500-901-01B_Sept_2019_Ref.pdf)
- [DMM6500 specifications](https://download.tek.com/document/SPEC-DMM6500A_April_2018.pdf)
- Local ADCMT 6540/6541 operation manual:
  `../6540-41-ope-foe-00000147b01-e.pdf`
