import re
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, Iterable


UNKNOWN_COMPETENCIA = "nao-identificada"


def normalize_competencia(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    for pattern in (
        r"^(\d{4})-(\d{2})(?:-\d{2})?",
        r"^(?:\d{2}/)?(\d{2})/(\d{4})$",
        r"^\d{2}/(\d{2})/(\d{4})",
    ):
        match = re.match(pattern, text)
        if not match:
            continue
        first, second = match.groups()
        if len(first) == 4:
            year, month = first, second
        else:
            month, year = first, second
        if 1 <= int(month) <= 12:
            return f"{year}-{month}"
    return None


def competencia_from_xml_bytes(content: bytes) -> str | None:
    if not content or len(content) > 25 * 1024 * 1024:
        return None
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return None
    preferred = {"dcompet", "competencia", "competência"}
    for element in root.iter():
        local_name = str(element.tag).rsplit("}", 1)[-1].lower()
        if local_name in preferred:
            normalized = normalize_competencia(element.text)
            if normalized:
                return normalized
    return None


def competencia_from_xml_path(path: str | Path | None) -> str | None:
    if not path:
        return None
    try:
        xml_path = Path(path)
        if not xml_path.is_file() or xml_path.stat().st_size <= 0:
            return None
        return competencia_from_xml_bytes(xml_path.read_bytes())
    except OSError:
        return None


def competencia_from_text(text: Any) -> str | None:
    match = re.search(
        r"(?:compet[eê]ncia|dcompet)\s*[:\-]?\s*(\d{4}-\d{2}(?:-\d{2})?|\d{2}/\d{4}|\d{2}/\d{2}/\d{4})",
        str(text or ""),
        flags=re.IGNORECASE,
    )
    return normalize_competencia(match.group(1)) if match else None


def competencia_from_item(item: Dict[str, Any]) -> str | None:
    explicit = normalize_competencia(item.get("competencia"))
    if explicit:
        return explicit
    files_by_tipo = item.get("files_by_tipo") or {}
    xml_path = files_by_tipo.get("xml") if isinstance(files_by_tipo, dict) else None
    competence = competencia_from_xml_path(xml_path)
    if competence:
        return competence
    for file_path in item.get("files") or []:
        if str(file_path).lower().endswith(".xml"):
            competence = competencia_from_xml_path(file_path)
            if competence:
                return competence
    return competencia_from_text(item.get("text"))


def normalize_emissao(value: Any) -> date | None:
    text = str(value or "").strip()
    if not text:
        return None
    probe = text[:10]
    for pattern in ("%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(probe, pattern).date()
        except ValueError:
            continue
    return None


def emissao_from_xml_bytes(content: bytes) -> date | None:
    if not content or len(content) > 25 * 1024 * 1024:
        return None
    try:
        root = ET.fromstring(content)
    except (ET.ParseError, ValueError):
        return None
    preferred = {"dhemi", "demi", "dataemissao", "dataemissão"}
    for element in root.iter():
        local_name = str(element.tag).rsplit("}", 1)[-1].lower()
        if local_name in preferred:
            normalized = normalize_emissao(element.text)
            if normalized:
                return normalized
    return None


def emissao_from_item(item: Dict[str, Any]) -> date | None:
    for key in ("data_emissao", "emissao", "dhEmi", "dEmi"):
        normalized = normalize_emissao(item.get(key))
        if normalized:
            return normalized
    files_by_tipo = item.get("files_by_tipo") or {}
    candidates = []
    if isinstance(files_by_tipo, dict) and files_by_tipo.get("xml"):
        candidates.append(files_by_tipo["xml"])
    candidates.extend(path for path in (item.get("files") or []) if str(path).lower().endswith(".xml"))
    for raw_path in candidates:
        try:
            path = Path(raw_path)
            if path.is_file() and path.stat().st_size > 0:
                normalized = emissao_from_xml_bytes(path.read_bytes())
                if normalized:
                    return normalized
        except OSError:
            continue
    match = re.search(
        r"(?:emiss[aã]o|dhemi|demi)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})",
        str(item.get("text") or ""),
        flags=re.IGNORECASE,
    )
    return normalize_emissao(match.group(1)) if match else None


def summarize_competencias(items: Iterable[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    grouped: Dict[str, Dict[str, Any]] = {}
    for item in items:
        competence = competencia_from_item(item) or UNKNOWN_COMPETENCIA
        group = grouped.setdefault(
            competence,
            {"competencia": competence, "total": 0, "baixados": 0, "pendentes": 0, "erros": 0},
        )
        group["total"] += 1
        status = str(item.get("status") or "pendente")
        if status == "baixado":
            group["baixados"] += 1
        elif status == "erro":
            group["erros"] += 1
        else:
            group["pendentes"] += 1
    return dict(sorted(grouped.items(), key=lambda pair: (pair[0] == UNKNOWN_COMPETENCIA, pair[0])))
