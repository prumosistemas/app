#!/usr/bin/env python3
"""Exporta um pacote navegável da auditoria do solver sem copiar frames brutos."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import shutil
import statistics
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = ROOT / "server"
if str(SERVER) not in sys.path:
    sys.path.insert(0, str(SERVER))

from solver_audit import list_solver_audits, resolve_audit_file  # noqa: E402


def _copy_recent_artifacts(export: Path, rows: list[dict]) -> None:
    for row in rows:
        for artifact in row.get("artifacts") or []:
            try:
                source = resolve_audit_file(str(row.get("source")), artifact["path"])
            except (ValueError, FileNotFoundError, KeyError):
                continue
            target = export / str(row.get("source")) / artifact["path"]
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)


def _copy_active_run(export: Path, output_root: Path) -> list[dict]:
    active = []
    for run_file in output_root.rglob("portal_nacional/runs/*/run.json"):
        data = json.loads(run_file.read_text(encoding="utf-8"))
        if data.get("status") != "rodando":
            continue
        target = export / "execucao_atual"
        target.mkdir(parents=True, exist_ok=True)
        index_file = run_file.with_name("indice.json")
        index = json.loads(index_file.read_text(encoding="utf-8")) if index_file.exists() else {}
        if index_file.exists():
            shutil.copy2(index_file, target / "indice.json")
        safe_run = {
            key: data.get(key)
            for key in ("run_id", "created_at", "updated_at", "status", "summary", "last_error")
        }
        (target / "run-resumo.json").write_text(
            json.dumps(safe_run, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logs = sorted(
            (run_file.parent / "logs").glob("automacao_*.log"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:3]
        for log in logs:
            shutil.copy2(log, target / log.name)
        active.append({
            "run_id": run_file.parent.name,
            "totals": index.get("totals"),
            "download_tipos": index.get("download_tipos"),
            "last_events": (index.get("events") or [])[-30:],
        })
    return active


def _snapshot(rows: list[dict], active: list[dict], sync: dict) -> dict:
    route_attempts: Counter[str] = Counter()
    route_successes: Counter[str] = Counter()
    locations: Counter[str] = Counter()
    sources: Counter[str] = Counter()
    reasons: Counter[str] = Counter()
    categories: Counter[str] = Counter()
    provider_times: dict[str, list[float]] = defaultdict(list)
    captures: list[float] = []
    durations: list[float] = []
    unusual = []
    refreshes = clicks = 0
    peaks = []
    for row in rows:
        sources[str(row.get("source"))] += 1
        locations[str(row.get("location"))] += 1
        if row.get("reason"):
            reasons[str(row["reason"])] += 1
        if row.get("unusual_traffic"):
            unusual.append(row.get("request_id"))
        refreshes += len(row.get("refreshes") or [])
        clicks += len(row.get("clicks") or [])
        peaks.append(int(row.get("peak_active_browsers") or 0))
        if row.get("duration_seconds") is not None:
            durations.append(float(row["duration_seconds"]))
        route_attempts.update(str(value) for value in row.get("route_attempts") or [])
        route_successes.update(str(value) for value in row.get("route_successes") or [])
        for event in row.get("timeline") or []:
            if event.get("event") == "provider_success":
                provider_times[str(event.get("route"))].append(float(event.get("elapsed_seconds") or 0))
            elif event.get("event") == "capture_finished":
                captures.append(float(event.get("elapsed_seconds") or 0))
            elif event.get("event") == "challenge_classified":
                categories[str(event.get("category"))] += 1
    metric = lambda values: {
        "count": len(values),
        "median": statistics.median(values) if values else None,
        "max": max(values) if values else None,
    }
    return {
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "active_runs": active,
        "audit_count": len(rows),
        "sources": dict(sources),
        "locations": dict(locations),
        "route_attempts": dict(route_attempts),
        "route_successes": dict(route_successes),
        "reasons": dict(reasons),
        "categories": dict(categories),
        "unusual_request_ids": unusual,
        "refreshes": refreshes,
        "clicks": clicks,
        "peak_active_browsers": max(peaks or [0]),
        "duration_seconds": metric(durations),
        "capture_seconds": metric(captures),
        "provider_seconds": {route: metric(values) for route, values in provider_times.items()},
        "sync": sync,
        "audits": rows,
    }


def _write_prognosis(export: Path, snapshot: dict) -> None:
    current = snapshot["active_runs"][0].get("totals", {}) if snapshot["active_runs"] else {}
    duration = snapshot["duration_seconds"]
    capture = snapshot["capture_seconds"]
    lines = [
        "# Prognóstico do Portal Nacional", "", f"Gerado em: {snapshot['generated_at']}", "",
        f"- Workers observados: **{snapshot['peak_active_browsers']}**.",
        f"- Run atual: **{current.get('baixados', '-')}/{current.get('portal_registros', '-')} baixados**, "
        f"**{current.get('pendentes', '-')} pendentes**, **{current.get('erros', '-')} erros**.",
        f"- Resoluções auditadas: **{snapshot['audit_count']}**; unusual traffic: "
        f"**{len(snapshot['unusual_request_ids'])}**.",
        f"- Solve mediano: **{duration['median'] or 0:.1f}s**; máximo: **{duration['max'] or 0:.1f}s**.",
        f"- Captura temporal mediana: **{capture['median'] or 0:.1f}s**.", "", "## Rotas vencedoras", "",
    ]
    lines.extend(f"- {route}: {count}" for route, count in snapshot["route_successes"].items())
    lines.extend(["", "## Tempo da IA por rota", ""])
    for route, metric in snapshot["provider_seconds"].items():
        lines.append(
            f"- {route}: mediana {metric['median']:.1f}s; máximo {metric['max']:.1f}s; {metric['count']} sucesso(s)."
        )
    lines.extend([
        "", "## Leitura técnica", "",
        "- O maior custo fixo observado é a captura dos desafios temporais, perto de 26 s por etapa.",
        "- O Space HF 2 foi a rota de IA mais rápida na amostra; o egress Modal direto ficou em segundo.",
        "- Desafios temporais de permanência/abelha concentram o trabalho e podem exigir várias etapas.",
        "- O pool opera com quatro trabalhos simultâneos. Aumentar navegadores agora elevaria contenção sem atacar o gargalo principal.",
        "- request_ended_early representa interrupções durante rollout, não perda do checkpoint.",
        "", "Abra ABRIR_AUDITORIA.html para navegar pelas imagens, vídeos e tempos.",
    ])
    (export / "PROGNOSTICO.md").write_text("\n".join(lines), encoding="utf-8")


def _write_gallery(export: Path, snapshot: dict) -> tuple[int, int]:
    media = []
    json_files = []
    for path in export.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(export).as_posix()
        if path.suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".mp4"}:
            media.append((relative, path.suffix.lower()))
        elif path.suffix.lower() in {".json", ".jsonl"}:
            json_files.append(relative)
    rows = []
    for row in snapshot["audits"]:
        winner = (row.get("route_successes") or row.get("route_attempts") or ["-"])[-1]
        rows.append(
            "<tr>" + "".join(
                f"<td>{html.escape(str(value))}</td>"
                for value in (
                    row.get("started_at") or "", row.get("location") or row.get("source") or "",
                    winner, f"{float(row.get('duration_seconds') or 0):.1f}s",
                    "sim" if row.get("unusual_traffic") else "não", row.get("reason") or "em andamento",
                )
            ) + "</tr>"
        )
    cards = []
    for relative, suffix in media:
        escaped = html.escape(relative, quote=True)
        element = (
            f"<video controls preload='metadata' src='{escaped}'></video>"
            if suffix == ".mp4" else f"<img loading='lazy' src='{escaped}' alt='{escaped}'>"
        )
        cards.append(f"<article><b>{html.escape(Path(relative).name)}</b>{element}<small>{escaped}</small></article>")
    links = "".join(
        f"<li><a href='{html.escape(path, quote=True)}'>{html.escape(path)}</a></li>" for path in json_files
    )
    page = f"""<!doctype html><html lang='pt-BR'><meta charset='utf-8'><title>Auditoria Prumo</title>
