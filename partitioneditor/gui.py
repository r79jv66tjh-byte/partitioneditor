"""PartitionEditor-Oberfläche (tkinter): Datenträger als Balken, Änderungen sammeln, Vorschau, anwenden."""
from __future__ import annotations

import queue
import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import model, ops, ps
from .model import Disk, Gap, MiB, Partition, fmt_size
from .settings import Settings

ROW_H = 74
LEFT_W = 210
PAD = 8
MIN_SEG = 54
FS_COLORS = {"NTFS": "#4f86c6", "FAT32": "#4caf72", "exFAT": "#2fa4a4", "ReFS": "#7a68c4", "": "#9aa0a6"}
STRIPE = {"primary": "#1f3f7a", "logical": "#3c8c3c", "system": "#8a5a00", "new": "#e08a00"}


def _fit(lines: list[str], w: float) -> str:
    """Zeilen auf die Segmentbreite kürzen (≈ 6 px pro Zeichen bei Segoe UI 8)."""
    n = max(3, int((w - 8) / 6))
    return "\n".join(ln if len(ln) <= n else ln[:n - 1] + "…" for ln in lines if ln)


def _seg_fill(s) -> str:
    if isinstance(s, Gap):
        return "#ffffff"
    return FS_COLORS.get(s.filesystem, "#9aa0a6")


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PartitionEditor")
        self.geometry("1150x720")
        self.minsize(900, 560)
        self.settings = Settings.load()
        self.admin = ps.is_admin()
        self.snap: model.Snapshot | None = None
        self.ops: list[dict] = []
        self.supported: dict[int, dict[int, tuple[int, int]]] = {}
        self.plan: ops.Plan | None = None
        self.sel: tuple[int, int, str] | None = None        # (disk, offset, "part"|"gap"|"disk")
        self.show_before = tk.BooleanVar(value=False)
        self._hit: list[tuple[tuple[int, int, int, int], tuple]] = []
        try:
            ttk.Style(self).theme_use("vista")
        except tk.TclError:
            pass
        self._build()
        self.after(50, self.refresh)

    # ------------------------------------------------------------ Aufbau
    def _build(self):
        m = tk.Menu(self)
        fm = tk.Menu(m, tearoff=False)
        fm.add_command(label="Aktualisieren (F5)", command=self.refresh)
        fm.add_separator()
        fm.add_command(label="Beenden", command=self.destroy)
        m.add_cascade(label="Datei", menu=fm)
        sm = tk.Menu(m, tearoff=False)
        self.var_internal = tk.BooleanVar(value=self.settings.allow_internal)
        sm.add_checkbutton(label="Interne Festplatten freischalten …", variable=self.var_internal,
                           command=self._toggle_internal)
        m.add_cascade(label="Einstellungen", menu=sm)
        self.config(menu=m)
        self.bind("<F5>", lambda e: self.refresh())
        self.bind("<Delete>", lambda e: self.act_delete())

        bar = ttk.Frame(self, padding=(8, 6, 8, 0))
        bar.pack(fill="x")
        self.btns = {}
        for key, text, fn in [("create", "Neu …", self.act_create), ("delete", "Löschen", self.act_delete),
                              ("resize", "Größe ändern …", self.act_resize), ("letter", "Buchstabe …", self.act_letter),
                              ("label", "Name …", self.act_label), ("active", "Aktiv setzen", self.act_active),
                              ("hidden", "Ausblenden", self.act_hidden), ("convert", "MBR/GPT …", self.act_convert)]:
            b = ttk.Button(bar, text=text, command=fn)
            b.pack(side="left", padx=(0, 4))
            self.btns[key] = b
        ttk.Button(bar, text="Aktualisieren", command=self.refresh).pack(side="right")
        ttk.Checkbutton(bar, text="Vorher-Zustand zeigen", variable=self.show_before,
                        command=self.redraw).pack(side="right", padx=10)

        self.lbl_status = ttk.Label(self, padding=(10, 4))
        self.lbl_status.pack(fill="x")

        mid = ttk.Frame(self)
        mid.pack(fill="both", expand=True, padx=8)
        self.canvas = tk.Canvas(mid, background="#f3f3f3", highlightthickness=0)
        sb = ttk.Scrollbar(mid, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=sb.set)
        sb.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.canvas.bind("<Configure>", lambda e: self.redraw())
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Double-Button-1>", self._dbl)
        self.canvas.bind("<Button-3>", self._ctx)
        self.canvas.bind_all("<MouseWheel>", lambda e: self.canvas.yview_scroll(int(-e.delta / 120), "units"))

        self.lbl_sel = ttk.Label(self, padding=(10, 4), font=("Segoe UI", 9, "bold"))
        self.lbl_sel.pack(fill="x")

        low = ttk.LabelFrame(self, text="Geplante Änderungen", padding=6)
        low.pack(fill="x", padx=8, pady=(0, 8))
        self.lst = tk.Listbox(low, height=6, activestyle="none")
        self.lst.pack(side="left", fill="both", expand=True)
        side = ttk.Frame(low)
        side.pack(side="right", fill="y", padx=(6, 0))
        ttk.Button(side, text="Schritt entfernen", command=self.remove_step).pack(fill="x")
        ttk.Button(side, text="Alle verwerfen", command=self.clear_ops).pack(fill="x", pady=4)
        self.btn_apply = ttk.Button(side, text="Vorschau & Anwenden …", command=self.apply)
        self.btn_apply.pack(fill="x", pady=(8, 0))

        self.menu = tk.Menu(self, tearoff=False)

    # ------------------------------------------------------------ Daten
    def refresh(self):
        self.config(cursor="watch")
        self.update_idletasks()
        try:
            self.snap = model.query()
        except ps.PSError as e:
            messagebox.showerror("PartitionEditor", f"Datenträger konnten nicht gelesen werden:\n{e}", parent=self)
            return
        finally:
            self.config(cursor="")
        self.supported = {}
        self._replan()

    def _replan(self) -> list[str]:
        self.plan = ops.make_plan(self.snap, self.settings, self.ops, self.supported, fetch_supported=False)
        self.ops = [o for o in self.plan.ops]   # nur gültige Schritte behalten (normalisiert)
        self._fill_list()
        self.redraw()
        return self.plan.errors

    def view_disk(self, n: int) -> Disk:
        if not self.show_before.get() and self.plan and n in self.plan.after:
            return self.plan.after[n]
        return self.snap.disk(n)

    def used_letters(self) -> set[str]:
        used = set(self.snap.used_letters)
        if self.plan:
            for d in self.plan.after.values():
                used |= {p.letter for p in d.partitions if p.letter}
            for d in self.plan.before.values():     # gelöschte/geänderte Buchstaben werden wieder frei
                used -= {p.letter for p in d.partitions if p.letter}
                used |= {p.letter for p in self.plan.after[d.number].partitions if p.letter}
        return used

    def add_op(self, op: dict):
        new = self.ops + [op]
        plan = ops.make_plan(self.snap, self.settings, new, self.supported, fetch_supported=False)
        if len(plan.ops) < len(new) or [e for e in plan.errors if "gesperrt" not in e]:
            messagebox.showerror("Nicht möglich", "\n".join(plan.errors) or "Änderung ungültig", parent=self)
            return
        self.ops = new
        self._replan()

    def remove_step(self):
        i = self.lst.curselection()
        if not i or i[0] >= len(self.ops):
            return
        rest = self.ops[:i[0]] + self.ops[i[0] + 1:]
        plan = ops.make_plan(self.snap, self.settings, rest, self.supported, fetch_supported=False)
        if len(plan.ops) < len(rest):
            if not messagebox.askyesno("Abhängige Schritte",
                                       "Spätere Schritte hängen von diesem ab und fallen dann weg:\n\n"
                                       + "\n".join(plan.errors) + "\n\nTrotzdem entfernen?", parent=self):
                return
        self.ops = rest
        self._replan()

    def clear_ops(self):
        if self.ops and messagebox.askyesno("Verwerfen", "Alle geplanten Änderungen verwerfen?", parent=self):
            self.ops = []
            self._replan()

    def _fill_list(self):
        self.lst.delete(0, "end")
        for i, s in enumerate(self.plan.steps, 1):
            self.lst.insert("end", f"{i}. Datenträger {s['op']['disk']}: {ops.OP_NAMES[s['op']['op']]} – {s['text']}")
        intern = "interne Platten FREIGESCHALTET" if self.settings.allow_internal else "nur USB-/Wechseldatenträger bearbeitbar"
        adm = "Administrator" if self.admin else "OHNE Adminrechte – nur Ansicht"
        self.lbl_status.config(text=f"{len(self.snap.disks)} Datenträger · {intern} · {adm} · "
                                    f"{len(self.ops)} geplante Änderung(en)")
        self.btn_apply.state(["!disabled"] if self.ops and self.admin else ["disabled"])

    # ------------------------------------------------------------ Zeichnen
    def redraw(self):
        c = self.canvas
        c.delete("all")
        self._hit = []
        if not self.snap:
            return
        W = max(c.winfo_width(), 600)
        y = PAD
        for d0 in self.snap.disks:
            d = self.view_disk(d0.number)
            locked = d0.blocks(self.settings)
            changed = self.plan and d0.number in self.plan.after and not self.show_before.get()
            sel_disk = self.sel and self.sel[0] == d0.number and self.sel[2] == "disk"
            c.create_rectangle(PAD, y, PAD + LEFT_W, y + ROW_H, fill="#dfe6ee" if not sel_disk else "#bcd4f0",
                               outline="#7a8a9a")
            txt = (f"Datenträger {d.number}{'  *geändert*' if changed else ''}\n{d.style or '?'} · {d.bus_type}\n"
                   f"{fmt_size(d.size)}\n{(d.model or '')[:28]}")
            c.create_text(PAD + 6, y + 4, text=txt, anchor="nw", font=("Segoe UI", 8))
            if locked:
                c.create_text(PAD + LEFT_W - 6, y + 4, text="🔒", anchor="ne", font=("Segoe UI", 11))
            self._hit.append(((PAD, y, PAD + LEFT_W, y + ROW_H), (d.number, -1, "disk")))
            x0, x1 = PAD * 2 + LEFT_W, W - PAD
            segs = d.segments()
            if d.style == "RAW":
                c.create_rectangle(x0, y, x1, y + ROW_H, fill="#ffffff", outline="#7a8a9a")
                c.create_text((x0 + x1) / 2, y + ROW_H / 2, text="Nicht initialisiert – „MBR/GPT …“ wählen",
                              font=("Segoe UI", 9))
            else:
                self._draw_segments(c, d, segs, x0, x1, y, locked)
            y += ROW_H + PAD
            if locked:
                c.create_text(x0, y - PAD + 2, text=_fit(["gesperrt: " + "; ".join(locked)], x1 - x0), anchor="nw",
                              font=("Segoe UI", 8), fill="#7a1010")
                y += 14
        c.configure(scrollregion=(0, 0, W, y))
        self._update_buttons()

    def _draw_segments(self, c, d: Disk, segs, x0, x1, y, locked):
        total = sum(s.size for s in segs) or 1
        avail = (x1 - x0) - MIN_SEG * len(segs)
        x = x0
        for i, s in enumerate(segs):
            w = MIN_SEG + max(0, avail) * s.size / total
            if i == len(segs) - 1:
                w = x1 - x
            is_sel = self.sel and self.sel[0] == d.number and self.sel[1] == s.offset and self.sel[2] != "disk"
            fill = _seg_fill(s)
            if locked:
                fill = "#d5d5d5" if isinstance(s, Gap) else "#b9bec4"
            c.create_rectangle(x, y, x + w, y + ROW_H, fill=fill, outline="#555" if not is_sel else "#000",
                               width=3 if is_sel else 1)
            if isinstance(s, Gap):
                c.create_text(x + 4, y + 14, anchor="nw", font=("Segoe UI", 8),
                              text=_fit(["Frei (erweitert)" if s.extended is not None else "Frei", fmt_size(s.size)], w))
                self._hit.append(((x, y, x + w, y + ROW_H), (d.number, s.offset, "gap")))
            else:
                key = "new" if s.new else ("system" if (s.is_system or s.is_boot) else
                                            ("logical" if s.type == "Logical" else "primary"))
                c.create_rectangle(x + 1, y + 1, x + w - 1, y + 9, fill=STRIPE[key], outline="")
                head = (f"{s.letter}: " if s.letter else "") + (s.label or ("(neu)" if s.new else s.kind))
                flags = " ".join(f for f, on in (("aktiv", s.is_active), ("versteckt", s.is_hidden)) if on)
                c.create_text(x + 4, y + 12, anchor="nw", font=("Segoe UI", 8),
                              fill="#ffffff" if not locked else "#333",
                              text=_fit([head, f"{fmt_size(s.size)} {s.filesystem}".strip(), flags], w))
                self._hit.append(((x, y, x + w, y + ROW_H), (d.number, s.offset, "part")))
            x += w

    # ------------------------------------------------------------ Auswahl
    def _at(self, e):
        cx, cy = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        for (a, b, c2, d), what in self._hit:
            if a <= cx <= c2 and b <= cy <= d:
                return what
        return None

    def _click(self, e):
        self.sel = self._at(e)
        self.redraw()

    def _dbl(self, e):
        self._click(e)
        if self.sel and self.sel[2] == "gap":
            self.act_create()
        elif self.sel and self.sel[2] == "part":
            self.act_resize()

    def _ctx(self, e):
        self._click(e)
        if not self.sel:
            return
        self.menu.delete(0, "end")
        for key, text, fn in [("create", "Neue Partition …", self.act_create), ("delete", "Löschen", self.act_delete),
                              ("resize", "Größe ändern …", self.act_resize),
                              ("letter", "Laufwerksbuchstabe ändern …", self.act_letter),
                              ("label", "Namen ändern …", self.act_label), ("active", None, self.act_active),
                              ("hidden", None, self.act_hidden), ("convert", "In MBR/GPT umstellen …", self.act_convert)]:
            if str(self.btns[key].cget("state")) == "disabled" or "disabled" in self.btns[key].state():
                continue
            self.menu.add_command(label=text or self.btns[key].cget("text"), command=fn)
        if self.menu.index("end") is not None:
            self.menu.tk_popup(e.x_root, e.y_root)

    def selected(self) -> tuple[Disk | None, Partition | Gap | None]:
        if not self.sel or not self.snap:
            return None, None
        d = self.view_disk(self.sel[0])
        if d is None:
            return None, None
        if self.sel[2] == "part":
            return d, d.by_offset(self.sel[1])
        if self.sel[2] == "gap":
            return d, next((g for g in d.gaps() if g.offset == self.sel[1]), None)
        return d, None

    def _update_buttons(self):
        d, s = self.selected()
        before = self.show_before.get()
        locked = bool(d and self.snap.disk(d.number).blocks(self.settings))
        part = isinstance(s, Partition) and not s.new and not s.is_container
        ok = {k: False for k in self.btns}
        if d and not locked and not before:
            ok["create"] = isinstance(s, Gap) or (s is None and bool(d.gaps()))
            ok["delete"] = (isinstance(s, Partition) and not s.new) or (
                isinstance(s, Gap) and s.extended is not None and not d.logicals(d.by_offset(s.extended)))
            ok["resize"] = part and s.filesystem in ops.RESIZABLE_FS
            ok["letter"] = part
            ok["label"] = part and bool(s.filesystem)
            ok["active"] = part and d.style == "MBR" and s.type != "Logical"
            ok["hidden"] = part
            ok["convert"] = True
        for k, b in self.btns.items():
            b.state(["!disabled"] if ok[k] else ["disabled"])
        if part:
            self.btns["active"].config(text="Inaktiv setzen" if s.is_active else "Aktiv setzen")
            self.btns["hidden"].config(text="Einblenden" if s.is_hidden else "Ausblenden")
        if d is None:
            self.lbl_sel.config(text="Datenträger, Partition oder freien Bereich anklicken (Doppelklick: anlegen bzw. Größe ändern, Rechtsklick: Menü)")
        else:
            info = d.title
            if isinstance(s, Partition):
                info += f"  ›  {s.describe()}  (Offset {s.offset})"
            elif isinstance(s, Gap):
                info += f"  ›  freier Bereich {fmt_size(s.size)}"
            locks = self.snap.disk(d.number).blocks(self.settings)
            if locks:
                info += "   —   GESPERRT: " + "; ".join(locks)
            self.lbl_sel.config(text=info)

    # ------------------------------------------------------------ Aktionen
    def act_create(self):
        d, s = self.selected()
        if d is None:
            return
        gap = s if isinstance(s, Gap) else (max(d.gaps(), key=lambda g: g.size) if d.gaps() else None)
        if gap is None:
            return
        dlg = CreateDialog(self, d, gap, [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if c not in self.used_letters()])
        self.wait_window(dlg)
        if dlg.result:
            self.add_op({"op": "create", "disk": d.number, "offset": gap.offset, **dlg.result})

    def act_delete(self):
        d, s = self.selected()
        if "disabled" in self.btns["delete"].state():
            return
        if isinstance(s, Partition):
            self.add_op({"op": "delete", "disk": d.number, "offset": s.offset})
        elif isinstance(s, Gap) and s.extended is not None:      # leere erweiterte Partition entfernen
            self.add_op({"op": "delete", "disk": d.number, "offset": s.extended})

    def act_resize(self):
        d, s = self.selected()
        if not isinstance(s, Partition) or "disabled" in self.btns["resize"].state():
            return
        rng = self.supported.get(d.number, {}).get(s.offset)
        if rng is None:
            self.config(cursor="watch")
            self.update_idletasks()
            try:
                rng = model.supported_size(d.number, s.offset)
            except ps.PSError as e:
                messagebox.showerror("Größe ändern", f"Größenbereich nicht ermittelbar:\n{e}", parent=self)
                return
            finally:
                self.config(cursor="")
            self.supported.setdefault(d.number, {})[s.offset] = rng
        nxt = min([q.offset for q in d.top_level() if q.offset > s.offset] + [d.usable_end])
        mx = model.align_down(nxt - s.offset)
        mn = model.align_up(rng[0])
        dlg = ResizeDialog(self, s, mn, mx)
        self.wait_window(dlg)
        if dlg.result:
            self.add_op({"op": "resize", "disk": d.number, "offset": s.offset, "size": dlg.result})

    def act_letter(self):
        d, s = self.selected()
        if not isinstance(s, Partition):
            return
        free = [c for c in "DEFGHIJKLMNOPQRSTUVWXYZ" if c not in self.used_letters()]
        dlg = ChoiceDialog(self, "Laufwerksbuchstabe", f"{s.describe()}\n\nNeuer Buchstabe:",
                           ["(keiner)"] + [f"{c}:" for c in free])
        self.wait_window(dlg)
        if dlg.result:
            self.add_op({"op": "letter", "disk": d.number, "offset": s.offset,
                         "letter": "" if dlg.result == "(keiner)" else dlg.result[0]})

    def act_label(self):
        d, s = self.selected()
        if not isinstance(s, Partition):
            return
        dlg = TextDialog(self, "Namen ändern", f"{s.describe()}\n\nNeuer Name (max. {ops.LABEL_MAX.get(s.filesystem, 32)} Zeichen):", s.label)
        self.wait_window(dlg)
        if dlg.result is not None:
            self.add_op({"op": "label", "disk": d.number, "offset": s.offset, "label": dlg.result})

    def act_active(self):
        d, s = self.selected()
        if isinstance(s, Partition):
            self.add_op({"op": "active", "disk": d.number, "offset": s.offset, "value": not s.is_active})

    def act_hidden(self):
        d, s = self.selected()
        if isinstance(s, Partition):
            self.add_op({"op": "hidden", "disk": d.number, "offset": s.offset, "value": not s.is_hidden})

    def act_convert(self):
        d, _ = self.selected()
        if d is None:
            return
        if d.style == "RAW":
            dlg = ChoiceDialog(self, "Datenträger initialisieren", f"{d.title}\n\nPartitionsstil:", ["GPT", "MBR"])
            self.wait_window(dlg)
            target = dlg.result
        else:
            target = "MBR" if d.style == "GPT" else "GPT"
            rest = [p for p in d.partitions if p.type != "Reserved"]
            if rest:
                messagebox.showinfo("MBR/GPT", f"Datenträger {d.number} ist nicht leer.\n\nUmstellen geht nur ohne "
                                    "Partitionen: zuerst alle Partitionen löschen (auch als geplante Schritte), "
                                    "dann erneut „MBR/GPT …“ wählen.", parent=self)
                return
            if not messagebox.askyesno("MBR/GPT", f"Datenträger {d.number} von {d.style} auf {target} umstellen?",
                                       parent=self):
                return
        if target:
            self.add_op({"op": "convert", "disk": d.number, "style": target})

    # ------------------------------------------------------------ Einstellungen
    def _toggle_internal(self):
        if self.var_internal.get():
            if not messagebox.askyesno(
                    "Interne Festplatten freischalten",
                    "ACHTUNG: Damit werden auch fest eingebaute Festplatten/SSDs bearbeitbar.\n\n"
                    "Der System-/Windows-Datenträger bleibt trotzdem immer gesperrt.\n"
                    "Falsches Löschen oder Verkleinern kann Daten unwiderruflich zerstören.\n\n"
                    "Wirklich freischalten?", icon="warning", parent=self):
                self.var_internal.set(False)
                return
        self.settings.allow_internal = bool(self.var_internal.get())
        self.settings.save()
        self._replan()

    # ------------------------------------------------------------ Anwenden
    def apply(self):
        if not self.ops:
            return
        plan = ops.make_plan(self.snap, self.settings, self.ops, self.supported, fetch_supported=False)
        if not plan.ok:
            messagebox.showerror("Nicht ausführbar", "\n".join(plan.errors), parent=self)
            return
        dlg = ApplyDialog(self, plan)
        self.wait_window(dlg)
        if dlg.done:
            self.ops = []
            self.sel = None
            self.refresh()


