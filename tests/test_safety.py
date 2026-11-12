"""Unit-Tests (ohne Adminrechte, ohne echte Laufwerke): Sperren, Bestätigung, Simulation.

    python -m unittest tests.test_safety -v
"""
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from partitioneditor import model, ops  # noqa: E402
from partitioneditor.settings import Settings  # noqa: E402

MiB = model.MiB
GiB = 1024 * MiB


def raw():
    return {
        "disks": [
            {"Number": 0, "Model": "NVMe SSD", "BusType": "NVMe", "Size": 500 * GiB, "IsSystem": True,
             "IsBoot": True, "PartitionStyle": "GPT", "UniqueId": "A"},
            {"Number": 1, "Model": "Data HDD", "BusType": "SATA", "Size": 1000 * GiB, "PartitionStyle": "GPT", "UniqueId": "B"},
            {"Number": 2, "Model": "USB Stick", "BusType": "USB", "Size": 8 * GiB, "PartitionStyle": "MBR", "UniqueId": "C"},
            {"Number": 3, "Model": "Empty USB", "BusType": "USB", "Size": 4 * GiB, "PartitionStyle": "RAW", "UniqueId": "D"},
            {"Number": 4, "Model": "Pagefile USB", "BusType": "USB", "Size": 4 * GiB, "PartitionStyle": "MBR", "UniqueId": "E"},
        ],
        "partitions": [
            {"DiskNumber": 0, "PartitionNumber": 1, "Offset": MiB, "Size": 100 * MiB, "Type": "System", "IsSystem": True},
            {"DiskNumber": 0, "PartitionNumber": 2, "Offset": 101 * MiB, "Size": 400 * GiB, "Type": "Basic",
             "DriveLetter": "C", "IsBoot": True},
            {"DiskNumber": 1, "PartitionNumber": 1, "Offset": MiB, "Size": 900 * GiB, "Type": "Basic", "DriveLetter": "D",
             "AccessPaths": ["D:\\", "\\\\?\\Volume{d}\\"]},
            {"DiskNumber": 2, "PartitionNumber": 1, "Offset": MiB, "Size": 2 * GiB, "Type": "IFS", "DriveLetter": "E",
             "AccessPaths": ["E:\\", "\\\\?\\Volume{e}\\"]},
            {"DiskNumber": 2, "PartitionNumber": 2, "Offset": MiB + 2 * GiB, "Size": 1 * GiB, "Type": "FAT32", "DriveLetter": "F",
             "AccessPaths": ["F:\\", "\\\\?\\Volume{f}\\"]},
            {"DiskNumber": 4, "PartitionNumber": 1, "Offset": MiB, "Size": 2 * GiB, "Type": "IFS", "DriveLetter": "P"},
        ],
        "volumes": [
            {"DriveLetter": "C", "FileSystem": "NTFS", "Path": "\\\\?\\Volume{c}\\"},
            {"DriveLetter": "D", "FileSystem": "NTFS", "Label": "Daten", "Path": "\\\\?\\Volume{d}\\"},
            {"DriveLetter": "E", "FileSystem": "NTFS", "Label": "STICK", "Path": "\\\\?\\Volume{e}\\", "DriveType": "Removable"},
            {"DriveLetter": "F", "FileSystem": "FAT32", "Label": "FAT", "Path": "\\\\?\\Volume{f}\\", "DriveType": "Removable"},
            {"DriveLetter": "P", "FileSystem": "NTFS", "Path": "\\\\?\\Volume{p}\\"},
        ],
        "pagefiles": ["P:\\pagefile.sys"],
        "letters": ["C:\\", "D:\\", "E:\\", "F:\\", "P:\\", "Z:\\"],
    }


def snap():
    return model.build_snapshot(raw(), {"C"})


