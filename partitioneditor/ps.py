"""Call PowerShell with JSON in and out."""
from __future__ import annotations

import base64
import json
import os
import subprocess

CREATE_NO_WINDOW = 0x08000000
_PRELUDE = ("$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
            "[Console]::OutputEncoding=[Text.Encoding]::UTF8;")


class PSError(RuntimeError):
    pass


def _cmd(script: str) -> list[str]:
    enc = base64.b64encode((_PRELUDE + script).encode("utf-16-le")).decode("ascii")
    return ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
            "-EncodedCommand", enc]


def _clean_err(text: str) -> str:
    if "<Objs" in text:   # PowerShell liefert Fehler bei -EncodedCommand oft als CLIXML
        import html
        import re
        parts = re.findall(r'<S S="Error">(.*?)</S>', text, re.S)
        text = html.unescape("".join(parts)).replace("_x000D__x000A_", "\n").replace("_x000A_", "\n")
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    # PowerShell hängt Positions-/Kategoriezeilen an – die erste Zeile ist die eigentliche Meldung.
    useful = [ln for ln in lines if not ln.startswith(("+", "At line", "In Zeile", "CategoryInfo",
                                                        "FullyQualifiedErrorId", "#<"))]
    return " ".join(useful[:3]) or text.strip()


def run(script: str, timeout: float = 120) -> str:
    p = subprocess.run(_cmd(script), capture_output=True, timeout=timeout,
                       creationflags=CREATE_NO_WINDOW)
    out = p.stdout.decode("utf-8", "replace")
    if p.returncode != 0:
        err = p.stderr.decode("utf-8", "replace") or p.stderr.decode("mbcs", "replace")
        raise PSError(_clean_err(err) or f"PowerShell-Fehler (Exit {p.returncode})")
    return out


def run_json(script: str, timeout: float = 120):
    out = run(script, timeout).strip()
    if not out:
        return None
    return json.loads(out)


def as_list(v) -> list:
    if v is None:
        return []
    return v if isinstance(v, list) else [v]


def quote(s: str) -> str:
    """String als PowerShell-Literal in einfachen Anführungszeichen."""
    return "'" + str(s).replace("'", "''") + "'"


def oem_encoding() -> str:
    import ctypes
    try:
        return f"cp{ctypes.windll.kernel32.GetOEMCP()}"
    except Exception:
        return "cp850"


def is_admin() -> bool:
    import ctypes
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# Both drives feed the system lock, so a guessed letter could leave the real one unlocked.
def system_drive() -> str:
    drive = os.environ.get("SystemDrive")
    if not drive:
        raise RuntimeError("Umgebungsvariable SystemDrive fehlt: Systemlaufwerk unbekannt, Sperre nicht möglich")
    return drive[0].upper()


def windows_drive() -> str:
    import ctypes
    buf = ctypes.create_unicode_buffer(260)
    if not ctypes.windll.kernel32.GetWindowsDirectoryW(buf, 260) or not buf.value:
        raise RuntimeError("Windows-Ordner nicht ermittelbar: Laufwerk unbekannt, Sperre nicht möglich")
    return buf.value[0].upper()
