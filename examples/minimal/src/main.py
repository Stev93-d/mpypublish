"""
The least an app needs to be updated over the network.

It says which version it is every few seconds, and once it is on the WiFi it
answers `mpypublish push`. It touches no pin, so it runs on any board.

WiFi name and password are in /wifi.json on the board, which a deploy never
touches (`keep` in mpypublish.toml):

    {"ssid": "my network", "password": "..."}
"""

import json
import time

import network

import mpypublish
from mpypublish.httpd import Server
from mpypublish.updater import Updater

SAY_EVERY_MS = 5000

wlan = network.WLAN(network.STA_IF)
wlan.active(True)
try:
    # An absolute path: the current folder is the copy of the app, /a or /b
    with open("/wifi.json") as f:
        wifi = json.load(f)
    wlan.connect(wifi["ssid"], wifi["password"])
except (OSError, ValueError, KeyError):
    print("minimal: no /wifi.json, staying off the network")

version = mpypublish.version()["version"]
server = None
said = time.ticks_ms()
print("minimal: running")

while True:
    if wlan.isconnected():
        if server is None:
            server = Server(Updater())
            print("minimal: updates at http://%s/ota" % wlan.ifconfig()[0])
        server.poll()
        # On the network and answering: if this copy is on trial, it has shown enough
        mpypublish.confirm()

    if time.ticks_diff(time.ticks_ms(), said) >= SAY_EVERY_MS:
        said = time.ticks_ms()
        print("minimal: version %s, from %s" % (version, mpypublish.app_dir()))

    time.sleep_ms(10)
