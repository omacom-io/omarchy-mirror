"""Lossless pacman field records and deterministic repository assembly."""

import base64
import binascii
import gzip
import io
import re
import tarfile

from .common import Error, digest, safe_path, sha


MAX_RECORD = 32 * 1024 * 1024
MAX_DATABASE = 1024 * 1024 * 1024
PACKAGE = re.compile(r"[A-Za-z0-9_+.~:@-]+\.pkg\.tar\.[A-Za-z0-9]+")
PACKAGE_NAME = re.compile(r"[A-Za-z0-9@_+][A-Za-z0-9@_+.:-]*")


def fields(body):
    try:
        text = body.decode("utf-8")
    except UnicodeError as exc:
        raise Error("Repository records must be UTF-8.") from exc
    if "\x00" in text or "\r" in text:
        raise Error("Malformed repository record: NUL or carriage return.")
    result, key = {}, None
    for line in text.split("\n"):
        if not line:
            key = None
        elif key is None:
            if not re.fullmatch(r"%[A-Z0-9_]+%", line) or line[1:-1] in result:
                raise Error(f"Invalid or duplicate repository field: {line[:100]!r}")
            key = line[1:-1]
            result[key] = []
        else:
            result[key].append(line)
    return result


def render(record):
    for key, values in record.items():
        if not re.fullmatch(r"[A-Z0-9_]+", key) or not isinstance(values, list):
            raise Error("Invalid stored repository field.")
        if any(not isinstance(v, str) or not v or any(c in v for c in "\r\n\x00") for v in values):
            raise Error(f"Invalid stored value in {key}.")
    return "".join(f"%{key}%\n" + "\n".join(record[key]) + "\n\n" for key in sorted(record)).encode()


def one(record, key):
    values = record.get(key, [])
    if len(values) != 1:
        raise Error(f"Expected one {key} in package metadata.")
    return values[0]


def unpack(body):
    result, expanded = {}, 0
    try:
        with tarfile.open(fileobj=io.BytesIO(body), mode="r|*") as archive:
            for entry in archive:
                raw = entry.name.removeprefix("./").rstrip("/")
                if entry.isdir():
                    if raw:
                        safe_path(raw)
                    continue
                path = safe_path(raw)
                parts = path.split("/")
                if len(parts) != 2 or parts[1] not in ("desc", "files", "depends"):
                    raise Error(f"Unexpected database member: {path}")
                expanded += entry.size
                if not entry.isfile() or entry.size > MAX_RECORD or expanded > MAX_DATABASE:
                    raise Error("Repository database record exceeds supported limits.")
                group = result.setdefault(parts[0], {})
                if parts[1] in group:
                    raise Error(f"Duplicate database member: {path}")
                group[parts[1]] = fields(archive.extractfile(entry).read())
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise Error("Unreadable repository database.", hint="Retry after upstream finishes syncing.") from exc
    if not result:
        raise Error("Empty upstream database; refusing implicit repository removal.")
    return result


def packages(database, file_database, arch):
    db, files_db = unpack(database), unpack(file_database)
    if set(db) != set(files_db):
        raise Error(".db and .files package sets differ.", hint="Retry after upstream finishes syncing; no ring was changed.")
    result = {}
    for entry, members in db.items():
        file_members = files_db[entry]
        if "desc" not in members or "desc" not in file_members or "files" not in file_members:
            raise Error(f"Incomplete repository records for {entry}.")
        desc = members["desc"]
        for field in ("NAME", "VERSION", "FILENAME", "ARCH", "SHA256SUM", "CSIZE"):
            if one(file_members["desc"], field) != one(desc, field):
                raise Error(f".db and .files identity fields disagree for {entry}.")
        # Preserve both records, but never accept contradictory shared metadata.
        for member in set(members) & set(file_members):
            if member == "files":
                continue
            left, right = members[member], file_members[member]
            if any(left[k] != right[k] for k in set(left) & set(right)):
                raise Error(f".db and .files disagree for {entry}.")
        pkgname, version = one(desc, "NAME"), one(desc, "VERSION")
        filename = one(desc, "FILENAME")
        if not PACKAGE_NAME.fullmatch(pkgname) or not PACKAGE.fullmatch(filename):
            raise Error(f"Invalid package identity: {entry}.")
        if "/" in version or not version or entry != f"{pkgname}-{version}":
            raise Error(f"Database entry does not match name/version: {entry}.")
        if one(desc, "ARCH") not in (arch, "any"):
            raise Error(f"Wrong package architecture for {entry}.")
        checksum = sha(one(desc, "SHA256SUM").lower())
        try:
            size = int(one(desc, "CSIZE"))
        except ValueError as exc:
            raise Error(f"Invalid package size for {entry}.") from exc
        if size <= 0 or pkgname in result:
            raise Error(f"Invalid size or duplicate package name: {entry}.")
        file_fields = file_members["files"]
        if "FILES" not in file_fields:
            raise Error(f"No file list for {entry}.")
        for path in file_fields["FILES"]:
            # These are opaque Unix filenames, never extraction destinations.
            # systemd units legitimately contain literal backslash escapes.
            if path.startswith("/") or ".." in path.split("/"):
                raise Error(f"Invalid file-list path for {entry}.")
        result[pkgname] = {
            "schema_version": 1, "entry": entry, "name": pkgname, "version": version,
            "arch": one(desc, "ARCH"), "filename": filename, "sha256": checksum, "size": size,
            "db": members, "files": file_members,
        }
    return result


def embedded_signature(metadata):
    encoded = metadata["db"]["desc"].get("PGPSIG", [])
    if not encoded:
        return None
    try:
        if len(encoded) != 1:
            raise ValueError()
        signature = base64.b64decode(encoded[0], validate=True)
        if not signature or len(signature) > 16384:
            raise ValueError()
        return signature
    except (ValueError, binascii.Error) as exc:
        raise Error(f"Invalid embedded signature: {metadata['filename']}") from exc


def attach_signature(metadata, signature):
    if not signature or len(signature) > 16384 or signature.startswith(b"-----BEGIN"):
        raise Error("A binary detached package signature is required.")
    encoded = base64.b64encode(signature).decode()
    for kind in ("db", "files"):
        metadata[kind]["desc"]["PGPSIG"] = [encoded]
    metadata["signature_sha256"] = digest(signature)
    metadata["signature_key"] = f"signatures/{digest(signature)}.sig"
    metadata["archive_key"] = f"pool/{metadata['sha256']}/{metadata['filename']}"


def assemble(records, kind):
    if kind not in ("db", "files"):
        raise Error("Unsupported database kind.")
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", filename="", mtime=0, compresslevel=6) as compressed:
        with tarfile.open(fileobj=compressed, mode="w|", format=tarfile.PAX_FORMAT) as archive:
            for metadata in sorted(records, key=lambda m: m["entry"]):
                for member, record in sorted(metadata[kind].items()):
                    body = render(record)
                    info = tarfile.TarInfo(safe_path(f"{metadata['entry']}/{member}"))
                    info.size, info.mode, info.mtime = len(body), 0o644, 0
                    archive.addfile(info, io.BytesIO(body))
    return output.getvalue()
