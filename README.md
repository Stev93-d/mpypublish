# mpypublish

Deploys a MicroPython app into two copies on the board, so that a bad upload never leaves you with nothing running.

I wrote it for a WiFi thermostat I am building on an ESP32-S3. A thermostat ends up in a wall box, and from then on a version that does not start means going there with a laptop and a cable. I wanted updates that either work or undo themselves. It began as a script inside that project; this is the same mechanism without the thermostat around it.

## What it does

- The app lives in `/a` or `/b` on the board. An upload always goes into the copy that is **not** running, so the version of before stays whole.
- Over USB it sends only the files that differ from what that copy already has, then reads the SHA-256 of every file back from the board.
- It can compile to `.mpy` with `mpy-cross` on the way.
- After an upload it starts the app and checks, from the boot log, that what runs is the version it just sent.
- The same package can be sent **over WiFi**. A version that arrives that way starts *on trial*: if it crashes, hangs, or never says it works, the board goes back to the one of before by itself.
- `mpypublish rollback` goes back by hand.

This is a package that raises an exception at start, sent on purpose. 72 seconds from the command to the last line:

```
$ mpypublish push 192.168.1.50 --sabotage crash
minimal: 5 files, 32 KB (sources), version 1.0.4 (not in git)
minimal: version 1.0.3 (not in git) in /a, MicroPython 1.27.0
Package broken on purpose: crash
minimal-1.0.4.zip: version 1.0.4-crash (not in git), 5 files, 30 KB
  5/5 files, 100%
Package checked: the board restarts with the new version on trial
  version 1.0.3 (not in git) answers
The new version did not confirm: version 1.0.3 (not in git) is back
```

## Install

```
pip install mpypublish
```

It needs Python 3.11 or later and brings `mpremote` with it. To compile to `.mpy` you also need the `mpy-cross` of the MicroPython that is on your board:

```
pip install mpy-cross==1.27.0.post2
```

## First deploy

Put a `mpypublish.toml` next to your code:

```toml
[app]
name = "thermostat"
version = "1.3.0"
mpy = true                      # needs mpy-cross

[[source]]
from = "src"                    # everything in here goes on the board
exclude = ["*.md", "tests/"]

[board]
keep = ["settings.json", "data/"]     # files of the board a deploy never touches
ready = "Main loop started"           # a line your app prints once it is up
```

Then, with the board on USB. This is `examples/minimal`, a whole project with the update over the network too:

```
$ mpypublish deploy
minimal: 5 files, 32 KB (sources), version 1.0.0 (77855df)
  for the update over the network: C:\mpypublish\examples\minimal\build\minimal-1.0.0.zip
Board on COM10
Stopping the app on the board...
Slot: /a
  to copy: 8 of 8 files
  8/8 files copied
8 files on the board, hashes verified
Starting the app...
Running: version 1.0.0 (77855df) in /a
```

Between 20 and 30 seconds on my board. Two deploys later, with one source changed, the copy was 4 files of 8: `main.py`, `version.json`, `manifest.json` and `ota.json`.

If the board has code in its root, as after `mpremote cp`, the first deploy lists the files it is going to remove and asks before doing it:

```
This board has code in its root: from now on the app lives in /a and /b.
These files will be removed, and boot.py replaced by the one that chooses the slot:
  main.py
  helper.py
Go on? y/N:
```

A no leaves the board as it was, with its app running again.

To try the example with the update over the network, give the board your WiFi first:

```
cd examples/minimal
cp wifi.example.json wifi.json          # then put your network in it
mpremote cp wifi.json :wifi.json
mpypublish deploy
mpremote                                # the app says the address it answers at; Ctrl-] to leave
mpypublish push 192.168.1.50
```

## What changes for your app

**The current folder is the copy, not the root.** `boot.py` enters `/a` or `/b` before `main.py` starts, so imports and the files that came with the package (`open("pages/index.html")`) work as before. Files that have to survive an update go in the root, with an absolute path, and in `keep`:

```python
with open("/settings.json") as f:      # not "settings.json": that would be /a/settings.json
    settings = json.load(f)
```

**`boot.py` belongs to mpypublish.** A `boot.py` in your sources stops the build: move what it does to the top of `main.py`.

**`/lib` is left alone.** What `mip` installed there is shared by the two copies. A `lib/` folder in your sources goes into the copy and is on the path as well.

**`main.py` has to be at the top of the package**, and is never compiled.

## Updates over the network

The package brings a small `mpypublish` module to the board. An app with no web server of its own needs this much:

```python
import mpypublish
from mpypublish.httpd import Server
from mpypublish.updater import Updater

server = Server(Updater())             # port 80, under /ota

while True:
    server.poll()                      # never waits
    do_the_work()
    if wifi.isconnected() and sensor_ok():
        mpypublish.confirm()           # does nothing when there is nothing to confirm
```

An app that has a web server passes the requests under `/ota` to `updater.handle(method, path, query, body)`, which returns a status and a dictionary to send back as JSON, and calls `updater.poll()` in its loop. With asyncio, `asyncio.create_task(server.serve())`.

Then, from the PC:

```
$ mpypublish push 192.168.1.50
minimal: 5 files, 32 KB (sources), version 1.0.3 (not in git)
minimal: version 1.0.1 (not in git) in /b, MicroPython 1.27.0
minimal-1.0.3.zip: version 1.0.3 (not in git), 5 files, 32 KB
  5/5 files, 100%
Package checked: the board restarts with the new version on trial
  version 1.0.3 (not in git) answers
Updated and confirmed: version 1.0.3 (not in git)
```

14 seconds, restart included.

`confirm()` is the point of the whole thing: call it when the app has shown it can do its job **and can still be updated**, so on the network and answering. A version that cannot be reached any more is one you can only fix with a cable.

A copy on trial starts with a 60 second watchdog. That is what brings back the version of before when the new one hangs.

### PIN

Without a PIN, whoever reaches the board on the network can update it. To ask for one:

```
mpypublish pin 4321
mpypublish push 192.168.1.50 --pin 4321
```

The PIN is not sent: the manifest of the package is signed with it (HMAC-SHA256) together with a random number the board gives out for one attempt, so a recorded update cannot be sent again. The content of the package still travels in the clear. This is for a home network, not for a board exposed to the internet.

## When something goes wrong

| What happens | What you get |
| --- | --- |
| The new version raises an exception at start | Back to the version of before, a little over a minute later. `push` ends with exit code 2 |
| The new version hangs at start | The same: the trial watchdog restarts the board |
| The new version runs but never calls `confirm()` | It stays until the next restart, then the one of before comes back. `push` says so |
| A file arrives different from its hash | The update is refused before anything is switched. The running copy was never touched |
| The PC goes away halfway through | After 60 seconds without data the board drops the update |
| The package is for another app, or compiled for another MicroPython | Refused at the first request |
| The board hangs during an upload over USB | A watchdog restarts it and the copy goes on from the files still missing |
| The upload over USB fails for good | The board is restarted with the app it had |
| The upload over USB is cut short (Ctrl-C, cable pulled) | The board starts nothing until `mpypublish deploy` is run again: `/hold` is still there |
| The version uploaded over USB does not start | `deploy` shows the boot log and ends with exit code 1. `mpypublish rollback` |

Four of these can be made to happen on a board on your desk:

```
mpypublish push 192.168.1.50 --sabotage crash      # or hang, or corrupt
mpypublish push 192.168.1.50 --stop-after 2
```

## Commands

| | |
| --- | --- |
| `mpypublish build` | Builds the package in `build/` and its `.zip`. The board is not touched |
| `mpypublish deploy` | Builds, uploads over USB, starts the app and checks it |
| `mpypublish push HOST` | Builds, sends over the network, waits for the confirmation. `--zip FILE` sends a package already built |
| `mpypublish rollback` | Makes the copy of before the active one and starts it |
| `mpypublish ls` | Copies, versions and files on the board |
| `mpypublish pin PIN` | Sets the PIN for the updates over the network; `--remove` takes it away |

