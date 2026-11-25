# RigWatch

A hardware monitor for Windows that shows what your PC is actually doing: processor, graphics,
memory, storage, network and power, with live charts, a full sensor table, a compact
always-on-top view and live numbers on the taskbar.

Everything on screen is **measured**. When a sensor cannot be read, RigWatch says "not available"
and explains why. It never fills the gap with an estimate.

![RigWatch](docs/preview.png)

*The computer name and network addresses are hidden in these screenshots
(Settings → Hide identifying details).*

> **Version 2 is a complete rewrite.** The original single-file tkinter version is kept as
> [`v1.0`](https://github.com/danielemilushev-hub/RigWatch/tree/v1.0).

## Start it

**Easiest:** download `RigWatch.exe` from the [latest release](https://github.com/danielemilushev-hub/RigWatch/releases/latest) and
double-click it. No Python needed.

**From source:** double-click **`RigWatch.bat`**. The first run creates a private Python environment
(about a minute, needs Python 3.11+ and an internet connection) and fetches the sensor engine used for
CPU temperature (about 3 MB); after that it opens straight away.

Optional: run `tools\create-shortcut.ps1` to put a RigWatch shortcut on your desktop.

### Standalone RigWatch.exe

`RigWatch.exe` is a single file that runs on Windows 10/11 PCs **without Python installed**. Copy it
anywhere and double-click it. It needs the Microsoft Edge WebView2 runtime, which is part of
Windows 11 and present on almost every up-to-date Windows 10 PC.

Build it yourself (the result lands in `dist\`):

```powershell
powershell -ExecutionPolicy Bypass -File tools\build-exe.ps1
```

The exe is not code-signed, so on another PC Windows SmartScreen may show "Windows protected your PC"
the first time (More info → Run anyway), and some antivirus tools are suspicious of unsigned
single-file apps. Signing it with a code-signing certificate removes both.

The command-line flags below work with the exe too (`RigWatch.exe --json`, `RigWatch.exe --server`, …).

From a terminal:

```powershell
.\.venv\Scripts\python.exe -m rigwatch            # the app window
.\.venv\Scripts\python.exe -m rigwatch --browser  # open in your browser instead
.\.venv\Scripts\python.exe -m rigwatch --server   # only the local service (http://127.0.0.1:47115)
.\.venv\Scripts\python.exe -m rigwatch --json     # print one reading as JSON and exit
.\.venv\Scripts\python.exe -m rigwatch --minimized  # start minimised, showing only the taskbar strip
```

## What you get

| View | Shows |
|---|---|
| Overview | Health verdict, one card per component, all temperatures, busiest apps |
| Processor | Utilisation, clock, measured package power, load per thread, clock and power per core |
| Graphics | Utilisation, temperature, hotspot, measured board power, fan, video memory, clocks, voltage, workload by engine, apps using the card |
| Memory | In use, available, committed, cache, kernel pools, largest apps, installed modules |
| Storage | Per drive: temperature, health (rated life left), activity, read/write speed, lifetime totals, volumes |
| Network | Download/upload on the default route, session and startup totals, every active adapter |
| Apps | All apps with processor, memory, graphics and video-memory use; sortable and filterable |
| All sensors | Every reading with min / max / average, CSV export, and where each number comes from |
| Mini | Compact always-on-top panel for gaming or working full-screen |

Dark and light themes, °C or °F, refresh rate from twice a second to every 5 seconds, and a switch
that hides the computer name and network addresses for screenshots and streaming.

![Graphics page](docs/graphics.png)

### Minimised: numbers on the taskbar

Minimise the window and a compact strip appears on the taskbar, next to the weather widget, showing
processor load, memory, each graphics card's load and temperature, and measured watts. Click it to
bring the window back, drag it anywhere (the position is remembered), right-click for a menu.
Turn it off under Settings → Taskbar strip.

![Taskbar strip](docs/taskbar-strip.png)

Windows 11 removed the deskband API, so nothing can add a real item *inside* the taskbar any more.
This is a borderless always-on-top strip sized to the taskbar and parked on it, which is as close as
third-party code can get. It never takes keyboard focus.

## Where the numbers come from

No administrator rights and no kernel driver are needed for anything in this table. CPU temperature,
case fans and voltages are the exception, covered below.

| Reading | Source |
|---|---|
| CPU utilisation and clock per thread | Windows performance counters (`Processor Information`), the same figures Task Manager uses |
| CPU package and per-core power | The processor's own energy counters (RAPL) via the Windows `Energy Meter` counters |
| GPU utilisation, video memory, per-app GPU use | Windows performance counters (`GPU Engine`, `GPU Adapter Memory`, `GPU Process Memory`) |
| GPU identity and real video-memory size | DXGI |
| Radeon temperature, hotspot, fan, clocks, voltage, board power | AMD ADL, loaded from the active driver's own folder |
| GeForce temperature, fan, clocks, power | NVIDIA NVML |
| GPU temperature on any other card | Windows display kernel (`D3DKMT`), the reading Task Manager shows |
| Drive temperature, wear, power-on hours, data written | Queried from each drive (`IOCTL_STORAGE_QUERY_PROPERTY`, NVMe health log) |
| Drive activity and speed | Windows disk statistics (`IOCTL_DISK_PERFORMANCE`) |
| Memory, network, processes | Windows, via `psutil` and `GetPerformanceInfo` |
| Motherboard, BIOS, memory modules | SMBIOS firmware table |

### CPU temperature, case fans and voltages

Windows keeps these behind administrator rights. The processor's thermal sensor and the motherboard's
sensor chip can only be reached through a kernel driver, which is why Task Manager shows neither, and
why every monitor that does show them (LibreHardwareMonitor, HWMonitor, HWiNFO) runs elevated.

RigWatch reads them with a small **sensor helper**. Turn it on under **Settings → Sensors**, or from the
"not available" link next to the processor temperature.

- Turning it on shows one Windows permission prompt. That registers an on-demand scheduled task,
  "RigWatch Sensor Helper". Nothing is added to Windows start-up.
- After that RigWatch starts the helper when it opens, without a prompt, and the helper exits about
  25 seconds after RigWatch closes. RigWatch itself never runs elevated.
- The helper only reads. It uses LibreHardwareMonitor's sensor engine (`rigwatch/vendor/lhm`, MPL-2.0)
  through the [PawnIO](https://pawnio.eu) driver, the same pair LibreHardwareMonitor and FanControl use.
- PawnIO is a separate, free install. RigWatch does not install a driver for you; it tells you when
  PawnIO is missing and picks it up as soon as it is there.
- A read that fails is dropped. It is never shown as 0.
- Turning the helper off removes the scheduled task again.

Two things to know before turning it on:

- The helper runs with administrator rights from the folder RigWatch is in. Anything able to change the
  files in that folder could use it to gain those rights, so keep RigWatch somewhere only you (or only
  administrators) can write to.
- If an online game's anti-cheat objects to the PawnIO driver, turn the helper off or uninstall PawnIO.

A running [LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) with
**Options → Remote Web Server → Run** is still picked up automatically (`127.0.0.1:8085`) while the
helper is off.

### Not included

- Frame rate (FPS): reading it reliably needs administrator rights or injecting into games.
- Whole-system power at the wall: only the CPU package and the graphics card report measured draw.

## For developers

The service listens on this computer only (`127.0.0.1:47115`; a free port is used if that one is taken).

| Endpoint | Returns |
|---|---|
| `GET /api/snapshot` | Latest reading of every sensor |
| `GET /api/static` | Hardware inventory and the status of each data source |
| `GET /api/history` | Up to 15 minutes of history for the charted series |
| `GET /api/sensors` | Flat sensor list with min / max / average |
| `GET /api/stream` | Server-sent events, one snapshot per sample |
| `GET /api/sensors.csv`, `/api/history.csv` | CSV exports |
| `GET /api/helper` | State of the sensor helper (off, starting, on, driver missing, …) |

Layout:

```
rigwatch/
  sensors/        one module per data source (pdh, dxgi, d3dkmt, adl, nvidia, storage, smbios, winsys, bridge)
                  helper.py is the elevated sensor helper (self-contained); helper_client.py is the app's side of it
  vendor/lhm/     LibreHardwareMonitor's sensor engine and its licences. Not kept in git: fetched from
                  nuget.org by tools/fetch-sensor-engine.py (pinned SHA-512), bundled into the exe by the build
  collector.py    background sampler: snapshot, history, min/max, health alerts
  server.py       local HTTP API and static files
  app.py          window, mini mode, command line
  strip.py        the taskbar strip shown while minimised
  ui/             the interface (plain HTML, CSS and ES modules; no build step)
tools/            build-exe.ps1 + rigwatch.spec (standalone exe), create-shortcut.ps1
art/              Blender scripts that build and export the 3D illustrations
tests/            python -m unittest discover -s tests
```

Run the tests with `.\.venv\Scripts\python.exe -m unittest discover -s tests -v`.

Notes for anyone extending the sensor layer:

- The sensor helper was run on an AMD Ryzen 5 7500F with an MSI B650 board (Nuvoton NCT6687D): CPU
  temperature, board temperatures, fans and voltages all read correctly. Intel processors and other
  board chips go through the same engine but have not been run here.
- The NVIDIA and Intel paths follow the vendors' documented APIs but were built on an all-AMD machine
  and have not been run on that hardware yet.
- On some Windows installs the classic performance counters (`\Memory`, `\PhysicalDisk`, …) fail when
  opened from Python (`psutil.swap_memory()` raises too). RigWatch only uses the newer counter sets and
  direct Win32 calls, so it is unaffected; avoid adding dependencies on the classic ones.
- AMD's `atiadlxx.dll` in `System32` can belong to a different driver package than the one running
  the card. `sensors/adl.py` loads the copy from the active driver's folder first for that reason.

## Artwork

- 3D hardware illustrations and the app icon are modelled and rendered in Blender:
  `art/blender/build_assets.py` → `art/export_assets.py` → `art/finalize_assets.py`.
- Backdrop photography (`rigwatch/ui/assets/bg`) was generated with cyberninjas.

## Tested on

Windows 11, Python 3.14, Ryzen 5 7500F, Radeon RX 7800 XT, MSI B650 board.

**Only AMD hardware has actually been exercised.** The NVIDIA and Intel paths use the vendors'
documented interfaces and degrade to "not available" rather than failing, but they are untested on
real hardware. If you run this on an NVIDIA or Intel card, an issue saying what worked and what
didn't would be genuinely useful; the output of `RigWatch.exe --json` is the most helpful thing to attach.

## Licence

MIT — see [LICENSE](LICENSE).