class Locks(unittest.TestCase):
    def test_system_disk_hard_locked_even_with_allow_internal(self):
        d = snap().disk(0)
        self.assertTrue(d.hard_locks)
        self.assertTrue(d.blocks(Settings(allow_internal=True)))

    def test_internal_soft_lock(self):
        d = snap().disk(1)
        self.assertTrue(d.blocks(Settings(allow_internal=False)))
        self.assertFalse(d.blocks(Settings(allow_internal=True)))

    def test_usb_editable(self):
        self.assertFalse(snap().disk(2).blocks(Settings()))

    def test_pagefile_locks(self):
        self.assertIn("Auslagerungsdatei", " ".join(snap().disk(4).hard_locks))

    def test_plan_blocked_on_system_disk(self):
        p = ops.make_plan(snap(), Settings(allow_internal=True), [{"op": "delete", "target": "C:"}], fetch_supported=False)
        self.assertFalse(p.ok)
        self.assertIn(0, p.blocked)

    def test_execute_refuses_without_admin_or_locks(self):
        with self.assertRaises(ops.OpError):
            ops.execute([{"op": "delete", "disk": 0, "partition": 2}], {0: "0"})


class Confirm(unittest.TestCase):
    def test_number(self):
        d = snap().disk(2)
        self.assertTrue(model.confirm_ok(d, set(), "2"))
        self.assertFalse(model.confirm_ok(d, set(), "3"))
        self.assertFalse(model.confirm_ok(d, set(), ""))
        self.assertFalse(model.confirm_ok(d, set(), None))

    def test_letter_only_for_single_partition(self):
        s = snap()
        p = ops.make_plan(s, Settings(), [{"op": "label", "target": "E:", "label": "X"}], fetch_supported=False)
        self.assertEqual(p.confirm_letters(2), {"E"})
        self.assertTrue(model.confirm_ok(s.disk(2), p.confirm_letters(2), "E:"))
        self.assertFalse(model.confirm_ok(s.disk(2), p.confirm_letters(2), "F:"))
        p = ops.make_plan(s, Settings(), [{"op": "label", "target": "E:", "label": "X"},
                                          {"op": "label", "target": "F:", "label": "Y"}], fetch_supported=False)
        self.assertEqual(p.confirm_letters(2), set())


