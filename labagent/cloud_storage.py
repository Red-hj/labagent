"""Private, versioned Supabase workspace snapshots; SQLite remains the live store."""
from datetime import datetime, timezone
import base64
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener
import uuid

from .workspace_archive import WorkspaceBusy

PART_BYTES = 4 * 1024 * 1024
MAX_BYTES = 200 * 1024 * 1024
TABLE = "/rest/v1/labagent_workspace_head"
REVISION = r"[a-f0-9]{32}"


class StorageError(ValueError):
    pass


class StorageConflict(StorageError):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class SupabaseStorage:
    def __init__(self, url, key, bucket="labagent-workspace"):
        parsed = urlparse(url)
        if not (parsed.scheme == "https" and re.fullmatch(r"[a-z0-9]+\.supabase\.co", parsed.netloc)
                and parsed.path in ("", "/") and not parsed.query and not parsed.fragment):
            raise StorageError("请填写 Supabase 项目的 HTTPS 地址。")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,62}", bucket):
            raise StorageError("Supabase 私有存储桶名称无效。")
        legacy_role = None
        try:
            section = key.split(".")[1]
            legacy_role = json.loads(base64.urlsafe_b64decode(section + "=" * (-len(section) % 4))).get("role")
        except (ValueError, IndexError, AttributeError, UnicodeError):
            pass
        if not re.fullmatch(r"sb_secret_[A-Za-z0-9_-]{20,}", key) and legacy_role != "service_role":
            raise StorageError("请配置 Supabase 服务端 Secret key。")
        self.url, self.key, self.bucket = url.rstrip("/"), key, bucket
        self.opener = build_opener(NoRedirect())

    @classmethod
    def from_environment(cls):
        return cls(os.environ.get("LABAGENT_SUPABASE_URL", ""), os.environ.get("LABAGENT_SUPABASE_KEY", ""),
                   os.environ.get("LABAGENT_SUPABASE_BUCKET", "labagent-workspace"))

    def request(self, method, path, data=None, *, binary=False, limit=PART_BYTES + 1):
        headers = {"apikey": self.key, "User-Agent": "LabAgent-server", "Prefer": "return=representation"}
        # New opaque secret keys belong only in apikey; legacy JWT keys also use Bearer.
        if not self.key.startswith("sb_secret_"):
            headers["Authorization"] = "Bearer " + self.key
        if data is not None:
            headers["Content-Type"] = "application/octet-stream" if binary else "application/json"
            if not binary:
                data = json.dumps(data).encode("utf-8")
        try:
            with self.opener.open(Request(self.url + path, data=data, headers=headers, method=method), timeout=45) as response:
                result = response.read(limit + 1)
                if len(result) > limit:
                    raise StorageError("云端返回的数据超出预期大小。")
                return result if binary else json.loads(result or b"null")
        except HTTPError as error:
            if error.code == 409:
                raise StorageConflict("云端版本已更新，请停止旧实例并重新加载最新工作区。") from None
            if error.code in (401, 403):
                raise StorageError("Supabase 服务端密钥或访问权限未通过验证。") from None
            if error.code == 404 and path.startswith(TABLE):
                raise StorageError("请先在 Supabase SQL Editor 执行 scripts/supabase_workspace.sql。") from None
            raise StorageError("云端存储请求失败（HTTP " + str(error.code) + "），稍后自动重试。") from None
        except (URLError, TimeoutError, OSError):
            raise StorageError("暂时无法连接云端存储，稍后自动重试。") from None
        except (ValueError, UnicodeError) as error:
            if isinstance(error, StorageError):
                raise
            raise StorageError("云端存储返回的数据格式无效。") from None

    def check(self):
        bucket = self.request("GET", "/storage/v1/bucket/" + self.bucket)
        if not isinstance(bucket, dict) or bucket.get("public") is not False:
            raise StorageError("工作区必须使用私有存储桶，请关闭 Public bucket。")

    def head(self):
        rows = self.request("GET", TABLE + "?id=eq.main&select=revision,manifest,previous")
        if not isinstance(rows, list) or len(rows) > 1:
            raise StorageError("云端工作区版本信息无效。")
        if not rows:
            return None
        row = rows[0]
        if not isinstance(row, dict):
            raise StorageError("云端工作区版本信息无效。")
        validate_manifest(row.get("manifest"))
        if row["revision"] != row["manifest"]["revision"] or not isinstance(row.get("previous"), list) or len(row["previous"]) > 1:
            raise StorageError("云端工作区版本信息无效。")
        for previous in row["previous"]:
            validate_manifest(previous)
        return row

    def publish(self, row, expected):
        if expected is None:
            result = self.request("POST", TABLE, {"id": "main", **row})
        else:
            result = self.request("PATCH", TABLE + "?id=eq.main&revision=eq." + expected, row)
        if not isinstance(result, list) or len(result) != 1:
            raise StorageConflict("另一个实例已保存新版本，本实例已停止同步，请重启后加载最新工作区。")

    def part(self, revision, number):
        return "v1/" + revision + "/part-" + str(number).zfill(4) + ".bin"

    def upload(self, revision, number, data):
        self.request("POST", "/storage/v1/object/" + self.bucket + "/" + self.part(revision, number), data, binary=True)

    def download(self, revision, number):
        return self.request("GET", "/storage/v1/object/" + self.bucket + "/" + self.part(revision, number), binary=True,
                            limit=PART_BYTES)

    def remove(self, manifest):
        self.request("DELETE", "/storage/v1/object/" + self.bucket,
                     {"prefixes": [self.part(manifest["revision"], i) for i in range(manifest["parts"])]})


