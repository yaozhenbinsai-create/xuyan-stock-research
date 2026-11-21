"""Authenticated credential files; no plaintext fallback after a storage failure.

Windows uses current-user DPAPI. Other hosts require a 32-byte external key file
via STOCK_SECRET_KEY_FILE and use AES-256-GCM. File permissions are additionally
enforced on POSIX. Callers must serialize read/modify/write transactions; cloud
deployments currently run one application worker.
"""
from __future__ import annotations

import ctypes
import json
import os
import stat
import threading
import uuid
from pathlib import Path
from typing import Callable

MAGIC = b"STOCK-SECRET-V1\n"
MAX_BYTES = 8 * 1024 * 1024
_LOCK = threading.RLock()
Validator = Callable[[dict], bool]


class SecretStorageError(RuntimeError):
    """Safe to display: underlying exceptions and secret contents are omitted."""


def _context(path: Path) -> bytes:
    return b"stock-system/credential-v1/" + path.name.encode("utf-8")


class _DPAPI:
    name = b"windows-dpapi-user"

    def _crypt(self, value: bytes, context: bytes, *, decrypt: bool) -> bytes:
        from ctypes import wintypes

        class Blob(ctypes.Structure):
            _fields_ = [("length", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]

        def blob(content):
            buffer = ctypes.create_string_buffer(content)
            return Blob(len(content), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte))), buffer

        source, source_buffer = blob(value)
        entropy, entropy_buffer = blob(context)
        output = Blob()
        crypt = ctypes.WinDLL("crypt32", use_last_error=True)
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        function = crypt.CryptUnprotectData if decrypt else crypt.CryptProtectData
        function.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.POINTER(Blob),
                             ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(Blob)]
        function.restype = wintypes.BOOL
        kernel.LocalFree.argtypes = [ctypes.c_void_p]
        kernel.LocalFree.restype = ctypes.c_void_p
        # CRYPTPROTECT_UI_FORBIDDEN; no LOCAL_MACHINE flag, so another Windows
        # account cannot decrypt by merely copying the credential file.
        if not function(ctypes.byref(source), None, ctypes.byref(entropy), None,
                        None, 1, ctypes.byref(output)):
            raise SecretStorageError("Windows 当前用户无法加密或解密凭据；请核对服务运行账户")
        try:
            return ctypes.string_at(output.data, output.length)
        finally:
            if output.data:
                ctypes.memset(output.data, 0, output.length)
                kernel.LocalFree(output.data)
            ctypes.memset(source_buffer, 0, len(source_buffer))
            ctypes.memset(entropy_buffer, 0, len(entropy_buffer))

    def encrypt(self, value: bytes, context: bytes) -> bytes:
        return self._crypt(value, context, decrypt=False)

    def decrypt(self, value: bytes, context: bytes) -> bytes:
        return self._crypt(value, context, decrypt=True)


def _private_path(path: Path, *, directory: bool = False) -> None:
    """Reject insecure permissions instead of silently rewriting deployments."""
    try:
        info = path.lstat()
        right_type = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
        if (not right_type or info.st_uid != os.geteuid() or
                stat.S_IMODE(info.st_mode) & 0o077):
            raise SecretStorageError("密钥和凭据目录必须归服务账户所有，目录权限 700，文件权限 600")
    except OSError:
        raise SecretStorageError("无法访问受保护的密钥或凭据路径") from None


def _external_key_path(data_path: Path) -> Path:
    configured = os.environ.get("STOCK_SECRET_KEY_FILE", "").strip()
    if not configured:
        raise SecretStorageError("此平台需要配置独立的 STOCK_SECRET_KEY_FILE 才能保存或读取凭据")
    key_path = Path(configured)
    if not key_path.is_absolute():
        raise SecretStorageError("STOCK_SECRET_KEY_FILE 必须是独立密钥文件的绝对路径")
    # Resolve for containment checks, but reject a symlink at the actual file
    # and parent before opening it. Keys must not live in the data directory.
    key_parent = key_path.parent.resolve()
    data_parent = data_path.parent.resolve()
    if key_parent == data_parent or key_parent.is_relative_to(data_parent) or data_parent.is_relative_to(key_parent):
        raise SecretStorageError("主密钥必须与应用数据存放在互不包含的独立目录")
    _private_path(key_path.parent, directory=True)
    _private_path(key_path)
    return key_path


