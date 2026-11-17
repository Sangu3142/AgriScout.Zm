# Arduino Uno soil sensor + automatic watering

Board: **Arduino Uno (CH340 clone)**. It sits next to the robot and is plugged into the Pi's USB
(`/dev/ttyUSB0`, 9600 baud). It never connects to the Pi's GPIO.

## Wiring (soil sensor → Uno)

| Sensor | Uno |
|---|---|
| VCC | 5V |
| GND | GND |
| AO | A0 |

## Wiring (relay + pump)

The sensor and the relay share the Uno's 5V and GND through the breadboard rails.

| Relay | Uno |
|---|---|
| VCC | 5V (breadboard + rail) |
| GND | GND (breadboard − rail) |
| IN | D7 |

Pump circuit: battery box + → relay **COM**, relay **NO** → pump +, pump − → battery box −.
**The pump is powered only from the battery box, never from the Uno or the Pi** (a motor's current
and spikes would reset or damage them). NO = the pump runs only while the relay is on.

If the relay clicks ON at power-up, it is active-low: set `RELAY_ACTIVE_HIGH = false` in the sketch.

## Watering safety rules (in `agriscout_soil.ino`)

- Dry = raw > 850 (same as the robot's `SOIL_DRY_ABOVE`).
- One burst lasts at most **3 s** (hard time limit, checked every loop).
- **30 s soak** between bursts so the water reaches the probe before deciding again (also 30 s
  after power-up).
- **Never pumps with the probe in air** (raw ≥ 1015).
- **Lockout** after 3 bursts in a row with the soil still dry (empty cup / probe moved): prints
  `PUMP LOCKOUT` and stops; cleared only when the soil reads moist (≤ 850) again.
- The relay pin is set to OFF before it becomes an output, so the pump never twitches at reset.

## Sketches

- **`agriscout_soil/`**: **this is the one running at the expo.** It prints `READY` at start, then
  `SOIL <raw>` every 2 s (average of 10 samples, 0-1023, higher = drier). It waters on its own
  (rules above) and prints `PUMP ON` / `PUMP OFF` / `PUMP LOCKOUT`; it takes no commands and works
  even if the Pi is off. The robot server reads it (`robot/soil.py`) and ignores the PUMP lines.
- `soil_calibrate/`: calibration only. It prints `SOIL <raw>` once a second (no `READY`), to measure
  air / dry / moist / wet values in the Serial Monitor. Results are in `robot/config.py` (`SOIL_RAW_*`).

## Upload

Use Arduino IDE 2: open the `.ino`, select board **Arduino Uno** and the CH340's COM port, then
click Upload. Close the Serial Monitor before the robot server reads the port.
