// Soil sensor calibration: prints the raw reading once a second
void setup() {
  Serial.begin(9600);
}`
void loop() {
  int raw = analogRead(A0);   // 0..1023; drier soil usually = higher number
  Serial.print("SOIL ");
  Serial.println(raw);
  delay(1000);
}