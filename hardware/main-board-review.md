# Main-board PCB Review — Aether-node

*Review of `main-board/main-board.kicad_pcb` (branch `sensor-board`, 2026-11-01)*

Checks performed: DRC (with schematic parity) and ERC via `kicad-cli`, netlist export, and direct parsing of the PCB (copper, zones, footprint positions).

DRC result: only the user-excluded items plus silkscreen-over-edge warnings on SW1/U1 (expected for edge-mounted parts). Board and schematic match exactly — so the issues below are **schematic** issues that DRC cannot catch.

---

## 🔴 Serious — fix before ordering

### 1. U3 (AP2112K LDO) GND pin is not connected
U3 pin 2 (GND) is on its own net `unconnected-(U3-GND-Pad2)` in the schematic. On the PCB, pad 2 at (130.8, 114.7) has no track and the GND pour keeps clearance from it.

- **Effect:** the regulator has no ground reference → no 3.3 V → ESP32 never powers up.
- **Why DRC missed it:** the PCB matches the schematic. ERC does flag it (`U3 Pin 2 [GND] Pin not connected`).
- **Fix:** connect U3 pin 2 to GND in the schematic, update the PCB, route a short track to a GND via next to the pad.

### 2. Q1 (load-sharing P-MOSFET) source and drain are swapped
Currently **S (pin 2) = /BAT+** and **D (pin 3) = /LDO_IN**. The standard load-sharing circuit has **drain on the battery, source on the load**.

- **Effect:** with USB connected, the body diode conducts LDO_IN → BAT+ and charges the battery directly, bypassing the TP4056 (no current limit, no CV regulation, no termination). LiPo safety risk.
- On battery alone it works (gate pulled low → channel on), which makes the fault easy to miss on the bench.
- **Fix:** swap the nets — pin 3 (D) → /BAT+, pin 2 (S) → /LDO_IN. Also, the schematic symbol is `DMG3415U-7` from a `diodes` library that isn't configured; the pinout (1 = G, 2 = S, 3 = D) matches DMG2305UX, but point the symbol at a real library.

---

## 🟡 Worth addressing

3. **Antenna flanked by GND copper.** The antenna overhang in the edge notch is good (local antenna keepout matches the notch; board edge at y = 77.75 sits exactly at the antenna boundary). But the wings on both sides of the notch reach y = 72.35 and carry GND fill on F.Cu/In1/B.Cu only ~2.1 mm from the antenna sides, along its full 5.4 mm length. Detuning → reduced range (moderate risk, not a failure).
   **Cheap fix:** add a keepout zone (copper pour not allowed, all layers) over the wings near the notch, e.g. y 72.35 → ~78 and 8–10 mm sideways from each notch edge. H1/H2 are ~12 mm away and unaffected. Trimming the wings is even better if not needed mechanically.
4. **Always-on LEDs drain the battery.** D5/D6 (green, 33 Ω) and D7 (red, 150 Ω) go from +3.3 V to GND — lit whenever SW1 is on, including on battery. Roughly 15–40 mA continuous depending on green Vf. Drive them from GPIOs if they're status LEDs. 33 Ω gives poorly controlled current if Vf ≈ 2.2 V. (D9 on +5 V is only lit on USB.)
5. **GPIO14 / SW8 had no pull-down.** → *Resolved: 10 k pull-down added.*
6. **AP2112 dropout on battery.** Needs ~3.55 V+ in to hold 3.3 V at TX peaks → brownouts in the last ~15–20 % of the cell. → *Deferred to board V2.*
7. **No battery protection IC on board.** Use protected cells and verify J6 (JST-SH) polarity: pin 1 = BAT+. → *Battery has its own protection.* Note: that protection (typically ~4.25–4.3 V OVP, 2–3 A OCP) does **not** make issue #2 safe; it's a last line of defence.

---

## 🟢 Checked and OK