class Simulation(unittest.TestCase):
    def plan(self, o, st=None):
        return ops.make_plan(snap(), st or Settings(), o, fetch_supported=False)

    def test_gaps(self):
        g = snap().disk(2).gaps()
        self.assertEqual(len(g), 1)
        self.assertEqual(g[0].offset, MiB + 3 * GiB)

    def test_create_max_and_letters(self):
        p = self.plan([{"op": "create", "disk": 2, "size": "max", "fs": "exFAT", "letter": "auto", "label": "NEU"}])
        self.assertTrue(p.ok, p.errors)
        new = [x for x in p.after[2].partitions if x.new][0]
        self.assertEqual(new.letter, "G")      # D,E,F belegt, Z belegt
        self.assertEqual(new.offset, MiB + 3 * GiB)

    def test_create_too_big(self):
        p = self.plan([{"op": "create", "disk": 2, "size": "5GB"}])
        self.assertFalse(p.ok)

    def test_letter_taken(self):
        p = self.plan([{"op": "letter", "target": "E:", "letter": "D"}])
        self.assertFalse(p.ok)
        p = self.plan([{"op": "letter", "target": "E:", "letter": "T"}, {"op": "letter", "target": "F:", "letter": "E"}])
        self.assertTrue(p.ok, p.errors)

    def test_delete_then_create(self):
        p = self.plan([{"op": "delete", "disk": 2, "partition": 2},
                       {"op": "create", "disk": 2, "offset": MiB + 2 * GiB, "size": "max", "fs": "NTFS"}])
        self.assertTrue(p.ok, p.errors)
        self.assertEqual(len(p.after[2].gaps()), 0)

    def test_convert_needs_empty(self):
        self.assertFalse(self.plan([{"op": "convert", "disk": 2, "style": "GPT"}]).ok)
        p = self.plan([{"op": "delete", "disk": 2, "partition": 1}, {"op": "delete", "disk": 2, "partition": 2},
                       {"op": "convert", "disk": 2, "style": "GPT"}, {"op": "create", "disk": 2, "fs": "NTFS"}])
        self.assertTrue(p.ok, p.errors)
        self.assertEqual(p.after[2].style, "GPT")

    def test_raw_needs_init(self):
        self.assertFalse(self.plan([{"op": "create", "disk": 3}]).ok)
        self.assertTrue(self.plan([{"op": "convert", "disk": 3, "style": "MBR"}, {"op": "create", "disk": 3}]).ok)

    def test_resize_rules(self):
        self.assertFalse(self.plan([{"op": "resize", "target": "F:", "size": "500MB"}]).ok)   # FAT32
        sup = {2: {MiB: (500 * MiB, 2 * GiB)}}
        p = ops.make_plan(snap(), Settings(), [{"op": "resize", "target": "E:", "size": "1GB"}], sup, fetch_supported=False)
        self.assertTrue(p.ok, p.errors)
        p = ops.make_plan(snap(), Settings(), [{"op": "resize", "target": "E:", "size": "100MB"}], sup, fetch_supported=False)
        self.assertFalse(p.ok)                                   # unter Minimum
        p = ops.make_plan(snap(), Settings(), [{"op": "resize", "target": "E:", "size": "3GB"}], sup, fetch_supported=False)
        self.assertFalse(p.ok)                                   # stößt an Partition 2
        p = ops.make_plan(snap(), Settings(), [{"op": "delete", "target": "F:"},
                                               {"op": "resize", "target": "E:", "size": "3GB"}], sup, fetch_supported=False)
        self.assertTrue(p.ok, p.errors)

    def test_active_only_mbr(self):
        self.assertTrue(self.plan([{"op": "active", "target": "E:", "value": True}]).ok)
        self.assertFalse(self.plan([{"op": "active", "target": "D:", "value": True}], Settings(allow_internal=True)).ok)

    def test_mbr_four_primaries(self):
        # 2 vorhanden + 1 primäre; die 4. legt (wie Windows) eine erweiterte an, danach weitere logische
        o = [{"op": "create", "disk": 2, "size": "100MB", "letter": ""} for _ in range(4)]
        p = self.plan(o)
        self.assertTrue(p.ok, p.errors)
        d = p.after[2]
        self.assertEqual(len(d.containers()), 1)
        self.assertEqual(len(d.top_level()), 4)
        self.assertEqual([q.type for q in d.partitions if not q.is_container][-2:], ["Logical", "Logical"])
        self.assertTrue(any(g.extended is not None for g in d.gaps()))
        # Erweiterte darf erst gelöscht werden, wenn sie leer ist
        s2 = snap()
        s2.disks[2] = p.after[2]
        for q in s2.disks[2].partitions:
            q.new = False
        ext = s2.disks[2].containers()[0]
        self.assertFalse(ops.make_plan(s2, Settings(), [{"op": "delete", "disk": 2, "offset": ext.offset}], fetch_supported=False).ok)
        lg = s2.disks[2].logicals(ext)
        dels = [{"op": "delete", "disk": 2, "offset": q.offset} for q in lg] + [{"op": "delete", "disk": 2, "offset": ext.offset}]
        self.assertTrue(ops.make_plan(s2, Settings(), dels, fetch_supported=False).ok)

    def test_fat32_limit_and_label(self):
        p = ops.make_plan(snap(), Settings(allow_internal=True), [{"op": "create", "disk": 1, "fs": "FAT32"}], fetch_supported=False)
        self.assertFalse(p.ok)
        self.assertFalse(self.plan([{"op": "create", "disk": 2, "fs": "FAT32", "label": "VIEL_ZU_LANG_X"}]).ok)

    def test_parse_size(self):
        self.assertEqual(ops.parse_size("500MB"), 500 * MiB)
        self.assertEqual(ops.parse_size("1,5 GB"), int(1.5 * GiB))
        self.assertIsNone(ops.parse_size("max"))
        with self.assertRaises(ops.OpError):
            ops.parse_size("viel")


if __name__ == "__main__":
    unittest.main()