With one board connected the port is found by itself; with more, `-p COM8`. `--no-mpy` uploads the sources. `ls`, `pin` and `rollback` interrupt the app to talk to the board, and restart it.

## How it works

```
/boot.py        chooses the copy to start. Written only over USB
/ota.json       which copy is active, whether the other is on trial, how the last update ended
/a/  /b/        the two copies, each with its version.json and manifest.json
/settings.json  your data, in the root, shared by the two copies
/lib/           what mip installed, shared as well
```

A deploy over USB stops the app (a file `/hold` tells `boot.py` to start nothing), takes `main.py` away from the copy it is about to write, copies the modules, then `main.py`, then `boot.py` and `ota.json` last. A copy without `main.py` is one `boot.py` does not start, so a half written copy is never mistaken for a whole one.

An update over the network writes the other copy while the app runs, reads every file back from the flash against the manifest, and only then writes `{"active": "a", "trial": "b", "boots": 0}` and restarts. `boot.py` starts a copy on trial once. `confirm()` rewrites the file as `{"active": "b"}`. Without it, the next restart finds `boots: 1`, goes back to `a` and records `rolled_back`.

Every package carries the version from `mpypublish.toml`, the git commit it was built from (with `-dirty` if there were uncommitted changes) and the build time. `mpypublish.version()` gives them to the app.

## Limits

- **Tried on one board**: an ESP32-S3-Zero with MicroPython 1.27, from Windows. There I ran the deploy over USB, the rollback, the updates over WiFi with and without a PIN, and five of the failures above on purpose (crash, hang, corrupted file, sending stopped halfway, a version that does not start over USB). The rest is covered only by the tests on the PC. Other ESP32 should behave the same; I have not tried any other port.
- **The watchdogs want 60 seconds.** Where the hardware watchdog cannot wait that long (RP2 stops at about 8) the upload and the trial go without, so a version that *hangs* on trial is not undone there. One that raises an exception stays at the REPL until the next restart.
- **An app with no watchdog of its own** gets one extra restart about a minute after an update over the network: the trial watchdog cannot be stopped, only fed. Either confirm within the minute and accept the restart, or arm your own `machine.WDT` and feed it.
- **Two copies take twice the room.** The device side of mpypublish adds about 10 KB per copy compiled, 32 KB as sources.
- **MicroPython itself and `boot.py`** are not updated over the network.
- **The app has to be interruptible from USB.** An app that turns off Ctrl-C cannot be stopped for an upload.
- **`ready` is looked for in what the app prints**, so an app that prints nothing can only be checked for not crashing in its first seconds.

## Two things I learned the hard way

**A board can go silent in the middle of an upload.** USB port still there, nothing answering, not even to `esptool`, until the cable is pulled. I saw it on the S3 with MicroPython 1.27. This is why every command of an upload arms a watchdog on the board and the copy can pick up from where it stopped.

**Nothing else should have the serial port.** Cura, with its USB printing plugin on, probes every serial port that appears and writes to it. The board took that as input and from then on accepted nothing from USB until it was restarted. Close it, or turn the plugin off, before a deploy.

## Compared with

- [mpremote](https://docs.micropython.org/en/latest/reference/mpremote.html) is what mpypublish uses to talk to the board. By itself it copies files in place: there is no version of before to go back to.
- [mpytool](https://github.com/cortexm/mpytool) and [mp-sync](https://github.com/stratomancer/mp-sync) also send only what changed, and do much more around the file system. They update the app in place as well.
- [micropython-esp32-ota](https://github.com/glenn20/micropython-esp32-ota) updates the MicroPython firmware itself over the network, with the rollback of the ESP32 bootloader. mpypublish does the same job one level up, for the `.py` and `.mpy` files of the app. The two do not overlap.

## Tests

```
python -m unittest discover -s tests
```

No board is needed: the code that runs on the board is tested on the PC against fake `machine`, sockets and clock.

## License

MIT
