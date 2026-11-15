"""Integrationstest an einer TEST-VHDX (braucht Administratorrechte).

Legt eine virtuelle Festplatte an, hängt sie an und testet daran CLI und GUI-Logik:
Initialisieren, Anlegen, Löschen, Vergrößern/Verkleinern, Buchstabe, Name, aktiv, ausgeblendet,
MBR ↔ GPT, Sammelanwendung, Sperren und Bestätigung. Danach wird die VHDX getrennt und gelöscht.
Echte Laufwerke werden nie verändert: vor jeder ändernden Aktion wird geprüft, dass das Ziel genau
die Test-VHDX ist (BusType + Dateipfad); am Systemdatenträger wird nur geprüft, dass er abgelehnt wird.

    python tests\\vhd_integration.py [--out ergebnis.json] [--size-mb 1024]
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from partitioneditor import model, ops, ps  # noqa: E402
from partitioneditor.settings import Settings  # noqa: E402

RESULTS: list[dict] = []
MiB = model.MiB


def log(name, ok, **info):
    RESULTS.append({"test": name, "ok": bool(ok), **info})
    print(("OK   " if ok else "FAIL ") + name, json.dumps(info, ensure_ascii=False, default=str)[:600], flush=True)


def diskpart(script: str) -> str:
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False, encoding="ascii") as f:
        f.write(script)
        name = f.name
    try:
        p = subprocess.run(["diskpart", "/s", name], capture_output=True, timeout=300)
        return p.stdout.decode(ps.oem_encoding(), "replace")
    finally:
        os.remove(name)


class TestDisk:
    def __init__(self, path: Path, size_mb: int):
        self.path, self.size_mb = path, size_mb
        self.number: int | None = None

    def create(self):
        if self.path.exists():
            self.path.unlink()
        out = diskpart(f'create vdisk file="{self.path}" maximum={self.size_mb} type=expandable\n'
                       f'select vdisk file="{self.path}"\nattach vdisk\n')
        for _ in range(30):
            data = ps.as_list(ps.run_json("Get-Disk | Where-Object { $_.Location -eq " + ps.quote(str(self.path))
                                          + " } | Select-Object Number | ConvertTo-Json -Compress"))
            if data:
                self.number = int(data[0]["Number"])
                return
            time.sleep(1)
        raise RuntimeError("Test-VHDX nicht gefunden:\n" + out)

    def check(self):
        """Sicherheitsnetz: Abbruch, wenn die Nummer nicht (mehr) die Test-VHDX ist."""
        d = ps.as_list(ps.run_json(f"Get-Disk -Number {int(self.number)} | Select-Object Number,"
                                   "@{n='Bus';e={[string]$_.BusType}},Location,IsSystem,IsBoot | ConvertTo-Json -Compress"))
        if not d or d[0]["Bus"] != "File Backed Virtual" or str(d[0]["Location"]).lower() != str(self.path).lower() \
                or d[0]["IsSystem"] or d[0]["IsBoot"]:
            raise SystemExit(f"SICHERHEITSABBRUCH: Datenträger {self.number} ist nicht die Test-VHDX: {d}")

    def state(self) -> model.Disk:
        for _ in range(20):
            snap = model.query()
            d = next((x for x in snap.disks if x.location.lower() == str(self.path).lower()), None)
            if d is not None:
                if d.number != self.number:
                    print(f"Hinweis: Test-VHDX hat jetzt Nummer {d.number} (vorher {self.number})")
                    self.number = d.number
                return d
            time.sleep(1)
        raise RuntimeError("Test-VHDX in der Abfrage nicht gefunden: " + str([(x.number, x.location) for x in snap.disks]))

    def destroy(self):
        diskpart(f'select vdisk file="{self.path}"\ndetach vdisk\n')
        time.sleep(1)
        try:
            self.path.unlink()
        except OSError as e:
            print("VHDX konnte nicht gelöscht werden:", e)


def cli(*args, env=None) -> tuple[int, dict | str]:
    p = subprocess.run([sys.executable, str(ROOT / "cli.py"), *map(str, args)], capture_output=True,
                       timeout=900, env=env, cwd=str(ROOT))
    out, err = p.stdout.decode("utf-8", "replace"), p.stderr.decode("utf-8", "replace")
    try:
        return p.returncode, json.loads(out)
    except ValueError:
        return p.returncode, err.strip() or out.strip()


def parts(d: model.Disk) -> list[dict]:
    return [{"n": p.number, "off": p.offset, "mb": p.size // MiB, "L": p.letter, "label": p.label,
             "fs": p.filesystem, "type": p.type, "active": p.is_active, "hidden": p.is_hidden} for p in d.partitions]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out")
    ap.add_argument("--size-mb", type=int, default=1024)
    a = ap.parse_args()
    if not ps.is_admin():
        print("Braucht Administratorrechte")
        return 1
    tmp = Path(tempfile.mkdtemp(prefix="pe_test_"))
    settings_file = tmp / "settings.json"
    os.environ["PARTITIONEDITOR_SETTINGS"] = str(settings_file)
    env = dict(os.environ)
    td = TestDisk(tmp / "partitioneditor_test.vhdx", a.size_mb)
    try:
        td.create()
        td.check()
        N = td.number
        log("VHDX angelegt", True, disk=N)

        # ---------------- Sperren
        Settings(allow_internal=False).save()
        code, r = cli("convert", "--disk", N, "--style", "MBR", "--confirm-target", N, env=env)
        log("Sperre: VHDX gilt als intern -> ohne Freischalten abgelehnt", code == 2 and "Interner" in str(r), code=code, r=r)
        sysdisk = next(d for d in model.query().disks if d.hard_locks)
        sp = max(sysdisk.partitions, key=lambda p: p.size)
        Settings(allow_internal=True).save()
        code, r = cli("delete", "--disk", sysdisk.number, "--partition", sp.number, "--confirm-target", sysdisk.number, env=env)
        log("Sperre: Systemdatenträger auch mit Freischalten abgelehnt (löschen)", code == 2, code=code, r=r)
        code, r = cli("resize", "--disk", sysdisk.number, "--partition", sp.number, "--size", "10GB",
                      "--confirm-target", sysdisk.number, env=env)
        log("Sperre: Systemdatenträger verkleinern abgelehnt", code == 2, code=code, r=str(r)[:200])
        code, r = cli("set", "--target", f"{ps.system_drive()}:", "--letter", "Q", "--confirm-target", f"{ps.system_drive()}:", env=env)
        log("Sperre: Buchstabe des Windows-Laufwerks ändern abgelehnt", code == 2, code=code, r=r)
        try:
            ops.execute([{"op": "delete", "disk": sysdisk.number, "offset": sp.offset}], {sysdisk.number: str(sysdisk.number)},
                        settings=Settings(allow_internal=True))
            log("Sperre: ops.execute am Systemdatenträger", False)
        except ops.OpError as e:
            log("Sperre: ops.execute am Systemdatenträger abgelehnt", True, err=str(e)[:200])
        code, r = cli("convert", "--disk", sysdisk.number, "--style", "MBR", "--dry-run", env=env)
        log("Dry-Run am Systemdatenträger zeigt Sperrgründe", code == 0 and r["disks"][0]["locked_reasons"], reasons=r["disks"][0]["locked_reasons"] if isinstance(r, dict) else r)

        # ---------------- Initialisieren MBR + Bestätigung
        td.check()
        code, r = cli("convert", "--disk", N, "--style", "MBR", "--dry-run", env=env)
        log("Dry-Run ändert nichts", code == 0 and r["dry_run"] and td.state().style == "RAW", style=td.state().style)
        code, r = cli("convert", "--disk", N, "--style", "MBR", env=env)
        log("Bestätigung fehlt -> abgelehnt", code == 2, r=r)
        code, r = cli("convert", "--disk", N, "--style", "MBR", "--confirm-target", N + 1, env=env)
        log("Falsche Bestätigung -> abgelehnt", code == 2 and td.state().style == "RAW", r=r)
        td.check()
        code, r = cli("convert", "--disk", N, "--style", "MBR", "--confirm-target", N, env=env)
        log("RAW -> MBR initialisieren", code == 0 and td.state().style == "MBR", r=r if code else "ok")

        # ---------------- Anlegen
        td.check()
        code, r = cli("create", "--disk", N, "--size", "300MB", "--fs", "NTFS", "--letter", "auto", "--label", "ERSTE",
                      "--confirm-target", N, env=env)
        d = td.state()
        p1 = d.partitions[0] if d.partitions else None
        log("Anlegen NTFS 300 MB mit Buchstabe+Name", code == 0 and p1 and p1.filesystem == "NTFS" and p1.label == "ERSTE"
            and p1.letter and p1.size == 300 * MiB, parts=parts(d), r=r if code else "")
        td.check()
        code, r = cli("create", "--disk", N, "--size", "200MB", "--fs", "exFAT", "--letter", "none", "--label", "ZWEITE",
                      "--confirm-target", N, env=env)
        d = td.state()
        log("Anlegen exFAT ohne Buchstabe", code == 0 and len(d.partitions) == 2 and d.partitions[1].filesystem == "exFAT"
            and not d.partitions[1].letter, parts=parts(d), r=r if code else "")
        td.check()
        code, r = cli("create", "--disk", N, "--size", "100MB", "--fs", "FAT32", "--letter", "auto", "--label", "DRITTE",
                      "--confirm-target", N, env=env)
        d = td.state()
        log("Anlegen FAT32", code == 0 and len(d.partitions) == 3 and d.partitions[2].filesystem == "FAT32", parts=parts(d), r=r if code else "")
        td.check()
        code, r = cli("create", "--disk", N, "--size", "100MB", "--fs", "RAW", "--letter", "none", "--confirm-target", N, env=env)
        d = td.state()
        lg = [p for p in d.partitions if p.type == "Logical"]
        log("4. Partition (unformatiert) -> Windows legt erweiterte + logische an", code == 0 and d.containers()
            and len(lg) == 1 and not lg[0].filesystem and lg[0].size == 100 * MiB, parts=parts(d), r=r if code else "")
        td.check()
        code, r = cli("create", "--disk", N, "--fs", "NTFS", "--letter", "none", "--label", "LOGISCH2", "--confirm-target", N, env=env)
        d = td.state()
        lg = [p for p in d.partitions if p.type == "Logical"]
        log("Zweite logische Partition im Rest der erweiterten", code == 0 and len(lg) == 2 and lg[1].label == "LOGISCH2"
            and not d.gaps(), parts=parts(d), gaps=[g.to_json() for g in d.gaps()], r=r if code else "")
        td.check()
        batch = [{"op": "delete", "disk": N, "offset": q.offset} for q in lg] +                 [{"op": "delete", "disk": N, "offset": d.containers()[0].offset}]
        code, r = cli("apply", "--ops", json.dumps(batch), "--confirm-target", N, env=env)
        d = td.state()
        log("Logische + erweiterte Partition löschen (Sammelanwendung)", code == 0 and not d.containers() and len(d.partitions) == 3,
            parts=parts(d), r=r if code else r.get("done"))

        # ---------------- Eigenschaften
        L1 = td.state().partitions[0].letter
        newL = model.query().free_letters()[-1]
        td.check()
        code, r = cli("set", "--target", f"{L1}:", "--letter", newL, "--label", "UMBENANNT", "--confirm-target", f"{L1}:", env=env)
        p = td.state().partitions[0]
        log("Buchstabe + Name ändern (Bestätigung per Buchstabe)", code == 0 and p.letter == newL and p.label == "UMBENANNT",
            letter=p.letter, label=p.label, r=r if code else "")
        td.check()
        code, r = cli("set", "--disk", N, "--partition", p.number, "--active", "on", "--confirm-target", N, env=env)
        p = td.state().partitions[0]
        log("Aktiv setzen (MBR)", code == 0 and p.is_active, active=p.is_active, r=r if code else "")
        td.check()
        code, r = cli("set", "--disk", N, "--partition", p.number, "--hidden", "on", "--confirm-target", N, env=env)
        p = td.state().partitions[0]
        log("Ausblenden (MBR)", code == 0 and p.is_hidden and not p.letter, hidden=p.is_hidden, letter=p.letter, mbr=p.mbr_type, r=r if code else "")
        td.check()
        code, r = cli("set", "--disk", N, "--partition", p.number, "--hidden", "off", "--letter", newL, "--confirm-target", N, env=env)
        p = td.state().partitions[0]
        log("Einblenden + Buchstabe wieder setzen", code == 0 and not p.is_hidden and p.letter == newL, hidden=p.is_hidden,
            letter=p.letter, r=r if code else "")
        td.check()
        code, r = cli("set", "--disk", N, "--partition", p.number, "--letter", "none", "--confirm-target", N, env=env)
        p = td.state().partitions[0]
        log("Buchstabe entfernen", code == 0 and not p.letter, r=r if code else "")

        # ---------------- Größe
        code, r = cli("sizes", "--disk", N, "--partition", p.number, env=env)
        log("Größenbereich (Get-PartitionSupportedSize)", code == 0 and r["min_bytes"] < r["max_bytes"], r=r)
        td.check()
        code, r = cli("resize", "--disk", N, "--partition", p.number, "--size", "200MB", "--confirm-target", N, env=env)
        p = td.state().partitions[0]
        log("Verkleinern NTFS 300 -> 200 MB", code == 0 and p.size == 200 * MiB, mb=p.size // MiB, r=r if code else "")
        td.check()
        code, r = cli("resize", "--disk", N, "--partition", p.number, "--size", "max", "--confirm-target", N, env=env)
        p = td.state().partitions[0]
        log("Vergrößern bis zur nächsten Partition", code == 0 and p.size == 300 * MiB, mb=p.size // MiB, r=r if code else "")
        code, r = cli("resize", "--disk", N, "--partition", td.state().partitions[1].number, "--size", "100MB",
                      "--confirm-target", N, "--dry-run", env=env)
        log("Größe ändern bei exFAT abgelehnt", code == 0 and not r["ok"], errors=r.get("errors") if isinstance(r, dict) else r)

        # ---------------- Sammelanwendung: löschen + vergrößern + neu anlegen
        d = td.state()
        batch = [{"op": "delete", "disk": N, "offset": d.partitions[1].offset},
                 {"op": "resize", "disk": N, "offset": d.partitions[0].offset, "size": "400MB"},
                 {"op": "create", "disk": N, "offset": d.partitions[0].offset + 400 * MiB, "size": "max",
                  "fs": "NTFS", "letter": "none", "label": "NACHRUECK"}]
        td.check()
        code, r = cli("apply", "--ops", json.dumps(batch), "--confirm-target", N, env=env)
        d = td.state()
        log("Sammelanwendung: löschen, vergrößern, anlegen", code == 0 and r.get("ok") and d.partitions[0].size == 400 * MiB
            and d.partitions[1].label == "NACHRUECK" and d.partitions[1].size == 100 * MiB, parts=parts(d),
            r=r if code else r.get("done"))

        # ---------------- MBR -> GPT (alles löschen + umstellen + anlegen in einem Rutsch)
        d = td.state()
        batch = [{"op": "delete", "disk": N, "offset": p.offset} for p in d.partitions] + \
                [{"op": "convert", "disk": N, "style": "GPT"},
                 {"op": "create", "disk": N, "size": "256MB", "fs": "NTFS", "letter": "auto", "label": "GPT1"},
                 {"op": "create", "disk": N, "size": "max", "fs": "exFAT", "letter": "none", "label": "GPT2"}]
        code, r = cli("plan", "--ops", json.dumps(batch), env=env)
        log("Vorschau (plan) vorher -> nachher", code == 0 and r["ok"] and r["disks"][0]["style_after"] == "GPT"
            and len(r["disks"][0]["after"]) >= 2, steps=[s["change"] for s in r["steps"]] if isinstance(r, dict) else r)
        td.check()
        code, r = cli("apply", "--ops", json.dumps(batch), "--confirm-target", N, env=env)
        d = td.state()
        data = [p for p in d.partitions if p.type != "Reserved"]
        log("MBR -> GPT mit Neuanlage in einem Durchgang", code == 0 and r.get("ok") and d.style == "GPT" and len(data) == 2
            and data[0].label == "GPT1" and data[1].filesystem == "exFAT", parts=parts(d), r=r if code else r.get("log"))

        # ---------------- GPT-Eigenschaften
        p = data[0]
        code, r = cli("set", "--disk", N, "--partition", p.number, "--active", "on", "--confirm-target", N, "--dry-run", env=env)
        log("Aktiv bei GPT abgelehnt", code == 0 and not r["ok"], errors=r.get("errors") if isinstance(r, dict) else r)
        td.check()
        code, r = cli("set", "--disk", N, "--partition", p.number, "--hidden", "on", "--confirm-target", N, env=env)
        p2 = td.state().by_offset(p.offset)
        log("Ausblenden (GPT-Attribut)", code == 0 and p2.is_hidden and not p2.letter, hidden=p2.is_hidden, letter=p2.letter,
            before_letter=p.letter, r=r if code else "")
        td.check()
        code, r = cli("set", "--disk", N, "--partition", p.number, "--hidden", "off", "--confirm-target", N, env=env)
        log("Einblenden (GPT)", code == 0 and not td.state().by_offset(p.offset).is_hidden, r=r if code else "")

        # ---------------- GPT -> MBR
        d = td.state()
        batch = [{"op": "delete", "disk": N, "offset": p.offset} for p in d.partitions if p.type != "Reserved"] + \
                [{"op": "convert", "disk": N, "style": "MBR"}]
        td.check()
        code, r = cli("apply", "--ops", json.dumps(batch), "--confirm-target", N, env=env)
        d = td.state()
        log("GPT -> MBR (leer)", code == 0 and d.style == "MBR" and not d.partitions, parts=parts(d), r=r if code else "")

        # ---------------- GUI: App laden, Änderung über Dialog-Logik anwenden
        try:
            gui_test(td)
        except Exception:
            log("GUI", False, err=traceback.format_exc()[-800:])
        return 0
    except SystemExit:
        raise
    except Exception:
        log("Unerwarteter Fehler", False, err=traceback.format_exc()[-1500:])
        return 1
    finally:
        td.destroy()
        log("VHDX getrennt und gelöscht", not td.path.exists())
        if a.out:
            Path(a.out).write_text(json.dumps(RESULTS, ensure_ascii=False, indent=1, default=str), encoding="utf-8")


def gui_test(td: TestDisk):
    from partitioneditor import gui
    app = gui.App()
    app.withdraw()
    app.refresh()
    app.update()
    N = td.number
    d = app.view_disk(N)
    gap = d.gaps()[0]
    app.sel = (N, gap.offset, "gap")
    app.redraw()
    log("GUI: Balken gezeichnet, freien Bereich gewählt, Anlegen erlaubt",
        "disabled" not in app.btns["create"].state() and "disabled" in app.btns["delete"].state())
    app.add_op({"op": "create", "disk": N, "offset": gap.offset, "size": 128 * MiB, "fs": "NTFS", "letter": "", "label": "GUI1"})
    app.add_op({"op": "create", "disk": N, "size": None, "fs": "exFAT", "letter": "", "label": "GUI2"})
    after = app.view_disk(N)
    log("GUI: zwei Änderungen gesammelt, Nachher-Ansicht", len(app.ops) == 2
        and len([p for p in after.partitions if p.new and not p.is_container]) == 2,
        steps=app.lst.get(0, "end"))
    sysd = next(x for x in app.snap.disks if x.hard_locks)
    app.sel = (sysd.number, max(sysd.partitions, key=lambda p: p.size).offset, "part")
    app.redraw()
    log("GUI: Systemdatenträger – alle Aktionen gesperrt", all("disabled" in b.state() for b in app.btns.values()))
    plan = ops.make_plan(app.snap, app.settings, app.ops, app.supported, fetch_supported=False)
    dlg = gui.ApplyDialog(app, plan)
    dlg.entries[N].set(str(N + 1))
    app.update()
    wrong_disabled = "disabled" in dlg.btn_run.state()
    dlg.entries[N].set(str(N))
    app.update()
    log("GUI: Ausführen erst nach richtiger Nummer", wrong_disabled and "disabled" not in dlg.btn_run.state())
    td.check()
    dlg.run()
    t0 = time.time()
    while not dlg.done and time.time() - t0 < 300:
        app.update()
        time.sleep(0.1)
    d = td.state()
    log("GUI: Änderungen angewendet", dlg.done and [p.label for p in d.partitions if not p.is_container][-2:] == ["GUI1", "GUI2"], parts=parts(d),
        log=dlg.log.get("1.0", "end")[-600:])
    dlg.destroy()
    app.destroy()


if __name__ == "__main__":
    sys.exit(main())
