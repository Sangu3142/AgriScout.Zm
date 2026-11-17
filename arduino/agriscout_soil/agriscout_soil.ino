// AgriScout soil + pump — Uno decides on its own, Pi only displays SOIL lines
const uint8_t SOIL_PIN   = A0;
const uint8_t RELAY_PIN  = 7;
const uint8_t SENSOR_PWR = 8;                // D8 powers the soil sensor
const bool RELAY_ACTIVE_HIGH = false;        // this relay switches ON with LOW

const int FAULT_BELOW    = 100;              // below this = A0 unplugged / probe shorted -> change nothing (0 turns this off)
const int DRY_ABOVE      = 800;              // above this = dry -> water
const int NO_PROBE_ABOVE = 1010;              // at/above this = probe in air, never pump (air = 1023)

const unsigned long START_MS       = 2000;   // wait after power-up before first burst
const unsigned long DRY_CONFIRM_MS = 10000;   // soil must read dry, unbroken, this long before a burst (5000-10000)
const unsigned long PUMP_MS        = 20000;   // each burst lasts 15 s
const unsigned long SOAK_MS        = 3000;   // wait between bursts so water reaches the probe
const int MAX_BURSTS               = 10;      // safety: stop after 5 dry bursts in a row
const unsigned long REPORT_MS      = 1000;   // check soil every 1 s
const uint8_t SAMPLES              = 10;     // readings averaged per check

bool pumpOn = false, lockedOut = false, pumpedYet = false;
int burstsInRow = 0;
unsigned long pumpStart = 0, lastPumpEnd = 0, lastReport = 0;
unsigned long dryStart = 0;                  // when the current unbroken dry spell began (0 = not dry now)

void relay(bool on) { digitalWrite(RELAY_PIN, (on == RELAY_ACTIVE_HIGH) ? HIGH : LOW); }

int readSoil() {                             // average SAMPLES readings to smooth the wobble
  int sum = 0;                               // 10 x 1023 = 10230 fits in a 16-bit int
  for (uint8_t i = 0; i < SAMPLES; i++) { sum += analogRead(SOIL_PIN); delay(2); }
  return sum / SAMPLES;
}

void setup() {
  relay(false); pinMode(RELAY_PIN, OUTPUT); relay(false);   // relay OFF before anything else (keep both calls)
  pinMode(SENSOR_PWR, OUTPUT); digitalWrite(SENSOR_PWR, HIGH);
  Serial.begin(9600); Serial.println(F("READY"));
}

void loop() {
  unsigned long now = millis();

  // 1. Turn the pump off when the burst time is up (always enforced)
  if (pumpOn && now - pumpStart >= PUMP_MS) {
    relay(false); pumpOn = false; lastPumpEnd = now; Serial.println(F("PUMP OFF"));
  }

  // 2. Every second: read soil, report it, decide
  if (now - lastReport >= REPORT_MS) {
    lastReport = now;
    int soil = readSoil();
    Serial.print(F("SOIL ")); Serial.println(soil);

    bool waitedEnough = pumpedYet ? (now - lastPumpEnd >= SOAK_MS) : (now >= START_MS);

    if (soil < FAULT_BELOW) {                      // implausibly low: wiring fault, not wet soil
      Serial.println(F("SENSOR FAULT"));
      dryStart = 0;                                // counters untouched, dry timer restarts
    } else if (soil <= DRY_ABOVE) {                // wet enough -> reset safety counter
      burstsInRow = 0; lockedOut = false; dryStart = 0;
    } else if (soil >= NO_PROBE_ABOVE) {           // probe in air -> restart the dry timer
      dryStart = 0;
    } else {                                       // reads dry soil
      if (dryStart == 0) dryStart = now;           // first dry reading of this spell
      if (!pumpOn && !lockedOut && waitedEnough && now - dryStart >= DRY_CONFIRM_MS) {
        if (burstsInRow >= MAX_BURSTS) { lockedOut = true; Serial.println(F("PUMP LOCKOUT")); }
        else {
          relay(true); pumpOn = true; pumpStart = now;
          burstsInRow++; pumpedYet = true; Serial.println(F("PUMP ON"));
        }
      }
    }
  }
}