- **Stackup:** 4 layers, In1 solid GND → USB pair on F.Cu has a continuous reference; 0.22 mm traces fine for USB Full-Speed.
- **USB-C:** 5.1 k on CC1/CC2, USBLC6 ESD, VBUS TVS (D8) ahead of PTC fuse (F1), 22 Ω series resistors on D+/D−. Receptacle protrudes ~1.2 mm past the edge — fine.
- **TP4056:** R20 = 5.1 k → ~235 mA (matches the 250 mA change). Thermal pad has 6 vias + B.Cu pad, enough for ~0.5 W worst case. TEMP grounded (NTC disabled).
- **ESP32-C6 support:** EN 10 k + 1.1 µF, GPIO8 pull-up, BOOT on GPIO9, 3V3 decoupled with 22 µF + 100 nF.
- **Edges:** SW1 slider protrudes ~0.9 mm with the body inside the edge; GND fill respects the 0.3 mm edge clearance.
- **Housekeeping:** unused off-sheet schematic symbols (D1–D4, R1–R5, C1, C2, U5) cause 22 of the 28 ERC errors — remove them so real errors (like #1) stand out.

**Bottom line:** fix #1 and #2 in the schematic, update the PCB, reroute around U3 and Q1. Everything else is optimisation.

---

## Follow-up Q&A

### Does the regulator GND need to be a thick track? Is there a big current?
No. Load current flows VIN → VOUT → ESP32 → back through the ground plane. The LDO's GND pin only carries its quiescent current (~55 µA). A short 0.3 mm track to a nearby GND via is plenty — but it must be connected, since it's the reference that sets 3.3 V.

### "When on USB, 5 V turns the PMOS off and current flows through the Schottky; on battery the 10 k pulls the gate low and the PMOS conducts." Isn't the Q1 wiring fine?
The channel reasoning is correct:
- **USB present:** G = 5 V, S ≈ 3.7 V → Vgs positive → channel off ✔
- **Battery only:** R17 pulls G low → Vgs ≈ −Vbat → channel on ✔

The problem is the **body diode**, which exists regardless of the gate. With USB present:

| Pin | Net | Voltage |
|---|---|---|
| D (anode) | LDO_IN | ≈ 5.0 − 0.3 (D12) ≈ **4.7 V** |
| S (cathode) | BAT+ | **3.0–4.1 V** |

Path: `VBUS → F1 → D12 → LDO_IN → Q1 body diode → BAT+` — uncontrolled charging (a flat cell can draw hundreds of mA to ~1 A; float voltage can exceed 4.2 V with VBUS at 5.25 V).

### "But the body diode is from D to S, so it's only leakage on USB."
The direction is right — in a P-MOSFET the body diode's anode is the drain, cathode the source, so it conducts D → S. But on this board D (4.7 V) is **higher** than S (3.0–4.1 V), so it is **forward-biased**, not leaking. Only a fully charged cell (~4.2 V) brings the difference near the ~0.6 V threshold; at 3.6 V there's ~1.1 V across it.

With the swapped wiring (D = BAT+, S = LDO_IN):

| Situation | Anode (D) | Cathode (S) | Diode |
|---|---|---|---|
| USB present | BAT 3.7 V | LDO_IN 4.7 V | **Reverse-biased → leakage only** ✔ |
| Battery only | BAT 3.7 V | LDO_IN | Forward, but channel is on anyway ✔ |

Gate logic is unchanged by the swap (USB: G = 5 V → off; battery: G = 0 V → on).

**How to verify:**
- **Datasheet:** the DMG2305UX symbol shows the diode anode on the drain pin.
- **Bench:** feed BAT+ from a lab supply at 3.5 V, plug in USB, watch the supply current — it will sink current into the supply well above leakage and above the TP4056's ~235 mA setting.

### How does current normally flow in a PMOS when it's on — source to drain?
**Textbook high-side switch:** source to the higher voltage (supply), drain to the load; current flows **S → D**. The body diode points D → S (load → supply) and stays reverse-biased.

```
+V ──S[PMOS]D── load
         G (low = on)
```

**But the channel is bidirectional.** When on, it behaves like a small resistor (tens of mΩ for DMG2305UX) and conducts S → D or D → S equally. The gate controls the channel, not the direction. **When off, only the body diode decides what is blocked** (blocks S → D, conducts D → S).

**Rule for power switching:** orient the MOSFET so that, when off, the body diode blocks the direction that must be blocked. Reverse-polarity protection and battery load-sharing both deliberately use the PMOS "backwards" (D → S through the channel).

For this circuit:

| | Requirement | Consequence |
|---|---|---|
| USB present (off) | Block LDO_IN → BAT | Diode cathode (S) must be on the LDO_IN side |
| Battery only (on) | BAT → LDO_IN | Channel conducts D → S; body diode points the same way, so it only helps |

→ **D = BAT+, S = LDO_IN.** The current layout uses the textbook orientation (source towards the battery), which leaves the body diode pointing LDO_IN → BAT+ — exactly the direction that must be blocked.

### LED resistor on 5 V for 20 mA — ~150 Ω?
**R = (Vsupply − Vf) / I**, so it depends on the LED's Vf:
- Emerald/pure green (InGaN), Vf ≈ 3.0 V → (5 − 3.0) / 0.02 = **100 Ω**
- Yellow-green, Vf ≈ 2.1 V → (5 − 2.1) / 0.02 ≈ **150 Ω**

20 mA is usually the LED's maximum rating; indicators are clearly visible at 2–5 mA (470 Ω–1 kΩ is typical). Current D9 with 220 Ω gives ~9 mA with a 3 V LED — already fine. For D5/D6/D7 on +3.3 V use the same formula with 3.3 V, and check the green LED's Vf in its datasheet (LCSC C965815): with 33 Ω, Vf 3.0 V → ~9 mA, but Vf 2.2 V → ~33 mA (over rating).

---

## Status

| # | Item | Status |
|---|---|---|
| 1 | U3 GND unconnected | **Open — must fix** |
| 2 | Q1 S/D swapped | **Open — must fix** |
| 3 | GND copper beside antenna | Open — recommended |
| 4 | Always-on LEDs / 33 Ω resistors | Open — recommended |
| 5 | GPIO14 pull-down | Done (10 k added) |
| 6 | LDO dropout on battery | Deferred to V2 |
| 7 | Battery protection | Cell has its own protection |
