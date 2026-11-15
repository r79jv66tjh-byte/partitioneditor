"""Datenträger, Partitionen und freie Bereiche ermitteln (Get-Disk/Get-Partition/Get-Volume) und Sperren prüfen.

Sperrlogik wie in DiskFormatter:
Harte Sperren (nie bearbeitbar, auch nicht mit Einstellung):
  * Datenträger mit IsSystem/IsBoot oder mit einer System-/Boot-Partition,
  * Datenträger mit dem Systemlaufwerk (%SystemDrive%) oder dem Windows-Ordner,
  * Datenträger mit einer Auslagerungsdatei,
  * offline bzw. schreibgeschützte Datenträger.
Weiche Sperre: interne Datenträger (kein USB/SD/Wechseldatenträger) nur mit ``allow_internal``.
"""
from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field

from . import ps
from .settings import Settings

MiB = 1024 * 1024
MIN_GAP = 2 * MiB            # kleinere Lücken (Ausrichtung) werden nicht als „frei“ gezeigt
REMOVABLE_BUS = {"USB", "SD", "MMC"}
_BUS_NUM = {"1": "SCSI", "2": "ATAPI", "3": "ATA", "4": "1394", "5": "SSA", "6": "Fibre Channel",
            "7": "USB", "8": "RAID", "9": "iSCSI", "10": "SAS", "11": "SATA", "12": "SD",
            "13": "MMC", "14": "Virtual", "15": "File Backed Virtual", "16": "Storage Spaces",
            "17": "NVMe"}
_STYLE_NUM = {"0": "RAW", "1": "MBR", "2": "GPT"}
GPT_TYPES = {
    "{ebd0a0a2-b9e5-4433-87c0-68b6b72699c7}": "Basisdaten",
    "{e3c9e316-0b5c-4db8-817d-f92df00215ae}": "MSR (reserviert)",
    "{c12a7328-f81f-11d2-ba4b-00a0c93ec93b}": "EFI-System",
    "{de94bba4-06d1-4d40-a16a-bfd50179d6ac}": "Wiederherstellung",
}


def fmt_size(n: int | float | None) -> str:
    if n is None:
        return "?"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(n) < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}".replace(".", ",").replace(",0 ", " ")
        n /= 1024
    return f"{n:.1f} TB"