# ---------------------------------------------------------------- Dialoge

class _Dialog(tk.Toplevel):
    def __init__(self, parent, title):
        super().__init__(parent)
        self.title(title)
        self.transient(parent)
        self.resizable(False, False)
        self.result = None
        self.body = ttk.Frame(self, padding=12)
        self.body.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda e: self.destroy())

    def buttons(self, ok_text="Zur Liste hinzufügen"):
        f = ttk.Frame(self, padding=(12, 0, 12, 12))
        f.pack(fill="x")
        ttk.Button(f, text="Abbrechen", command=self.destroy).pack(side="right")
        ttk.Button(f, text=ok_text, command=self.ok).pack(side="right", padx=6)
        self.bind("<Return>", lambda e: self.ok())
        self.grab_set()

    def ok(self):
        self.destroy()


class CreateDialog(_Dialog):
    def __init__(self, parent, d: Disk, gap: Gap, free_letters: list[str]):
        super().__init__(parent, "Neue Partition")
        b = self.body
        self.maxmb = gap.size // MiB
        ttk.Label(b, text=f"Datenträger {d.number}, freier Bereich {fmt_size(gap.size)}").grid(row=0, column=0, columnspan=2, sticky="w")
        self.v_size = tk.StringVar(value=str(self.maxmb))
        self.v_fs = tk.StringVar(value="NTFS")
        self.v_letter = tk.StringVar(value=f"{free_letters[0]}:" if free_letters else "(keiner)")
        self.v_label = tk.StringVar(value="")
        rows = [("Größe in MB (max. %d):" % self.maxmb, ttk.Spinbox(b, from_=1, to=self.maxmb, textvariable=self.v_size, width=12)),
                ("Dateisystem:", ttk.Combobox(b, textvariable=self.v_fs, values=["NTFS", "exFAT", "FAT32", "RAW (nicht formatieren)"], state="readonly", width=24)),
                ("Laufwerksbuchstabe:", ttk.Combobox(b, textvariable=self.v_letter, values=["(keiner)"] + [f"{c}:" for c in free_letters], state="readonly", width=10)),
                ("Name:", ttk.Entry(b, textvariable=self.v_label, width=26))]
        for i, (t, w) in enumerate(rows, 1):
            ttk.Label(b, text=t).grid(row=i, column=0, sticky="w", pady=3)
            w.grid(row=i, column=1, sticky="w", pady=3)
        self.buttons()

    def ok(self):
        try:
            mb = int(self.v_size.get())
        except ValueError:
            messagebox.showerror("Größe", "Bitte eine ganze Zahl (MB) eingeben", parent=self)
            return
        if not 1 <= mb <= self.maxmb:
            messagebox.showerror("Größe", f"1 … {self.maxmb} MB", parent=self)
            return
        L = self.v_letter.get()
        self.result = {"size": None if mb == self.maxmb else mb * MiB, "fs": self.v_fs.get().split()[0],
                       "letter": "" if L == "(keiner)" else L[0], "label": self.v_label.get().strip()}
        self.destroy()


