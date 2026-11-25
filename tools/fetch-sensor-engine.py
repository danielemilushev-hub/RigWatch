"""Fetches the sensor helper's engine into rigwatch/vendor/lhm.

The helper (rigwatch/sensors/helper.py) reads CPU temperature, fans and voltages
through LibreHardwareMonitorLib. This script downloads that library and the
packages it depends on from nuget.org, checks each download against the SHA-512
pinned below, and unpacks the .NET Framework builds plus their licences.

    .venv\\Scripts\\python.exe tools\\fetch-sensor-engine.py          verify against the pinned hashes
    .venv\\Scripts\\python.exe tools\\fetch-sensor-engine.py --pin    print hashes for a new version

To move to a newer engine: change ROOT_PACKAGE, run with --pin, review, paste the hashes.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import sys
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT_PACKAGE = ("LibreHardwareMonitorLib", "0.9.6")
FEED = "https://api.nuget.org/v3-flatcontainer"
TARGET = Path(__file__).resolve().parents[1] / "rigwatch" / "vendor" / "lhm"

# Best match first: the helper runs on the .NET Framework that ships with Windows.
LIB_FOLDERS = ["net472", "net471", "net47", "net462", "net461", "netstandard2.0"]
DEP_GROUPS = [".NETFramework4.7.2", ".NETFramework4.7.1", ".NETFramework4.7", ".NETFramework4.6.2",
              ".NETFramework4.6.1", ".NETStandard2.0"]

# SHA-512 of each .nupkg, recorded when the version was adopted (--pin prints them).
PINNED: dict[str, str] = {
    "LibreHardwareMonitorLib/0.9.6":
        "9f09fa278b44ba2414701660c6d5870722923c4ea4c7d55f405c77e8b6b44df7b38ce5c94df9637508edd6330558670d04b5f438797caf3fe8c063cd4ea24f14",
    "DiskInfoToolkit/1.1.2":
        "4e33413c4b620bb813b3f90aec7b32a596eba2a2787c13a610a016f9778e4d484629bfd9617d1cb7b8c4c4455057f36058a33ca65b60ded59a60eaee091e8c8a",
    "HidSharp/2.6.4":
        "e6c8b488d3391594877e8755f7146722ecb04e519712749b74e79e54b970c0f1e839711cd620ee1e4557800ef35738551792bbae99f7cc3be879070e3e3f8a67",
    "RAMSPDToolkit-NDD/1.4.2":
        "4691c8dec31204844ef0354c557c8748b763e0c581fab0f9aeb3a332d8e87af91915d1b42a510e8c764633057f74d0cbab4e63e18e0a2e30def978c18b220197",
    "System.Management/10.0.2":
        "17029ab8f8836d98c6c85beec1ae94fab0c99b82dea00a2483fa54389846cbf7d61ed6a294290764a7132c262db741d521a1baeef482b3885667e54b6d1174fd",
    "System.Memory/4.6.3":
        "35770d6255a85dee5ccfdb1bf07ba8eb1d9d099558921c0ab60134d27fcca08f15e1923feffb7e108e5b3a14059597c58e3a8cf3e53aa6b8d4fda011b7b1ffb4",
    "System.Threading.AccessControl/10.0.3":
        "e576d130f75c7d2ce67305073a74329742d21be4877ea9b424ed30a15169ad5f0a38bae4f316554e93b2c46160db56da431d63c63c55f734d7af7b7a43084719",
    "BlackSharp.Core/1.0.7":
        "b7951aab75ba837deaf8f9a68e9ff38c253fe2ff9a50c7ce76476d139eadb1faeec44c35f3e17fd67d384d27b402331b2259f7d2d4f8839b92c758a01a33175e",
    "System.CodeDom/10.0.2":
        "0078088c5a7ac104f4255cc334201c854164aa77a4c121f5c88f9c0cdf3f0fe52fbd8e40e2467c812d026a402d440b547944ca56e96cafda7099fd8fad49658f",
    "System.Buffers/4.6.1":
        "aaf7bf745c040b07a14969599a992bae521e0130afa3f1f0da4a3232b515703072e605c042b9f05fca47128aa08fc0e092b5ae596d43ad06c5aa831ba3e16fae",
    "System.Numerics.Vectors/4.6.1":
        "feb92fa547943e5098ff6a985502a252c8f920a69765ccb6f924001807de980772105e408088183853524cc5835e8d3d256157f4707ec15d720b28b63308b74c",
    "System.Runtime.CompilerServices.Unsafe/6.1.2":
        "b76697589641900911ad33a7c37d4e0442ca1154830f962f0b73b975768716c47aebf9d14ca9b5cb722ae8dc05239bb92252ab5987dd6a7eba57e18a2a463c85",
    "System.Security.AccessControl/6.0.0":
        "64a36a103b954ab4b7e8a76c0e876579bd484c308e444c2d915fb9a0fd05ad63614501ed235c544afc9b431cb8a4cf0f0715b8ed414e85958e6d68579168fb45",
    "System.Security.Principal.Windows/5.0.0":
        "44a920aaaf22b2172d41319bb57ab2b8e1a4531d5f02192a6f53a81d875125195b60ba0b5a44a45981d137fd7b0f3a65b12959b5fd97afc0578cd84ef27467cd",
}


def download(package: str, version: str) -> bytes:
    name = package.lower()
    url = f"{FEED}/{name}/{version}/{name}.{version}.nupkg"
    with urllib.request.urlopen(url, timeout=60) as resp:
        return resp.read()


def dependencies(nuspec: ET.Element) -> list[tuple[str, str]]:
    ns = {"n": nuspec.tag.split("}")[0].strip("{")}
    groups = nuspec.findall(".//n:dependencies/n:group", ns)
    by_framework = {g.get("targetFramework"): g for g in groups}
    group = next((by_framework[f] for f in DEP_GROUPS if f in by_framework), None)
    if group is None:
        return []
    out = []
    for dep in group.findall("n:dependency", ns):
        version = re.sub(r"[\[\]\(\)]", "", dep.get("version", "")).split(",")[0].strip()
        out.append((dep.get("id"), version))
    return out


def licence_of(nuspec: ET.Element) -> str:
    ns = {"n": nuspec.tag.split("}")[0].strip("{")}
    lic = nuspec.find(".//n:metadata/n:license", ns)
    if lic is not None and lic.text:
        return lic.text.strip()
    url = nuspec.find(".//n:metadata/n:licenseUrl", ns)
    return url.text.strip() if url is not None and url.text else "see package"


def project_url(nuspec: ET.Element) -> str:
    ns = {"n": nuspec.tag.split("}")[0].strip("{")}
    for tag in ("repository", "projectUrl"):
        node = nuspec.find(f".//n:metadata/n:{tag}", ns)
        if node is not None:
            value = node.get("url") or (node.text or "").strip()
            if value:
                return value
    return ""


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch the sensor helper's engine from nuget.org")
    parser.add_argument("--pin", action="store_true", help="print SHA-512 hashes instead of verifying them")
    args = parser.parse_args()

    queue, seen, files, notice, hashes = [ROOT_PACKAGE], {}, {}, [], {}
    while queue:
        package, version = queue.pop(0)
        if package.lower() in seen:
            continue
        seen[package.lower()] = version
        blob = download(package, version)
        digest = hashlib.sha512(blob).hexdigest()
        key = f"{package}/{version}"
        hashes[key] = digest
        if not args.pin and PINNED.get(key) != digest:
            print(f"REFUSED {key}: SHA-512 {digest[:16]}… does not match the pinned value.")
            return 1
        with zipfile.ZipFile(io.BytesIO(blob)) as zf:
            names = zf.namelist()
            nuspec = ET.fromstring(zf.read(next(n for n in names if n.endswith(".nuspec"))))
            # Most packages keep their builds in lib/<framework>/; ones with per-platform builds
            # (LibreHardwareMonitorLib) keep them in runtimes/win-x64/lib/<framework>/.
            prefixes = [f"{root}lib/{f}/" for f in LIB_FOLDERS for root in ("runtimes/win-x64/", "")]
            prefix = next((p for p in prefixes if any(n.lower().startswith(p) and n.lower().endswith(".dll")
                                                      for n in names)), None)
            dlls = [n for n in names if prefix and n.lower().startswith(prefix) and n.lower().endswith(".dll")]
            for name in dlls:
                files[Path(name).name] = zf.read(name)
            licences = [n for n in names if "/" not in n and re.match(r"(licen[sc]e|notice|third-?party)", n, re.I)]
            for name in licences:
                files[f"licenses/{package}-{Path(name).name}"] = zf.read(name)
            notice.append(f"{package} {version}\n  licence: {licence_of(nuspec)}\n  source:  {project_url(nuspec)}\n"
                          f"  files:   {', '.join(Path(n).name for n in dlls) or '(part of the .NET Framework)'}\n"
                          f"  sha512:  {digest}\n")
            queue.extend(dependencies(nuspec))

    if args.pin:
        print("PINNED: dict[str, str] = {")
        for key, digest in hashes.items():
            print(f'    "{key}":\n        "{digest}",')
        print("}")
        return 0

    if TARGET.exists():
        shutil.rmtree(TARGET)
    for name, data in files.items():
        path = TARGET / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    (TARGET / "NOTICE.txt").write_text(
        "Sensor engine used by the RigWatch sensor helper (rigwatch/sensors/helper.py).\n"
        "These are unmodified binaries from nuget.org, fetched by tools/fetch-sensor-engine.py.\n"
        "LibreHardwareMonitorLib is licensed under the Mozilla Public License 2.0; its source is at the\n"
        "address listed below. Licence texts shipped inside the packages are in the licenses folder.\n\n"
        + "\n".join(notice), encoding="utf-8")
    total = sum(len(d) for d in files.values())
    print(f"Wrote {len(files)} files ({total / 2 ** 20:.1f} MB) to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
