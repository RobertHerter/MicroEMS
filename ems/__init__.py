"""EMS – Energy Management System für Haus-Akku, PV und Fahrzeug.

Liest Eingangsdaten aus InfluxDB, prognostiziert den Hausverbrauch,
berechnet per MILP die kostenoptimale Steuertabelle (48 h) und gibt die
Steuerbefehle per MQTT an Homey aus. Läuft als Python-Dienst auf dem Pi.
"""

__version__ = "1.4.0"
