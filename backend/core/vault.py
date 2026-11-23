# -*- coding: utf-8 -*-
"""Password-gated local media encryption. Existing plaintext files are untouched."""
from __future__ import annotations

import json
import os
import secrets
import struct
import threading
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.scrypt import Scrypt

MAGIC = b"ORANGE01"
CHUNK = 1024 * 1024
HEADER = struct.Struct(">8sQI")


class Vault:
    def __init__(self, root: str):
        self.root = Path(root)
        self.meta = self.root / ".media-vault.json"
        self._key: bytes | None = None
        self._lock = threading.RLock()

    @property
    def initialized(self) -> bool:
        return self.meta.is_file()

    @property
    def unlocked(self) -> bool:
        return self._key is not None

    @staticmethod
    def _derive(password: str, salt: bytes) -> bytes:
        if not password:
            raise ValueError("请输入主密码")
        return Scrypt(salt=salt, length=32, n=2**15, r=8, p=1).derive(password.encode("utf-8"))

    def setup(self, password: str) -> None:
        with self._lock:
            if self.initialized:
                raise ValueError("主密码已设置")
            salt = secrets.token_bytes(16)
            key = self._derive(password, salt)
            nonce = secrets.token_bytes(12)
            check = AESGCM(key).encrypt(nonce, b"orange-vault-check", None)
            self.root.mkdir(parents=True, exist_ok=True)
            tmp = self.meta.with_suffix(".tmp")
            tmp.write_text(json.dumps({"version": 1, "salt": salt.hex(), "nonce": nonce.hex(), "check": check.hex()}), encoding="utf-8")
            os.replace(tmp, self.meta)
            self._key = key

    def _verified_key(self, password: str, meta: dict) -> bytes:
        password_key = self._derive(password, bytes.fromhex(meta["salt"]))
        try:
            if meta["version"] == 1:
                key = password_key
            elif meta["version"] == 2:
                key = AESGCM(password_key).decrypt(
                    bytes.fromhex(meta["wrap_nonce"]),
                    bytes.fromhex(meta["wrapped_key"]), b"orange-vault-key")
            else:
                raise ValueError("主密码文件格式不支持")
            check = AESGCM(key).decrypt(
                bytes.fromhex(meta["nonce"]), bytes.fromhex(meta["check"]), None)
        except InvalidTag as exc:
            raise ValueError("主密码不正确") from exc
        if check != b"orange-vault-check":
            raise ValueError("主密码不正确")
        return key

    def unlock(self, password: str) -> None:
        with self._lock:
            if not self.initialized:
                raise ValueError("尚未设置主密码")
            meta = json.loads(self.meta.read_text(encoding="utf-8"))
            self._key = self._verified_key(password, meta)

    def change_password(self, old_password: str, new_password: str) -> None:
        with self._lock:
            if not self.initialized:
                raise ValueError("尚未设置主密码")
            if not new_password:
                raise ValueError("请输入新密码")
            if old_password == new_password:
                raise ValueError("新密码不能与当前密码相同")
            meta = json.loads(self.meta.read_text(encoding="utf-8"))
            key = self._verified_key(old_password, meta)
            salt = secrets.token_bytes(16)
            wrapping_key = self._derive(new_password, salt)
            wrap_nonce = secrets.token_bytes(12)
            updated = {
                "version": 2,
                "salt": salt.hex(),
                "wrap_nonce": wrap_nonce.hex(),
                "wrapped_key": AESGCM(wrapping_key).encrypt(
                    wrap_nonce, key, b"orange-vault-key").hex(),
                "nonce": meta["nonce"],
                "check": meta["check"],
            }
            tmp = self.meta.with_suffix(".tmp")
            try:
                with open(tmp, "w", encoding="utf-8") as fp:
                    json.dump(updated, fp)
                    fp.flush()
                    os.fsync(fp.fileno())
                os.replace(tmp, self.meta)
            finally:
                if tmp.exists():
                    tmp.unlink()
            if self._key is not None:
                self._key = key

    def lock(self) -> None:
        with self._lock:
            self._key = None

    def encrypt_file(self, src: str) -> str:
        with self._lock:
            if self._key is None:
                raise ValueError("文件加密未解锁")
            dest = src + ".enc"
            tmp = dest + ".tmp"
            size = os.path.getsize(src)
            try:
                with open(src, "rb") as inf, open(tmp, "wb") as outf:
                    outf.write(HEADER.pack(MAGIC, size, CHUNK))
                    index = 0
                    while True:
                        part = inf.read(CHUNK)
                        if not part:
                            break
                        nonce = secrets.token_bytes(12)
                        aad = struct.pack(">Q", index)
                        cipher = AESGCM(self._key).encrypt(nonce, part, aad)
                        outf.write(nonce)
                        outf.write(cipher)
                        index += 1
                    outf.flush()
                    os.fsync(outf.fileno())
                os.replace(tmp, dest)
                os.remove(src)
                return dest
            finally:
                if os.path.exists(tmp):
                    os.remove(tmp)

    def read_range(self, path: str, start: int = 0, end: int | None = None) -> tuple[bytes, int]:
        with self._lock:
            if self._key is None:
                raise ValueError("文件加密未解锁")
            with open(path, "rb") as fp:
                magic, size, chunk = HEADER.unpack(fp.read(HEADER.size))
                if magic != MAGIC or chunk != CHUNK:
                    raise ValueError("加密文件格式不支持")
                if end is None:
                    end = size - 1
                if start < 0 or end < start or end >= size:
                    raise ValueError("读取范围无效")
                out = bytearray()
                first = start // chunk
                last = end // chunk
                for index in range(first, last + 1):
                    plain_len = min(chunk, size - index * chunk)
                    fp.seek(HEADER.size + index * (12 + chunk + 16))
                    nonce = fp.read(12)
                    encrypted = fp.read(plain_len + 16)
                    plain = AESGCM(self._key).decrypt(nonce, encrypted, struct.pack(">Q", index))
                    lo = max(0, start - index * chunk)
                    hi = min(plain_len, end - index * chunk + 1)
                    out.extend(plain[lo:hi])
                return bytes(out), size

    @staticmethod
    def plaintext_size(path: str) -> int:
        with open(path, "rb") as fp:
            magic, size, chunk = HEADER.unpack(fp.read(HEADER.size))
        if magic != MAGIC or chunk != CHUNK:
            raise ValueError("加密文件格式不支持")
        return size
