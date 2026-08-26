# RigWatch

A single-file Windows dashboard for live CPU, RAM, per-GPU, per-disk and system-power readings.
Pure Python and tkinter — `psutil` is the only dependency, the icons are embedded in the script, and
there is nothing to install beyond Python itself.

![RigWatch](docs/preview.png)

*Screenshot taken in `--demo` mode, so the numbers are synthetic.*

Built for a two-GPU AMD machine, because AMD has no `nvidia-smi` equivalent on Windows and most
lightweight monitors either merge multiple GPUs into one figure or ignore the second card entirely.
Nothing here is AMD-specific though: utilisation, VRAM and adapter names come from vendor-neutral
WDDM interfaces, so AMD, NVIDIA and Intel cards all show up — separately, one card per panel.

## Run it

```
pip install psutil
python hw_monitor.py
```

Or double-click `run_monitor.bat`, which installs `psutil` on first run.

| Flag | |
| --- | --- |
| `--demo` | synthetic data; runs on any OS, useful for seeing the layout |
| `--debug` | prints discovered adapters, disks and raw counter instances each second |
| `--psu 850` | power-supply rating for this run |
| `--scale 1.25` | force a UI scale factor if DPI detection guesses wrong |

| Key | |
| --- | --- |
| `S` | settings (PSU rating, baseline power, sensor bridge URL) |
| `M` | send the window to the taskbar strip |
| `T` | keep on top |
| `Esc` | quit |

## What it shows

| Card | Contents |
| --- | --- |
| CPU | utilisation ring, 60-second history, one bar per logical core, package watts and temperature, clock, uptime |
| Memory | usage ring, 60-second history, used / total / free, swap |
| Each GPU | utilisation ring, 60-second history, watts and temperature, VRAM bar, shared memory, busiest engine, live/no-data badge |
| Storage | per physical disk: model, bus type, volumes, capacity bar, live read/write, I/O history |
| System power | total draw as a stacked bar against your PSU rating, with headroom |

Every GPU gets its own labelled card and its own colour — nothing is merged or summed across cards.
Gauges turn amber past 90% and red past 97%.

**Resizing.** Drag the window to anything from roughly 620×600 upwards and the whole dashboard
scales: fonts, gauges, graphs, padding and icons (embedded at six sizes, nearest one picked). Below
about 85% it sheds detail — per-core bars first, then the disk I/O plots — to stay readable. The
60-second histories survive the resize.

**Minimised.** Minimise and a compact strip appears on the taskbar showing CPU, RAM, every GPU and
total watts. Click it to restore, drag it anywhere (the position is remembered), right-click for a
menu. Windows 11 removed the deskband API, so nothing can add a real item *inside* the taskbar any
more; this is a borderless always-on-top strip sized to the taskbar and parked next to it, which is
as close as third-party code can get.

![Taskbar strip](docs/taskbar-strip.png)

## Where the numbers come from

**CPU, RAM, disk throughput and capacity** — `psutil`.

**GPU utilisation and VRAM** — the WDDM performance counters, read through PDH (`pdh.dll`) with
ctypes. These are the same counters Task Manager uses, so they work for any vendor:

| Counter | Used for |
| --- | --- |
| `\GPU Engine(*)\Utilization Percentage` | per-adapter utilisation |
| `\GPU Adapter Memory(*)\Dedicated Usage` | VRAM in use |
| `\GPU Adapter Memory(*)\Shared Usage` | shared system memory |

Instance names look like `pid_1234_luid_0x00000000_0x0054E10A_phys_0_eng_0_engtype_3D`, so
utilisation is summed across every process per engine and the busiest engine on an adapter becomes
that adapter's number — the same aggregation Task Manager displays. The engine type driving it
(`3D`, `Compute`, `Copy`, `VideoDecode`, …) is shown beside the VRAM figure.

**GPU names and total VRAM** — those counters identify adapters only by LUID, so names and sizes come
from **D3DKMT** (`gdi32`: `D3DKMTEnumAdapters2` + `D3DKMTQueryAdapterInfo`). It shares the LUID
namespace with the counters, so the mapping is exact rather than guessed. DXGI is kept as a fallback;
it is not the primary path because `CreateDXGIFactory1` returns `E_NOINTERFACE` on some machines,
including the one this was built on.

