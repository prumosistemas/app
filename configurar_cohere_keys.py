from __future__ import annotations

import argparse
from getpass import getpass
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
SECRET_NAME = "prumo-portal-nacional-solver"
DEPLOY_FILE = ROOT / "deploy" / "modal_portal_nacional_solver.py"
HEALTH_URL = "https://jorhinhogames--prumo-portal-nacional-solver-solver-server.modal.run/health"


def run(command: list[str]) -> None:
    print("+", " ".join(command))
    subprocess.run(command, cwd=ROOT, check=True)


def load_keys() -> list[str]:
    keys = [getpass(f"COHERE_API_KEY_{index} (entrada oculta): ").strip() for index in range(1, 4)]

    for index, key in enumerate(keys, start=1):
        if not key:
            raise ValueError(f"Informe a chave {index}.")

    if len(set(keys)) != 3:
        raise ValueError("As tres chaves precisam ser diferentes.")

    return keys


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Atualiza as tres chaves Cohere do solver do Portal Nacional no Modal."
    )
    parser.add_argument("--profile", default="jorhinhogames", help="Perfil Modal a usar.")
    parser.add_argument("--no-deploy", action="store_true", help="Atualiza apenas o Secret, sem redeploy.")
    args = parser.parse_args()

    modal = shutil.which("modal")
    if not modal:
        print("ERRO: o comando 'modal' nao foi encontrado no PATH.", file=sys.stderr)
        return 2

    if not DEPLOY_FILE.exists():
        print(f"ERRO: arquivo de deploy nao encontrado: {DEPLOY_FILE}", file=sys.stderr)
        return 2

    try:
        keys = load_keys()
    except ValueError as exc:
        print(f"ERRO: {exc}", file=sys.stderr)
        return 2

    payload = {
        "COHERE_API_KEY": keys[0],
        "COHERE_API_KEY_2": keys[1],
        "COHERE_API_KEY_3": keys[2],
    }

    temp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            suffix=".json",
            prefix="prumo-cohere-",
            delete=False,
        ) as handle:
            json.dump(payload, handle)
            temp_path = Path(handle.name)

        run([modal, "profile", "activate", args.profile])
        run([
            modal,
            "secret",
            "create",
            "--force",
            "--from-json",
            str(temp_path),
            SECRET_NAME,
        ])
    except subprocess.CalledProcessError as exc:
        print(f"ERRO: o Modal retornou codigo {exc.returncode}.", file=sys.stderr)
        return exc.returncode or 1
    finally:
        keys[:] = ["", "", ""]
        payload.clear()
        if temp_path is not None:
            temp_path.unlink(missing_ok=True)

    print("Secret atualizado com 3 chaves.")

    if not args.no_deploy:
        try:
            run([modal, "deploy", str(DEPLOY_FILE.relative_to(ROOT))])
        except subprocess.CalledProcessError as exc:
            print(
                "O Secret foi atualizado, mas o deploy falhou. Rode novamente ou execute: "
                r"modal deploy deploy\modal_portal_nacional_solver.py",
                file=sys.stderr,
            )
            return exc.returncode or 1

    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=30) as response:
            health = json.loads(response.read().decode("utf-8"))
        configured = (health.get("cohere_keys") or {}).get("configured_keys")
        version = health.get("version")
        print(f"Health OK. Versao: {version}. Chaves configuradas detectadas: {configured}.")
    except Exception as exc:
        print(f"Atualizacao concluida. Nao consegui confirmar o health agora: {exc}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
