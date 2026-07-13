from __future__ import annotations

import argparse
import importlib.util
import json
import math
import os
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from types import ModuleType
from typing import Any, Iterable

from PIL import Image, ImageDraw, ImageFont, ImageOps


PROJECT_DIR = Path(__file__).resolve().parent
DEFAULT_GOOGLE_AI_PROJECT = PROJECT_DIR.parent / "google_modo_ia_perfil_limpo"
COORDINATE_SCALE = 1000.0


class VisualDetectionError(RuntimeError):
    pass


@dataclass(frozen=True)
class Detection:
    label: str
    x1: float
    y1: float
    x2: float
    y2: float
    confidence: float | None = None
    note: str | None = None

    def normalized(self) -> "Detection":
        x1, x2 = sorted((float(self.x1), float(self.x2)))
        y1, y2 = sorted((float(self.y1), float(self.y2)))
        x1 = max(0.0, min(COORDINATE_SCALE, x1))
        y1 = max(0.0, min(COORDINATE_SCALE, y1))
        x2 = max(0.0, min(COORDINATE_SCALE, x2))
        y2 = max(0.0, min(COORDINATE_SCALE, y2))
        confidence = self.confidence
        if confidence is not None:
            confidence = max(0.0, min(1.0, float(confidence)))
        return Detection(
            label=self.label.strip(),
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            confidence=confidence,
            note=self.note.strip() if self.note else None,
        )

    def pixel_box(self, width: int, height: int) -> tuple[int, int, int, int]:
        item = self.normalized()
        left = round(item.x1 / COORDINATE_SCALE * width)
        top = round(item.y1 / COORDINATE_SCALE * height)
        right = round(item.x2 / COORDINATE_SCALE * width)
        bottom = round(item.y2 / COORDINATE_SCALE * height)
        left = max(0, min(width - 1, left))
        top = max(0, min(height - 1, top))
        right = max(left + 1, min(width, right))
        bottom = max(top + 1, min(height, bottom))
        return left, top, right, bottom


@dataclass(frozen=True)
class DetectionRun:
    input_image: str
    output_image: str | None
    output_json: str
    detections: list[Detection]
    raw_answer: str
    http_requests: int
    ai_queries: int
    source_count: int


