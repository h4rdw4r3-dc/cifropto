"""
aprendizado_comandos.py
=======================
Módulo de aprendizado inteligente de comandos para o bot Shell (Discord).

MELHORIAS NESTA VERSÃO:
  - contexto_completo_para_ia() → bloco pronto para injetar no system prompt
    com bots conhecidos + comandos frequentes + padrões aprendidos + ordens recentes
  - registrar_ordem_dono()    → registra ordens específicas do proprietário para
    retroalimentação mais rápida (separado do registrar_execucao_interna)
  - buscar_padroes_semelhantes() → busca textual + fuzzy (sem embedding) para
    recuperar padrões mesmo sem OpenAI configurado
  - confirmar_resultado_cmd() → marca se um comando de bot funcionou ou não,
    aumentando a confiança no banco para próximas sugestões

Usa o PostgreSQL já provisionado no Railway (DATABASE_URL).
Dependências: asyncpg (já usado pelo memoria_vetorial.py)
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
    funcionou       BOOLEAN,
    UNIQUE (bot_nome, comando)
);

-- Execuções de comandos internos do Shell (ordens dos donos/colaboradores)
CREATE TABLE IF NOT EXISTS execucoes_internas (
    id              BIGSERIAL   PRIMARY KEY,
    canal_id        BIGINT      NOT NULL,
    canal_nome      TEXT        NOT NULL DEFAULT '',
    autor_id        BIGINT      NOT NULL,
    autor_nome      TEXT        NOT NULL DEFAULT '',
    nivel_hierarq   TEXT        NOT NULL DEFAULT 'membro',
    comando_raw     TEXT        NOT NULL,
    comando_parsed  TEXT,
    alvo_nome       TEXT,
    resultado       TEXT,
    detalhe         TEXT,
    ts              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Padrões aprendidos: associação inteligente de intenção → comando
CREATE TABLE IF NOT EXISTS padroes_aprendidos (
    id          BIGSERIAL   PRIMARY KEY,
    gatilho     TEXT        NOT NULL UNIQUE,
    bot_nome    TEXT,
    comando     TEXT        NOT NULL,
    prefixo     TEXT,
    confianca   FLOAT       NOT NULL DEFAULT 0.5,
    usos        INTEGER     NOT NULL DEFAULT 1,
    ultimo_uso  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- NOVO: ordens diretas do proprietário para retroalimentação no prompt
-- Separado de execucoes_internas para não misturar moderação com preferências
CREATE TABLE IF NOT EXISTS ordens_dono (
    id          BIGSERIAL   PRIMARY KEY,
    conteudo    TEXT        NOT NULL,
    tipo        TEXT        NOT NULL DEFAULT 'ordem',  -- 'ordem' | 'preferencia' | 'restricao'
    alvo_nome   TEXT,
    ativa       BOOLEAN     NOT NULL DEFAULT TRUE,
    usos_inj    INTEGER     NOT NULL DEFAULT 0,   -- quantas vezes foi injetado no prompt
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado  TIMESTAMPTZ NOT NULL DEFAULT NOW()
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
CREATE INDEX IF NOT EXISTS idx_ordens_dono_ativa  ON ordens_dono (ativa, tipo);
"""


