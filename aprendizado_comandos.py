"""
aprendizado_comandos.py
=======================
Módulo de aprendizado inteligente de comandos para o bot Shell (Discord).

Usa o PostgreSQL já provisionado no Railway (DATABASE_URL) para persistir:
  - comandos observados de outros bots (prefixo, slash, frequência)
  - comandos internos digitados pelos proprietários/colaboradores
  - contexto de execução (quem pediu, quando, resultado, canal)
  - cruzamento de dados: padrões de uso, horário, autor, sucesso/falha

Schema: tabelas separadas, Foreign Keys para cruzamento eficiente.

Dependências: asyncpg (já usado pelo memoria_vetorial.py)
Variável de ambiente: DATABASE_URL (Railway provê automaticamente)
"""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import Optional

log = logging.getLogger("shell")

try:
    import asyncpg
    _ASYNCPG_OK = True
except ImportError:
    _ASYNCPG_OK = False
    log.warning("[APREND] asyncpg não instalado — aprendizado de comandos desativado.")

# ── SQL: criação das tabelas ──────────────────────────────────────────────────

_SQL_INIT = """
-- Bots conhecidos no servidor (catálogo dinâmico)
CREATE TABLE IF NOT EXISTS bots_conhecidos (
    id          BIGSERIAL   PRIMARY KEY,
    nome        TEXT        NOT NULL UNIQUE,
    prefixo     TEXT        NOT NULL DEFAULT '!',
    usa_slash   BOOLEAN     NOT NULL DEFAULT FALSE,
    usos_total  INTEGER     NOT NULL DEFAULT 0,
    visto_em    TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Comandos aprendidos por observação de humanos e respostas de bots
CREATE TABLE IF NOT EXISTS comandos_bots (
    id              BIGSERIAL   PRIMARY KEY,
    bot_nome        TEXT        NOT NULL,
    comando         TEXT        NOT NULL,
    prefixo         TEXT,
    eh_slash        BOOLEAN     NOT NULL DEFAULT FALSE,
    usos            INTEGER     NOT NULL DEFAULT 1,
    ultimo_uso      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    primeiro_visto  TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    funcionou       BOOLEAN,       -- NULL = desconhecido, TRUE/FALSE = confirmado
    UNIQUE (bot_nome, comando)
);

-- Execuções de comandos internos do Shell (ordens dos donos/colaboradores)
CREATE TABLE IF NOT EXISTS execucoes_internas (
    id              BIGSERIAL   PRIMARY KEY,
    canal_id        BIGINT      NOT NULL,
    canal_nome      TEXT        NOT NULL DEFAULT '',
    autor_id        BIGINT      NOT NULL,
    autor_nome      TEXT        NOT NULL DEFAULT '',
    nivel_hierarq   TEXT        NOT NULL DEFAULT 'membro',  -- dono/colaborador/moderador/membro
    comando_raw     TEXT        NOT NULL,   -- texto original digitado
    comando_parsed  TEXT,                  -- comando identificado (ex: "silenciar")
    alvo_nome       TEXT,                  -- quem foi afetado (se houver)
    resultado       TEXT,                  -- "ok" | "erro" | "ignorado"
    detalhe         TEXT,                  -- mensagem de erro ou confirmação
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Padrões aprendidos: associação inteligente de intenção → comando
CREATE TABLE IF NOT EXISTS padroes_aprendidos (
    id          BIGSERIAL   PRIMARY KEY,
    gatilho     TEXT        NOT NULL UNIQUE,  -- texto/frase que ativa
    bot_nome    TEXT,                         -- NULL = comando interno do Shell
    comando     TEXT        NOT NULL,
    prefixo     TEXT,
    confianca   FLOAT       NOT NULL DEFAULT 0.5,  -- 0.0 a 1.0
    usos        INTEGER     NOT NULL DEFAULT 1,
    ultimo_uso  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Índices para cruzamento rápido
CREATE INDEX IF NOT EXISTS idx_cmdbots_nome      ON comandos_bots (bot_nome);
CREATE INDEX IF NOT EXISTS idx_cmdbots_uso        ON comandos_bots (usos DESC);
CREATE INDEX IF NOT EXISTS idx_exec_canal         ON execucoes_internas (canal_id);
CREATE INDEX IF NOT EXISTS idx_exec_autor         ON execucoes_internas (autor_id);
CREATE INDEX IF NOT EXISTS idx_exec_ts            ON execucoes_internas (ts DESC);
CREATE INDEX IF NOT EXISTS idx_exec_cmd           ON execucoes_internas (comando_parsed);
CREATE INDEX IF NOT EXISTS idx_padroes_gatilho    ON padroes_aprendidos (gatilho);
CREATE INDEX IF NOT EXISTS idx_padroes_confianca  ON padroes_aprendidos (confianca DESC);
"""