class GoogleAIAdapter:
    """Carrega o cliente HTTP do projeto irmão sem copiar sessão nem cookies."""

    def __init__(self, project_path: Path | None = None) -> None:
        env_path = os.environ.get("GOOGLE_AI_PROJECT", "").strip()
        self.project_path = (
            Path(env_path).expanduser().resolve()
            if env_path
            else (project_path or DEFAULT_GOOGLE_AI_PROJECT).resolve()
        )
        self.module = self._load_module()

    def _load_module(self) -> ModuleType:
        module_path = self.project_path / "google_ia_requests.py"
        if not module_path.is_file():
            raise VisualDetectionError(
                "Cliente do Modo IA não encontrado em "
                f"{module_path}. Defina GOOGLE_AI_PROJECT se ele estiver em outro local."
            )
        module_name = "_shared_google_ia_requests"
        spec = importlib.util.spec_from_file_location(module_name, module_path)
        if spec is None or spec.loader is None:
            raise VisualDetectionError(f"Não foi possível carregar {module_path}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        if not hasattr(module, "query_google_ai"):
            raise VisualDetectionError("O cliente compartilhado não expõe query_google_ai().")
        return module

    def ask_with_image(
        self,
        image_path: Path,
        prompt: str,
        timeout: float,
        attempts: int,
    ) -> Any:
        return self.module.query_google_ai(
            prompt,
            timeout=timeout,
            image_path=image_path,
            attempts=attempts,
        )


def build_detection_prompt(
    image_width: int,
    image_height: int,
    max_objects: int = 30,
    extra_instruction: str = "",
) -> str:
    extra = extra_instruction.strip()
    extra_block = f"\nINSTRUÇÃO EXTRA DO USUÁRIO:\n{extra}\n" if extra else ""
    return f"""
Analise SOMENTE os pixels da imagem anexada. NÃO pesquise na web, NÃO consulte páginas,
NÃO use fontes externas e NÃO acrescente conhecimento que não esteja visualmente comprovado.
Não forneça links, citações, explicações longas nem texto fora do JSON.

Objetivo: localizar os objetos visíveis e devolver caixas delimitadoras precisas.
A imagem original mede {image_width} x {image_height} pixels, mas as coordenadas devem ser
normalizadas numa escala fixa de 0 a 1000:
- x=0 é a borda esquerda; x=1000 é a borda direita;
- y=0 é o topo; y=1000 é a borda inferior;
- caixa deve ser um objeto JSON com as chaves x1, y1, x2 e y2;
- x1/y1 representam o canto superior esquerdo;
- x2/y2 representam o canto inferior direito;
- NÃO use listas ou colchetes para as coordenadas, pois o produto pode removê-los.

Regras:
1. Retorne no máximo {max_objects} objetos.
2. Crie uma entrada por instância visível. Duas bolas devem gerar duas caixas.
3. Use rótulos curtos em português, como "pessoa", "bola", "cesta", "carro".
4. Para pessoas, use rótulos genéricos; não tente descobrir identidade.
5. A caixa deve envolver o objeto, não a imagem inteira nem uma área vaga do fundo.
6. Não invente objetos parcialmente sugeridos ou invisíveis.
7. Omita itens cuja posição não possa ser estimada com segurança.
8. confiança deve ser um número entre 0 e 1.
9. A resposta deve ser JSON válido, sem Markdown e sem comentários.
{extra_block}
Formato obrigatório:
{{
  "escala_coordenadas": 1000,
  "objetos": [
    {{
      "rotulo": "bola",
      "caixa": {{"x1": 120, "y1": 80, "x2": 360, "y2": 330}},
      "confianca": 0.94,
      "observacao": "opcional e curta"
    }}
  ]
}}

Se não houver objeto localizável, retorne exatamente:
{{"escala_coordenadas":1000,"objetos":[]}}
""".strip()


def _json_candidates(text: str) -> Iterable[str]:
    stripped = text.strip().lstrip("\ufeff")
    if stripped:
        yield stripped

    for match in re.finditer(r"```(?:json)?\s*(.*?)```", text, flags=re.I | re.S):
        candidate = match.group(1).strip()
        if candidate:
            yield candidate

    starts = [index for index in (text.find("{"), text.find("[")) if index >= 0]
    if starts:
        start = min(starts)
        for closing in ("}", "]"):
            end = text.rfind(closing)
            if end > start:
                yield text[start : end + 1]


def _load_json_answer(text: str) -> Any:
    errors: list[str] = []
    seen: set[str] = set()
    for candidate in _json_candidates(text):
        if candidate in seen:
            continue
        seen.add(candidate)
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as exc:
            errors.append(f"linha {exc.lineno}, coluna {exc.colno}: {exc.msg}")
    details = "; ".join(errors[-3:]) if errors else "nenhum bloco JSON encontrado"
    raise VisualDetectionError(f"A resposta do Modo IA não contém JSON válido: {details}")


def _first_value(mapping: dict[str, Any], names: Iterable[str]) -> Any:
    for name in names:
        if name in mapping:
            return mapping[name]
    return None


def _parse_confidence(value: Any) -> float | None:
    if value is None or value == "":
        return None
    if isinstance(value, str):
        value = value.strip().replace(",", ".")
        if value.endswith("%"):
            try:
                return float(value[:-1]) / 100.0
            except ValueError:
                return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number > 1.0 and number <= 100.0:
        number /= 100.0
    return max(0.0, min(1.0, number))


def _extract_box(item: dict[str, Any]) -> list[float] | None:
    raw = _first_value(
        item,
        ("caixa", "bbox", "box", "bounding_box", "boundingBox", "coordenadas"),
    )
    if isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            return [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        except (TypeError, ValueError):
            return None
    if isinstance(raw, dict):
        item = raw

    direct = [
        _first_value(item, ("x1", "left", "esquerda")),
        _first_value(item, ("y1", "top", "topo")),
        _first_value(item, ("x2", "right", "direita")),
        _first_value(item, ("y2", "bottom", "base", "inferior")),
    ]
    if all(value is not None for value in direct):
        try:
            return [float(value) for value in direct]
        except (TypeError, ValueError):
            return None

    x = _first_value(item, ("x", "left", "esquerda"))
    y = _first_value(item, ("y", "top", "topo"))
    width = _first_value(item, ("w", "width", "largura"))
    height = _first_value(item, ("h", "height", "altura"))
    if all(value is not None for value in (x, y, width, height)):
        try:
            x_f, y_f, w_f, h_f = map(float, (x, y, width, height))
            return [x_f, y_f, x_f + w_f, y_f + h_f]
        except (TypeError, ValueError):
            return None
    return None


def _convert_box_to_scale(
    box: list[float],
    declared_scale: float | None,
    image_width: int,
    image_height: int,
) -> list[float]:
    if declared_scale and declared_scale > 0:
        return [value / declared_scale * COORDINATE_SCALE for value in box]

    maximum = max(abs(value) for value in box)
    if maximum <= 1.01:
        return [value * COORDINATE_SCALE for value in box]
    if maximum <= 100.01:
        return [value * 10.0 for value in box]
    if maximum <= COORDINATE_SCALE * 1.05:
        return box

    # Fallback para respostas em pixels, apesar do prompt pedir escala 1000.
    x1, y1, x2, y2 = box
    return [
        x1 / max(1, image_width) * COORDINATE_SCALE,
        y1 / max(1, image_height) * COORDINATE_SCALE,
        x2 / max(1, image_width) * COORDINATE_SCALE,
        y2 / max(1, image_height) * COORDINATE_SCALE,
    ]


def _intersection_over_union(a: Detection, b: Detection) -> float:
    a = a.normalized()
    b = b.normalized()
    left = max(a.x1, b.x1)
    top = max(a.y1, b.y1)
    right = min(a.x2, b.x2)
    bottom = min(a.y2, b.y2)
    intersection = max(0.0, right - left) * max(0.0, bottom - top)
    if intersection <= 0:
        return 0.0
    area_a = max(0.0, a.x2 - a.x1) * max(0.0, a.y2 - a.y1)
    area_b = max(0.0, b.x2 - b.x1) * max(0.0, b.y2 - b.y1)
    union = area_a + area_b - intersection
    return intersection / union if union > 0 else 0.0


def parse_detections(
    answer: str,
    image_width: int,
    image_height: int,
    max_objects: int = 30,
    min_confidence: float = 0.0,
) -> list[Detection]:
    payload = _load_json_answer(answer)
    declared_scale: float | None = None
    if isinstance(payload, dict):
        scale_value = _first_value(
            payload,
            ("escala_coordenadas", "escala", "coordinate_scale", "scale"),
        )
        try:
            declared_scale = float(scale_value) if scale_value is not None else None
        except (TypeError, ValueError):
            declared_scale = None
        objects = _first_value(payload, ("objetos", "objects", "deteccoes", "detections"))
        if objects is None and any(key in payload for key in ("caixa", "bbox", "box")):
            objects = [payload]
    elif isinstance(payload, list):
        objects = payload
    else:
        raise VisualDetectionError("O JSON retornado não é um objeto nem uma lista.")

    if not isinstance(objects, list):
        raise VisualDetectionError("O campo 'objetos' não é uma lista.")

    detections: list[Detection] = []
    for raw_item in objects:
        if not isinstance(raw_item, dict):
            continue
        label_value = _first_value(
            raw_item,
            ("rotulo", "rótulo", "label", "nome", "objeto", "object"),
        )
        label = re.sub(r"\s+", " ", str(label_value or "")).strip()
        if not label:
            continue
        box = _extract_box(raw_item)
        if box is None:
            continue
        item_scale = _first_value(raw_item, ("escala", "scale"))
        try:
            effective_scale = float(item_scale) if item_scale is not None else declared_scale
        except (TypeError, ValueError):
            effective_scale = declared_scale
        box = _convert_box_to_scale(box, effective_scale, image_width, image_height)
        confidence = _parse_confidence(
            _first_value(raw_item, ("confianca", "confiança", "confidence", "score"))
        )
        if confidence is not None and confidence < min_confidence:
            continue
        note_value = _first_value(
            raw_item,
            ("observacao", "observação", "note", "descricao", "descrição"),
        )
        detection = Detection(
            label=label[:80],
            x1=box[0],
            y1=box[1],
            x2=box[2],
            y2=box[3],
            confidence=confidence,
            note=str(note_value).strip()[:240] if note_value else None,
        ).normalized()
        if detection.x2 - detection.x1 < 2 or detection.y2 - detection.y1 < 2:
            continue

        duplicate = False
        for existing in detections:
            if existing.label.casefold() == detection.label.casefold() and _intersection_over_union(existing, detection) >= 0.92:
                duplicate = True
                break
        if not duplicate:
            detections.append(detection)
        if len(detections) >= max_objects:
            break
    return detections


def _load_font(font_size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidates = [
        Path(r"C:\Windows\Fonts\arialbd.ttf"),
        Path(r"C:\Windows\Fonts\arial.ttf"),
        Path(r"C:\Windows\Fonts\segoeuib.ttf"),
        Path(r"C:\Windows\Fonts\segoeui.ttf"),
    ]
    for path in candidates:
        if path.is_file():
            try:
                return ImageFont.truetype(str(path), font_size)
            except OSError:
                pass
    return ImageFont.load_default()


def draw_detections(
    image_path: Path,
    output_path: Path,
    detections: list[Detection],
    show_confidence: bool = True,
) -> None:
    with Image.open(image_path) as opened:
        base = ImageOps.exif_transpose(opened).convert("RGBA")
    width, height = base.size
    overlay = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)

    line_width = max(2, round(min(width, height) * 0.004))
    font_size = max(13, round(min(width, height) * 0.025))
    padding_x = max(4, round(font_size * 0.38))
    padding_y = max(3, round(font_size * 0.22))
    font = _load_font(font_size)

    box_fill = (0, 230, 80, 48)
    box_outline = (0, 235, 80, 245)
    label_fill = (0, 145, 55, 235)
    label_text = (255, 255, 255, 255)

    for detection in detections:
        left, top, right, bottom = detection.pixel_box(width, height)
        draw.rectangle(
            (left, top, right, bottom),
            fill=box_fill,
            outline=box_outline,
            width=line_width,
        )

        text = detection.label
        if show_confidence and detection.confidence is not None:
            text += f" {detection.confidence:.0%}"
        text_box = draw.textbbox((0, 0), text, font=font)
        text_width = text_box[2] - text_box[0]
        text_height = text_box[3] - text_box[1]
        panel_width = min(width, text_width + padding_x * 2)
        panel_height = text_height + padding_y * 2

        panel_left = max(0, min(width - panel_width, left))
        panel_top = top - panel_height
        if panel_top < 0:
            panel_top = min(height - panel_height, top + line_width)
        panel_right = panel_left + panel_width
        panel_bottom = panel_top + panel_height
        draw.rounded_rectangle(
            (panel_left, panel_top, panel_right, panel_bottom),
            radius=max(2, round(font_size * 0.18)),
            fill=label_fill,
        )
        draw.text(
            (panel_left + padding_x, panel_top + padding_y - text_box[1]),
            text,
            font=font,
            fill=label_text,
        )

    result = Image.alpha_composite(base, overlay)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    suffix = output_path.suffix.lower()
    if suffix in {".jpg", ".jpeg"}:
        result.convert("RGB").save(output_path, quality=95, optimize=True)
    else:
        result.save(output_path)


def default_output_path(image_path: Path) -> Path:
    return PROJECT_DIR / "saida" / f"{image_path.stem}_marcado.png"


def process_image(
    image_path: str | Path,
    output_path: str | Path | None = None,
    json_output_path: str | Path | None = None,
    *,
    analysis_image_path: str | Path | None = None,
    max_objects: int = 30,
    min_confidence: float = 0.15,
    timeout: float = 90.0,
    attempts: int = 3,
    show_confidence: bool = True,
    extra_instruction: str = "",
    only_json: bool = False,
    adapter: GoogleAIAdapter | None = None,
) -> DetectionRun:
    image = Path(image_path).expanduser().resolve()
    if not image.is_file():
        raise VisualDetectionError(f"Imagem não encontrada: {image}")
    if image.stat().st_size == 0:
        raise VisualDetectionError("A imagem está vazia.")

    with Image.open(image) as opened:
        oriented = ImageOps.exif_transpose(opened)
        image_width, image_height = oriented.size
        oriented.load()

    analysis_image = (
        Path(analysis_image_path).expanduser().resolve()
        if analysis_image_path is not None
        else image
    )
    if not analysis_image.is_file():
        raise VisualDetectionError(f"Imagem de análise não encontrada: {analysis_image}")
    if analysis_image.stat().st_size == 0:
        raise VisualDetectionError("A imagem de análise está vazia.")

    prompt = build_detection_prompt(
        image_width,
        image_height,
        max_objects=max_objects,
        extra_instruction=extra_instruction,
    )
    client = adapter or GoogleAIAdapter()
    ai_result = client.ask_with_image(analysis_image, prompt, timeout, attempts)
    detections = parse_detections(
        ai_result.answer,
        image_width,
        image_height,
        max_objects=max_objects,
        min_confidence=min_confidence,
    )

    output = None if only_json else Path(output_path).expanduser().resolve() if output_path else default_output_path(image)
    json_output = (
        Path(json_output_path).expanduser().resolve()
        if json_output_path
        else PROJECT_DIR / "saida" / f"{image.stem}_deteccoes.json"
    )

    if output is not None:
        draw_detections(image, output, detections, show_confidence=show_confidence)

    payload = {
        "imagem_entrada": str(image),
        "imagem_analise": str(analysis_image),
        "imagem_saida": str(output) if output is not None else None,
        "dimensoes": {"largura": image_width, "altura": image_height},
        "escala_coordenadas": int(COORDINATE_SCALE),
        "objetos": [asdict(item) for item in detections],
        "modo_ia": {
            "requisicoes_http": int(ai_result.http_requests),
            "consultas": int(ai_result.ai_queries),
            "fontes_retornadas": len(ai_result.sources),
            "instrucao_sem_web": True,
        },
        "resposta_bruta": ai_result.answer,
    }
    json_output.parent.mkdir(parents=True, exist_ok=True)
    json_output.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    return DetectionRun(
        input_image=str(image),
        output_image=str(output) if output is not None else None,
        output_json=str(json_output),
        detections=detections,
        raw_answer=ai_result.answer,
        http_requests=int(ai_result.http_requests),
        ai_queries=int(ai_result.ai_queries),
        source_count=len(ai_result.sources),
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Pede ao Google Modo IA para localizar objetos e desenha caixas verdes "
            "transparentes via Python/Pillow. Aceita exatamente uma imagem."
        )
    )
    parser.add_argument("imagem", type=Path, nargs="?")
    parser.add_argument("--saida", type=Path)
    parser.add_argument("--json-saida", type=Path)
    parser.add_argument("--max-objetos", type=int, default=30)
    parser.add_argument("--min-confianca", type=float, default=0.15)
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--tentativas", type=int, default=3)
    parser.add_argument("--sem-confianca", action="store_true")
    parser.add_argument("--instrucao-extra", default="")
    parser.add_argument("--somente-json", action="store_true")
    parser.add_argument("--mostrar-prompt", action="store_true")
    args = parser.parse_args()

    if args.max_objetos < 1:
        parser.error("--max-objetos deve ser pelo menos 1")
    if not 0 <= args.min_confianca <= 1:
        parser.error("--min-confianca deve ficar entre 0 e 1")

    if args.mostrar_prompt:
        if args.imagem and args.imagem.is_file():
            with Image.open(args.imagem) as opened:
                width, height = opened.size
        else:
            width, height = 1000, 1000
        print(
            build_detection_prompt(
                width,
                height,
                max_objects=args.max_objetos,
                extra_instruction=args.instrucao_extra,
            )
        )
        return 0

    if args.imagem is None:
        parser.error("informe uma imagem ou use --mostrar-prompt")

    try:
        result = process_image(
            args.imagem,
            output_path=args.saida,
            json_output_path=args.json_saida,
            max_objects=args.max_objetos,
            min_confidence=args.min_confianca,
            timeout=args.timeout,
            attempts=args.tentativas,
            show_confidence=not args.sem_confianca,
            extra_instruction=args.instrucao_extra,
            only_json=args.somente_json,
        )
    except Exception as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 1

    print(f"Objetos encontrados: {len(result.detections)}")
    print(f"JSON: {result.output_json}")
    if result.output_image:
        print(f"Imagem marcada: {result.output_image}")
    print(f"Requisições HTTP: {result.http_requests}")
    print(f"Consultas ao Modo IA: {result.ai_queries}")
    print(f"Fontes retornadas pelo produto: {result.source_count}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