**Disk models, bus type and volume mapping** — the storage IOCTLs:
`IOCTL_STORAGE_QUERY_PROPERTY` on `\\.\PhysicalDriveN` for model and bus (NVMe / USB / SATA), and
`IOCTL_STORAGE_GET_DEVICE_NUMBER` on `\\.\C:` to find which physical disk a volume lives on. Both
open the device with zero access rights, so neither needs elevation.

**Memory stick count** — parsed from the firmware's SMBIOS table via
`GetSystemFirmwareTable('RSMB')`, type 17 records, so the RAM figure in the power estimate matches
the machine rather than assuming a number.

**CPU package power** — `\Energy Meter(*)\Power` exposes the CPU's RAPL rails in milliwatts and is
readable without elevation. This is a real measurement, and the UI labels it as such. RAPL exists on
both AMD and Intel.

**GPU temperature** — `D3DKMT_ADAPTER_PERFDATA`, query type 62 of `D3DKMTQueryAdapterInfo`. Windows
exposes this to user mode, so GPU die temperature is readable on **any vendor** with no driver, no
vendor library and no elevation. The struct layout was confirmed empirically rather than trusted:
at offset 8 the memory clock matches what Radeon Software reports to the megahertz, and offset 56 is
temperature in deci-Celsius, which read 57.0 °C against Adrenalin's 58 °C and tracked a second card
cooling from 56 °C to 47 °C as it idled. The decode is sanity-gated to 0–150 °C, so on a Windows
build that lays the struct out differently the field simply reads `N/A` instead of showing nonsense.

**NVIDIA GPU power and temperature** — `nvml.dll` ships with every NVIDIA driver (it is the library
behind `nvidia-smi`), so on an NVIDIA card watts are read directly too, with no extra software and
no kernel driver. Devices are matched to adapters by PCI bus/device, falling back to name. If
`nvml.dll` is absent the whole path is a silent no-op.

## GPU support by vendor

| | AMD | NVIDIA | Intel |
| --- | --- | --- | --- |
| Utilisation, VRAM, engine type | yes | yes | yes |
| Card name, total VRAM | yes | yes | yes |
| **GPU temperature**, no extra software | **yes**, via D3DKMT | **yes** | **yes**, via D3DKMT |
| GPU power, no extra software | no — estimated | **yes**, via NVML | no — estimated |
| GPU power with LibreHardwareMonitor running | yes | yes | yes |
| CPU package power | yes, RAPL | yes, RAPL | yes, RAPL |
| CPU temperature | needs LibreHardwareMonitor | needs LHM | needs LHM |
| Board-power figure for the estimate | RX 5000–9000 | GTX 16 / RTX 20–50 | Arc A- and B-series |

Integrated graphics are recognised by name and by having little or no dedicated VRAM, and are given
a small power profile instead of a discrete-card default.

## About power, honestly

Temperature is solved — see D3DKMT above. **Power** is the part that is not. On NVIDIA, NVML gives
it. On **AMD** there is no unprivileged route to board power at all. Every one was tried and
refused:

| Route | Result |
| --- | --- |
| `ADL2_Adapter_PMLog_Support_Get` / `PMLog_Start` | `ADL_ERR_INVALID_ADL_IDX` for every adapter index |
| `ADL2_Overdrive6_CurrentPower_Get`, `Overdrive_Caps` | `ADL_ERR_INVALID_ADL_IDX` |
| ADLX (`amdadlx64.dll`) `ADLXInitialize` | initialises correctly — `TotalSystemRAM` returns the right value, so the ctypes binding is sound — but `GetGPUs` returns an empty list |
| ADLX `ADLXInitializeWithCallerAdl` with a live ADL context | initialises, then access-violates inside `GetGPUs` |
| `D3DKMTQueryAdapterInfo` perf-data types | nothing with a usable power field |

