"""DHT11 test: prints temperature and humidity every 2 s. Ctrl+C to stop.

Usage (on the Pi):
  python3 ~/agriscout/robot/tools/dht_test.py    DATA on BCM 17 (config.DHT11_PIN)
  python3 ~/agriscout/robot/tools/dht_test.py --pin 25
  python3 ~/agriscout/robot/tools/dht_test.py --power-pin none   VCC on a fixed 3.3 V pin

pigpiod starts at boot via the systemd drop-in
/etc/systemd/system/pigpiod.service.d/zz-agriscout.conf (/usr/bin/pigpiod -l -p 8889;
-l = local only, 8889 because 8888 is Jupyter). Only if it has been disabled, start it by hand
first: sudo pigpiod -l -p 8889. Leave it running afterwards: the robot server's climate monitor
uses it.

Wiring: CLAUDE.md "DHT11". VCC must be 3.3 V, never 5 V: by default it comes from BCM 12
(config.DHT11_POWER_PIN, HW2 upper pin, driven HIGH while this runs, LOW again at the end).
The STM32 header 3.3V pin measured 0 V on this board, so --power-pin none is not usable here.
Don't run this while the robot server runs (agriscout.service or robot_server.py by hand): its
climate monitor reads the same DHT11, so both would send start signals on BCM 17, and this tool's
close() switches BCM 12 (the sensor's power) off under the server. Stop it first:
  sudo systemctl stop agriscout          (start it again afterwards: sudo systemctl start agriscout)
The first reading after power-up may fail or be old; the DHT11 needs >= 1 s between readings.
"""

import argparse
import os
import sys
import time

# robot/ uses flat imports (import config), like when its files run as scripts.
# This file is one folder down, so add robot/ to the path.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import config  # noqa: E402
import hardware  # noqa: E402

PERIOD_S = 2.0


def _power_pin(text):
    """--power-pin value: a BCM number, or 'none' (module on a fixed 3.3 V pin)."""
    if text.lower() == "none":
        return None
    try:
        return int(text)
    except ValueError:
        raise argparse.ArgumentTypeError("expected a BCM pin number or 'none', got %r" % text)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Print DHT11 temperature and humidity every 2 s")
    parser.add_argument("--pin", type=int, default=config.DHT11_PIN,
                        help="BCM pin of DATA (default %(default)s)")
    parser.add_argument("--power-pin", type=_power_pin, default=config.DHT11_POWER_PIN,
                        help="BCM pin that powers VCC (3.3 V), or 'none' for a fixed 3.3 V pin "
                             "(default %s)" % config.DHT11_POWER_PIN)
    args = parser.parse_args(argv)

    pi, reason = hardware.connect_pigpio()
    if pi is None:
        print("Cannot read the DHT11: %s." % reason)
        print("Start pigpiod first:  sudo pigpiod -l -p %d" % config.PIGPIO_PORT)
        return 1
    if args.power_pin is None:
        power = "powered from a fixed 3.3 V pin"
    else:
        power = "powered from BCM %d" % args.power_pin
        print("Powering the DHT11 from BCM %d, waiting %.0f s for it to stabilise..."
              % (args.power_pin, config.DHT11_POWER_ON_WAIT_S))
    sensor = hardware.Dht11(pi, args.pin, power_pin=args.power_pin)
    ok = failed = 0
    print("DHT11 on BCM %d, %s, every %.0f s. Ctrl+C to stop." % (args.pin, power, PERIOD_S))
    try:
        while True:
            started = time.monotonic()
            now = time.strftime("%H:%M:%S")
            try:
                temperature, humidity = sensor.read()
            except ValueError as err:
                failed += 1
                print("%s  read failed: %s  (ok %d, failed %d)" % (now, err, ok, failed))
            else:
                ok += 1
                print("%s  %.1f C  %.0f %%RH  (ok %d, failed %d)"
                      % (now, temperature, humidity, ok, failed))
            time.sleep(max(0.0, PERIOD_S - (time.monotonic() - started)))
    except KeyboardInterrupt:
        print("")
    finally:
        sensor.close()
        pi.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
