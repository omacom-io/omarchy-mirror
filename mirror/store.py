"""Local and R2 stores share immutable writes and conditional activation."""

from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import shutil
import tempfile
from urllib.parse import urlsplit

from .common import Conflict, Error, Missing, digest, encode, file_digest, safe_path


class Store:
    def json(self, key):
        try:
            return json.loads(self.read(key))
        except (ValueError, UnicodeError) as exc:
            raise Error(f"Invalid stored JSON: {key}", hint="Restore the object from a known-good backup.") from exc

    def content_json(self, category, value):
        body = encode(value)
        identifier = digest(body)
        self.put(f"{category}/{identifier}.json", body, "application/json")
        self.cache_json(identifier, body)
        return identifier

    def cache_json(self, identifier, body):
        cache = getattr(self, "cache", None)
        if not cache:
            return
        cached = cache / f"{identifier}.json"
        if cached.is_file() and digest(cached.read_bytes()) == identifier:
            return
        cached.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=cached.parent, prefix=".cache-")
        try:
            with os.fdopen(fd, "wb") as output:
                output.write(body)
            os.replace(temporary, cached)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def verified_json(self, category, identifier):
        cache = getattr(self, "cache", None)
        cached = cache / f"{identifier}.json" if cache else None
        body = cached.read_bytes() if cached and cached.is_file() else None
        if body is None or digest(body) != identifier:
            body = self.read(f"{category}/{identifier}.json")
        if digest(body) != identifier:
            raise Error(f"Corrupt {category} object: {identifier}")
        self.cache_json(identifier, body)
        return json.loads(body)