def _key_material(data_path: Path) -> bytes:
    key_path = _external_key_path(data_path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        with os.fdopen(os.open(key_path, flags), "rb") as stream:
            info = os.fstat(stream.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or stat.S_IMODE(info.st_mode) & 0o077:
                raise SecretStorageError("主密钥文件权限不安全")
            key = stream.read(33)
    except OSError:
        raise SecretStorageError("无法读取主密钥文件") from None
    if len(key) != 32:
        raise SecretStorageError("主密钥格式无效，必须是安全生成的 32 字节密钥")
    return key


class _AESGCM:
    name = b"aes-256-gcm-external-key"

    def __init__(self, key: bytes):
        try:
            from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        except ImportError:
            raise SecretStorageError("请安装项目锁定的 cryptography 依赖以启用凭据加密") from None
        self.cipher = AESGCM(key)

    def encrypt(self, value: bytes, context: bytes) -> bytes:
        nonce = os.urandom(12)
        return nonce + self.cipher.encrypt(nonce, value, context)

    def decrypt(self, value: bytes, context: bytes) -> bytes:
        if len(value) < 28:
            raise SecretStorageError("加密凭据无效，原文件未修改")
        try:
            return self.cipher.decrypt(value[:12], value[12:], context)
        except Exception:
            raise SecretStorageError("凭据认证解密失败，请核对主密钥；原文件未修改") from None


def _backend(path: Path):
    if os.name == "nt":
        return _DPAPI()
    return _AESGCM(_key_material(path))


def _validate(data, validator: Validator | None) -> dict:
    if not isinstance(data, dict) or (validator is not None and not validator(data)):
        raise SecretStorageError("凭据数据结构无效，原文件未修改")
    return data


def _decode(path: Path, content: bytes, validator: Validator | None = None) -> dict:
    try:
        if not content.startswith(MAGIC):
            raise SecretStorageError("加密凭据格式无效，原文件未修改")
        provider, ciphertext = content[len(MAGIC):].split(b"\n", 1)
        backend = _backend(path)
        if provider != backend.name:
            raise SecretStorageError("凭据的加密平台不匹配，请使用原账户迁移授权")
        return _validate(json.loads(backend.decrypt(ciphertext, _context(path))), validator)
    except SecretStorageError:
        raise
    except Exception:
        raise SecretStorageError("凭据认证或数据格式无效，原文件未修改") from None


def _atomic_write(path: Path, content: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        if os.name != "nt":
            _private_path(path.parent, directory=True)
            if path.exists() or path.is_symlink():
                _private_path(path)
        elif path.is_symlink():
            raise SecretStorageError("凭据文件不可使用符号链接")
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(temporary, flags, 0o600), "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except SecretStorageError:
        raise
    except OSError:
        raise SecretStorageError("凭据保存失败，原文件已保留；请检查磁盘和目录权限") from None
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass  # It contains only authenticated ciphertext, never plaintext.


def _read_bytes(path: Path) -> bytes | None:
    if not path.exists() and not path.is_symlink():
        return None
    if os.name != "nt":
        _private_path(path.parent, directory=True)
        _private_path(path)
    elif path.is_symlink():
        raise SecretStorageError("凭据文件不可使用符号链接")
    with path.open("rb") as stream:
        content = stream.read(MAX_BYTES + 4097)
    if len(content) > MAX_BYTES + 4096:
        raise SecretStorageError("凭据文件过大，无法读取")
    return content


def _existing_data(path: Path, content: bytes, validator: Validator | None) -> dict:
    if content.startswith(MAGIC):
        return _decode(path, content, validator)
    return _validate(json.loads(content.decode("utf-8-sig")), validator)


def save_json(path: Path, data: dict, validator: Validator | None = None) -> None:
    path = Path(path)
    with _LOCK:
        try:
            _validate(data, validator)
            previous = _read_bytes(path)
            if previous is not None:
                # Never replace an unreadable store with a new apparently valid
                # empty store, including when a caller did not load it first.
                _existing_data(path, previous, validator)
            raw = json.dumps(data, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(raw) > MAX_BYTES:
                raise SecretStorageError("凭据文件过大，无法保存")
            backend = _backend(path)
            content = MAGIC + backend.name + b"\n" + backend.encrypt(raw, _context(path))
            _atomic_write(path, content)
        except SecretStorageError:
            raise
        except Exception:
            raise SecretStorageError("凭据加密保存失败，原文件未修改") from None


def load_json(path: Path, validator: Validator | None = None) -> dict:
    """Valid legacy JSON is atomically encrypted before any values are returned."""
    path = Path(path)
    with _LOCK:
        try:
            content = _read_bytes(path)
            if content is None:
                return {}
            if content.startswith(MAGIC):
                return _decode(path, content, validator)
            data = _existing_data(path, content, validator)
            save_json(path, data, validator)
            return data
        except SecretStorageError:
            raise
        except Exception:
            raise SecretStorageError("凭据无法读取或格式无效，原文件未修改") from None


def write_encrypted_bytes(path: Path, content: bytes) -> None:
    """Restore an authenticated encrypted snapshot after a database rollback."""
    path = Path(path)
    with _LOCK:
        _decode(path, content)
        _atomic_write(path, content)


def inspect_file(path: Path) -> dict:
    """Read only format/configuration status, never decrypt, parse, or migrate.

    'encrypted' identifies an envelope, not proof its tag or contents are valid.
    This helper is suitable for a readiness report without exposing values.
    """
    path = Path(path)
    report = {"exists": False, "encrypted": False, "backend": "", "configured": False,
              "verified": False, "error": ""}
    try:
        report["exists"] = path.exists() or path.is_symlink()
        if os.name == "nt":
            backend_name = _DPAPI.name
        else:
            # Inspect only metadata, never read the external master key here.
            key_path = _external_key_path(path)
            if key_path.stat().st_size != 32:
                raise SecretStorageError("主密钥格式无效，必须是安全生成的 32 字节密钥")
            try:
                from cryptography.hazmat.primitives.ciphers import aead
                if not hasattr(aead, "AESGCM"):
                    raise ImportError()
            except ImportError:
                raise SecretStorageError("缺少项目要求的凭据加密依赖") from None
            backend_name = _AESGCM.name
        report["configured"] = True
        report["backend"] = backend_name.decode("ascii")
        if report["exists"]:
            if os.name != "nt":
                _private_path(path.parent, directory=True)
                _private_path(path)
            elif path.is_symlink():
                raise SecretStorageError("凭据文件不可使用符号链接")
            with path.open("rb") as stream:
                prefix = stream.read(len(MAGIC) + 80)
            if prefix.startswith(MAGIC):
                name = prefix[len(MAGIC):].split(b"\n", 1)[0]
                report["encrypted"] = True
                if name != backend_name:
                    raise SecretStorageError("加密平台与当前服务账户配置不匹配")
            else:
                report["error"] = "文件尚未加密，需要验证后迁移"
    except SecretStorageError as exc:
        report["error"] = str(exc)
    except Exception:
        report["error"] = "无法检查凭据文件配置"
    return report