class ResizeDialog(_Dialog):
    def __init__(self, parent, p: Partition, mn: int, mx: int):
        super().__init__(parent, "Größe ändern")
        b = self.body
        self.mn, self.mx = mn // MiB, mx // MiB
        ttk.Label(b, text=p.describe()).pack(anchor="w")
        ttk.Label(b, text=f"Aktuell {p.size // MiB} MB · möglich {self.mn} … {self.mx} MB "
                          f"(kleiner geht nicht wegen belegter Daten, größer nur bis zur nächsten Partition)").pack(anchor="w", pady=4)
        self.v = tk.IntVar(value=p.size // MiB)
        if self.mx > self.mn:
            ttk.Scale(b, from_=self.mn, to=self.mx, orient="horizontal", length=420,
                      command=lambda x: self.v.set(int(float(x)))).pack(fill="x")
        f = ttk.Frame(b)
        f.pack(anchor="w", pady=4)
        ttk.Label(f, text="Neue Größe in MB:").pack(side="left")
        ttk.Spinbox(f, from_=self.mn, to=self.mx, textvariable=self.v, width=12).pack(side="left", padx=6)
        self.buttons()

    def ok(self):
        try:
            mb = int(self.v.get())
        except (ValueError, tk.TclError):
            return
        if not self.mn <= mb <= self.mx:
            messagebox.showerror("Größe", f"Erlaubt: {self.mn} … {self.mx} MB", parent=self)
            return
        self.result = mb * MiB
        self.destroy()


class ChoiceDialog(_Dialog):
    def __init__(self, parent, title, text, values):
        super().__init__(parent, title)
        ttk.Label(self.body, text=text, justify="left").pack(anchor="w")
        self.v = tk.StringVar(value=values[min(1, len(values) - 1)] if values[0].startswith("(") else values[0])
        ttk.Combobox(self.body, textvariable=self.v, values=values, state="readonly", width=14).pack(anchor="w", pady=6)
        self.buttons()

    def ok(self):
        self.result = self.v.get()
        self.destroy()


class TextDialog(_Dialog):
    def __init__(self, parent, title, text, value=""):
        super().__init__(parent, title)
        ttk.Label(self.body, text=text, justify="left").pack(anchor="w")
        self.v = tk.StringVar(value=value)
        e = ttk.Entry(self.body, textvariable=self.v, width=34)
        e.pack(anchor="w", pady=6)
        e.focus_set()
        self.buttons()

    def ok(self):
        self.result = self.v.get().strip()
        self.destroy()


def _layout_text(d: Disk | None) -> str:
    if d is None:
        return "–"
    if d.style == "RAW":
        return "  nicht initialisiert"
    out = [f"  Stil {d.style}"]
    for s in d.segments():
        out.append(f"  • frei {fmt_size(s.size)}" if isinstance(s, Gap) else f"  • {s.describe()}")
    return "\n".join(out)


class ApplyDialog(tk.Toplevel):
    """Vorschau vorher → nachher, Bestätigung pro Datenträger durch Eintippen der Nummer, dann Ausführung."""

    def __init__(self, parent: App, plan: ops.Plan):
        super().__init__(parent)
        self.app, self.plan, self.done = parent, plan, False
        self.title("Vorschau und Anwenden")
        self.geometry("980x640")
        self.transient(parent)
        self.grab_set()
        txt = tk.Text(self, wrap="word", font=("Consolas", 9), height=20)
        txt.pack(fill="both", expand=True, padx=10, pady=10)
        txt.insert("end", "SCHRITTE (werden in dieser Reihenfolge ausgeführt)\n")
        for i, s in enumerate(plan.steps, 1):
            txt.insert("end", f"  {i}. Datenträger {s['op']['disk']}: {ops.OP_NAMES[s['op']['op']]} – {s['text']}\n")
        for n in sorted(plan.before):
            b = plan.before[n]
            txt.insert("end", f"\n{b.title}   (Modell {b.model}, Seriennr. {b.serial or '?'})\n")
            txt.insert("end", " VORHER:\n" + _layout_text(b) + "\n NACHHER:\n" + _layout_text(plan.after[n]) + "\n")
        txt.insert("end", "\nACHTUNG: Gelöschte Partitionen und ihre Daten sind danach weg. Verkleinern verschiebt "
                          "keine Daten, kann aber bei Fehlern Daten gefährden – vorher sichern.\n")
        txt.config(state="disabled")
        self.log = txt

        f = ttk.Frame(self, padding=(10, 0, 10, 10))
        f.pack(fill="x")
        self.entries: dict[int, tk.StringVar] = {}
        for i, n in enumerate(sorted(plan.before)):
            ttk.Label(f, text=f"Zur Bestätigung die Datenträgernummer eintippen ({plan.before[n].model}, "
                              f"{fmt_size(plan.before[n].size)}):").grid(row=i, column=0, sticky="w")
            v = tk.StringVar()
            v.trace_add("write", lambda *a: self._check())
            ttk.Entry(f, textvariable=v, width=6).grid(row=i, column=1, padx=6, pady=2, sticky="w")
            self.entries[n] = v
        bf = ttk.Frame(self, padding=(10, 0, 10, 10))
        bf.pack(fill="x")
        self.btn_close = ttk.Button(bf, text="Abbrechen", command=self.destroy)
        self.btn_close.pack(side="right")
        self.btn_run = ttk.Button(bf, text="Jetzt ausführen", command=self.run)
        self.btn_run.pack(side="right", padx=6)
        self.btn_run.state(["disabled"])
        self.q: queue.Queue = queue.Queue()

    def _check(self):
        ok = all(v.get().strip() == str(n) for n, v in self.entries.items())
        self.btn_run.state(["!disabled"] if ok else ["disabled"])

    def _write(self, s: str):
        self.log.config(state="normal")
        self.log.insert("end", s + "\n")
        self.log.see("end")
        self.log.config(state="disabled")

    def run(self):
        self.btn_run.state(["disabled"])
        self.btn_close.state(["disabled"])
        self.protocol("WM_DELETE_WINDOW", lambda: None)
        self._write("\n=== AUSFÜHRUNG ===")
        confirms = {n: v.get().strip() for n, v in self.entries.items()}
        uids = {n: ops.disk_uid(d) for n, d in self.plan.before.items()}

        def work():
            try:
                r = ops.execute(self.plan.ops, confirms, expect_uids=uids, settings=self.app.settings,
                                log=lambda s: self.q.put(("log", s)))
                self.q.put(("done", r))
            except (ops.OpError, ps.PSError) as e:
                self.q.put(("error", str(e)))

        threading.Thread(target=work, daemon=True).start()
        self.after(100, self._poll)

    def _poll(self):
        try:
            while True:
                kind, val = self.q.get_nowait()
                if kind == "log":
                    self._write(val)
                elif kind == "done":
                    self.done = True
                    self._write("Fertig – alle Schritte ausgeführt." if val["ok"] else
                                f"ABGEBROCHEN bei Schritt {val['failed']['step']}: {val['failed']['error']}\n"
                                f"Nicht ausgeführt: {len(val.get('not_run', []))} Schritt(e).")
                    self._finish()
                    return
                elif kind == "error":
                    self._write("FEHLER: " + val + "\nEs wurde nichts geändert.")
                    self._finish()
                    return
        except queue.Empty:
            pass
        self.after(100, self._poll)

    def _finish(self):
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self.btn_close.config(text="Schließen")
        self.btn_close.state(["!disabled"])


def main():
    App().mainloop()
