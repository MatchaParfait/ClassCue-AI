"""Build the branded in-process Windows desktop host with the system C# compiler."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import sysconfig
import tempfile
import tomllib

ROOT = Path(__file__).resolve().parents[1]


def prepare_host(project=ROOT):
    if os.name != "nt" or struct.calcsize("P") != 8:
        raise RuntimeError("The desktop host requires 64-bit Windows Python.")
    project = Path(project).resolve()
    source = project / "scripts" / "desktop_host.cs"
    icon = project / "assets" / "desktop.ico"
    version = tomllib.loads((project / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    library = Path(sys.base_prefix) / (sysconfig.get_config_var("LDLIBRARY") or f"python{sys.version_info.major}{sys.version_info.minor}.dll")
    compiler = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    for path in (source, icon, pythonw, library, compiler):
        if not path.is_file():
            raise RuntimeError(f"Required desktop host input is missing: {path.name}")
    directory = project / ".runtime" / "desktop-host"
    directory.mkdir(parents=True, exist_ok=True)
    host = directory / "ClassCue-AI.exe"
    marker = directory / "build.json"
    fingerprint = hashlib.sha256(
        source.read_bytes() + icon.read_bytes() + version.encode() + compiler.read_bytes()
    ).hexdigest()
    try:
        current = json.loads(marker.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        current = {}
    if not isinstance(current, dict):
        current = {}
    # Rebuild on source/icon/version changes and repair a damaged generated exe.
    if not (host.is_file() and current.get("inputs") == fingerprint
            and current.get("exe") == hashlib.sha256(host.read_bytes()).hexdigest()):
        with tempfile.TemporaryDirectory(prefix="build-", dir=directory) as temporary:
            temporary = Path(temporary)
            metadata = temporary / "version.cs"
            numeric = version.split("+")[0].split("-")[0].split(".")
            if not 1 <= len(numeric) <= 4 or any(not item.isdecimal() for item in numeric):
                raise ValueError("Invalid desktop host version")
            file_version = ".".join(numeric + ["0"] * (4 - len(numeric)))
            metadata.write_text(
                'using System.Reflection;\n'
                f'[assembly: AssemblyFileVersion("{file_version}")]\n'
                f'[assembly: AssemblyVersion("{file_version}")]\n'
                f'[assembly: AssemblyInformationalVersion({json.dumps(version)})]\n',
                encoding="ascii",
            )
            output = temporary / host.name
            result = subprocess.run(
                [str(compiler), "/nologo", "/target:winexe", "/platform:x64", "/optimize+",
                 "/reference:System.Windows.Forms.dll", f"/win32icon:{icon}",
                 f"/out:{output}", str(source), str(metadata)],
                capture_output=True, text=True, errors="replace", timeout=60,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            if result.returncode:
                raise RuntimeError(f"Desktop host compilation failed:\n{result.stdout}\n{result.stderr}")
            output.replace(host)
        marker.write_text(json.dumps({
            "inputs": fingerprint, "exe": hashlib.sha256(host.read_bytes()).hexdigest(),
        }), encoding="utf-8")
    runtime = directory / "python-runtime.txt"
    content = f"{pythonw.resolve()}\n{library.resolve()}\n"
    if not runtime.is_file() or runtime.read_text(encoding="utf-8") != content:
        temporary = runtime.with_suffix(".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(runtime)
    return host


if __name__ == "__main__":
    print(prepare_host())