class AprendizadoComandos:
    """
    Gerencia o aprendizado persistente de comandos via PostgreSQL.

    NOVOS MÉTODOS:
      registrar_ordem_dono()       — persiste ordem/preferência do proprietário
      buscar_padroes_semelhantes() — busca textual fuzzy de padrões aprendidos
      confirmar_resultado_cmd()    — marca se comando de bot funcionou
      contexto_completo_para_ia()  — bloco de texto para injetar no system prompt
    """

    def __init__(self, db_url: str):
        self._db_url = db_url
        self._pool: Optional["asyncpg.Pool"] = None
        self._cache_bots: dict[str, dict] = {}
        self._cache_ts: float = 0.0
        self._CACHE_TTL = 120

        # Cache local das ordens do dono (evita hit ao banco em cada mensagem)
        self._cache_ordens_dono: list[dict] = []
        self._cache_ordens_ts: float = 0.0
        self._CACHE_ORDENS_TTL = 60  # segundos

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
        if not self._pool or not bot_nome or not comando:
            return
        nome = bot_nome.lower().strip()
        cmd  = comando.lower().strip()
        pref = prefixo.strip() if prefixo else None
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO comandos_bots (bot_nome, comando, prefixo, eh_slash, usos, ultimo_uso, funcionou)
                    VALUES ($1, $2, $3, $4, 1, NOW(), $5)
                    ON CONFLICT (bot_nome, comando) DO UPDATE
                        SET usos       = comandos_bots.usos + 1,
                            ultimo_uso = NOW(),
                            prefixo    = COALESCE($3, comandos_bots.prefixo),
                            funcionou  = COALESCE($5, comandos_bots.funcionou)
                """, nome, cmd, pref, eh_slash, funcionou)

                await conn.execute("""
                    INSERT INTO bots_conhecidos (nome, prefixo, usa_slash, usos_total)
                    VALUES ($1, $2, $3, 1)
                    ON CONFLICT (nome) DO UPDATE
                        SET usos_total = bots_conhecidos.usos_total + 1,
                            prefixo    = COALESCE($2, bots_conhecidos.prefixo),
                            usa_slash  = bots_conhecidos.usa_slash OR $3,
                            atualizado = NOW()
                """, nome, pref or "!", eh_slash)

            self._cache_ts = 0
        except Exception as e:
            log.debug(f"[APREND] Erro ao registrar cmd bot: {e}")

    # ── Confirmar resultado de comando de bot ─────────────────────────────────

    async def confirmar_resultado_cmd(
        self,
        bot_nome: str,
        comando: str,
        funcionou: bool,
    ) -> None:
        """
        Atualiza se um comando de bot funcionou ou não.
        Chamar quando o bot responde com erro ou sucesso confirmado.
        """
        if not self._pool:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    UPDATE comandos_bots
                    SET funcionou = $3
                    WHERE bot_nome = $1 AND comando = $2
                """, bot_nome.lower(), comando.lower(), funcionou)
        except Exception as e:
            log.debug(f"[APREND] Erro ao confirmar resultado: {e}")

    # ── Registro de execução interna ──────────────────────────────────────────

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

    # ── NOVO: Registrar ordem do proprietário ─────────────────────────────────

    async def registrar_ordem_dono(
        self,
        conteudo: str,
        tipo: str = "ordem",
        alvo_nome: Optional[str] = None,
    ) -> None:
        """
        Persiste uma ordem, preferência ou restrição do proprietário.
        Estas são injetadas no system prompt via contexto_completo_para_ia().

        tipo: 'ordem' | 'preferencia' | 'restricao'
        alvo_nome: membro específico afetado (None = ordem global)

        Exemplo:
            await aprend.registrar_ordem_dono(
                "nunca punir o viadinhodaboca sem minha autorização",
                tipo="restricao", alvo_nome="viadinhodaboca"
            )
        """
        if not self._pool or not conteudo.strip():
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute("""
                    INSERT INTO ordens_dono (conteudo, tipo, alvo_nome)
                    VALUES ($1, $2, $3)
                """, conteudo.strip()[:500], tipo, alvo_nome.lower() if alvo_nome else None)
            # Invalida cache
            self._cache_ordens_ts = 0
            log.info(f"[APREND] Ordem do dono registrada: tipo={tipo} alvo={alvo_nome!r}")
        except Exception as e:
            log.debug(f"[APREND] Erro ao registrar ordem do dono: {e}")

    async def desativar_ordem_dono(self, alvo_nome: str) -> int:
        """Desativa todas as ordens ativas sobre um membro."""
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute("""
                    UPDATE ordens_dono SET ativa = FALSE, atualizado = NOW()
                    WHERE alvo_nome = $1 AND ativa = TRUE
                """, alvo_nome.lower())
                n = int(result.split()[-1]) if result else 0
            self._cache_ordens_ts = 0
            return n
        except Exception as e:
            log.debug(f"[APREND] Erro ao desativar ordens: {e}")
            return 0

    async def _carregar_ordens_dono(self) -> list[dict]:
        """Carrega ordens do dono com cache TTL."""
        import time
        if (time.time() - self._cache_ordens_ts) < self._CACHE_ORDENS_TTL:
            return self._cache_ordens_dono
        if not self._pool:
            return []
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch("""
                    SELECT tipo, alvo_nome, conteudo, usos_inj
                    FROM ordens_dono
                    WHERE ativa = TRUE
                    ORDER BY criado_em DESC
                    LIMIT 50
                """)
            self._cache_ordens_dono = [dict(r) for r in rows]
            self._cache_ordens_ts = time.time()
            return self._cache_ordens_dono
        except Exception as e:
            log.debug(f"[APREND] Erro ao carregar ordens: {e}")
            return []

    async def carregar_regras_membro(self) -> dict[str, list[str]]:
        """
        Retorna {alvo_nome: [conteudo, ...]} para restaurar _regras_membro ao iniciar.
        """
        ordens = await self._carregar_ordens_dono()
        resultado: dict[str, list[str]] = {}
        for o in ordens:
            alvo = o.get("alvo_nome") or ""
            if alvo:
                resultado.setdefault(alvo, []).append(o["conteudo"])
        return resultado

    # ── Registrar padrão aprendido ────────────────────────────────────────────

    async def registrar_padrao(
        self,
        gatilho: str,
        comando: str,
        bot_nome: Optional[str] = None,
        prefixo: Optional[str] = None,
        confianca: float = 0.7,
    ) -> None:
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
        if not self._pool:
            return {}
        nome = bot_nome.lower().strip()
        import time
        if (time.time() - self._cache_ts) < self._CACHE_TTL and nome in self._cache_bots:
            return self._cache_bots[nome]
        try:
            async with self._pool.acquire() as conn:
                row_bot = await conn.fetchrow(
                    "SELECT * FROM bots_conhecidos WHERE nome = $1", nome
                )
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

    # ── NOVO: Busca fuzzy de padrões ──────────────────────────────────────────

    async def buscar_padroes_semelhantes(self, texto: str, limite: int = 5) -> list[dict]:
        """
        Busca textual fuzzy de padrões aprendidos.
        Não precisa de embedding — usa ILIKE + trigramas PostgreSQL.
        Retorna lista de padrões ordenados por confiança.
        """
        if not self._pool:
            return []
        palavras = [p.lower() for p in texto.split() if len(p) > 3]
        if not palavras:
            return []
        try:
            async with self._pool.acquire() as conn:
                # Busca por qualquer palavra-chave
                condicoes = " OR ".join(
                    f"gatilho ILIKE '%' || ${i+1} || '%'"
                    for i in range(len(palavras[:5]))
                )
                query = f"""
                    SELECT gatilho, bot_nome, comando, prefixo, confianca, usos
                    FROM padroes_aprendidos
                    WHERE {condicoes}
                    ORDER BY confianca DESC, usos DESC
                    LIMIT ${ len(palavras[:5]) + 1 }
                """
                rows = await conn.fetch(query, *palavras[:5], limite)
            return [dict(r) for r in rows]
        except Exception as e:
            log.debug(f"[APREND] Erro na busca fuzzy: {e}")
            return []

    # ── NOVO: contexto completo para injetar no system prompt ─────────────────

    async def contexto_completo_para_ia(self, mensagem: str = "") -> str:
        """
        Gera bloco de texto compacto para injetar no system prompt do bot.
        Inclui:
          - Bots mais usados e seus comandos frequentes
          - Padrões aprendidos relevantes para a mensagem atual
          - Ordens do proprietário (ativas)
          - Execuções recentes bem-sucedidas (para aprendizado por reforço)

        Exemplo de uso em responder_com_groq:
            ctx_aprend = await aprend.contexto_completo_para_ia(pergunta)
            if ctx_aprend:
                base += "\\n" + ctx_aprend + "\\n"
        """
        if not self._pool:
            return ""

        partes: list[str] = []

        # 1. Bots conhecidos + comandos top
        try:
            async with self._pool.acquire() as conn:
                bots = await conn.fetch("""
                    SELECT bc.nome, bc.prefixo, bc.usa_slash,
                           ARRAY_AGG(cb.comando ORDER BY cb.usos DESC) AS cmds
                    FROM bots_conhecidos bc
                    LEFT JOIN comandos_bots cb
                        ON cb.bot_nome = bc.nome AND (cb.funcionou IS NULL OR cb.funcionou = TRUE)
                    GROUP BY bc.nome, bc.prefixo, bc.usa_slash, bc.usos_total
                    ORDER BY bc.usos_total DESC
                    LIMIT 8
                """)

            if bots:
                linhas_bots = []
                for b in bots:
                    pref = b["prefixo"] or "!"
                    cmds_lista = [c for c in (b["cmds"] or []) if c][:6]
                    cmds_txt = ", ".join(f"{pref}{c}" for c in cmds_lista)
                    slash_txt = " | usa slash" if b["usa_slash"] else ""
                    linhas_bots.append(f"  {b['nome']}(prefixo '{pref}'): {cmds_txt}{slash_txt}")
                partes.append(
                    "[BOTS APRENDIDOS — use estes comandos quando precisar]\n"
                    + "\n".join(linhas_bots)
                )
        except Exception as e:
            log.debug(f"[APREND] Erro ao montar bots: {e}")

        # 2. Padrões relevantes para a mensagem atual
        if mensagem:
            padroes = await self.buscar_padroes_semelhantes(mensagem, limite=4)
            if padroes:
                linhas_pad = []
                for p in padroes:
                    pref = p.get("prefixo") or ""
                    bot = p.get("bot_nome") or "interno"
                    conf = int(p["confianca"] * 100)
                    linhas_pad.append(
                        f"  '{p['gatilho']}' → {bot}: {pref}{p['comando']} (conf {conf}%)"
                    )
                partes.append(
                    "[PADRÕES APRENDIDOS relevantes]\n" + "\n".join(linhas_pad)
                )

        # 3. Ordens do proprietário ativas
        ordens = await self._carregar_ordens_dono()
        if ordens:
            globais = [o for o in ordens if not o.get("alvo_nome")]
            especificas = [o for o in ordens if o.get("alvo_nome")]

            linhas_ord = []
            for o in globais[:5]:
                linhas_ord.append(f"  [{o['tipo'].upper()}] {o['conteudo']}")
            for o in especificas[:8]:
                linhas_ord.append(f"  [{o['tipo'].upper()} → {o['alvo_nome']}] {o['conteudo']}")

            if linhas_ord:
                partes.append(
                    "[ORDENS DO PROPRIETÁRIO — nunca ignorar]\n" + "\n".join(linhas_ord)
                )
            # Incrementa contador de injeções
            asyncio.ensure_future(self._incrementar_usos_inj(ordens))

        # 4. Últimos comandos internos bem-sucedidos (aprendizado por exemplo)
        try:
            async with self._pool.acquire() as conn:
                recentes = await conn.fetch("""
                    SELECT autor_nome, nivel_hierarq, comando_parsed, alvo_nome
                    FROM execucoes_internas
                    WHERE resultado = 'ok' AND comando_parsed IS NOT NULL
                    ORDER BY ts DESC
                    LIMIT 5
                """)
            if recentes:
                ex = "; ".join(
                    f"{r['nivel_hierarq']} pediu {r['comando_parsed']}"
                    + (f" sobre {r['alvo_nome']}" if r["alvo_nome"] else "")
                    for r in recentes
                )
                partes.append(f"[EXECUÇÕES RECENTES APROVADAS] {ex}")
        except Exception as e:
            log.debug(f"[APREND] Erro ao carregar execuções recentes: {e}")

        if not partes:
            return ""

        return (
            "=== CONTEXTO DE APRENDIZADO ===\n"
            + "\n\n".join(partes)
            + "\n=== FIM DO APRENDIZADO ==="
        )

    async def _incrementar_usos_inj(self, ordens: list[dict]) -> None:
        """Incrementa usos_inj nas ordens que foram injetadas no prompt."""
        if not self._pool:
            return
        ids = [o.get("id") for o in ordens if o.get("id")]
        if not ids:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    "UPDATE ordens_dono SET usos_inj = usos_inj + 1 WHERE id = ANY($1::bigint[])",
                    ids,
                )
        except Exception:
            pass

    # ── Relatório de uso ──────────────────────────────────────────────────────

    async def relatorio_uso(self, limite: int = 10) -> str:
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
                rows_ordens = await conn.fetch("""
                    SELECT tipo, alvo_nome, conteudo, usos_inj
                    FROM ordens_dono WHERE ativa = TRUE
                    ORDER BY usos_inj DESC LIMIT 10
                """)

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

            linhas.append("\n**Ordens do proprietário ativas:**")
            for r in rows_ordens:
                alvo = f" [{r['alvo_nome']}]" if r["alvo_nome"] else ""
                linhas.append(f"  [{r['tipo']}]{alvo} {r['conteudo'][:60]} (injetado {r['usos_inj']}x)")

            return "\n".join(linhas) or "Nenhum dado ainda."
        except Exception as e:
            log.debug(f"[APREND] Erro ao gerar relatório: {e}")
            return "Erro ao gerar relatório."

    async def resumo_para_ia(self, bot_nome: str) -> str:
        """Bloco compacto de um bot específico para injetar no prompt."""
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
