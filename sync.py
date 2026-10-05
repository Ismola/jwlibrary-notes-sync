#!/usr/bin/env python3
"""Mantiene una carpeta de notas Markdown idéntica a la última copia JW Library."""
import argparse
import fcntl
import hashlib
import json
import logging
import os
import re
import shutil
import signal
import sqlite3
import tempfile
import threading
import time
import zipfile
import unicodedata
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

LOG = logging.getLogger("jwlibrary-notes-sync")


def signature(path):
    stat = path.stat()
    return stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino


def digest(path):
    result = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(chunk)
    return result.hexdigest()


def write_json(path, data):
    temporary = path.with_suffix(".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def parse_date(value):
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("La fecha del respaldo debe incluir zona horaria")
    return result.astimezone(timezone.utc)


@dataclass(frozen=True)
class Backup:
    path: Path
    created: datetime
    modified: datetime
    stat: tuple


class Readiness:
    def __init__(self, settle_seconds):
        self.settle_seconds = settle_seconds
        self.observations = {}

    def ready(self, path, now):
        current = signature(path)
        previous = self.observations.get(path)
        if previous is None or previous[0] != current:
            self.observations[path] = current, now
            return self.settle_seconds == 0
        return now - previous[1] >= self.settle_seconds


def latest_backup(directory, readiness, now):
    candidates = []
    paths = [path for path in directory.iterdir()
             if path.is_file() and not path.is_symlink() and path.suffix.lower() == ".jwlibrary"]
    readiness.observations = {key: value for key, value in readiness.observations.items() if key in paths}
    for path in paths:
        try:
            if not readiness.ready(path, now):
                continue
            before = signature(path)
            with zipfile.ZipFile(path) as archive:
                manifest = json.loads(archive.read("manifest.json"))
            created = parse_date(manifest["creationDate"])
            modified = parse_date(manifest.get("userDataBackup", {}).get("lastModifiedDate", manifest["creationDate"]))
            if signature(path) != before:
                continue
            candidates.append(Backup(path, created, modified, before))
        except (OSError, ValueError, KeyError, TypeError, zipfile.BadZipFile) as error:
            LOG.warning("Copia todavía no válida %s: %s", path.name, error)
    return max(candidates, key=lambda item: (item.created, item.modified, item.path.name), default=None)


def note_title(value):
    return " ".join((value or "").split()) or "Sin título"


def filename_title(title):
    value = unicodedata.normalize("NFKD", title)
    value = "".join(char for char in value if not unicodedata.combining(char))
    # ASCII, sin puntuación inicial, extensiones aparentes ni caracteres reservados.
    value = re.sub(r"[^A-Za-z0-9 _-]", " ", value)
    value = re.sub(r"\s+", " ", value).strip(" _-") or "Sin titulo"
    if value.upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        value = "Nota " + value
    return value[:180].rstrip(" _-") or "Sin titulo"


def empty_names():
    return {"version": 1, "families": {}}


def validate_names(registry):
    if registry.get("version") != 1 or not isinstance(registry.get("families"), dict):
        raise ValueError("Registro de nombres no válido")
    for family, data in registry["families"].items():
        if not isinstance(data.get("numbered"), bool) or not isinstance(data.get("assignments"), dict):
            raise ValueError("Familia de nombres no válida")
        numbers = set()
        for identity, assignment in data["assignments"].items():
            number, base = assignment["number"], assignment["base"]
            if (not identity or type(number) is not int or number < 1 or number in numbers
                    or filename_title(base) != base or base.casefold() != family):
                raise ValueError("Asignación de nombre no válida")
            numbers.add(number)
        if len(numbers) > 1 and not data["numbered"]:
            raise ValueError("Una familia con varios identificadores debe estar numerada")


def assign_names(notes, registry):
    validate_names(registry)
    groups = {}
    identities = set()
    for note in notes:
        guid = note["Guid"]
        if not isinstance(guid, str) or not guid.strip():
            raise ValueError("Cada nota debe tener un GUID para mantener su nombre estable")
        identity = guid.strip().casefold()
        if identity in identities:
            raise ValueError("La copia contiene GUID de notas duplicados")
        identities.add(identity)
        base = filename_title(note_title(note["Title"]))
        groups.setdefault(base.casefold(), []).append((identity, base))
    names = {}
    for family, items in groups.items():
        data = registry["families"].setdefault(family, {"numbered": False, "assignments": {}})
        assignments = data["assignments"]
        # GUID fija el orden inicial; las altas posteriores reciben el siguiente número.
        next_number = max((entry["number"] for entry in assignments.values()), default=0) + 1
        for identity, base in sorted(items):
            if identity not in assignments:
                assignments[identity] = {"number": next_number, "base": base}
                next_number += 1
        data["numbered"] = data["numbered"] or len(assignments) > 1
        for identity, _ in items:
            assignment = assignments[identity]
            suffix = f" ({assignment['number']})" if data["numbered"] else ""
            names[identity] = assignment["base"] + suffix + ".md"
    return names


def hashtag(value):
    value = re.sub(r"\s+", "_", value.strip().lstrip("#"))
    value = re.sub(r"[^\w/-]", "_", value, flags=re.UNICODE)
    value = re.sub(r"_+", "_", value).strip("_/")
    return "#" + value if value else None


def extract_notes(snapshot, staging, temporary, registry):
    with zipfile.ZipFile(snapshot) as archive:
        bad_entry = archive.testzip()
        if bad_entry:
            raise ValueError(f"CRC incorrecto: {bad_entry}")
        manifest = json.loads(archive.read("manifest.json"))
        name = manifest["userDataBackup"]["databaseName"]
        if not isinstance(name, str) or PurePosixPath(name).name != name or "\\" in name:
            raise ValueError("Nombre de base de datos no válido")
        database = temporary / "userData.db"
        database.write_bytes(archive.read(name))
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        if [row[0] for row in connection.execute("PRAGMA integrity_check")] != ["ok"]:
            raise ValueError("La base de datos no supera la comprobación de integridad")
        tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "Note" not in tables:
            raise ValueError("No existe la tabla Note")
        notes = [dict(row) for row in connection.execute("SELECT * FROM Note ORDER BY Guid, NoteId")]
        names = assign_names(notes, registry)
        tags = {}
        if {"Tag", "TagMap"}.issubset(tables):
            for row in connection.execute("SELECT m.NoteId, t.Name FROM TagMap m JOIN Tag t ON t.TagId=m.TagId "
                                          "WHERE m.NoteId IS NOT NULL AND t.Type=1 ORDER BY m.NoteId,m.Position,t.TagId"):
                tag = hashtag(row["Name"])
                if tag and tag not in tags.setdefault(row["NoteId"], []):
                    tags[row["NoteId"]].append(tag)
        staging.mkdir()
        for note in notes:
            title = note_title(note["Title"])
            name = names[note["Guid"].strip().casefold()]
            text = f"# {title}\n\n"
            if tags.get(note["NoteId"]):
                text += " ".join(tags[note["NoteId"]]) + "\n\n"
            text += note["Content"] or ""
            if not text.endswith("\n"):
                text += "\n"
            with (staging / name).open("w", encoding="utf-8", newline="") as stream:
                stream.write(text)
        return len(notes)
    finally:
        connection.close()


def tree_digest(directory):
    result = {}
    for path in sorted(directory.rglob("*")):
        relative = str(path.relative_to(directory))
        if path.is_symlink():
            result[relative] = "link:" + os.readlink(path)
        elif path.is_file():
            result[relative] = "file:" + digest(path)
        elif path.is_dir():
            result[relative] = "directory"
        else:
            result[relative] = "special"
    return result


def clear_directory(directory):
    # Nunca sigue enlaces simbólicos al eliminar el contenido del destino.
    for path in directory.iterdir():
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()


def replace_output(staging, output, state):
    rollback = state / "rollback"
    journal = state / "pending.json"
    if rollback.exists():
        shutil.rmtree(rollback)
    # Copia previa antes de tocar el destino; si falla, el destino sigue intacto.
    shutil.copytree(output, rollback, symlinks=True)
    write_json(journal, {"output": str(output.resolve())})
    try:
        clear_directory(output)
        shutil.copytree(staging, output, dirs_exist_ok=True)
    except BaseException:
        clear_directory(output)
        shutil.copytree(rollback, output, dirs_exist_ok=True, symlinks=True)
        journal.unlink()
        shutil.rmtree(rollback)
        raise
    journal.unlink()
    shutil.rmtree(rollback)


def recover_output(output, state):
    journal = state / "pending.json"
    if not journal.exists():
        return
    pending = json.loads(journal.read_text(encoding="utf-8"))
    if pending["output"] != str(output.resolve()) or not (state / "rollback").is_dir():
        raise ValueError("La recuperación pendiente no corresponde al destino configurado")
    clear_directory(output)
    shutil.copytree(state / "rollback", output, dirs_exist_ok=True, symlinks=True)
    journal.unlink()
    shutil.rmtree(state / "rollback")
    LOG.warning("Destino anterior recuperado después de una publicación interrumpida")


class Synchronizer:
    def __init__(self, source, output, state, settle_seconds=10):
        raw_paths = (Path(source), Path(output), Path(state))
        if any(path.is_symlink() for path in raw_paths):
            raise ValueError("Las carpetas configuradas no deben ser enlaces simbólicos")
        self.source, self.output, self.state = (path.resolve() for path in raw_paths)
        directories = (self.source, self.output, self.state)
        for path in directories:
            if path == Path("/"):
                raise ValueError("No se puede usar la raíz como carpeta configurada")
            for other in directories:
                if path != other and (path.is_relative_to(other) or other.is_relative_to(path)):
                    raise ValueError("Las carpetas de entrada, salida y estado deben estar separadas")
        if len(set(directories)) != 3:
            raise ValueError("Las carpetas de entrada, salida y estado deben ser diferentes")
        if not self.source.is_dir():
            raise ValueError("No existe la carpeta de copias")
        self.output.mkdir(parents=True, exist_ok=True)
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock = (self.state / "sync.lock").open("a")
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.lock.close()
            raise ValueError("Ya hay otro proceso utilizando la misma carpeta de estado") from None
        recover_output(self.output, self.state)
        self.readiness = Readiness(settle_seconds)
        self.first_run = True
        path = self.state / "last-sync.json"
        self.last = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
        path = self.state / "filenames.json"
        self.names = json.loads(path.read_text(encoding="utf-8")) if path.exists() else empty_names()
        validate_names(self.names)

    def close(self):
        self.lock.close()

    def cycle(self, now=None):
        candidate = latest_backup(self.source, self.readiness, time.monotonic() if now is None else now)
        if candidate is None:
            return False
        if self.last and candidate.created < parse_date(self.last["created"]):
            LOG.warning("Se ignora una copia anterior a la última exportada: %s", candidate.path.name)
            return False
        with tempfile.TemporaryDirectory(prefix="extract-", dir=self.state) as folder:
            temporary = Path(folder)
            snapshot = temporary / "backup.jwlibrary"
            shutil.copyfile(candidate.path, snapshot)
            if signature(candidate.path) != candidate.stat:
                LOG.info("La copia sigue cambiando; se reintentará")
                return False
            backup_hash = digest(snapshot)
            if (not self.first_run and self.last and self.last["backup_sha256"] == backup_hash
                    and tree_digest(self.output) == self.last["files"]):
                return False
            staging = temporary / "notes"
            registry = deepcopy(self.names)
            count = extract_notes(snapshot, staging, temporary, registry)
            files = tree_digest(staging)
            # Reserva los números antes de publicar, también frente a fallos o reinicios.
            write_json(self.state / "filenames.json", registry)
            self.names = registry
            replace_output(staging, self.output, self.state)
            self.last = {"created": candidate.created.isoformat(), "backup_sha256": backup_hash,
                         "exported_at": datetime.now(timezone.utc).isoformat(), "notes": count,
                         "output": str(self.output), "files": files}
            write_json(self.state / "last-sync.json", self.last)
            self.first_run = False
            LOG.info("Destino reemplazado: %s notas desde %s", count, candidate.path.name)
            return True


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--once", action="store_true", help="Una ejecución inmediata, sin esperar estabilización")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    interval = float(os.getenv("POLL_INTERVAL_SECONDS", "30"))
    settle = float(os.getenv("SETTLE_SECONDS", "10"))
    if interval <= 0 or settle < 0:
        parser.error("El intervalo debe ser positivo y la estabilización no negativa")
    sync = Synchronizer(os.getenv("BACKUP_DIR", "/backups"), os.getenv("NOTES_DIR", "/notes"),
                        os.getenv("STATE_DIR", "/state"), 0 if args.once else settle)
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    try:
        if args.once:
            if not sync.cycle():
                raise SystemExit("No hay una copia válida y elegible para exportar")
            return
        LOG.info("Servicio iniciado: entrada=%s destino=%s intervalo=%ss", sync.source, sync.output, interval)
        while not stopped.is_set():
            try:
                sync.cycle()
            except Exception:
                LOG.exception("No se pudo completar la sincronización; se reintentará")
            stopped.wait(interval)
    finally:
        sync.close()


if __name__ == "__main__":
    main()
