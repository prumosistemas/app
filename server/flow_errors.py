#!/usr/bin/env python3

import logging
import os
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from flow_core import FlowContext

logger = logging.getLogger("iss.errors")


class FlowError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        short_message: Optional[str] = None,
        action: str = "",
        retryable: bool = False,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.short_message = short_message or message
        self.action = action
        self.retryable = retryable

    def __str__(self) -> str:
        return f"{self.code}: {self.message}"


class LoginError(FlowError):
    def __init__(self, msg: str):
        super().__init__(
            "LOGIN_ERROR",
            msg,
            short_message="Falha de login no portal ISS.",
            action="Validar usuário, senha e bloqueios de autenticação.",
            retryable=False,
        )


class PortalAccessBlockedError(FlowError):
    def __init__(self, detail: str = ""):
        msg = detail.strip() or "Portal ISS bloqueou a origem de rede antes do login."
        super().__init__(
            "PORTAL_ACCESS_BLOCKED",
            msg,
            short_message="Portal ISS bloqueou o IP/origem antes do login.",
            action="Usar Browserless local ou um proxy/IP liberado pelo portal; nao usar Modal direto para login enquanto a SEFIN bloquear a origem.",
            retryable=False,
        )


class CnpjInexistenteError(FlowError):
    def __init__(self, cnpj: str):
        super().__init__(
            "CNPJ_INEXISTENTE",
            f"CNPJ não encontrado: {cnpj}",
            short_message=f"CNPJ não encontrado no portal: {cnpj}",
            action="Validar se o CNPJ está cadastrado ou se foi digitado corretamente.",
            retryable=False,
        )


class CnpjMismatchError(FlowError):
    def __init__(self, esperado: str, encontrado: str):
        super().__init__(
            "CNPJ_MISMATCH",
            f"CNPJ esperado={esperado}, retornado={encontrado}",
            short_message="O CNPJ retornado pelo portal não corresponde ao pesquisado.",
            action="Repetir a pesquisa; se persistir, revisar máscara e resultado retornado pela tabela.",
            retryable=True,
        )


class MensagemTelaError(FlowError):
    def __init__(self, assunto: str = "Mensagem na tela", data_msg: str = ""):
        detalhe = (assunto or "Mensagem na tela").strip()
        if data_msg:
            detalhe = f"{detalhe} | {data_msg.strip()}"

        super().__init__(
            "MENSAGEM_NA_TELA",
            detalhe,
            short_message="Erro de mensagem na tela.",
            action="Verificar a mensagem pendente no portal e tratar manualmente antes de repetir a automação.",
            retryable=False,
        )


@dataclass(frozen=True)
class ErrorSpec:
    code: str
    short_message: str
    action: str
    retryable: bool
    capture_screenshot: bool = True