Most sensors are not "already reported to Windows" waiting to be picked up. CPU package temperature
sits behind model-specific registers and the power-management mailbox, board temperatures and fan
tachometers behind the Super I/O chip's I/O ports, DIMM telemetry behind SMBus, and AMD GPU board
power behind the display driver. All of those are ring-0, which is why HWMonitor, HWiNFO,
LibreHardwareMonitor, MSI Center and Adrenalin each ship a kernel driver.

But the subset Windows *does* publish to user mode is bigger than it first appears, and this app
reads all of it: RAPL energy, the WDDM GPU counters, SMART, SMBIOS, the storage IOCTLs — and
`D3DKMT_ADAPTER_PERFDATA`, which is where GPU temperature turned out to be hiding.

### Optional: LibreHardwareMonitor bridge

[LibreHardwareMonitor](https://github.com/LibreHardwareMonitor/LibreHardwareMonitor) is free, open
source, ships that driver, and — unlike MSI Center or the free build of CPUID HWMonitor — publishes
everything it reads over a local HTTP endpoint. Turn on **Options → Remote Web Server → Run** and
this app picks it up on the next sample: GPU watts and both CPU and GPU temperatures switch from
modelled to measured, and the System power card says so. Nothing to configure. If it is not running,
the app falls back quietly and retries every 20 seconds.

Without it, GPU wattage is modelled from utilisation against the card's board power:
`idle + (TBP - idle) × (util/100)^0.85`, always labelled *estimated*. It is a load curve, not a
sensor — compute workloads in particular can sit at 100% utilisation while drawing well under TBP.

### The PSU figure

**A power supply cannot be detected.** It has no data link to the motherboard; nothing anywhere
reports its rating or its output. The exceptions are "digital" units with a USB header — Corsair
HXi/RMi/AXi, NZXT C-series and a few others — which enumerate as USB devices and report real
wattage. Everything else is electrically a dumb box.

So the PSU rating is yours to enter: press **S**, or click the System power card. The same dialog
holds the parts no sensor reports at all — motherboard, RAM, fans, drives, USB — itemised rather
than hidden in one lump, and printed under the bar so you can see what is being assumed:

```json
"baseline_items": {
  "motherboard": 35,   "ram_per_stick": 3, "ram_sticks": 4,
  "fans": 10,          "per_drive": 4,     "usb_peripherals": 10
}
```

`ram_sticks` is filled in from SMBIOS automatically. To calibrate the rest: read a wall meter,
divide by PSU efficiency (~0.90), and adjust until the total lines up. The System power total is an
estimate with a measured core, not a measurement — the card is explicit about which parts are which.

## Failure handling

Every reading is independent. If a query fails, returns nothing, or a LUID can't be matched, that
reading alone shows `N/A`, the card's badge flips to "no data", and everything else keeps updating.
If the GPU counter query stops producing data for ~10 samples the PDH query is rebuilt (this covers
driver restarts). The sampler thread cannot die — a bad value in the config costs you the power
readout, not the app — and the UI refresh loop always re-arms itself.

`debug_counters.bat` (or `--debug`) prints what was discovered, how, and the raw counter instances,
which is the fastest way to see why something reads `N/A`.

## Tested on

Windows 11, Python 3.14, Ryzen 5 7500F, Radeon RX 7800 XT + RX 6650 XT, MSI B650 board.
Python 3.9+ should work.

**Only AMD hardware has actually been exercised.** The NVIDIA and Intel paths are built on
vendor-neutral interfaces (WDDM counters, D3DKMT) plus NVIDIA's own NVML, and every one of them
degrades to `N/A` rather than failing if something is missing — but they are untested on real
hardware. If you run this on an NVIDIA or Intel card, an issue saying what worked and what didn't
would be genuinely useful. `--debug` output is the most helpful thing to attach.

## Files

| File | |
| --- | --- |
| `hw_monitor.py` | the whole app, including the embedded icon assets |
| `run_monitor.bat` | launcher; installs `psutil` if needed |
| `debug_counters.bat` | runs with `--debug` and keeps the console open |
| `make_icons.py` | regenerates the embedded icon set (needs Pillow; only if you want to change the icons) |
| `monitor_config.json` | written on first run next to the script; git-ignored, since it is per-machine |

## Licence

MIT — see [LICENSE](LICENSE).