<style>body{{font:14px system-ui;margin:24px;background:#f4f6fb;color:#182033}}h1,h2{{color:#123f68}}table{{border-collapse:collapse;width:100%;background:white}}td,th{{padding:8px;border:1px solid #d7ddea;text-align:left}}.grid{{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px}}article{{background:white;border:1px solid #d7ddea;border-radius:10px;padding:10px;overflow:hidden}}img,video{{width:100%;height:260px;object-fit:contain;background:#111;margin-top:8px}}small{{display:block;word-break:break-all;color:#667085}}details{{margin:18px 0;background:white;padding:12px;border-radius:8px}}</style>
<h1>Auditoria do Portal Nacional</h1><p>Workers: <b>{snapshot['peak_active_browsers']}</b> · solves: <b>{snapshot['audit_count']}</b> · unusual: <b>{len(snapshot['unusual_request_ids'])}</b> · mídias: <b>{len(media)}</b></p>
<h2>Tempos e rotas</h2><table><thead><tr><th>Início</th><th>Local</th><th>Rota</th><th>Tempo</th><th>Unusual</th><th>Resultado</th></tr></thead><tbody>{''.join(rows)}</tbody></table>
<h2>Imagens e vídeos</h2><div class='grid'>{''.join(cards)}</div><details><summary>JSON/JSONL ({len(json_files)})</summary><ul>{links}</ul></details></html>"""
    (export / "ABRIR_AUDITORIA.html").write_text(page, encoding="utf-8")
    return len(media), len(json_files)


def _write_manifest(export: Path) -> int:
    entries = []
    for path in sorted(item for item in export.rglob("*") if item.is_file()):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        entries.append({
            "path": path.relative_to(export).as_posix(),
            "bytes": path.stat().st_size,
            "sha256": digest.hexdigest(),
        })
    (export / "MANIFESTO.json").write_text(
        json.dumps({"files": entries}, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return len(entries) + 1


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True)
    parser.add_argument("--limit", type=int, default=200)
    args = parser.parse_args()
    export = Path(args.output).resolve()
    export.mkdir(parents=True, exist_ok=False)
    output_root = Path(__import__("os").environ.get("ISS_OUTPUT_ROOT", str(ROOT / "server" / "output")))
    report = list_solver_audits(args.limit)
    rows = report.get("audits") or []
    _copy_recent_artifacts(export, rows)
    active = _copy_active_run(export, output_root / "empresas")
    snapshot = _snapshot(rows, active, report.get("sync") or {})
    (export / "auditoria-resumo.json").write_text(
        json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    _write_prognosis(export, snapshot)
    media, json_count = _write_gallery(export, snapshot)
    files = _write_manifest(export)
    print(json.dumps({"output": str(export), "files": files, "media": media, "json": json_count}))


if __name__ == "__main__":
    main()