def validate_manifest(value):
    if not isinstance(value, dict) or value.get("format") != "labagent-cloud-v1" or not re.fullmatch(REVISION, str(value.get("revision", ""))):
        raise StorageError("云端工作区说明无效。")
    size, parts = value.get("bytes"), value.get("parts")
    if type(size) is not int or not 0 < size <= MAX_BYTES or type(parts) is not int or parts != (size + PART_BYTES - 1) // PART_BYTES:
        raise StorageError("云端工作区大小无效。")
    if not re.fullmatch(r"[a-f0-9]{64}", str(value.get("sha256", ""))) or not isinstance(value.get("created_at"), str):
        raise StorageError("云端工作区校验信息无效。")


class CloudPersistence:
    """Restore before serving; snapshot after changes, with an atomic remote version check."""
    def __init__(self, workspace, remote):
        self.workspace, self.remote = workspace, remote
        self.marker = workspace.store.path.with_name("cloud-workspace.json")
        self.revision, self.last_saved = None, None
        self.error, self.conflict = None, False
        self.lock, self.sync_lock = threading.Lock(), threading.Lock()
        self.stop = threading.Event()
        self.thread = None
        self.saved_fingerprint = None
        self.fingerprint = None
        self.changed_at = 0
        self.saving = False

    def bootstrap(self):
        self.remote.check()
        head = self.remote.head()
        if head:
            if self.workspace.summary()["can_restore"]:
                self.restore(head)
            else:
                try:
                    marker = json.loads(self.marker.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    marker = {}
                if marker != self.marker_value(head["revision"]):
                    raise StorageConflict("本地与云端都有工作区且版本不一致。请先备份本地数据，再在空数据目录启动以恢复云端版本。")
            self.revision = head["revision"]
            self.last_saved = head["manifest"]["created_at"]
        self.observe()

    def marker_value(self, revision):
        return {"url": self.remote.url, "bucket": self.remote.bucket, "revision": revision}

    def write_marker(self, revision):
        temporary = self.marker.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.marker_value(revision)), encoding="utf-8")
        temporary.replace(self.marker)

    def restore(self, head):
        manifest = head["manifest"]
        with tempfile.TemporaryDirectory(dir=self.workspace.temporary) as directory:
            target = Path(directory) / "restore.zip"
            digest = hashlib.sha256()
            with target.open("wb") as output:
                for number in range(manifest["parts"]):
                    data = self.remote.download(manifest["revision"], number)
                    expected = min(PART_BYTES, manifest["bytes"] - number * PART_BYTES)
                    if len(data) != expected:
                        raise StorageError("云端备份分片不完整，尚未恢复本地数据。")
                    output.write(data)
                    digest.update(data)
            if digest.hexdigest() != manifest["sha256"]:
                raise StorageError("云端备份校验失败，尚未恢复本地数据。")
            if self.remote.head() != head:
                raise StorageConflict("下载期间云端工作区发生变化，请重启以恢复最新版本。")
            self.workspace.restore(target)
            self.write_marker(head["revision"])

    def current_fingerprint(self):
        paths = [self.workspace.store.path, self.workspace.store.path.with_name(self.workspace.store.path.name + "-wal")]
        paths.extend(self.workspace.notes.glob("*.json"))
        paths.extend(self.workspace.recordings.root.glob("*/status.json"))
        values = []
        for path in sorted(paths):
            try:
                stat = path.stat()
                values.append((str(path), stat.st_size, stat.st_mtime_ns))
            except FileNotFoundError:
                pass
        return tuple(values)

    def observe(self):
        fingerprint = self.current_fingerprint()
        with self.lock:
            if fingerprint != self.fingerprint:
                self.fingerprint = fingerprint
                self.changed_at = time.monotonic()

    def status(self):
        self.observe()
        with self.lock:
            state = "conflict" if self.conflict else "error" if self.error else "saving" if self.saving else "pending" if self.fingerprint != self.saved_fingerprint else "saved"
            return {"enabled": True, "state": state, "last_saved": self.last_saved, "message": self.error,
                    "mode": "workspace_snapshot"}

    def sync(self):
        with self.sync_lock:
            if self.conflict:
                raise StorageConflict(self.error)
            self.observe()
            with self.lock:
                fingerprint = self.fingerprint
                self.saving = True
            revision, uploaded = uuid.uuid4().hex, 0
            committed = False
            try:
                head = self.remote.head()
                if (head["revision"] if head else None) != self.revision:
                    raise StorageConflict("另一个实例已保存新版本，本实例已停止同步，请停止旧实例并恢复云端工作区。")
                with self.workspace.export() as archive:
                    size = archive.stat().st_size
                    if size > MAX_BYTES:
                        raise StorageError("自动备份超过200MB，请先导出报告并清理不再使用的媒体。")
                    digest = hashlib.sha256()
                    with archive.open("rb") as source:
                        number = 0
                        while data := source.read(PART_BYTES):
                            # Include the current part even if its successful response is lost.
                            uploaded = number + 1
                            self.remote.upload(revision, number, data)
                            digest.update(data)
                            number += 1
                    manifest = {"format": "labagent-cloud-v1", "revision": revision, "bytes": size,
                                "parts": number, "sha256": digest.hexdigest(),
                                "created_at": datetime.now(timezone.utc).isoformat()}
                row = {"revision": revision, "manifest": manifest, "previous": [head["manifest"]] if head else []}
                try:
                    self.remote.publish(row, self.revision)
                except StorageError:
                    # A response may be lost after a successful commit. Confirm before retrying.
                    if self.remote.head() != row:
                        raise
                committed = True
                self.write_marker(revision)
                with self.lock:
                    self.revision, self.last_saved = revision, manifest["created_at"]
                    self.saved_fingerprint, self.error = fingerprint, None
                if head:
                    for previous in head["previous"]:
                        try:
                            self.remote.remove(previous)
                        except StorageError:
                            pass  # Retain data if cleanup is unavailable; never undo a committed head.
            except WorkspaceBusy:
                raise
            except (ValueError, OSError) as error:
                with self.lock:
                    self.conflict = isinstance(error, StorageConflict)
                    self.error = str(error) if isinstance(error, ValueError) else "本地备份保存失败，请检查服务端存储空间。"
                raise
            finally:
                if uploaded and not committed:
                    try:
                        current = self.remote.head()
                        retained = [current["manifest"], *current["previous"]] if current else []
                        if all(item["revision"] != revision for item in retained):
                            self.remote.remove({"revision": revision, "parts": uploaded})
                    except StorageError:
                        pass
                with self.lock:
                    self.saving = False

    def start(self):
        def worker():
            retry_at = 0
            while not self.stop.wait(5):
                self.observe()
                with self.lock:
                    ready = not self.conflict and self.fingerprint != self.saved_fingerprint and time.monotonic() - self.changed_at >= 5
                if ready and time.monotonic() >= retry_at:
                    try:
                        self.sync()
                        retry_at = 0
                    except WorkspaceBusy:
                        pass
                    except (ValueError, OSError):
                        retry_at = time.monotonic() + 60
        self.thread = threading.Thread(target=worker, daemon=True, name="workspace-cloud-save")
        self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=2)
