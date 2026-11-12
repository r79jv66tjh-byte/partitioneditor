# PartitionEditor

Partitionen unter Windows bearbeiten wie in der Datenträgerverwaltung – mit Balkenansicht pro Datenträger, gesammelten Änderungen und einer Vorschau „vorher → nachher“ vor dem Ausführen: anlegen und formatieren, löschen, vergrößern/verkleinern, Laufwerksbuchstabe und Name ändern, aktiv setzen, aus-/einblenden sowie leere Datenträger zwischen MBR und GPT umstellen. System- und Windows-Datenträger sind immer gesperrt.

## Start

- **GUI:** `PartitionEditor.pyw` doppelklicken. Windows fragt per UAC nach Adminrechten (ohne Adminrechte nur Ansicht).
- **CLI / Claude:** `python cli.py …` (JSON-Ausgabe, siehe unten und `toolhub.json`).

Voraussetzung: Windows 10/11, Python 3.10+ (nur Standardbibliothek: tkinter, ctypes). Technik: PowerShell-Storage-Cmdlets
`Get-Disk`, `Get-Partition`, `Get-Volume`, `New-Partition`, `Format-Volume`, `Remove-Partition`, `Resize-Partition`,
`Get-PartitionSupportedSize`, `Set-Partition`, `Set-Volume`, `Clear-Disk`, `Initialize-Disk`.

## Bedienung

1. Datenträger, Partition oder freien Bereich im Balken anklicken (Rechtsklick = Menü, Doppelklick = anlegen bzw. Größe ändern).
2. Aktion wählen – sie landet unter **Geplante Änderungen**, der Balken zeigt sofort den Zielzustand
   („Vorher-Zustand zeigen“ schaltet zum Vergleich auf den echten Zustand).
3. **Vorschau & Anwenden …** zeigt alle Schritte und jeden betroffenen Datenträger vorher/nachher.
   Ausführen erst, wenn für jeden Datenträger seine **Nummer eingetippt** ist.
4. Die Schritte laufen nacheinander; beim ersten Fehler wird abgebrochen und angezeigt, was schon erledigt ist.

Hinweise:
- **Größe ändern** geht unter Windows nur bei NTFS/ReFS: verkleinern bis zum von Windows gemeldeten Minimum, vergrößern nur in direkt
  dahinter liegenden freien Platz (Partition davor löschen geht in derselben Liste).
- **MBR**: höchstens 4 Einträge. Bei der 4. Partition legt Windows automatisch eine *erweiterte* Partition an, darin weitere *logische*.
  Eine leere erweiterte Partition entfernt man, indem man ihren freien Bereich wählt und „Löschen“ drückt.
- **Aktiv** gibt es nur bei MBR (setzt alle anderen Partitionen dieses Datenträgers inaktiv).
- **Ausblenden** entfernt den Laufwerksbuchstaben; zum Wiedereinblenden zuerst einblenden, dann Buchstabe vergeben.
- **MBR ↔ GPT** nur bei leeren Datenträgern (bzw. zum Initialisieren eines neuen). Partitionen vorher löschen – das geht als
  Schritte in derselben Liste. `mbr2gpt.exe` wird bewusst nicht benutzt: es wandelt nur den *Systemdatenträger* um, und der ist hier immer gesperrt.

## Sicherheit

Gleiche Regeln wie DiskFormatter:

| Sperre | Art |
|---|---|
| Datenträger mit IsSystem/IsBoot oder System-/Boot-Partition | immer gesperrt |
| Datenträger mit `%SystemDrive%`, dem Windows-Ordner oder einer Auslagerungsdatei | immer gesperrt |
| offline / schreibgeschützt | gesperrt |
| interne Platten (nicht USB/SD) | nur nach *Einstellungen → Interne Festplatten freischalten* (nur in der GUI änderbar, `%APPDATA%\PartitionEditor\settings.json`) |

