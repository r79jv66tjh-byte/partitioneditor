"""PartitionEditor-Kommandozeile für Claude/ToolHub.

Jeder Befehl schreibt genau ein JSON-Objekt auf stdout. Fehler: Meldung auf stderr, Exit-Code ≠ 0
(2 = gesperrt/Bestätigung falsch). Es gelten dieselben Sperren wie in der GUI: System-/Boot-/
Windows-Datenträger nie, interne Platten nur, wenn sie in der GUI freigeschaltet wurden.

    python cli.py disks
    python cli.py sizes --disk 3 --partition 2
    python cli.py create --disk 3 --size 500MB --fs NTFS --letter auto --label DATEN --confirm-target 3 --dry-run
    python cli.py delete --target T: --confirm-target T:
    python cli.py resize --disk 3 --partition 2 --size 2GB --confirm-target 3
    python cli.py set --target T: --letter U --label NEU --confirm-target T:
    python cli.py convert --disk 3 --style GPT --confirm-target 3
    python cli.py apply --ops "[{\"op\":\"delete\",\"disk\":3,\"partition\":1}, ...]" --confirm-target 3

Ändernde Befehle ohne --dry-run brauchen Administratorrechte; fehlen sie, fragt Windows per UAC
nach (der Vorgang läuft dann in einem versteckten Admin-Prozess, das Ergebnis kommt hierher).
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from partitioneditor import elevate, model, ops, ps  # noqa: E402
from partitioneditor.settings import Settings, settings_path  # noqa: E402


class CliError(Exception):
    def __init__(self, msg: str, code: int = 1):
        super().__init__(msg)
        self.code = code


def cmd_disks(a) -> dict:
    st = Settings.load()
    snap = model.query()
    ds = [d.to_json(st) for d in snap.disks]
    if a.editable:
        ds = [d for d in ds if d["editable"]]
    return {"admin": ps.is_admin(), "allow_internal": st.allow_internal,
            "settings_file": str(settings_path()), "free_letters": snap.free_letters(), "disks": ds}


def cmd_sizes(a) -> dict:
    snap = model.query()
    o = ops.normalize({"op": "resize", "disk": a.disk, "partition": a.partition, "target": a.target}, snap)
    p = snap.disk(o["disk"]).by_offset(o["offset"])
    if not ps.is_admin():       # Get-PartitionSupportedSize verweigert ohne Adminrechte den Zugriff
        return _run_elevated()
    lo, hi = model.supported_size(o["disk"], o["offset"])
    return {"disk": o["disk"], "partition": p.to_json(), "min_bytes": lo, "min": model.fmt_size(lo),
            "max_bytes": hi, "max": model.fmt_size(hi),
            "resizable": p.filesystem in ops.RESIZABLE_FS and lo < hi}


def _ops_from_args(a) -> list[dict]:
    base = {"disk": getattr(a, "disk", None), "target": getattr(a, "target", None), "partition": getattr(a, "partition", None)}
    base = {k: v for k, v in base.items() if v not in (None, "")}
    if a.cmd == "create":
        o = {"op": "create", "disk": a.disk, "size": a.size, "fs": a.fs, "letter": a.letter, "label": a.label or ""}
        if a.offset is not None:
            o["offset"] = a.offset
        return [o]
    if a.cmd == "delete":
        return [{"op": "delete", **base}]
    if a.cmd == "resize":
        return [{"op": "resize", **base, "size": a.size}]
    if a.cmd == "convert":
        return [{"op": "convert", "disk": a.disk, "style": a.style}]
    if a.cmd == "set":
        out = []
        if a.hidden == "off":       # erst einblenden, dann Buchstabe/Name
            out.append({"op": "hidden", **base, "value": False})
        if a.letter is not None:
            out.append({"op": "letter", **base, "letter": a.letter})
        if a.label is not None:
            out.append({"op": "label", **base, "label": a.label})
        if a.active is not None:
            out.append({"op": "active", **base, "value": a.active == "on"})
        if a.hidden == "on":
            out.append({"op": "hidden", **base, "value": True})
        if not out:
            raise CliError("Nichts zu ändern: --letter, --label, --active oder --hidden angeben")
        return out
    if a.cmd in ("apply", "plan"):
        try:
            data = json.loads(Path(a.ops_file).read_text(encoding="utf-8") if a.ops_file else (a.ops or ""))
        except (OSError, ValueError) as e:
            raise CliError(f"Änderungsliste (JSON) nicht lesbar: {e}") from None
        if isinstance(data, dict):
            data = data.get("ops", [data])
        if not isinstance(data, list) or not data:
            raise CliError("Änderungsliste muss eine nicht leere JSON-Liste sein")
        return data
    raise CliError(a.cmd)


def _parse_confirms(text: str | None, disks: list[int]) -> dict[int, str]:
    """'3' oder 'T:' für einen Datenträger; bei mehreren: '3,4' oder '3=3,4=4'."""
    t = (text or "").strip()
    if not t:
        return {}
    parts = [x.strip() for x in t.split(",") if x.strip()]
    if all("=" in x for x in parts):
        return {int(k): v.strip() for k, v in (x.split("=", 1) for x in parts) if k.strip().isdigit()}
    if len(disks) == 1 and len(parts) == 1:
        return {disks[0]: parts[0]}
    out = {}
    for x in parts:          # Liste von Nummern: jede bestätigt „ihren“ Datenträger
        if re.fullmatch(r"\d+", x) and int(x) in disks:
            out[int(x)] = x
    return out


def _change(a) -> dict:
    st = Settings.load()
    snap = model.query()
    raw_ops = _ops_from_args(a)
    plan = ops.make_plan(snap, st, raw_ops)
    res = plan.to_json(st)
    confirms = _parse_confirms(getattr(a, "confirm_target", None), sorted(plan.before))
    res["confirm_target_ok"] = bool(plan.before) and all(
        model.confirm_ok(d, plan.confirm_letters(n), confirms.get(n)) for n, d in plan.before.items())
    if a.cmd == "plan" or a.dry_run:
        return {"dry_run": True, **res}
    if any(plan.blocked.values()):
        raise CliError("Gesperrt: " + " | ".join(f"Datenträger {n}: " + "; ".join(b) for n, b in plan.blocked.items() if b), 2)
    if not plan.ok:
        raise CliError("Nicht ausführbar: " + " | ".join(plan.errors))
    if not a.confirm_target:
        raise CliError("--confirm-target fehlt (Datenträgernummer: " + ", ".join(map(str, sorted(plan.before))) + ")", 2)
    if not res["confirm_target_ok"]:
        raise CliError(f"--confirm-target '{a.confirm_target}' passt nicht (erwartet Datenträgernummer: "
                       + ", ".join(map(str, sorted(plan.before))) + ")", 2)
    if not ps.is_admin():
        return _run_elevated()
    log: list[str] = []
    r = ops.execute(plan.ops, confirms, expect_uids={n: ops.disk_uid(d) for n, d in plan.before.items()},
                    settings=st, log=log.append)
    after = model.query()
    return {"dry_run": False, **r, "log": log,
            "disks_now": [after.disk(n).to_json(st) for n in sorted(plan.before) if after.disk(n)]}


def _run_elevated() -> dict:
    fd, out = tempfile.mkstemp(prefix="partitioneditor_", suffix=".json")
    os.close(fd)
    args = [str(HERE / "cli.py")] + sys.argv[1:] + ["--result-file", out]
    try:
        code = elevate.run_elevated(sys.executable, args, cwd=str(HERE), wait=True, show=0)
    except OSError as e:
        raise CliError(str(e)) from None
    try:
        data = json.loads(Path(out).read_text(encoding="utf-8") or "{}")
    except (OSError, ValueError):
        data = {}
    finally:
        try:
            os.remove(out)
        except OSError:
            pass
    if "error" in data and set(data) <= {"error", "code"}:
        raise CliError("(Admin-Prozess) " + data["error"], data.get("code", 1))
    if not data:
        raise CliError(f"Admin-Prozess endete mit Exit {code} ohne Ergebnis")
    data["elevated"] = True
    return data


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="cli.py", description="PartitionEditor (JSON-Ausgabe)")
    p.add_argument("--result-file", help=argparse.SUPPRESS)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("disks", help="Datenträger mit Partitionen, freien Bereichen und Sperren")
    s.add_argument("--editable", action="store_true", help="Nur bearbeitbare Datenträger")
    s.add_argument("--result-file", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_disks)

    def target(s, need_part=True):
        s.add_argument("--disk", type=int, help="Datenträgernummer")
        if need_part:
            s.add_argument("--partition", type=int, help="Partitionsnummer (mit --disk)")
            s.add_argument("--target", help="Alternativ: Laufwerksbuchstabe der Partition, z. B. T:")

    def change(s):
        s.add_argument("--confirm-target", dest="confirm_target",
                       help="Bestätigung: Datenträgernummer (bei nur einer betroffenen Partition auch ihr Buchstabe)")
        s.add_argument("--dry-run", dest="dry_run", action="store_true", help="Nur Vorschau")
        s.add_argument("--result-file", help=argparse.SUPPRESS)
        s.set_defaults(fn=_change)

    s = sub.add_parser("sizes", help="Möglicher Größenbereich einer Partition (Get-PartitionSupportedSize)")
    target(s)
    s.add_argument("--result-file", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_sizes)

    s = sub.add_parser("create", help="Partition anlegen (und formatieren)")
    target(s, need_part=False)
    s.add_argument("--size", default="max", help="z. B. 500MB, 2GB oder max (Standard)")
    s.add_argument("--offset", type=int, help="Start in Bytes (Standard: größter freier Bereich)")
    s.add_argument("--fs", default="NTFS", help="NTFS, exFAT, FAT32 oder RAW (unformatiert)")
    s.add_argument("--letter", default="auto", help="Buchstabe, auto oder none")
    s.add_argument("--label", default="", help="Name")
    change(s)

    s = sub.add_parser("delete", help="Partition löschen")
    target(s)
    change(s)

    s = sub.add_parser("resize", help="Partition vergrößern/verkleinern (NTFS/ReFS)")
    target(s)
    s.add_argument("--size", required=True, help="Neue Größe, z. B. 2GB, oder max")
    change(s)

    s = sub.add_parser("set", help="Buchstabe, Name, aktiv, ausgeblendet ändern")
    target(s)
    s.add_argument("--letter", help="Neuer Buchstabe oder none")
    s.add_argument("--label", help="Neuer Name")
    s.add_argument("--active", choices=["on", "off"], help="Aktiv setzen (nur MBR)")
    s.add_argument("--hidden", choices=["on", "off"], help="Ausblenden")
    change(s)

    s = sub.add_parser("convert", help="Leeren Datenträger auf GPT oder MBR umstellen / initialisieren")
    target(s, need_part=False)
    s.add_argument("--style", required=True, choices=["GPT", "MBR"])
    change(s)

    for name, hlp in (("plan", "Vorschau einer Änderungsliste (ändert nichts)"),
                      ("apply", "Änderungsliste (JSON) gemeinsam ausführen")):
        s = sub.add_parser(name, help=hlp)
        s.add_argument("--ops", help="JSON-Liste der Änderungen")
        s.add_argument("--ops-file", dest="ops_file", help="Datei mit der JSON-Liste")
        change(s)
    return p


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except Exception:
            pass
    a = build_parser().parse_args(argv)
    if a.cmd in ("delete", "resize", "set", "sizes") and not a.target and (a.disk is None or a.partition is None):
        res, code = {"error": "Partition angeben: --target T: oder --disk N --partition P", "code": 1}, 1
    elif a.cmd in ("create", "convert") and a.disk is None:
        res, code = {"error": "--disk fehlt", "code": 1}, 1
    else:
        try:
            res, code = a.fn(a), 0
        except CliError as e:
            res, code = {"error": str(e), "code": e.code}, e.code
        except (ops.OpError, ps.PSError) as e:
            res, code = {"error": str(e), "code": 1}, 1
    if res.get("ok") is False and not res.get("dry_run", True):
        code = code or 1
    if getattr(a, "result_file", None):   # versteckter Admin-Prozess: Ergebnis an den Aufrufer zurückgeben
        Path(a.result_file).write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        return code
    if "error" in res and set(res) <= {"error", "code"}:
        print(res["error"], file=sys.stderr)
        return code
    print(json.dumps(res, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