class LocalStore(Store):
    def __init__(self, root):
        self.root = Path(root).absolute()

    def path(self, key):
        result = self.root / safe_path(key)
        if not result.resolve().is_relative_to(self.root.resolve()):
            raise Error("Object path escapes the configured store.")
        return result

    @contextmanager
    def lock(self):
        self.root.mkdir(parents=True, exist_ok=True)
        with (self.root / ".write.lock").open("a") as stream:
            fcntl.flock(stream, fcntl.LOCK_EX)
            yield

    def read(self, key):
        try:
            return self.path(key).read_bytes()
        except FileNotFoundError as exc:
            raise Missing(key) from exc

    def versioned(self, key):
        try:
            body = self.read(key)
            return body, digest(body)
        except Missing:
            return None, None

    def head(self, key):
        path = self.path(key)
        if not path.is_file():
            return None
        stat = path.stat()
        stamp = [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]
        cache = self.root / ".checksums" / digest(key.encode())
        if cache.exists():
            record = json.loads(cache.read_bytes())
            if record["stamp"] == stamp:
                return {"size": stat.st_size, "sha256": record["sha256"]}
        checksum = file_digest(path)
        return {"size": stat.st_size, "sha256": checksum}

    def _write(self, key, writer):
        path = self.path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=".upload-", dir=path.parent)
        try:
            with os.fdopen(fd, "wb") as output:
                writer(output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(tmp, path)
            directory = os.open(path.parent, os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            Path(tmp).unlink(missing_ok=True)

    def put(self, key, body, content_type="application/octet-stream"):
        with self.lock():
            current, _ = self.versioned(key)
            if current is not None:
                if current != body:
                    raise Conflict(f"Immutable object collision: {key}")
                return False
            self._write(key, lambda output: output.write(body))
        return True

    def put_file(self, key, path, checksum):
        with self.lock():
            existing = self.head(key)
            if existing:
                if existing["sha256"] != checksum:
                    raise Conflict(f"Immutable package collision: {key}")
                return False
            with open(path, "rb") as source:
                self._write(key, lambda output: shutil.copyfileobj(source, output))
            stat = self.path(key).stat()
            cache = encode({"sha256": checksum, "stamp": [stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns, stat.st_ino]})
            self._write(".checksums/" + digest(key.encode()), lambda output: output.write(cache))
        return True

    def cas(self, key, body, expected):
        with self.lock():
            _, current = self.versioned(key)
            if current != expected:
                raise Conflict()
            self._write(key, lambda output: output.write(body))

    def keys(self, prefix):
        path = self.path(prefix)
        if path.is_file():
            yield prefix
        elif path.exists():
            for item in sorted(path.rglob("*")):
                if item.is_file() and not item.name.startswith("."):
                    yield item.relative_to(self.root).as_posix()


class S3Store(Store):
    def __init__(self, url, endpoint=None, region="auto", client=None, cache=None):
        parsed = urlsplit(url)
        if parsed.scheme != "s3" or not parsed.netloc or parsed.query or parsed.fragment:
            raise Error("Store must be s3://bucket/optional-prefix.")
        self.bucket, self.prefix = parsed.netloc, parsed.path.strip("/")
        self.cache = Path(cache) if cache else None
        if self.prefix:
            safe_path(self.prefix)
        if client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:
                raise Error("R2 support is not installed.", hint="Install with: pip install '.[r2]'") from exc
            client = boto3.client("s3", endpoint_url=endpoint, region_name=region, config=Config(
                retries={"mode": "standard", "max_attempts": 4},
                max_pool_connections=32,
                connect_timeout=15, read_timeout=120,
                request_checksum_calculation="when_required", response_checksum_validation="when_required"))
        self.client = client

    def key(self, key):
        return "/".join(filter(None, (self.prefix, safe_path(key))))

    @staticmethod
    def status(exc):
        return getattr(exc, "response", {}).get("ResponseMetadata", {}).get("HTTPStatusCode")

    def versioned(self, key):
        try:
            result = self.client.get_object(Bucket=self.bucket, Key=self.key(key))
            try:
                return result["Body"].read(), result["ETag"]
            finally:
                result["Body"].close()
        except Exception as exc:
            if self.status(exc) == 404:
                return None, None
            raise

    def read(self, key):
        body, _ = self.versioned(key)
        if body is None:
            raise Missing(key)
        return body

    def head(self, key):
        try:
            result = self.client.head_object(Bucket=self.bucket, Key=self.key(key))
            return {"size": result["ContentLength"], "sha256": result.get("Metadata", {}).get("sha256")}
        except Exception as exc:
            if self.status(exc) == 404:
                return None
            raise

    def put(self, key, body, content_type="application/octet-stream"):
        checksum = digest(body)
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key(key), Body=body,
                                   ContentType=content_type, Metadata={"sha256": checksum}, IfNoneMatch="*")
            return True
        except Exception as exc:
            if self.status(exc) not in (409, 412):
                raise
            if self.read(key) != body:
                raise Conflict(f"Immutable object collision: {key}") from exc
            return False

    def put_file(self, key, path, checksum):
        existing = self.head(key)
        size = Path(path).stat().st_size
        if existing:
            if existing != {"size": size, "sha256": checksum}:
                raise Conflict(f"Immutable package collision: {key}")
            return False
        # Managed transfers stream and use multipart for large packages. Every writer
        # uses the archive hash in the key; concurrent identical uploads are harmless.
        self.client.upload_file(str(path), self.bucket, self.key(key), ExtraArgs={
            "ContentType": "application/octet-stream", "Metadata": {"sha256": checksum}})
        if self.head(key) != {"size": size, "sha256": checksum}:
            raise Error(f"Uploaded package verification failed: {key}")
        return True

    def cas(self, key, body, expected):
        condition = {"IfMatch": expected} if expected else {"IfNoneMatch": "*"}
        try:
            self.client.put_object(Bucket=self.bucket, Key=self.key(key), Body=body,
                                   ContentType="application/json", CacheControl="no-store", **condition)
        except Exception as exc:
            if self.status(exc) in (409, 412):
                raise Conflict() from exc
            raise

    def keys(self, prefix):
        full_prefix = self.key(prefix)
        for page in self.client.get_paginator("list_objects_v2").paginate(Bucket=self.bucket, Prefix=full_prefix):
            for item in page.get("Contents", []):
                yield item["Key"][len(self.prefix) + 1:] if self.prefix else item["Key"]
