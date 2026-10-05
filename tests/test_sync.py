import importlib.util
import json
import shutil
import sqlite3
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

import sync


def backup(path, notes=None, created="2026-01-02T12:00:00Z", tags=None, invalid_database=False):
    if notes is None:
        notes = [(1, "a", "Una nota", "Contenido de ejemplo")]
    if tags is None:
        tags = [(1, 1, "Vida cristiana"), (2, 1, "Ánimo"), (3, 2, "Playlist ignorada")]
    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "db"
        connection = sqlite3.connect(db)
        connection.executescript("""
            CREATE TABLE Note (NoteId INTEGER PRIMARY KEY, Guid TEXT, Title TEXT, Content TEXT);
            CREATE TABLE Tag (TagId INTEGER PRIMARY KEY, Type INTEGER, Name TEXT);
            CREATE TABLE TagMap (NoteId INTEGER, TagId INTEGER, Position INTEGER);
            CREATE TABLE UserMark (UserMarkId INTEGER PRIMARY KEY, ColorIndex INTEGER);
            CREATE TABLE PlaylistItem (PlaylistItemId INTEGER PRIMARY KEY, Label TEXT);
        """)
        connection.executemany("INSERT INTO Note VALUES (?,?,?,?)", notes)
        connection.executemany("INSERT INTO Tag VALUES (?,?,?)", tags)
        if notes:
            connection.executemany("INSERT INTO TagMap VALUES (?,?,?)",
                                   [(notes[0][0], row[0], index) for index, row in enumerate(tags)])
        connection.execute("INSERT INTO UserMark VALUES (1,3)")
        connection.execute("INSERT INTO PlaylistItem VALUES (1,'Contenido multimedia ignorado')")
        connection.commit()
        connection.close()
        manifest = {"creationDate": created, "userDataBackup": {
            "databaseName":"userData.db", "lastModifiedDate":created}}
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("manifest.json", json.dumps(manifest))
            archive.writestr("userData.db", b"invalid sqlite" if invalid_database else db.read_bytes())
            archive.writestr("image.jpg", b"synthetic media, never exported")


class SyncTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "backups"
        self.output = self.root / "notes"
        self.state = self.root / "state"
        self.source.mkdir()
        self.output.mkdir()
        self.instance = None

    def tearDown(self):
        if self.instance:
            self.instance.close()
        self.temporary.cleanup()

    def start(self, settle=0):
        if self.instance:
            self.instance.close()
        self.instance = sync.Synchronizer(self.source, self.output, self.state, settle)
        return self.instance

    def test_only_notes_and_hashtags_are_exported_and_destination_is_replaced(self):
        (self.output / "manual.md").write_text("nota manual")
        (self.output / ".hidden").write_text("archivo oculto")
        (self.output / "subcarpeta").mkdir()
        (self.output / "subcarpeta/otra.txt").write_text("eliminar")
        backup(self.source / "backup.jwlibrary")
        original = (self.source / "backup.jwlibrary").read_bytes()
        self.assertTrue(self.start().cycle())
        self.assertEqual([path.name for path in self.output.iterdir()], ["Una nota.md"])
        self.assertEqual((self.output / "Una nota.md").read_text(),
                         "# Una nota\n\n#Vida_cristiana #Ánimo\n\nContenido de ejemplo\n")
        self.assertEqual((self.source / "backup.jwlibrary").read_bytes(), original)
        self.assertFalse((self.state / "rollback").exists())
        self.assertFalse((self.state / "pending.json").exists())

    def test_manual_changes_are_removed_without_a_new_backup(self):
        backup(self.source / "backup.jwlibrary")
        instance = self.start()
        self.assertTrue(instance.cycle())
        self.assertFalse(instance.cycle())
        (self.output / "manual.md").write_text("added")
        (self.output / "Una nota.md").write_text("changed")
        self.assertTrue(instance.cycle())
        self.assertFalse((self.output / "manual.md").exists())
        self.assertIn("Contenido de ejemplo", (self.output / "Una nota.md").read_text())

    def test_restart_replaces_the_destination_even_when_backup_is_unchanged(self):
        backup(self.source / "backup.jwlibrary")
        self.assertTrue(self.start().cycle())
        self.assertTrue(self.start().cycle())

    def test_latest_is_chosen_by_internal_date_and_deleted_notes_disappear(self):
        backup(self.source / "z-old.jwlibrary", notes=[(1,"a","Antigua","old")], created="2025-12-31T12:00:00Z")
        self.assertTrue(self.start().cycle())
        backup(self.source / "a-new.jwlibrary", notes=[(2,"b","Nueva","new")], created="2026-01-02T12:00:00Z")
        self.assertTrue(self.instance.cycle())
        self.assertEqual([path.name for path in self.output.iterdir()], ["Nueva.md"])
        (self.source / "a-new.jwlibrary").unlink()
        self.assertFalse(self.instance.cycle())
        self.assertTrue((self.output / "Nueva.md").exists())

    def test_a_damaged_new_backup_does_not_change_destination(self):
        backup(self.source / "good.jwlibrary")
        self.assertTrue(self.start().cycle())
        before = sync.tree_digest(self.output)
        backup(self.source / "new-invalid.jwlibrary", created="2026-01-03T12:00:00Z", invalid_database=True)
        with self.assertRaises(sqlite3.DatabaseError):
            self.instance.cycle()
        self.assertEqual(sync.tree_digest(self.output), before)

    def test_empty_valid_backup_removes_all_destination_contents(self):
        backup(self.source / "empty.jwlibrary", notes=[])
        (self.output / "manual.md").write_text("delete")
        self.assertTrue(self.start().cycle())
        self.assertEqual(list(self.output.iterdir()), [])

    def test_names_are_safe_unique_and_content_is_preserved(self):
        text = "España 🗺️\r\nTexto\x00 con 'comillas'"
        notes = [(1,"a","../Título: prueba",text), (2,"b","../Título: prueba","second"),
                 (3,"c",None,None), (4,"d","ñ"*200,"long title")]
        backup(self.source / "backup.jwlibrary", notes=notes)
        self.assertTrue(self.start().cycle())
        paths = list(self.output.iterdir())
        self.assertEqual(len(paths), 4)
        self.assertEqual(len({path.name.casefold() for path in paths}), 4)
        self.assertTrue(all(len(path.name.encode()) <= 255 for path in paths))
        matching = [path for path in paths if text.encode() in path.read_bytes()]
        self.assertEqual(len(matching), 1)
        self.assertTrue(matching[0].read_bytes().startswith("# ../Título: prueba\n\n".encode()))
        self.assertTrue((self.output / "Sin título.md").exists())

    def test_uploaded_backup_must_stop_changing(self):
        path = self.source / "backup.jwlibrary"
        backup(path)
        instance = self.start(settle=10)
        self.assertFalse(instance.cycle(now=0))
        self.assertFalse(instance.cycle(now=9))
        backup(path, notes=[(1,"a","Actualizada","final")])
        self.assertFalse(instance.cycle(now=10))
        self.assertTrue(instance.cycle(now=20))
        self.assertTrue((self.output / "Actualizada.md").exists())

    def test_failed_publish_restores_previous_files(self):
        (self.output / "manual.md").write_text("previous")
        backup(self.source / "backup.jwlibrary")
        instance = self.start()
        actual_copytree = shutil.copytree

        def copytree(source, target, *args, **kwargs):
            if Path(source).name == "notes" and Path(target) == self.output:
                (self.output / "partial.md").write_text("partial")
                raise OSError("simulated disk failure")
            return actual_copytree(source, target, *args, **kwargs)

        with patch("sync.shutil.copytree", side_effect=copytree):
            with self.assertRaises(OSError):
                instance.cycle()
        self.assertEqual([path.name for path in self.output.iterdir()], ["manual.md"])
        self.assertEqual((self.output / "manual.md").read_text(), "previous")

    def test_interrupted_publish_is_recovered_on_startup(self):
        (self.output / "manual.md").write_text("previous")
        instance = self.start()
        shutil.copytree(self.output, self.state / "rollback")
        sync.write_json(self.state / "pending.json", {"output":str(self.output.resolve())})
        sync.clear_directory(self.output)
        (self.output / "partial.md").write_text("partial")
        self.start()
        self.assertEqual([path.name for path in self.output.iterdir()], ["manual.md"])
        self.assertEqual((self.output / "manual.md").read_text(), "previous")

    def test_a_symlink_in_destination_does_not_delete_its_target(self):
        external = self.root / "external"
        external.mkdir()
        (external / "keep.txt").write_text("keep")
        (self.output / "link").symlink_to(external, target_is_directory=True)
        backup(self.source / "backup.jwlibrary")
        self.assertTrue(self.start().cycle())
        self.assertEqual((external / "keep.txt").read_text(), "keep")
        self.assertFalse((self.output / "link").exists())

    def test_same_or_nested_paths_are_rejected_before_removal(self):
        with self.assertRaises(ValueError):
            sync.Synchronizer(self.source, self.source, self.state)
        with self.assertRaises(ValueError):
            sync.Synchronizer(self.source, self.source / "notes", self.state)

    def test_second_process_cannot_use_the_same_state_directory(self):
        self.start()
        with self.assertRaises(ValueError):
            sync.Synchronizer(self.source, self.output, self.state)


if __name__ == "__main__":
    unittest.main()
