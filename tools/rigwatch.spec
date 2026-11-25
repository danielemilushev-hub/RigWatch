# PyInstaller build recipe for a single-file, windowed RigWatch.exe.
# Run through tools/build-exe.ps1 (it prepares the build environment first).

import os
import re

ROOT = os.path.abspath(os.path.join(SPECPATH, ".."))

with open(os.path.join(ROOT, "rigwatch", "__init__.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'__version__\s*=\s*"([^"]+)"', fh.read()).group(1)
parts = ([int(p) for p in VERSION.split(".") if p.isdigit()] + [0, 0, 0, 0])[:4]

# Windows version resource, so the file shows a proper name and version in Explorer.
version_file = os.path.join(workpath, "version_info.txt")
os.makedirs(workpath, exist_ok=True)
with open(version_file, "w", encoding="utf-8") as fh:
    fh.write(f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={tuple(parts)}, prodvers={tuple(parts)}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'RigWatch hardware monitor'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'RigWatch'),
      StringStruct('OriginalFilename', 'RigWatch.exe'),
      StringStruct('ProductName', 'RigWatch'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""")

a = Analysis(
    [os.path.join(ROOT, "tools", "rigwatch_entry.py")],
    pathex=[ROOT],
    datas=[(os.path.join(ROOT, "rigwatch", "ui"), os.path.join("rigwatch", "ui"))],
    hiddenimports=["pynvml"],
    excludes=["tkinter", "unittest", "pydoc_data", "test"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="RigWatch",
    console=False,
    icon=os.path.join(ROOT, "rigwatch", "ui", "assets", "icon.ico"),
    version=version_file,
    upx=False,
)
