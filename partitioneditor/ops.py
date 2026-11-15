"""Änderungen sammeln, simulieren (Vorschau vorher → nachher) und ausführen.

Eine Änderung ist ein kleines dict, z. B.
    {"op": "create", "disk": 3, "offset": 1048576, "size": 524288000, "fs": "NTFS", "letter": "auto", "label": "DATEN"}
    {"op": "delete", "disk": 3, "offset": 1048576}
    {"op": "resize", "disk": 3, "offset": 1048576, "size": 1073741824}
    {"op": "letter", "disk": 3, "offset": 1048576, "letter": "T"}      ("" = Buchstabe entfernen)
    {"op": "label",  "disk": 3, "offset": 1048576, "label": "NEU"}
    {"op": "active", "disk": 3, "offset": 1048576, "value": true}
    {"op": "hidden", "disk": 3, "offset": 1048576, "value": true}
    {"op": "convert", "disk": 3, "style": "GPT"}                       (nur leere/RAW-Datenträger)

Partitionen werden über ihren Offset (Startposition in Bytes) angesprochen, weil sich die
Partitionsnummern beim Löschen/Anlegen ändern können. Statt "offset" darf auch "partition"
(Nummer) oder "target" (Laufwerksbuchstabe) angegeben werden – ``normalize`` übersetzt das.

Vor dem Ausführen wird alles gegen eine frische Abfrage neu geprüft: Sperren, Kennung des
Datenträgers, Bestätigung und eine erneute Simulation aller Schritte.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from . import model, ps
from .model import Disk, MiB, Partition, Snapshot, align_down, align_up, fmt_size
from .settings import Settings

OPS = ("create", "delete", "resize", "letter", "label", "active", "hidden", "convert")
FILESYSTEMS = ("NTFS", "exFAT", "FAT32", "RAW")       # RAW = nicht formatieren
LABEL_MAX = {"NTFS": 32, "exFAT": 15, "FAT32": 11, "ReFS": 32}
FAT32_MAX = 32 * 1024 ** 3
RESIZABLE_FS = ("NTFS", "ReFS")
OP_NAMES = {"create": "Partition anlegen", "delete": "Partition löschen", "resize": "Größe ändern",
            "letter": "Laufwerksbuchstabe ändern", "label": "Namen ändern", "active": "Aktiv setzen",
            "hidden": "Ausblenden", "convert": "Partitionsstil umstellen"}
_BAD_LABEL = set('*?/\\|.,;:+=[]<>"')


class OpError(Exception):
    pass


# ---------------------------------------------------------------- Normalisieren

def _int(v, what: str) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        raise OpError(f"{what}: Zahl erwartet, erhalten '{v}'") from None


def parse_size(v) -> int | None:
    """'500MB', '2 GB', '1048576', 'max' -> Bytes (Binärpräfixe) bzw. None für 'max'."""
    if v is None or str(v).strip().lower() in ("", "max", "maximum", "rest"):
        return None
    if isinstance(v, (int, float)):
        return int(v)
    s = str(v).strip().upper().replace(" ", "").replace(",", ".")
    for unit, mul in (("TB", 1024 ** 4), ("GB", 1024 ** 3), ("MB", MiB), ("KB", 1024), ("B", 1)):
        for suffix in (unit, unit[0] + "IB" if unit != "B" else "B"):
            if s.endswith(suffix) and s[:-len(suffix)]:
                try:
                    return int(float(s[:-len(suffix)]) * mul)
                except ValueError:
                    raise OpError(f"Ungültige Größe '{v}'") from None
    try:
        return int(s)
    except ValueError:
        raise OpError(f"Ungültige Größe '{v}' (z. B. 500MB, 2GB oder max)") from None


def normalize(op: dict, snap: Snapshot) -> dict:
    """Op prüfen und vereinheitlichen (Ziel → disk + offset). Arbeitet auf dem *Ausgangs*zustand."""
    o = dict(op)
    kind = o.get("op")
    if kind not in OPS:
        raise OpError(f"Unbekannte Änderung '{kind}' (erlaubt: {', '.join(OPS)})")
    if o.get("target") not in (None, "") and o.get("disk") in (None, ""):
        t = str(o["target"]).strip()
        try:
            L = model.parse_letter(t)
        except ValueError:
            L = ""
        if L:
            hit = snap.find_letter(L)
            if not hit:
                raise OpError(f"Laufwerk {L}: gehört zu keiner Partition")
            o["disk"], o["offset"] = hit[0].number, hit[1].offset
        else:
            o["disk"] = _int(t, "target")
    o.pop("target", None)
    if o.get("disk") in (None, ""):
        raise OpError("Datenträger fehlt (disk)")
    o["disk"] = _int(o["disk"], "disk")
    d = snap.disk(o["disk"])
    if d is None:
        raise OpError(f"Datenträger {o['disk']} nicht gefunden")
    if kind not in ("create", "convert"):
        if o.get("offset") in (None, ""):
            if o.get("partition") in (None, ""):
                raise OpError(f"{OP_NAMES[kind]}: Partition fehlt (partition, offset oder target)")
            p = d.by_number(_int(o["partition"], "partition"))
            if not p:
                raise OpError(f"Partition {o['partition']} auf Datenträger {d.number} nicht gefunden")
            o["offset"] = p.offset
        o["offset"] = _int(o["offset"], "offset")
        o.pop("partition", None)
    if kind == "create":
        o["size"] = parse_size(o.get("size"))
        o["offset"] = None if o.get("offset") in (None, "") else _int(o["offset"], "offset")
        fs = str(o.get("fs") or "NTFS")
        match = next((f for f in FILESYSTEMS if f.lower() == fs.lower()), None)
        if not match:
            raise OpError(f"Dateisystem '{fs}' nicht unterstützt (NTFS, exFAT, FAT32 oder RAW)")
        o["fs"] = match
        L = str(o.get("letter") if o.get("letter") is not None else "auto")
        o["letter"] = "auto" if L.lower() == "auto" else _letter(L)
        o["label"] = str(o.get("label") or "")
    elif kind == "resize":
        o["size"] = parse_size(o.get("size"))
        if o["size"] is None:
            o["size"] = "max"
    elif kind == "letter":
        o["letter"] = _letter(o.get("letter"))
    elif kind == "label":
        o["label"] = str(o.get("label") or "")
    elif kind in ("active", "hidden"):
        v = o.get("value", True)
        o["value"] = v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "ja", "yes", "on")
    elif kind == "convert":
        st = str(o.get("style") or "").upper()
        if st not in ("GPT", "MBR"):
            raise OpError("Partitionsstil muss GPT oder MBR sein")
        o["style"] = st
    return o


def _letter(v) -> str:
    try:
        return model.parse_letter(None if v is None else str(v))
    except ValueError as e:
        raise OpError(str(e)) from None


# ---------------------------------------------------------------- Simulation

def _check_label(label: str, fs: str) -> None:
    if not label:
        return
    if fs in LABEL_MAX and len(label) > LABEL_MAX[fs]:
        raise OpError(f"Name '{label}' ist zu lang für {fs} (max. {LABEL_MAX[fs]} Zeichen)")
    if fs in ("FAT32", "exFAT") and any(c in _BAD_LABEL for c in label):
        raise OpError(f"Name '{label}' enthält Zeichen, die {fs} nicht erlaubt")


def _part(d: Disk, offset: int, kind: str) -> Partition:
    p = d.by_offset(offset)
    if p is None:
        raise OpError(f"{OP_NAMES[kind]}: auf Datenträger {d.number} gibt es (zu diesem Zeitpunkt) "
                      f"keine Partition bei Offset {offset}")
    if p.new:
        raise OpError(f"{OP_NAMES[kind]}: die Partition wird erst angelegt – Eigenschaften gleich beim "
                      "Anlegen setzen oder erst anwenden")
    if p.is_container and kind != "delete":
        raise OpError("Die erweiterte Partition ist nur ein Container – bitte eine logische Partition wählen")
    return p


def apply_op(d: Disk, o: dict, used_letters: set[str], supported: dict | None = None) -> str:
    """Op auf die (kopierte) Datenträgerdarstellung anwenden. Gibt eine Zeile „vorher → nachher“ zurück."""
    k = o["op"]
    if k != "convert" and d.style == "RAW":
        raise OpError(f"Datenträger {d.number} ist nicht initialisiert – zuerst in MBR oder GPT umstellen")
    if k == "create":
        gaps = d.gaps()
        if not gaps:
            raise OpError(f"Kein freier Bereich auf Datenträger {d.number}")
        if o["offset"] is None:
            g = max(gaps, key=lambda g: g.size)
        else:
            g = next((g for g in gaps if g.offset <= o["offset"] < g.end), None)
            if g is None:
                raise OpError(f"Bei Offset {o['offset']} ist kein freier Platz")
        start = align_up(o["offset"]) if o["offset"] is not None else g.offset
        room = align_down(g.end - start)
        # MBR: 4 Einträge. Windows legt bei der 4. Partition (ohne erweiterte) selbst eine
        # erweiterte Partition über den ganzen freien Bereich an und die neue darin als logische.
        ptype, auto_ext = ("Basic" if d.style == "GPT" else "IFS"), None
        if d.style == "MBR":
            top = d.top_level()
            if g.extended is not None:
                ptype = "Logical"
            elif len(top) >= 4:
                raise OpError("MBR erlaubt nur 4 Einträge (primär oder erweitert) – kein Platz für eine weitere Partition")
            elif len(top) == 3 and not d.containers():
                ptype = "Logical"
                auto_ext = Partition(number=0, offset=g.offset, size=align_down(g.end) - g.offset, type="Extended", new=True)
                start = g.offset
                room = align_down(g.end - start) - MiB
        size = room if o["size"] is None else align_down(o["size"])
        if size < MiB:
            raise OpError("Die Partition muss mindestens 1 MB groß sein")
        if size > room:
            raise OpError(f"Zu groß: im freien Bereich sind ab dieser Stelle nur {fmt_size(room)} frei")
        fs = o["fs"]
        if fs == "FAT32" and size > FAT32_MAX:
            raise OpError("Windows formatiert FAT32 nur bis 32 GB – exFAT oder NTFS wählen")
        _check_label(o["label"], fs)
        letter = o["letter"]
        if letter == "auto":
            free = [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if c not in used_letters]
            if not free:
                raise OpError("Kein Laufwerksbuchstabe mehr frei")
            letter = free[0]
        elif letter:
            if letter in used_letters:
                raise OpError(f"Laufwerksbuchstabe {letter}: ist schon belegt")
            if letter in "AB":
                raise OpError("A: und B: sind nicht erlaubt")
        if letter:
            used_letters.add(letter)
        o["_resolved"] = {"offset": start, "size": size, "letter": letter, "fill": o["size"] is None}
        p = Partition(number=0, offset=start + (MiB if auto_ext else 0), size=size, type=ptype,
                      letter=letter, label=o["label"] if fs != "RAW" else "",
                      filesystem="" if fs == "RAW" else fs, new=True)
        if auto_ext:
            d.partitions.append(auto_ext)
        d.partitions.append(p)
        d.partitions.sort(key=lambda p: p.offset)
        extra = " (Windows legt dafür eine erweiterte Partition an)" if auto_ext else ""
        return f"Freier Bereich {fmt_size(g.size)} → {p.describe()}{extra}"

    if k == "convert":
        rest = [p for p in d.partitions if not (p.type == "Reserved" or p.gpt_type.lower() == "{e3c9e316-0b5c-4db8-817d-f92df00215ae}")]
        if rest:
            raise OpError(f"Datenträger {d.number} ist nicht leer ({len(rest)} Partition(en)) – "
                          "erst alle Partitionen löschen (geht in derselben Liste)")
        if d.style == o["style"]:
            raise OpError(f"Datenträger {d.number} ist schon {d.style}")
        before = d.style
        d.style, d.partitions = o["style"], []
        return f"Partitionsstil {before} → {d.style} (Datenträger leer)"

    p = _part(d, o["offset"], k)
    before = p.describe()
    if k == "delete":
        if p.is_container and any(q for q in d.partitions if q is not p and p.offset <= q.offset < p.end):
            raise OpError("Die erweiterte Partition enthält noch logische Laufwerke – diese zuerst löschen")
        d.partitions.remove(p)
        if p.letter:
            used_letters.discard(p.letter)
        return f"{before} → gelöscht (freier Bereich)"
    if k == "resize":
        if p.filesystem not in RESIZABLE_FS:
            raise OpError(f"Größe ändern geht unter Windows nur bei NTFS/ReFS (diese Partition: {p.filesystem or 'kein Dateisystem'})")
        nxt = min([q.offset for q in d.top_level() if q.offset > p.offset and q is not p] + [d.usable_end])
        if p.type == "Logical":
            ext = next(e for e in d.partitions if e.is_container and e.offset <= p.offset < e.end)
            nxt = min([q.offset for q in d.partitions if q.offset > p.offset and not q.is_container] + [ext.end])
        room = align_down(nxt - p.offset)
        smin = (supported or {}).get(p.offset, (None, None))[0]
        size = room if o["size"] == "max" else align_down(o["size"])
        if size == p.size:
            raise OpError("Die neue Größe ist gleich der alten")
        if size > room:
            raise OpError(f"Zu groß: maximal {fmt_size(room)} (bis zur nächsten Partition/zum Ende)")
        if smin is not None and size < smin:
            raise OpError(f"Zu klein: Windows kann diese Partition höchstens auf {fmt_size(align_up(smin))} verkleinern")
        if size < MiB:
            raise OpError("Zu klein")
        o["_resolved"] = {"size": size}
        p.size = size
        return f"{before} → {fmt_size(size)}"
    if k == "letter":
        L = o["letter"]
        if p.is_hidden and L:
            raise OpError("Ausgeblendete Partitionen bekommen keinen Buchstaben – zuerst einblenden")
        if L == p.letter:
            raise OpError(f"Partition hat schon {('den Buchstaben ' + L + ':') if L else 'keinen Buchstaben'}")
        if L:
            if L in used_letters:
                raise OpError(f"Laufwerksbuchstabe {L}: ist schon belegt")
            if L in "AB":
                raise OpError("A: und B: sind nicht erlaubt")
            used_letters.add(L)
        if p.letter:
            used_letters.discard(p.letter)
        old = f"{p.letter}:" if p.letter else "ohne Buchstabe"
        p.letter = L
        return f"{before}: {old} → {(L + ':') if L else 'ohne Buchstabe'}"
    if k == "label":
        if not p.filesystem:
            raise OpError("Die Partition hat kein Dateisystem – Namen gibt es erst nach dem Formatieren")
        _check_label(o["label"], p.filesystem)
        old = p.label
        p.label = o["label"]
        return f"{before}: Name „{old}“ → „{p.label}“"
    if k == "active":
        if d.style != "MBR":
            raise OpError("„Aktiv“ gibt es nur bei MBR-Datenträgern")
        if p.type == "Logical":
            raise OpError("Nur primäre Partitionen können aktiv sein")
        if p.is_active == o["value"]:
            raise OpError("Partition ist schon " + ("aktiv" if p.is_active else "nicht aktiv"))
        if o["value"]:
            for q in d.partitions:
                q.is_active = False
        p.is_active = o["value"]
        return f"{before} → " + ("aktiv (andere Partitionen dieses Datenträgers werden inaktiv)" if p.is_active else "nicht aktiv")
    if k == "hidden":
        if p.is_hidden == o["value"]:
            raise OpError("Partition ist schon " + ("ausgeblendet" if p.is_hidden else "sichtbar"))
        p.is_hidden = o["value"]
        if p.is_hidden and p.letter:
            used_letters.discard(p.letter)
            p.letter = ""
        return f"{before} → " + ("ausgeblendet (ohne Laufwerksbuchstabe)" if p.is_hidden else "wieder sichtbar")
    raise OpError(f"Unbekannte Änderung {k}")


@dataclass
class Plan:
    snapshot: Snapshot
    ops: list[dict]
    steps: list[dict] = field(default_factory=list)          # {op, disk, text}
    before: dict[int, Disk] = field(default_factory=dict)
    after: dict[int, Disk] = field(default_factory=dict)
    blocked: dict[int, list[str]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and not any(self.blocked.values())

    def confirm_hint(self, disk: int) -> str:
        return str(disk)

    def confirm_letters(self, disk: int) -> set[str]:
        """Buchstabe als Bestätigung nur, wenn genau eine bestehende Partition betroffen ist."""
        mine = [o for o in self.ops if o["disk"] == disk]
        if any(o["op"] in ("create", "convert") for o in mine):
            return set()
        offs = {o["offset"] for o in mine}
        if len(offs) != 1:
            return set()
        p = self.before[disk].by_offset(next(iter(offs)))
        return {p.letter} if p and p.letter else set()

    def to_json(self, settings: Settings) -> dict:
        disks = []
        for n in sorted(self.before):
            b, a = self.before[n], self.after.get(n)
            disks.append({"disk": n, "title": b.title, "locked_reasons": self.blocked.get(n, []),
                          "confirm_target": str(n),
                          "confirm_target_alternatives": [f"{L}:" for L in sorted(self.confirm_letters(n))],
                          "before": [s.to_json() for s in b.segments()],
                          "after": [s.to_json() for s in a.segments()] if a else None,
                          "style_before": b.style, "style_after": a.style if a else None})
        return {"ok": self.ok, "errors": self.errors,
                "steps": [{"op": s["op"]["op"], "disk": s["op"]["disk"], "change": s["text"]} for s in self.steps],
                "disks": disks}


def make_plan(snap: Snapshot, settings: Settings, ops: list[dict], supported: dict | None = None,
              fetch_supported: bool = True) -> Plan:
    """Alle Änderungen nacheinander simulieren. Fehler stehen in plan.errors (nichts wird geändert)."""
    plan = Plan(snapshot=snap, ops=[])
    used = set(snap.used_letters)
    supported = dict(supported or {})
    for i, raw in enumerate(ops, 1):
        try:
            o = normalize(raw, snap)
        except OpError as e:
            plan.errors.append(f"Schritt {i}: {e}")
            continue
        n = o["disk"]
        if n not in plan.before:
            d = snap.disk(n)
            plan.before[n], plan.after[n] = d, d.clone()
            b = d.blocks(settings)
            if b:
                plan.blocked[n] = b
        if o["op"] == "resize" and o["offset"] not in supported.get(n, {}) and fetch_supported \
                and not plan.blocked.get(n) and snap.disk(n).by_offset(o["offset"]):
            try:
                supported.setdefault(n, {})[o["offset"]] = model.supported_size(n, o["offset"])
            except ps.PSError as e:
                plan.errors.append(f"Schritt {i}: Größenbereich nicht ermittelbar: {e}")
                continue
        try:
            text = apply_op(plan.after[n], o, used, supported.get(n))
        except OpError as e:
            plan.errors.append(f"Schritt {i} ({OP_NAMES[o['op']]}, Datenträger {n}): {e}")
            continue
        plan.ops.append(o)
        plan.steps.append({"op": o, "text": text})
    for n, b in plan.blocked.items():
        plan.errors.append(f"Datenträger {n} gesperrt: " + "; ".join(b))
    return plan


# ---------------------------------------------------------------- Ausführen

def disk_uid(d: Disk) -> str:
    return f"{d.unique_id}|{d.serial}|{d.size}|{d.location}"


def _find_part(n: int, offset: int) -> str:
    return (f"$p = Get-Partition -DiskNumber {n} | Where-Object Offset -eq {int(offset)};"
            f"if (-not $p) {{ throw 'Partition bei Offset {int(offset)} nicht gefunden' }};")


def _real_gap(n: int, want_off: int, want_size: int, fill: bool) -> tuple[int, int]:
    """Gewünschte Lage gegen den echten Datenträger prüfen (Windows legt z. B. selbst eine MSR an)."""
    d = model.query().disk(n)
    best, overlap = None, 0
    for g in d.gaps():
        ov = min(g.end, want_off + want_size) - max(g.offset, want_off)
        if ov > overlap:
            best, overlap = g, ov
    if best is None:
        raise OpError("Der geplante freie Bereich ist auf dem Datenträger nicht (mehr) frei")
    off = max(want_off, best.offset)
    room = align_down(best.end - off)
    if fill or want_size <= room:
        return off, room if fill else want_size
    if want_size - room <= 32 * MiB:     # z. B. 16-MB-MSR, die Windows beim Initialisieren anlegt
        return off, room
    raise OpError(f"Nur {fmt_size(room)} frei statt {fmt_size(want_size)}")


def _script(o: dict, real: tuple[int, int] | None = None) -> str:
    n = o["disk"]
    q = ps.quote
    if o["op"] == "create":
        r = o["_resolved"]
        off, size = real or (r["offset"], r["size"])
        sz = "-UseMaximumSize" if r.get("fill") else f"-Size {size}"
        s = (f"$p = New-Partition -DiskNumber {n} -Offset {off} {sz}"
             + (f" -DriveLetter {r['letter']}" if r["letter"] else "") + ";")
        if o["fs"] != "RAW":
            s += (f"$null = $p | Format-Volume -FileSystem {o['fs']} -NewFileSystemLabel {q(o['label'])}"
                  " -Confirm:$false -Force;")
        return s + "[pscustomobject]@{Offset=$p.Offset; Number=$p.PartitionNumber} | ConvertTo-Json -Compress"
    if o["op"] == "convert":
        return (f"$d = Get-Disk -Number {n};"
                f"if ([string]$d.PartitionStyle -ne 'RAW') {{ Clear-Disk -Number {n} -RemoveData -RemoveOEM -Confirm:$false }};"
                f"Initialize-Disk -Number {n} -PartitionStyle {o['style']};")
    s = _find_part(n, o["offset"])
    if o["op"] == "delete":
        return s + "$p | Remove-Partition -Confirm:$false;"
    if o["op"] == "resize":
        return s + f"Resize-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber -Size {o['_resolved']['size']};"
    if o["op"] == "letter":
        if o["letter"]:
            return s + f"Set-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber -NewDriveLetter {o['letter']};"
        return s + ("if ($p.DriveLetter) { Remove-PartitionAccessPath -DiskNumber " + str(n) +
                    " -PartitionNumber $p.PartitionNumber -AccessPath ($p.DriveLetter + ':\\') };")
    if o["op"] == "label":
        return s + f"$v = $p | Get-Volume; Set-Volume -InputObject $v -NewFileSystemLabel {q(o['label'])};"
    if o["op"] == "active":
        return s + f"Set-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber -IsActive ${str(o['value']).lower()};"
    if o["op"] == "hidden":
        pre = ""
        if o["value"]:
            pre = ("if ($p.DriveLetter) { Remove-PartitionAccessPath -DiskNumber " + str(n) +
                   " -PartitionNumber $p.PartitionNumber -AccessPath ($p.DriveLetter + ':\\') };")
        return s + pre + f"Set-Partition -DiskNumber {n} -PartitionNumber $p.PartitionNumber -IsHidden ${str(o['value']).lower()};"
    raise OpError(o["op"])


LogFn = Callable[[str], None]


def execute(ops: list[dict], confirms: dict[int, str], *, expect_uids: dict[int, str] | None = None,
            settings: Settings | None = None, log: LogFn | None = None) -> dict:
    """Frisch abfragen, neu planen, Sperren/Bestätigung/Kennung prüfen, dann Schritt für Schritt ausführen.

    ``confirms``: Datenträgernummer → eingetippte Bestätigung (Nummer oder bei einer einzelnen
    Partition auch deren Laufwerksbuchstabe).
    """
    settings = settings or Settings.load()
    log = log or (lambda s: None)
    if not ps.is_admin():
        raise OpError("Administratorrechte nötig")
    snap = model.query()
    plan = make_plan(snap, settings, ops)
    if not plan.ok:
        raise OpError("Nicht ausführbar: " + " | ".join(plan.errors))
    for n, d in plan.before.items():
        if not model.confirm_ok(d, plan.confirm_letters(n), confirms.get(n, confirms.get(str(n)))):
            raise OpError(f"Bestätigung für Datenträger {n} fehlt oder passt nicht (erwartet '{n}')")
        if expect_uids and n in expect_uids and expect_uids[n] != disk_uid(d):
            raise OpError(f"Datenträger {n} hat sich seit der Vorschau geändert – bitte neu laden")
        # Letzte Absicherung direkt vor dem Schreiben: harte Sperren dürfen nie umgangen werden.
        if d.hard_locks:
            raise OpError(f"Datenträger {n} ist gesperrt: " + "; ".join(d.hard_locks))

    done, t0 = [], time.monotonic()
    for i, step in enumerate(plan.steps, 1):
        o = step["op"]
        log(f"[{i}/{len(plan.steps)}] {step['text']}")
        try:
            real = None
            if o["op"] == "create":
                r = o["_resolved"]
                real = _real_gap(o["disk"], r["offset"], r["size"], r["fill"])
            if o["op"] == "resize":
                lo, hi = model.supported_size(o["disk"], o["offset"])
                sz = o["_resolved"]["size"]
                if not lo <= sz <= hi:
                    raise OpError(f"Windows erlaubt jetzt nur {fmt_size(lo)} … {fmt_size(hi)}")
            ps.run(_script(o, real), timeout=1800)
        except (OpError, ps.PSError) as e:
            log(f"  FEHLER: {e}")
            return {"ok": False, "done": done, "failed": {"step": i, "change": step["text"], "error": str(e)},
                    "not_run": [s["text"] for s in plan.steps[i:]],
                    "seconds": round(time.monotonic() - t0, 1)}
        done.append(step["text"])
        log("  OK")
    return {"ok": True, "done": done, "seconds": round(time.monotonic() - t0, 1)}
