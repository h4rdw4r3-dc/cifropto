"""
github_webhook.py
=================
Servidor de webhook do GitHub para o bot Discord.
Recebe eventos do GitHub (push, pull_request, issues, etc.) via HTTP POST,
valida a assinatura HMAC-SHA256 e envia resumos inteligentes no canal de auditoria.

Dependências:
    aiohttp>=3.13.5
    openai>=1.0.0          (resumo de diffs/commits via Groq ou OpenAI)

Variáveis de ambiente necessárias (lidas pelo bot):
    GITHUB_WEBHOOK_SECRET  — segredo configurado no GitHub repo > Settings > Webhooks
    PORT                   — porta HTTP onde escutar (padrão: 8080)

No GitHub, configure o Payload URL como:
    https://<seu-domínio-railway>.up.railway.app/github
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
from typing import Optional, TYPE_CHECKING

log = logging.getLogger("shell")

try:
    from aiohttp import web as _web
    AIOHTTP_OK = True
except ImportError:
    AIOHTTP_OK = False
    log.warning("[WEBHOOK] aiohttp não instalado — servidor de webhook indisponível.")

try:
    from openai import AsyncOpenAI
    OPENAI_OK = True
except ImportError:
    OPENAI_OK = False

if TYPE_CHECKING:
    import discord
    from memoria_vetorial import MemoriaVetorial


# ── Mapeamento de eventos para emoji/descrição ────────────────────────────────
_EVENTOS = {
    "push":              ("📦", "Push"),
    "pull_request":      ("🔀", "Pull Request"),
    "issues":            ("🐛", "Issue"),
    "issue_comment":     ("💬", "Comentário"),
    "create":            ("🌿", "Branch/Tag criado"),
    "delete":            ("🗑️", "Branch/Tag deletado"),
    "release":           ("🚀", "Release"),
    "workflow_run":      ("⚙️", "Workflow"),
    "check_run":         ("✅", "Check"),
    "star":              ("⭐", "Star"),
    "fork":              ("🍴", "Fork"),
    "member":            ("👤", "Membro"),
    "repository":        ("📁", "Repositório"),
    "deployment":        ("🚢", "Deploy"),
    "deployment_status": ("📊", "Status de deploy"),
    "ping":              ("🏓", "Ping"),
}


class WebhookServer:
    """
    Servidor HTTP assíncrono que recebe webhooks do GitHub.

    Parâmetros:
        secret         — GITHUB_WEBHOOK_SECRET (validação HMAC)
        port           — porta TCP onde escutar
        groq_key       — chave Groq/OpenAI para resumos inteligentes (opcional)
        canal_discord  — canal discord.TextChannel onde postar notificações
        mem            — instância de MemoriaVetorial para ingerir eventos (opcional)
    """

    def __init__(
        self,
        secret: str,
        port: int = 8080,
        groq_key: str = "",
        canal_discord: Optional["discord.TextChannel"] = None,
        mem: Optional["MemoriaVetorial"] = None,
    ):
        self._secret = secret.encode() if isinstance(secret, str) else secret
        self._port   = port
        self._canal  = canal_discord
        self._mem    = mem
        self._runner: Optional["_web.AppRunner"] = None

        self._oai: Optional["AsyncOpenAI"] = None
        if OPENAI_OK and groq_key:
            try:
                self._oai = AsyncOpenAI(
                    api_key=groq_key,
                    base_url="https://api.groq.com/openai/v1",
                )
            except Exception as e:
                log.debug(f"[WEBHOOK] Falha ao inicializar cliente IA: {e}")

    # ── Ciclo de vida ─────────────────────────────────────────────────────────

    async def iniciar(self) -> None:
        """Inicia o servidor aiohttp. Chame com asyncio.ensure_future()."""
        if not AIOHTTP_OK:
            log.warning("[WEBHOOK] aiohttp indisponível — servidor não iniciado.")
            return

        app = _web.Application()
        app.router.add_post("/github", self._handle_github)
        app.router.add_get("/health", self._handle_health)

        self._runner = _web.AppRunner(app)
        await self._runner.setup()
        site = _web.TCPSite(self._runner, "0.0.0.0", self._port)
        await site.start()
        log.info(f"[WEBHOOK] Servidor GitHub escutando em 0.0.0.0:{self._port}/github")

    async def parar(self) -> None:
        """Desliga o servidor (opcional, para shutdown limpo)."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            log.info("[WEBHOOK] Servidor GitHub encerrado.")

    # ── Handlers HTTP ─────────────────────────────────────────────────────────

    async def _handle_health(self, request: "_web.Request") -> "_web.Response":
        """Endpoint de health-check para o Railway."""
        return _web.Response(text="ok", status=200)

    async def _handle_github(self, request: "_web.Request") -> "_web.Response":
        """Recebe, valida e processa o payload do GitHub."""
        body = await request.read()

        # Validação HMAC-SHA256
        sig_header = request.headers.get("X-Hub-Signature-256", "")
        if not self._validar_assinatura(body, sig_header):
            log.warning("[WEBHOOK] Assinatura inválida — payload rejeitado.")
            return _web.Response(text="Unauthorized", status=401)

        evento = request.headers.get("X-GitHub-Event", "unknown")
        delivery = request.headers.get("X-GitHub-Delivery", "?")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return _web.Response(text="Bad Request", status=400)

        log.info(f"[WEBHOOK] Evento recebido: {evento} (delivery={delivery})")

        # Processa de forma assíncrona para não travar o handler
        asyncio.ensure_future(self._processar_evento(evento, payload))

        return _web.Response(text="ok", status=200)

    # ── Validação HMAC ────────────────────────────────────────────────────────

    def _validar_assinatura(self, body: bytes, sig_header: str) -> bool:
        """Valida X-Hub-Signature-256 enviada pelo GitHub."""
        if not sig_header.startswith("sha256="):
            return False
        expected = "sha256=" + hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(expected, sig_header)

    # ── Processamento de eventos ──────────────────────────────────────────────

    async def _processar_evento(self, evento: str, payload: dict) -> None:
        """Despacha o evento para o formatador correto e envia no Discord."""
        try:
            mensagem = await self._formatar_evento(evento, payload)
            if mensagem and self._canal:
                # Divide mensagens longas em partes de 2000 chars
                for parte in self._dividir(mensagem, 1990):
                    await self._canal.send(parte)

            # Ingere na memória vetorial se disponível
            if self._mem and mensagem:
                repo = payload.get("repository", {}).get("full_name", "github")
                asyncio.ensure_future(
                    self._mem.ingerir_mensagem(
                        canal_id=0,
                        canal_nome=f"github:{repo}",
                        autor_id=0,
                        autor_nome="GitHub",
                        conteudo=mensagem[:1500],
                    )
                )
        except Exception as e:
            log.warning(f"[WEBHOOK] Erro ao processar evento '{evento}': {e}")

    async def _formatar_evento(self, evento: str, payload: dict) -> str:
        """Retorna string formatada para o Discord com base no tipo de evento."""
        emoji, label = _EVENTOS.get(evento, ("🔔", evento.replace("_", " ").title()))
        repo = payload.get("repository", {})
        repo_nome = repo.get("full_name", "?")
        repo_url  = repo.get("html_url", "")

        # ── Ping (teste de configuração) ──────────────────────────────────────
        if evento == "ping":
            zen = payload.get("zen", "")
            return f"🏓 **Webhook configurado!** `{repo_nome}`\n> _{zen}_"

        # ── Push ──────────────────────────────────────────────────────────────
        if evento == "push":
            return await self._formatar_push(payload, repo_nome, repo_url)

        # ── Pull Request ──────────────────────────────────────────────────────
        if evento == "pull_request":
            return self._formatar_pr(payload, repo_nome)

        # ── Issues ────────────────────────────────────────────────────────────
        if evento == "issues":
            return self._formatar_issue(payload, repo_nome)

        # ── Issue Comment ─────────────────────────────────────────────────────
        if evento == "issue_comment":
            return self._formatar_issue_comment(payload, repo_nome)

        # ── Release ───────────────────────────────────────────────────────────
        if evento == "release":
            return self._formatar_release(payload, repo_nome)

        # ── Workflow Run ──────────────────────────────────────────────────────
        if evento == "workflow_run":
            return self._formatar_workflow(payload, repo_nome)

        # ── Create / Delete (branches/tags) ───────────────────────────────────
        if evento in ("create", "delete"):
            ref_type = payload.get("ref_type", "ref")
            ref      = payload.get("ref", "?")
            sender   = payload.get("sender", {}).get("login", "?")
            acao     = "criou" if evento == "create" else "deletou"
            return f"{emoji} **{sender}** {acao} {ref_type} `{ref}` em **{repo_nome}**"

        # ── Star ──────────────────────────────────────────────────────────────
        if evento == "star":
            sender = payload.get("sender", {}).get("login", "?")
            stars  = repo.get("stargazers_count", "?")
            return f"⭐ **{sender}** deu star em **{repo_nome}** — total: {stars} ⭐"

        # ── Fork ──────────────────────────────────────────────────────────────
        if evento == "fork":
            forkee = payload.get("forkee", {})
            sender = payload.get("sender", {}).get("login", "?")
            return f"🍴 **{sender}** fez fork de **{repo_nome}** → `{forkee.get('full_name', '?')}`"

        # ── Evento genérico ───────────────────────────────────────────────────
        sender = payload.get("sender", {}).get("login", "?")
        action = payload.get("action", "")
        acao_str = f" ({action})" if action else ""
        return f"{emoji} **{label}**{acao_str} em **{repo_nome}** por `{sender}`"

    # ── Formatadores específicos ──────────────────────────────────────────────

    async def _formatar_push(self, payload: dict, repo_nome: str, repo_url: str) -> str:
        ref    = payload.get("ref", "").replace("refs/heads/", "")
        pusher = payload.get("pusher", {}).get("name", "?")
        commits: list[dict] = payload.get("commits", [])
        compare_url = payload.get("compare", "")

        if not commits:
            return f"📦 **Push** em `{ref}` de **{repo_nome}** por `{pusher}` (sem commits)"

        n = len(commits)
        linhas = [f"📦 **Push** em `{ref}` de **{repo_nome}** por `{pusher}` — {n} commit(s)"]
        if compare_url:
            linhas[0] += f"\n🔗 {compare_url}"

        # Lista até 5 commits
        for c in commits[:5]:
            sha   = c.get("id", "")[:7]
            msg   = c.get("message", "").split("\n")[0][:80]
            autor = c.get("author", {}).get("name", "?")
            url   = c.get("url", "")
            linha = f"• `{sha}` {msg} — _{autor}_"
            if url:
                linha += f" [→]({url})"
            linhas.append(linha)

        if n > 5:
            linhas.append(f"_…e mais {n - 5} commits._")

        texto = "\n".join(linhas)

        # Resumo inteligente dos diffs via IA (só se poucos commits)
        if self._oai and n <= 10:
            resumo = await self._resumir_push(commits)
            if resumo:
                texto += f"\n\n**Resumo das mudanças:** {resumo}"

        return texto

    async def _resumir_push(self, commits: list[dict]) -> str:
        """Gera um resumo em linguagem natural das mudanças do push."""
        msgs = [c.get("message", "").split("\n")[0] for c in commits]
        # Limita o texto total enviado para evitar erro 413
        texto_commits = "\n".join(f"- {m}" for m in msgs if m)[:2000]
        
        if not texto_commits:
            return ""
        try:
            resp = await self._oai.chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=80,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você resume commit messages de Git em 1 frase clara em português, "
                            "descrevendo o que foi feito. Seja direto e técnico."
                        ),
                    },
                    {"role": "user", "content": texto_commits},
                ],
            )
            return resp.choices[0].message.content.strip()
        except Exception as e:
            log.warning(f"[WEBHOOK] Erro ao resumir com IA: {e}")
            return ""

    def _formatar_pr(self, payload: dict, repo_nome: str) -> str:
        action = payload.get("action", "")
        pr     = payload.get("pull_request", {})
        titulo = pr.get("title", "?")
        numero = pr.get("number", "?")
        autor  = pr.get("user", {}).get("login", "?")
        url    = pr.get("html_url", "")
        base   = pr.get("base", {}).get("ref", "?")
        head   = pr.get("head", {}).get("ref", "?")
        body   = (pr.get("body") or "")[:200]

        emojis_acao = {
            "opened":      "🟢 Aberto",
            "closed":      "🔴 Fechado" + (" (merged)" if pr.get("merged") else ""),
            "reopened":    "🔄 Reaberto",
            "review_requested": "👀 Review solicitado",
            "ready_for_review": "✅ Pronto para review",
            "synchronize": "🔃 Atualizado",
        }
        acao_str = emojis_acao.get(action, action)

        linhas = [
            f"🔀 **PR #{numero}** {acao_str} — **{repo_nome}**",
            f"**{titulo}**",
            f"`{head}` → `{base}` | por `{autor}`",
        ]
        if url:
            linhas.append(f"🔗 {url}")
        if body and action == "opened":
            linhas.append(f"_{body.strip()}_")

        return "\n".join(linhas)

    def _formatar_issue(self, payload: dict, repo_nome: str) -> str:
        action = payload.get("action", "")
        issue  = payload.get("issue", {})
        titulo = issue.get("title", "?")
        numero = issue.get("number", "?")
        autor  = issue.get("user", {}).get("login", "?")
        url    = issue.get("html_url", "")
        labels = ", ".join(lb.get("name", "") for lb in issue.get("labels", []))

        emojis_acao = {
            "opened":   "🟢 Aberta",
            "closed":   "🔴 Fechada",
            "reopened": "🔄 Reaberta",
            "assigned": "👤 Atribuída",
            "labeled":  "🏷️ Rotulada",
        }
        acao_str = emojis_acao.get(action, action)

        linhas = [
            f"🐛 **Issue #{numero}** {acao_str} — **{repo_nome}**",
            f"**{titulo}** por `{autor}`",
        ]
        if labels:
            linhas.append(f"🏷️ {labels}")
        if url:
            linhas.append(f"🔗 {url}")

        return "\n".join(linhas)

    def _formatar_issue_comment(self, payload: dict, repo_nome: str) -> str:
        issue   = payload.get("issue", {})
        comment = payload.get("comment", {})
        numero  = issue.get("number", "?")
        titulo  = issue.get("title", "?")
        autor   = comment.get("user", {}).get("login", "?")
        corpo   = (comment.get("body") or "")[:300].strip()
        url     = comment.get("html_url", "")

        linhas = [
            f"💬 **{autor}** comentou na **Issue #{numero}** — **{repo_nome}**",
            f"_{titulo}_",
        ]
        if corpo:
            linhas.append(f"> {corpo[:200]}")
        if url:
            linhas.append(f"🔗 {url}")

        return "\n".join(linhas)

    def _formatar_release(self, payload: dict, repo_nome: str) -> str:
        action  = payload.get("action", "")
        release = payload.get("release", {})
        nome    = release.get("name") or release.get("tag_name", "?")
        autor   = release.get("author", {}).get("login", "?")
        url     = release.get("html_url", "")
        pre     = release.get("prerelease", False)
        body    = (release.get("body") or "")[:300].strip()

        tipo = "🧪 Pré-release" if pre else "🚀 Release"
        linhas = [
            f"{tipo} **{nome}** ({action}) — **{repo_nome}** por `{autor}`",
        ]
        if body:
            linhas.append(f"_{body[:200]}_")
        if url:
            linhas.append(f"🔗 {url}")

        return "\n".join(linhas)

    def _formatar_workflow(self, payload: dict, repo_nome: str) -> str:
        wf     = payload.get("workflow_run", {})
        nome   = wf.get("name", "?")
        status = wf.get("status", "?")
        concl  = wf.get("conclusion", "")
        branch = wf.get("head_branch", "?")
        url    = wf.get("html_url", "")

        status_emoji = {
            "success":   "✅",
            "failure":   "❌",
            "cancelled": "⏹️",
            "skipped":   "⏭️",
            "in_progress": "🔄",
            "queued":    "⏳",
        }
        e = status_emoji.get(concl or status, "⚙️")

        linha = f"{e} **Workflow** `{nome}` — `{branch}` em **{repo_nome}**"
        if concl:
            linha += f" → **{concl}**"
        if url:
            linha += f"\n🔗 {url}"

        return linha

    # ── Utilitário ────────────────────────────────────────────────────────────

    @staticmethod
    def _dividir(texto: str, max_len: int) -> list[str]:
        """Divide texto em partes de até max_len caracteres."""
        partes = []
        while len(texto) > max_len:
            corte = texto.rfind("\n", 0, max_len)
            if corte == -1:
                corte = max_len
            partes.append(texto[:corte])
            texto = texto[corte:].lstrip("\n")
        if texto:
            partes.append(texto)
        return partes
