import sys
from pathlib import Path

import requests


SERVER_DIR = Path(__file__).resolve().parents[1] / "server"
if str(SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(SERVER_DIR))

from portal_nacional_automation import solver_response_json  # noqa: E402


def response_with(body: str) -> requests.Response:
    response = requests.Response()
    response.status_code = 200
    response._content = body.encode("utf-8")
    response.encoding = "utf-8"
    return response


def test_solver_response_accepts_regular_json() -> None:
    assert solver_response_json(response_with('{"success":true,"token":"abc"}'))["token"] == "abc"


def test_solver_response_extracts_first_object_after_keepalive() -> None:
    response = response_with('   {"success":true,"token":"abc"}\n0\r\n')
    assert solver_response_json(response)["token"] == "abc"
