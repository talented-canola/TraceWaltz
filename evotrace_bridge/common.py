from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import shutil
import stat
import subprocess
import tarfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from uuid import uuid4


class BridgeError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def now():
    return datetime.now(timezone.utc).isoformat()


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,120}", value):
        raise BridgeError("invalid_id", "编号只能包含字母、数字、下划线和连字符")
    return value


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def digest(value):
    return "sha256:" + hashlib.sha256(canonical(value)).hexdigest()


def gear_digest(value):
    """Gear canonical v1, including typed IEEE754 numbers (not ordinary JSON hashing)."""
    def encode(obj):
        if obj is None:
            return "null"
        if type(obj) is bool:
            return "true" if obj else "false"
        if isinstance(obj, str):
            obj.encode("utf-8", "strict")
            return json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
        if type(obj) in (int, float):
            import math
            if not math.isfinite(obj) or (float(obj).is_integer() and abs(obj) > 2**53 - 1):
                raise BridgeError("invalid_number", str(obj))
            return "n" + struct.pack(">d", 0.0 if obj == 0 else obj).hex()
        if isinstance(obj, list):
            return "[" + ",".join(encode(v) for v in obj) + "]"
        if isinstance(obj, dict) and all(isinstance(k, str) for k in obj):
            return "{" + ",".join(encode(k) + ":" + encode(obj[k]) for k in sorted(obj, key=lambda k: k.encode("utf-8"))) + "}"
        raise BridgeError("invalid_gear_json", type(obj).__name__)
    return "sha256:" + hashlib.sha256(("gear-canonical-v1\n" + encode(value)).encode()).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    path = Path(path)
    if os.name == "nt" and not str(path).startswith("\\\\?\\"):
        path = Path("\\\\?\\" + str(path.resolve()))
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return "sha256:" + h.hexdigest()


def read_json(path):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise BridgeError("invalid_json", f"重复字段：{key}")
            result[key] = value
        return result
    def bad(value):
        raise BridgeError("invalid_json", f"不允许的 JSON 数值：{value}")
    return json.loads(Path(path).read_text(encoding="utf-8"), object_pairs_hook=pairs, parse_constant=bad)


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name("." + path.name + "." + uuid4().hex)
    try:
        with temp.open("xb") as f:
            f.write(canonical(value) + b"\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def relative_path(value):
    if not isinstance(value, str) or not value or "\\" in value or ":" in value:
        raise BridgeError("invalid_path", f"无效相对路径：{value!r}")
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in {"", ".", ".."} for p in value.split("/")):
        raise BridgeError("invalid_path", f"路径越界：{value}")
    return path


def inventory(root, *, exclude=()):
    root = Path(root)
    result = []
    for path in sorted(root.rglob("*"), key=lambda p: p.relative_to(root).as_posix()):
        rel = path.relative_to(root).as_posix()
        if any(part in exclude for part in path.relative_to(root).parts):
            continue
        if path.is_symlink():
            raise BridgeError("linked_file", f"不接收符号链接：{rel}")
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise BridgeError("linked_file", f"不接收硬链接：{rel}")
            result.append({"path": rel, "digest": file_digest(path), "bytes": path.stat().st_size})
        elif not path.is_dir():
            raise BridgeError("special_file", rel)
    return result


@contextmanager
def locked(root, key):
    # Reuse the existing cross-platform, process-lifetime EvoTrace lock.
    from evotrace.locks import session_lock
    from evotrace.store import Store
    with session_lock(Store(Path(root)), key):
        yield


@contextmanager
def staging(destination):
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name("." + destination.name + "-" + uuid4().hex)
    temp.mkdir()
    try:
        yield temp
        if destination.exists():
            raise BridgeError("destination_exists", str(destination))
        temp.rename(destination)
    finally:
        if temp.exists():
            remove_tree(temp, destination.parent)


def remove_tree(path, owned_parent):
    """Delete only a generated child; Git object files are read-only on Windows."""
    path, parent = Path(path).resolve(), Path(owned_parent).resolve()
    if path == parent or not path.is_relative_to(parent):
        raise BridgeError("unsafe_cleanup", str(path))
    def retry(function, name, info):
        os.chmod(name, stat.S_IWRITE | stat.S_IREAD)
        function(name)
    shutil.rmtree(path, onerror=retry)


def execute(args, *, cwd=None, timeout=120, env=None):
    return subprocess.run([str(a) for a in args], cwd=cwd, env=env, timeout=timeout,
                          stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)


def git(repo, *args):
    result = execute(["git", "-c", "core.autocrlf=false", *args], cwd=repo)
    if result.returncode:
        raise BridgeError("git_failed", result.stderr.decode("utf-8", "replace")[-2000:])
    return result.stdout


def init_git(path):
    for args in [("init", "-q"), ("config", "user.email", "bridge@example.invalid"),
                 ("config", "user.name", "EvoTrace Bridge"), ("add", "-A"),
                 ("commit", "-qm", "frozen initial state", "--allow-empty")]:
        git(path, *args)


def extract(archive, destination):
    destination = Path(destination)
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:*") as tar:
        for member in tar.getmembers():
            relative_path(member.name.rstrip("/"))
            if not (member.isfile() or member.isdir()):
                raise BridgeError("unsafe_archive", member.name)
        tar.extractall(destination, filter="data")


def pack_tree(root, archive):
    root, archive = Path(root), Path(archive)
    inventory(root, exclude={".git", "__pycache__", ".pytest_cache"})
    with tarfile.open(archive, "w:gz") as tar:
        for path in sorted(root.rglob("*")):
            if path.is_file() and not {".git", "__pycache__", ".pytest_cache"}.intersection(path.relative_to(root).parts):
                tar.add(path, arcname=path.relative_to(root).as_posix(), recursive=False)