class AprendizadoComandos:
    """
    Gerencia o aprendizado persistente de comandos via PostgreSQL.

    Uso (em on_ready):
        aprend = AprendizadoComandos(db_url=DATABASE_URL)
        await aprend.inicializar()

    No on_message (humano usou comando de bot):
        await aprend.registrar_cmd_bot("Mudae", "$", "im", eh_slash=False)

    Quando Shell executa uma ordem:
        await aprend.registrar_execucao_interna(...)

    Para consultar o melhor comando de um bot:
        info = await aprend.consultar_bot("Mudae")
    """

    def __init__(self, db_url: str):
        self._db_url = db_url
        self._pool: Optional["asyncpg.Pool"] = None
        # Cache local (evita hits ao banco em cada mensagem)
        self._cache_bots: dict[str, dict] = {}
        self._cache_ts: float = 0.0
        self._CACHE_TTL = 120  # segundos

    # ── Inicialização ─────────────────────────────────────────────────────────

    async def inicializar(self) -> None:
        if not _ASYNCPG_OK:
            raise RuntimeError("asyncpg não disponível.")
        self._pool = await asyncpg.create_pool(
            self._db_url,
            min_size=1,
            max_size=3,
            command_timeout=30,
            ssl="require",
        )
        async with self._pool.acquire() as conn:
            await conn.execute(_SQL_INIT)
        log.info("[APREND] Banco de aprendizado inicializado.")

    # ── Registro de comando de bot observado ──────────────────────────────────

    async def registrar_cmd_bot(
        self,
        bot_nome: str,
        prefixo: Optional[str],
        comando: str,
        eh_slash: bool = False,
        funcionou: Optional[bool] = None,
    ) -> None:
        """
        Registra (ou atualiza) um comando observado de um bot externo.
        Chamado sempre que um humano usa um comando e o bot responde.
        """
        if not self._pool or not bot_nome or not comando:
            return
        nome = bot_nome.lower().strip()
        cmd  = comando.lower().strip()
        pref = prefixo.strip() if prefixo else None
        try:
            async with self._pool.acquire() as conn:
                # Upsert em comandos_bots
                await conn.execute("""
                    INSERT INTO comandos_bots (bot_nome, comando, prefixo, eh_slash, usos, ultimo_uso, funcionou)
                    VALUES ($1, $2, $3, $4, 1, NOW(), $5)
                    ON CONFLICT (bot_nome, comando) DO UPDATE
                        SET usos       = comandos_bots.usos + 1,
                            ultimo_uso = NOW(),
                            prefixo    = COALESCE($3, comandos_bots.prefixo),
                            funcionou  = COALESCE($5, comandos_bots.funcionou)
                """, nome, cmd, pref, eh_slash, funcionou)

                # Upsert em bots_conhecidos
                await conn.execute("""
                    INSERT INTO bots_conhecidos (nome, prefixo, usa_slash, usos_total)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (nome) DO UPDATE
                        SET usos_total = bots_conhecidos.usos_total + 1,
                            prefixo    = COALESCE($2, bots_conhecidos.prefixo),
                            usa_slash  = bots_conhecidos.usa_slash OR $3,
                            atualizado = NOW()
                """, nome, pref or "!", eh_slash)

            self._cache_ts = 0  # invalida cache
        except Exception as e:
            log.debug(f"[APREND] Erro ao registrar cmd bot: {e}")

    # ── Registro de execução interna (ordem de dono/colaborador) ─────────────

    async def registrar_execucao_interna(
        self,
        canal_id: int,
        canal_nome: str,
        autor_id: int,
        autor_nome: str,
        nivel_hierarq: str,
        comando_raw: str,
        comando_parsed: Optional[str] = None,
        alvo_nome: Optional[str] = None,
        resultado: str = "ok",
        detalhe: Optional[str] = None,
    ) -> None:
        """
        Grava cada execução de ordem interna para análise e aprendizado.
        Permite ao bot entender padrões: quem pede o quê, quando, com qual resultado.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO execucoes_internas
                        (canal_id, canal_nome, autor_id, autor_nome, nivel_hierarq,
                         comando_raw, comando_parsed, alvo_nome, resultado, detalhe)
                    VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)
                """, canal_id, canal_nome, autor_id, autor_nome, nivel_hierarq,
                    comando_raw[:500], comando_parsed, alvo_nome, resultado,
                    (detalhe or "")[:300])
        except Exception as e:
            log.debug(f"[APREND] Erro ao registrar execução: {e}")

    # ── Registrar padrão aprendido (intenção → comando) ───────────────────────

    async def registrar_padrao(
        self,
        gatilho: str,
        comando: str,
        bot_nome: Optional[str] = None,
        prefixo: Optional[str] = None,
        confianca: float = 0.7,
    ) -> None:
        """
        Aprende a associação: 'quando alguém diz X, o comando correto é Y'.
        Ex: gatilho='pegar personagem', bot='Mudae', comando='$im', confiança=0.9
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO padroes_aprendidos (gatilho, bot_nome, comando, prefixo, confianca)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (gatilho) DO UPDATE
                        SET usos       = padroes_aprendidos.usos + 1,
                            confianca  = LEAST(1.0, padroes_aprendidos.confianca + 0.05),
                            ultimo_uso = NOW()
                """, gatilho.lower()[:200], bot_nome, comando, prefixo, confianca)
        except Exception as e:
            log.debug(f"[APREND] Erro ao registrar padrão: {e}")

    # ── Consultas ─────────────────────────────────────────────────────────────

    async def consultar_bot(self, bot_nome: str) -> dict:
        """
        Retorna o perfil completo de um bot: prefixo, comandos mais usados,
        slash commands, taxa de sucesso.
        """
        if not self._pool:
            return {}
        nome = bot_nome.lower().strip()

        # Cache válido?
        import time
        if (time.time() - self._cache_ts) < self._CACHE_TTL and nome in self._cache_bots:
            return self._cache_bots[nome]

        try:
            async with self._pool.acquire() as conn:
                # Info geral do bot
                row_bot = await conn.fetchrow(
                    "SELECT * FROM bots_conhecidos WHERE nome = $1", nome
                )
                # Comandos mais usados (top 20)
                rows_cmd = await conn.fetch("""
                    SELECT comando, prefixo, eh_slash, usos, funcionou
                    FROM comandos_bots
                    WHERE bot_nome = $1
                    ORDER BY usos DESC
                    LIMIT 20
                """, nome)

            resultado = {
                "nome": nome,
                "prefixo": row_bot["prefixo"] if row_bot else "!",
                "usa_slash": row_bot["usa_slash"] if row_bot else False,
                "usos_total": row_bot["usos_total"] if row_bot else 0,
                "comandos": [
                    {
                        "cmd": r["comando"],
                        "prefixo": r["prefixo"],
                        "slash": r["eh_slash"],
                        "usos": r["usos"],
                        "funcionou": r["funcionou"],
                    }
                    for r in rows_cmd
                ],
            }
            self._cache_bots[nome] = resultado
            self._cache_ts = time.time()
            return resultado
        except Exception as e:
            log.debug(f"[APREND] Erro ao consultar bot: {e}")
            return {}

    async def consultar_padrao(self, texto: str) -> Optional[dict]:
        """
        Busca o padrão mais confiante que corresponde ao texto.
        Usado para sugerir comandos automaticamente.
        """
        if not self._pool:
            return None
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow("""
                    SELECT * FROM padroes_aprendidos
                    WHERE $1 ILIKE '%' || gatilho || '%'
                       OR gatilho ILIKE '%' || $1 || '%'
                    ORDER BY confianca DESC, usos DESC
                    LIMIT 1
                """, texto.lower()[:200])
            if row:
                return dict(row)
        except Exception as e:
            log.debug(f"[APREND] Erro ao consultar padrão: {e}")
        return None

    async def relatorio_uso(self, limite: int = 10) -> str:
        """
        Gera um resumo dos comandos mais usados — para o dono do servidor.
        """
        if not self._pool:
            return "Banco de aprendizado não disponível."
        try:
            async with self._pool.acquire() as conn:
                rows_bots = await conn.fetch("""
                    SELECT nome, prefixo, usos_total FROM bots_conhecidos
                    ORDER BY usos_total DESC LIMIT $1
                """, limite)
                rows_cmds = await conn.fetch("""
                    SELECT bot_nome, comando, usos FROM comandos_bots
                    ORDER BY usos DESC LIMIT $1
                """, limite)
                rows_exec = await conn.fetch("""
                    SELECT comando_parsed, COUNT(*) as total,
                           SUM(CASE WHEN resultado='ok' THEN 1 ELSE 0 END) as ok
                    FROM execucoes_internas
                    WHERE ts > NOW() - INTERVAL '7 days'
                    GROUP BY comando_parsed
                    ORDER BY total DESC LIMIT $1
                """, limite)

            linhas = ["**Bots mais usados:**"]
            for r in rows_bots:
                linhas.append(f"  {r['nome']} ({r['prefixo']}) — {r['usos_total']} usos")

            linhas.append("\n**Comandos externos mais observados:**")
            for r in rows_cmds:
                linhas.append(f"  {r['bot_nome']} → {r['comando']} ({r['usos']}x)")

            linhas.append("\n**Comandos internos (últimos 7 dias):**")
            for r in rows_exec:
                cmd = r["comando_parsed"] or "?"
                linhas.append(f"  {cmd} — {r['total']}x (ok: {r['ok']})")

            return "\n".join(linhas) or "Nenhum dado ainda."
        except Exception as e:
            log.debug(f"[APREND] Erro ao gerar relatório: {e}")
            return "Erro ao gerar relatório."

    async def resumo_para_ia(self, bot_nome: str) -> str:
        """
        Gera um bloco de texto compacto para injetar no prompt da IA.
        Ex: 'Bot Mudae | prefixo $ | cmds: im(42x), daily(18x), rt(12x)'
        """
        info = await self.consultar_bot(bot_nome)
        if not info:
            return ""
        pref = info.get("prefixo", "!")
        slash = info.get("usa_slash", False)
        cmds = info.get("comandos", [])

        partes = [f"Bot {bot_nome}"]
        cmd_pref = [c for c in cmds if not c["slash"]]
        cmd_slash = [c for c in cmds if c["slash"]]

        if cmd_pref:
            top = ", ".join(
                f"{pref}{c['cmd']}({c['usos']}x)" for c in cmd_pref[:8]
            )
            partes.append(f"prefixo '{pref}': {top}")
        if slash and cmd_slash:
            top_s = ", ".join(f"/{c['cmd']}({c['usos']}x)" for c in cmd_slash[:5])
            partes.append(f"slash: {top_s}")

        return " | ".join(partes)

    async def fechar(self) -> None:
        if self._pool:
            await self._pool.close()
            self._pool = None


# ── Instância global (inicializada em on_ready) ────────────────────────────────
_aprend: Optional["AprendizadoComandos"] = None
APREND_OK: bool = False


async def inicializar_aprendizado(db_url: str) -> bool:
    """Inicializa o módulo de aprendizado. Retorna True em sucesso."""
    global _aprend, APREND_OK
    if not db_url:
        log.warning("[APREND] DATABASE_URL não definida — aprendizado desativado.")
        return False
    try:
        _aprend = AprendizadoComandos(db_url=db_url)
        await _aprend.inicializar()
        APREND_OK = True
        log.info("[APREND] Módulo de aprendizado de comandos ativo.")
        return True
    except Exception as e:
        log.warning(f"[APREND] Falha ao inicializar: {e}")
        return False


def get_aprend() -> Optional["AprendizadoComandos"]:
    """Retorna a instância global, ou None se não inicializada."""
    return _aprend if APREND_OK else None
