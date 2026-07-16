"""Cofre local DPAPI para credenciais operacionais.

O arquivo criptografado fica fora do repositorio e so pode ser aberto pelo
mesmo usuario do Windows. Esta API deliberadamente nao oferece um comando para
imprimir segredos.
"""

from __future__ import annotations

import base64
import ctypes
import getpass
import json
import os
import re
import tempfile
from ctypes import wintypes
from pathlib import Path
from typing import Iterable


STORE_VERSION = 1
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,100}$")
CRYPTPROTECT_UI_FORBIDDEN = 0x1


class SecretStoreError(RuntimeError):
    """Erro seguro do cofre, sem incluir valores sigilosos."""


class _DataBlob(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_byte))]


def default_store_path() -> Path:
    override = os.getenv("PRUMO_SECRET_STORE_PATH")
    if override:
        return Path(override).expanduser().resolve()
    base = Path(os.getenv("LOCALAPPDATA") or (Path.home() / "AppData" / "Local"))
    return base / "Prumo" / "operator-secrets.dpapi.json"


def _validate_name(name: str) -> str:
    value = str(name or "").strip()
    if not NAME_RE.fullmatch(value):
        raise SecretStoreError("Nome de segredo invalido.")
    return value


def _protect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("Este cofre local exige Windows DPAPI.")
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "Prumo operator secrets",
        None,
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise SecretStoreError("O Windows nao conseguiu proteger o cofre.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _unprotect(data: bytes) -> bytes:
    if os.name != "nt":
        raise SecretStoreError("Este cofre local exige Windows DPAPI.")
    in_buffer = ctypes.create_string_buffer(data)
    in_blob = _DataBlob(len(data), ctypes.cast(in_buffer, ctypes.POINTER(ctypes.c_byte)))
    out_blob = _DataBlob()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob), None, None, None, None, CRYPTPROTECT_UI_FORBIDDEN, ctypes.byref(out_blob)
    )
    if not ok:
        raise SecretStoreError("O cofre nao pode ser aberto por este usuario do Windows.")
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


class SecretStore:
    def __init__(self, path: Path | None = None) -> None:
        self.path = Path(path or default_store_path())

    def _read_all(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        try:
            envelope = json.loads(self.path.read_text(encoding="utf-8"))
            if envelope.get("version") != STORE_VERSION:
                raise SecretStoreError("Versao de cofre nao suportada.")
            clear = _unprotect(base64.b64decode(envelope["ciphertext"], validate=True))
            values = json.loads(clear.decode("utf-8"))
        except SecretStoreError:
            raise
        except Exception as exc:
            raise SecretStoreError("O cofre local esta invalido ou corrompido.") from exc
        if not isinstance(values, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in values.items()):
            raise SecretStoreError("O conteudo do cofre local e invalido.")
        return values

    def _write_all(self, values: dict[str, str]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        clear = json.dumps(values, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        envelope = {
            "version": STORE_VERSION,
            "protection": "windows-dpapi-current-user",
            "ciphertext": base64.b64encode(_protect(clear)).decode("ascii"),
        }
        fd, temp_name = tempfile.mkstemp(prefix=".operator-secrets-", suffix=".tmp", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                json.dump(envelope, handle, ensure_ascii=True, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, self.path)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)

    def names(self) -> list[str]:
        return sorted(self._read_all())

    def has(self, name: str) -> bool:
        return _validate_name(name) in self._read_all()

    def require(self, name: str) -> str:
        key = _validate_name(name)
        value = self._read_all().get(key, "")
        if not value:
            raise SecretStoreError(f"Segredo ausente: {key}. Cadastre-o pelo comando interativo.")
        return value

    def set(self, name: str, value: str) -> None:
        key = _validate_name(name)
        clean = str(value).strip()
        if not clean:
            raise SecretStoreError("Valor vazio nao foi salvo.")
        values = self._read_all()
        values[key] = clean
        self._write_all(values)

    def set_many(self, incoming: dict[str, str]) -> None:
        values = self._read_all()
        for raw_name, raw_value in incoming.items():
            name = _validate_name(raw_name)
            value = str(raw_value).strip()
            if not value:
                raise SecretStoreError(f"Valor vazio para {name}.")
            values[name] = value
        self._write_all(values)

    def delete(self, name: str) -> bool:
        key = _validate_name(name)
        values = self._read_all()
        existed = key in values
        values.pop(key, None)
        if existed:
            self._write_all(values)
        return existed

    def prompt_set(self, name: str, *, label: str | None = None) -> None:
        key = _validate_name(name)
        first = getpass.getpass(f"{label or key}: ")
        second = getpass.getpass("Confirme: ")
        if first != second:
            raise SecretStoreError("Os valores nao conferem.")
        self.set(key, first)


def redact(text: str, secrets: Iterable[str]) -> str:
    result = str(text)
    for value in sorted({str(v) for v in secrets if v}, key=len, reverse=True):
        result = result.replace(value, "[REDACTED]")
    return result