Vor dem Ausführen wird alles mit einer **frischen Abfrage** erneut geplant und geprüft: Sperren, Kennung des Datenträgers
(UniqueId/Seriennummer/Größe/Ort, muss zur Vorschau passen), Bestätigung und jeder Schritt. Partitionen werden intern über ihren
Offset angesprochen, weil sich Partitionsnummern beim Löschen/Anlegen ändern können.

## CLI (für Claude/ToolHub)

Jeder Befehl gibt ein JSON-Objekt aus; Fehler auf stderr, Exit 2 = gesperrt/Bestätigung falsch.

```
python cli.py disks [--editable]
python cli.py sizes --target T:                      (Adminrechte/UAC)
python cli.py plan  --ops "[{\"op\":\"delete\",\"disk\":3,\"partition\":2}, {\"op\":\"resize\",\"disk\":3,\"partition\":1,\"size\":\"max\"}]"
python cli.py apply --ops "[…]" --confirm-target 3 [--dry-run]
python cli.py create  --disk 3 --size 500MB --fs NTFS --letter auto --label DATEN --confirm-target 3 [--dry-run]
python cli.py delete  --target T: --confirm-target T:
python cli.py resize  --disk 3 --partition 1 --size 2GB --confirm-target 3
python cli.py set     --target T: --letter U --label NEU --active on --hidden off --confirm-target T:
python cli.py convert --disk 3 --style GPT --confirm-target 3
```

`--confirm-target` ist die Datenträgernummer; betrifft die Änderung genau eine bestehende Partition mit Buchstabe, geht auch
dieser Buchstabe. Bei mehreren Datenträgern: `3,4`. Ändernde Befehle ohne Adminrechte starten sich per UAC in einem versteckten
Admin-Prozess; das Ergebnis kommt zurück. Ops in `plan`/`apply`: `create`, `delete`, `resize`, `letter`, `label`, `active`,
`hidden`, `convert` (Felder siehe `toolhub.json` bzw. Docstring in `partitioneditor/ops.py`).

## Tests

- `python -m unittest tests.test_safety -v` – 20 Tests ohne Adminrechte (Sperren, Bestätigung, Simulation mit nachgebauten Daten).
- `python tests\vhd_integration.py --out ergebnis.json` – als Administrator. Legt eine 1-GB-Test-VHDX an und testet daran CLI und GUI
  (39 Prüfungen: alle Funktionen inkl. erweiterter/logischer Partitionen, MBR → GPT → MBR, Sammelanwendung, Sperren am
  Systemdatenträger, fehlende/falsche Bestätigung, Dry-Run). Vor jeder Änderung wird geprüft, dass das Ziel genau die Test-VHDX ist;
  danach wird sie getrennt und gelöscht.

## Aufbau

```
PartitionEditor.pyw         Start (UAC)
cli.py                      Kommandozeile, JSON
toolhub.json                Befehle für ToolHub/Claude
partitioneditor/model.py    Abfrage, Partitionen/freie Bereiche, Sperren, Bestätigung
partitioneditor/ops.py      Änderungen prüfen, simulieren (vorher → nachher), ausführen
partitioneditor/gui.py      tkinter-Oberfläche
partitioneditor/ps.py, elevate.py, settings.py   PowerShell-Aufruf, UAC, Einstellungen
```

## Verwandte Open-Source-Projekte

- [GParted](https://gparted.org/) – Vorbild für „Änderungen sammeln, dann anwenden“ (Linux, GPL).
- [py-win-part](https://sourceforge.net/projects/py-win-part/) – Python-Wrapper um diskpart (BSD); hier nicht nötig, da PowerShell-Cmdlets JSON liefern.
- gptgen – verlustfreie MBR→GPT-Umwandlung von Datenplatten (GPL, alt); bewusst nicht eingebaut.

> **Achtung:** Dieses Tool verändert Datenträger. Falsch angewendet kann es Daten unwiderruflich löschen. Nutzung auf eigenes Risiko, ohne Gewähr. Systemdatenträger sind gesperrt, trotzdem vorher Backups machen.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
