"""AgriScout plant scouting (runs on the laptop, not the Pi).

Photo from the robot camera -> plant ID from the QR stake -> diagnosis -> advice -> SQLite history
-> field report. Talks to the robot only through the mjpg-streamer snapshot URL (read-only).
"""