def align_up(x: int, a: int = MiB) -> int:
    return -(-x // a) * a


def align_down(x: int, a: int = MiB) -> int:
    return x // a * a


@dataclass
class Partition:
    number: int
    offset: int
    size: int
    type: str = ""               # Basic, Reserved, System, Recovery, IFS, FAT32, Extended, Logical …
    gpt_type: str = ""
    mbr_type: int | None = None
    letter: str = ""
    label: str = ""
    filesystem: str = ""
    is_active: bool = False
    is_hidden: bool = False
    is_boot: bool = False
    is_system: bool = False
    new: bool = False            # nur in der Simulation: wird erst angelegt

    @property
    def end(self) -> int:
        return self.offset + self.size

    @property
    def kind(self) -> str:
        if self.gpt_type:
            return GPT_TYPES.get(self.gpt_type.lower(), self.type or "GPT")
        if self.type == "Extended":
            return "Erweitert"
        if self.type == "Logical":
            return "Logisch"
        if self.type in ("System", "Recovery", "Reserved"):
            return {"System": "System", "Recovery": "Wiederherstellung", "Reserved": "Reserviert"}[self.type]
        return "Primär"

    @property
    def is_container(self) -> bool:
        return self.type == "Extended"

    def name(self) -> str:
        parts = [f"Partition {self.number}" if self.number else "Neue Partition"]
        if self.letter:
            parts.append(f"{self.letter}:")
        if self.label:
            parts.append(f"„{self.label}“")
        return " ".join(parts)

    def describe(self) -> str:
        bits = [self.name(), fmt_size(self.size), self.filesystem or "kein Dateisystem", self.kind]
        if self.is_active:
            bits.append("aktiv")
        if self.is_hidden:
            bits.append("ausgeblendet")
        return ", ".join(bits)

    def to_json(self) -> dict:
        return {"number": self.number, "offset": self.offset, "size_bytes": self.size,
                "size": fmt_size(self.size), "type": self.type, "kind": self.kind,
                "letter": f"{self.letter}:" if self.letter else None, "label": self.label,
                "filesystem": self.filesystem or None, "active": self.is_active,
                "hidden": self.is_hidden, "boot": self.is_boot, "system": self.is_system,
                **({"pending_new": True} if self.new else {})}


@dataclass
class Gap:
    offset: int
    size: int
    extended: int | None = None      # Offset der erweiterten Partition, in der dieser Bereich liegt

    @property
    def end(self) -> int:
        return self.offset + self.size

    def to_json(self) -> dict:
        return {"free": True, "offset": self.offset, "size_bytes": self.size, "size": fmt_size(self.size),
                **({"inside_extended": True} if self.extended is not None else {})}


@dataclass
class Disk:
    number: int
    model: str
    serial: str
    bus_type: str
    size: int
    style: str = ""              # RAW, MBR, GPT
    is_system: bool = False
    is_boot: bool = False
    is_offline: bool = False
    is_readonly: bool = False
    location: str = ""
    unique_id: str = ""
    sector_size: int = 512
    removable_volume: bool = False
    partitions: list[Partition] = field(default_factory=list)
    hard_locks: list[str] = field(default_factory=list)

    @property
    def removable(self) -> bool:
        return self.bus_type in REMOVABLE_BUS or self.removable_volume

    def blocks(self, settings: Settings) -> list[str]:
        r = list(self.hard_locks)
        if self.is_offline:
            r.append("Datenträger ist offline")
        if self.is_readonly:
            r.append("Datenträger ist schreibgeschützt")
        if not self.removable and not settings.allow_internal:
            r.append("Interner Datenträger – nur nach Freischalten in den Einstellungen")
        return r

    @property
    def usable_start(self) -> int:
        return MiB

    @property
    def usable_end(self) -> int:
        if self.style == "GPT":   # Sicherungskopie der GPT am Ende (33 Sektoren)
            return align_down(self.size - 33 * self.sector_size)
        return align_down(self.size)

    def top_level(self) -> list[Partition]:
        """Partitionen ohne die logischen innerhalb einer erweiterten Partition."""
        ext = [p for p in self.partitions if p.is_container]
        return [p for p in self.partitions
                if p.is_container or not any(e.offset <= p.offset < e.end for e in ext)]

    def gaps(self) -> list[Gap]:
        """Freie Bereiche; bei MBR auch die freien Bereiche innerhalb einer erweiterten Partition."""
        if self.style == "RAW":
            return []
        out, pos = [], self.usable_start
        for p in sorted(self.top_level(), key=lambda p: p.offset):
            if p.offset - pos >= MIN_GAP:
                out.append(Gap(pos, p.offset - pos))
            pos = max(pos, align_up(p.end))
        if self.usable_end - pos >= MIN_GAP:
            out.append(Gap(pos, self.usable_end - pos))
        for ext in self.containers():
            pos = ext.offset
            for q in self.logicals(ext):
                start = align_up(pos + 1)          # Platz für den Erweiterungs-Bootsektor (EBR)
                if q.offset - start >= MIN_GAP:
                    out.append(Gap(start, q.offset - start, extended=ext.offset))
                pos = q.end
            start = align_up(pos + 1)
            if ext.end - start >= MIN_GAP:
                out.append(Gap(start, align_down(ext.end) - start, extended=ext.offset))
        return sorted(out, key=lambda g: g.offset)

    def containers(self) -> list[Partition]:
        return [p for p in self.partitions if p.is_container]

    def logicals(self, ext: Partition) -> list[Partition]:
        return sorted((q for q in self.partitions if not q.is_container and ext.offset <= q.offset < ext.end),
                      key=lambda q: q.offset)

    def segments(self) -> list[Partition | Gap]:
        """Partitionen (ohne erweiterte Container) und freie Bereiche nach Position sortiert."""
        items: list = [p for p in self.partitions if not p.is_container] + self.gaps()
        return sorted(items, key=lambda x: x.offset)

    def by_offset(self, offset: int) -> Partition | None:
        return next((p for p in self.partitions if p.offset == offset), None)

    def by_number(self, number: int) -> Partition | None:
        return next((p for p in self.partitions if p.number == number), None)

    def by_letter(self, letter: str) -> Partition | None:
        return next((p for p in self.partitions if p.letter and p.letter == letter.upper()), None)

    @property
    def title(self) -> str:
        return f"Datenträger {self.number}: {self.model or '?'} ({fmt_size(self.size)}, {self.bus_type}, {self.style or '?'})"

    def to_json(self, settings: Settings) -> dict:
        b = self.blocks(settings)
        return {"number": self.number, "model": self.model, "serial": self.serial,
                "bus_type": self.bus_type, "size_bytes": self.size, "size": fmt_size(self.size),
                "partition_style": self.style, "removable": self.removable,
                "system": bool(self.hard_locks), "location": self.location,
                "editable": not b, "locked_reasons": b,
                "layout": [s.to_json() for s in self.segments()],
                **({"extended_containers": [p.to_json() for p in self.partitions if p.is_container]}
                   if any(p.is_container for p in self.partitions) else {})}

    def clone(self) -> "Disk":
        return copy.deepcopy(self)


@dataclass
class Snapshot:
    disks: list[Disk]
    used_letters: set[str]

    def disk(self, number: int) -> Disk | None:
        return next((d for d in self.disks if d.number == number), None)

    def find_letter(self, letter: str) -> tuple[Disk, Partition] | None:
        for d in self.disks:
            p = d.by_letter(letter)
            if p:
                return d, p
        return None

    def free_letters(self, extra_used: set[str] = frozenset()) -> list[str]:
        used = self.used_letters | set(extra_used)
        return [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if c not in used]


# ---------------------------------------------------------------- Abfrage

_QUERY = r"""
$d = @(Get-Disk | ForEach-Object { [pscustomobject]@{
  Number=$_.Number; Model=$_.Model; FriendlyName=$_.FriendlyName; Serial=$_.SerialNumber;
  BusType=[string]$_.BusType; Size=$_.Size; IsSystem=$_.IsSystem; IsBoot=$_.IsBoot;
  IsOffline=$_.IsOffline; IsReadOnly=$_.IsReadOnly; PartitionStyle=[string]$_.PartitionStyle;
  Location=$_.Location; UniqueId=$_.UniqueId; SectorSize=$_.LogicalSectorSize } })
$p = @(Get-Partition -ErrorAction SilentlyContinue | ForEach-Object { [pscustomobject]@{
  DiskNumber=$_.DiskNumber; PartitionNumber=$_.PartitionNumber; DriveLetter=[string]$_.DriveLetter;
  Offset=$_.Offset; Size=$_.Size; IsBoot=$_.IsBoot; IsSystem=$_.IsSystem; Type=[string]$_.Type;
  GptType=[string]$_.GptType; MbrType=$_.MbrType; IsActive=$_.IsActive; IsHidden=$_.IsHidden;
  AccessPaths=@($_.AccessPaths) } })
$v = @(Get-Volume | ForEach-Object { [pscustomobject]@{
  DriveLetter=[string]$_.DriveLetter; Label=$_.FileSystemLabel; FileSystem=[string]$_.FileSystemType;
  DriveType=[string]$_.DriveType; Path=$_.Path } })
$pf = @(Get-CimInstance Win32_PageFileUsage -ErrorAction SilentlyContinue | ForEach-Object { $_.Name })
$dl = @([System.IO.DriveInfo]::GetDrives() | ForEach-Object { $_.Name })
[pscustomobject]@{disks=$d; partitions=$p; volumes=$v; pagefiles=$pf; letters=$dl} | ConvertTo-Json -Depth 4 -Compress
"""


def _letter(x) -> str:
    s = str(x or "").strip().replace("\x00", "")
    return s[0].upper() if s and s[0].isalpha() else ""


def _bus(x) -> str:
    s = str(x or "").strip()
    return _BUS_NUM.get(s, s or "?")


def _style(x) -> str:
    s = str(x or "").strip()
    return _STYLE_NUM.get(s, s.upper() or "?")


def build_snapshot(raw: dict, system_letters: set[str]) -> Snapshot:
    disks: dict[int, Disk] = {}
    for r in ps.as_list(raw.get("disks")):
        n = int(r["Number"])
        disks[n] = Disk(
            number=n, model=(r.get("Model") or r.get("FriendlyName") or "").strip(),
            serial=(r.get("Serial") or "").strip(), bus_type=_bus(r.get("BusType")),
            size=int(r.get("Size") or 0), style=_style(r.get("PartitionStyle")),
            is_system=bool(r.get("IsSystem")), is_boot=bool(r.get("IsBoot")),
            is_offline=bool(r.get("IsOffline")), is_readonly=bool(r.get("IsReadOnly")),
            location=str(r.get("Location") or ""), unique_id=str(r.get("UniqueId") or ""),
            sector_size=int(r.get("SectorSize") or 512))

    vols = {}
    for r in ps.as_list(raw.get("volumes")):
        path = str(r.get("Path") or "").lower().rstrip("\\")
        if path:
            vols[path] = r
    vols_by_letter = {_letter(r.get("DriveLetter")): r for r in vols.values() if _letter(r.get("DriveLetter"))}

    used = {_letter(x) for x in ps.as_list(raw.get("letters")) if _letter(x)}
    for p in ps.as_list(raw.get("partitions")):
        d = disks.get(int(p["DiskNumber"]))
        if d is None:
            continue
        mbr = p.get("MbrType")
        part = Partition(
            number=int(p["PartitionNumber"]), offset=int(p.get("Offset") or 0), size=int(p.get("Size") or 0),
            type=str(p.get("Type") or ""), gpt_type=str(p.get("GptType") or "") if d.style == "GPT" else "",
            mbr_type=int(mbr) if mbr not in (None, "") else None, letter=_letter(p.get("DriveLetter")),
            is_active=bool(p.get("IsActive")), is_hidden=bool(p.get("IsHidden")),
            is_boot=bool(p.get("IsBoot")), is_system=bool(p.get("IsSystem")))
        vol = None
        for ap in ps.as_list(p.get("AccessPaths")):
            vol = vols.get(str(ap).lower().rstrip("\\")) or vol
        if vol is None and part.letter:
            vol = vols_by_letter.get(part.letter)
        if vol is not None:
            part.label = str(vol.get("Label") or "")
            part.filesystem = str(vol.get("FileSystem") or "")
            if str(vol.get("DriveType")) == "Removable":
                d.removable_volume = True
        if part.filesystem.lower() in ("unknown", "0"):
            part.filesystem = ""
        if part.letter:
            used.add(part.letter)
        if part.is_boot or part.is_system:
            _lock(d, "Enthält die System-/Boot-Partition")
        d.partitions.append(part)

    page_letters = {_letter(x) for x in ps.as_list(raw.get("pagefiles")) if _letter(x)}
    for d in disks.values():
        if d.is_system:
            _lock(d, "Systemdatenträger (IsSystem)")
        if d.is_boot:
            _lock(d, "Boot-Datenträger (IsBoot)")
        letters = {p.letter for p in d.partitions if p.letter}
        for L in sorted(letters & system_letters):
            _lock(d, f"Enthält das Windows-/Systemlaufwerk {L}:")
        for L in sorted(letters & page_letters):
            _lock(d, f"Enthält eine Auslagerungsdatei ({L}:)")
        d.partitions.sort(key=lambda p: p.offset)
        # Windows meldet formatierte logische Laufwerke als „IFS“ – maßgeblich ist die Lage
        for ext in d.containers():
            for q in d.logicals(ext):
                q.type = "Logical"
    return Snapshot(disks=sorted(disks.values(), key=lambda d: d.number), used_letters=used)


def _lock(d: Disk, reason: str) -> None:
    if reason not in d.hard_locks:
        d.hard_locks.append(reason)


def system_letters() -> set[str]:
    return {ps.system_drive(), ps.windows_drive()}


def query() -> Snapshot:
    raw = ps.run_json(_QUERY, timeout=90) or {}
    return build_snapshot(raw, system_letters())


def supported_size(disk: int, partition_offset: int) -> tuple[int, int]:
    """(min, max) laut Get-PartitionSupportedSize."""
    r = ps.run_json(
        f"Update-Disk -Number {int(disk)} -ErrorAction SilentlyContinue;"   # sonst veraltete Werte nach Änderungen
        f"$p = Get-Partition -DiskNumber {int(disk)} | Where-Object Offset -eq {int(partition_offset)};"
        "if (-not $p) { throw 'Partition nicht gefunden' };"
        "$s = $p | Get-PartitionSupportedSize; [pscustomobject]@{Min=$s.SizeMin; Max=$s.SizeMax} | ConvertTo-Json -Compress",
        timeout=180)
    return int(r["Min"]), int(r["Max"])


# ---------------------------------------------------------------- Bestätigung

def parse_letter(text: str | None) -> str:
    """'E', 'E:', 'E:\\' -> 'E'; leer/none/- -> ''."""
    t = (text or "").strip()
    if t.lower() in ("", "none", "keiner", "kein", "-"):
        return ""
    m = re.fullmatch(r"([A-Za-z]):?\\?", t)
    if not m:
        raise ValueError(f"Ungültiger Laufwerksbuchstabe '{text}'")
    return m.group(1).upper()


def confirm_ok(disk: Disk, letters: set[str], typed: str | None) -> bool:
    """Bestätigung = exakt die Datenträgernummer, oder der Buchstabe einer betroffenen Partition."""
    t = (typed or "").strip()
    if t == str(disk.number):
        return True
    if letters and re.fullmatch(r"[A-Za-z]:?", t):
        return t[0].upper() in letters
    return False