def classify_exception(exc: Exception) -> ErrorSpec:
    if isinstance(exc, FlowError):
        return ErrorSpec(
            code=exc.code,
            short_message=exc.short_message,
            action=exc.action,
            retryable=exc.retryable,
            capture_screenshot=exc.code != "ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA",
        )

    text = str(exc or "").strip()
    text_low = text.lower()
    exc_name = type(exc).__name__.lower()

    if exc_name == "timeouterror":
        return ErrorSpec(
            code="TIMEOUT",
            short_message="Tempo excedido durante a execução do fluxo.",
            action="Verificar lentidão do portal e considerar aumentar os timeouts.",
            retryable=True,
        )

    if "net::err_connection_timed_out" in text_low or "net::err_timed_out" in text_low:
        return ErrorSpec(
            code="NETWORK_TIMEOUT",
            short_message="Timeout de conexão ao acessar o portal ISS.",
            action="Verificar internet, VPN, firewall, proxy ou indisponibilidade do portal.",
            retryable=True,
        )

    transient_network_markers = (
        "net::err_connection_reset",
        "net::err_connection_closed",
        "net::err_empty_response",
        "net::err_network_changed",
        "net::err_http2_protocol_error",
        "net::err_aborted",
        "connection reset",
        "connection closed",
    )
    if any(marker in text_low for marker in transient_network_markers):
        return ErrorSpec(
            code="NETWORK_ERROR",
            short_message="Falha transitória de rede ao acessar o portal ISS.",
            action="Repetir o fluxo; se persistir, verificar portal, proxy e estabilidade do CDP.",
            retryable=True,
        )

    if "chrome-error://chromewebdata" in text_low or "interrupted by another navigation" in text_low:
        return ErrorSpec(
            code="NETWORK_NAVIGATION_ERROR",
            short_message="Falha de navegação ao acessar o portal ISS.",
            action="Verificar proxy, disponibilidade do portal e repetir o fluxo.",
            retryable=True,
        )

    if "execution context was destroyed" in text_low or "most likely because of a navigation" in text_low:
        return ErrorSpec(
            code="NAVIGATION_RACE",
            short_message="O portal recarregou a tela durante uma ação da automação.",
            action="Repetir o fluxo; se persistir, aumentar esperas do passo afetado.",
            retryable=True,
        )

    if "net::err_name_not_resolved" in text_low:
        return ErrorSpec(
            code="DNS_ERROR",
            short_message="Não foi possível resolver o endereço do portal ISS.",
            action="Verificar DNS, internet e bloqueios locais de rede.",
            retryable=True,
        )

    if "net::err_connection_refused" in text_low:
        return ErrorSpec(
            code="CONNECTION_REFUSED",
            short_message="Conexão recusada pelo portal ISS.",
            action="Verificar indisponibilidade do portal ou bloqueio de rede.",
            retryable=True,
        )

    if "net::err_internet_disconnected" in text_low:
        return ErrorSpec(
            code="INTERNET_DISCONNECTED",
            short_message="Sem conexão com a internet durante o acesso ao portal ISS.",
            action="Restabelecer a conexão e executar novamente.",
            retryable=True,
        )

    if "429" in text_low or "too many requests" in text_low:
        return ErrorSpec(
            code="BROWSERLESS_BUSY",
            short_message="Browserless recusou novas sessões por excesso de concorrência.",
            action="Alinhar MAX_BROWSERS da API com MAX_CONCURRENT_SESSIONS do Browserless e executar retry.",
            retryable=True,
        )

    if "timeout" in text_low:
        return ErrorSpec(
            code="TIMEOUT",
            short_message="Tempo excedido durante a execução do fluxo.",
            action="Verificar lentidão do portal e considerar aumentar os timeouts.",
            retryable=True,
        )

    if "target page, context or browser has been closed" in text_low:
        return ErrorSpec(
            code="BROWSER_CLOSED",
            short_message="Página ou navegador foi fechado durante a execução.",
            action="Verificar encerramento manual, crash do navegador ou falha de contexto.",
            retryable=True,
        )

    return ErrorSpec(
        code="UNEXPECTED",
        short_message="Erro inesperado durante a execução do fluxo.",
        action="Consultar os logs detalhados da run.",
        retryable=False,
    )


async def save_error_screenshot(page, cnpj_dir: str) -> Optional[str]:
    if page is None:
        return None

    os.makedirs(cnpj_dir, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    path = os.path.join(cnpj_dir, f"erro_{stamp}.png")
    await page.screenshot(path=path, full_page=True)
    return path


async def handle_job_error(ctx: "FlowContext", page, exc: Exception) -> None:
    from flow_core import log_flow

    spec = classify_exception(exc)
    log_level = logging.WARNING if spec.code == "ESCRITURACAO_FECHADA_REABERTURA_DESATIVADA" else logging.ERROR
    event_prefix = "WARN" if log_level == logging.WARNING else "ERROR"

    await log_flow(
        ctx,
        f"Erro classificado: {spec.short_message}",
        event=event_prefix,
        level=log_level,
        code=spec.code,
    )

    await log_flow(
        ctx,
        f"Ação sugerida: {spec.action}",
        event=f"{event_prefix}_ACTION",
        level=log_level,
        code=spec.code,
    )

    await log_flow(
        ctx,
        f"Detalhe técnico: {type(exc).__name__}: {exc}",
        event=f"{event_prefix}_DETAIL",
        level=log_level,
        code=spec.code,
    )

    if spec.capture_screenshot:
        try:
            shot = await save_error_screenshot(page, ctx.config.cnpj_dir)
            if shot:
                await log_flow(
                    ctx,
                    f"Screenshot de erro salvo em: {shot}",
                    event="ERROR_SCREENSHOT",
                    level=logging.ERROR,
                    code=spec.code,
                )
        except Exception as shot_err:
            await log_flow(
                ctx,
                f"Falha ao salvar screenshot: {type(shot_err).__name__}: {shot_err}",
                event="ERROR_SCREENSHOT",
                level=logging.ERROR,
                code="SCREENSHOT_ERROR",
            )
