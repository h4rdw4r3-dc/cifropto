"""
memoria_vetorial.py
===================
Módulo de memória de longo prazo para o bot Discord.
Usa PostgreSQL + pgvector para armazenar e buscar mensagens por similaridade semântica.

MELHORIAS NESTA VERSÃO:
  - Tabela `perfis_usuarios`   → perfis comportamentais persistidos no banco (não só JSON)
  - Tabela `ordens_proprietario` → ordens/regras do proprietário persistidas com vetor semântico
  - Tabela `snapshot_servidor` → snapshot compacto do servidor atualizado a cada hora
  - `buscar_ordens_relevantes()` → recupera ordens do proprietário por similaridade vetorial
  - `salvar_perfil_usuario()`  → upsert do perfil completo de um usuário
  - `carregar_perfis_usuarios()` → carrega todos os perfis do banco ao iniciar
  - `salvar_snapshot_servidor()` → persiste contexto compacto do servidor

Dependências:
    asyncpg>=0.29.0
    openai>=1.0.0          (embeddings via OpenAI text-embedding-3-small)

Variáveis de ambiente necessárias:
    DATABASE_URL       — URL de conexão PostgreSQL (Railway provê automaticamente)
    OPENAI_API_KEY     — chave de API da OpenAI para geração de embeddings
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from collections import OrderedDict
from datetime import datetime, timezone
from typing import Optional


# ── Cache LRU de embeddings ───────────────────────────────────────────────────
class _LRUCache:
    def __init__(self, maxsize: int = 512):
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        self._maxsize = maxsize

    def get(self, key: str) -> Optional[list[float]]:
        if key in self._cache:
            self._cache.move_to_end(key)
            return self._cache[key]
        return None

    def put(self, key: str, value: list[float]) -> None:
        if key in self._cache:
            self._cache.move_to_end(key)
        else:
            if len(self._cache) >= self._maxsize:
                self._cache.popitem(last=False)
            self._cache[key] = value

    @staticmethod
    def key(texto: str) -> str:
        return hashlib.md5(texto.encode(), usedforsecurity=False).hexdigest()

log = logging.getLogger("shell")

try:
    import asyncpg
    ASYNCPG_OK = True
except ImportError:
    ASYNCPG_OK = False
    log.warning("[CEREBRO] asyncpg não instalado — memória vetorial indisponível.")

try:
    from openai import AsyncOpenAI
    OPENAI_OK = True
except ImportError:
    OPENAI_OK = False
    log.warning("[CEREBRO] openai não instalado — embeddings indisponíveis.")


_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIM   = 1536

_SQL_INIT = """
CREATE EXTENSION IF NOT EXISTS vector;

CREATE TABLE IF NOT EXISTS mensagens (
    id          BIGSERIAL PRIMARY KEY,
    canal_id    BIGINT      NOT NULL,
    canal_nome  TEXT        NOT NULL DEFAULT '',
    autor_id    BIGINT      NOT NULL,
    autor_nome  TEXT        NOT NULL DEFAULT '',
    conteudo    TEXT        NOT NULL,
    ts          TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    embedding   vector({dim}),
    resumida    BOOLEAN     NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS resumos (
    id          BIGSERIAL PRIMARY KEY,
    canal_id    BIGINT      NOT NULL,
    canal_nome  TEXT        NOT NULL DEFAULT '',
    periodo_ini TIMESTAMPTZ NOT NULL,
    periodo_fim TIMESTAMPTZ NOT NULL,
    conteudo    TEXT        NOT NULL,
    embedding   vector({dim}),
    criado_em   TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Perfis comportamentais de usuários — persiste entre reinícios
CREATE TABLE IF NOT EXISTS perfis_usuarios (
    user_id         BIGINT      PRIMARY KEY,
    autor_nome      TEXT        NOT NULL DEFAULT '',
    resumo          TEXT        NOT NULL DEFAULT '',
    n_interacoes    INTEGER     NOT NULL DEFAULT 0,
    episodios       JSONB       NOT NULL DEFAULT '[]',
    preferencias    JSONB       NOT NULL DEFAULT '[]',
    horarios        JSONB       NOT NULL DEFAULT '[]',
    canais          JSONB       NOT NULL DEFAULT '{{}}',
    ultima_vez_visto TEXT,
    atualizado      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Ordens e regras do proprietário sobre membros — persistidas com embedding
-- Permite busca semântica: "qual a regra sobre X?" → recupera ordens relevantes
CREATE TABLE IF NOT EXISTS ordens_proprietario (
    id              BIGSERIAL   PRIMARY KEY,
    tipo            TEXT        NOT NULL DEFAULT 'regra',  -- 'regra' | 'ordem' | 'preferencia'
    alvo_nome       TEXT,                                  -- NULL = ordem global
    alvo_id         BIGINT,                                -- ID do membro alvo (se houver)
    conteudo        TEXT        NOT NULL,                  -- texto da ordem/regra
    ativa           BOOLEAN     NOT NULL DEFAULT TRUE,
    embedding       vector({dim}),
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    atualizado      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Snapshot compacto do servidor — atualizado periodicamente (a cada hora)
CREATE TABLE IF NOT EXISTS snapshot_servidor (
    id              BIGSERIAL   PRIMARY KEY,
    guild_id        BIGINT      NOT NULL,
    conteudo        TEXT        NOT NULL,
    criado_em       TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_mensagens_canal    ON mensagens (canal_id);
CREATE INDEX IF NOT EXISTS idx_mensagens_ts       ON mensagens (ts DESC);
CREATE INDEX IF NOT EXISTS idx_mensagens_resumida ON mensagens (resumida);
CREATE INDEX IF NOT EXISTS idx_resumos_canal      ON resumos (canal_id);
CREATE INDEX IF NOT EXISTS idx_ordens_ativa       ON ordens_proprietario (ativa);
CREATE INDEX IF NOT EXISTS idx_ordens_alvo        ON ordens_proprietario (alvo_nome);
CREATE INDEX IF NOT EXISTS idx_snapshot_guild     ON snapshot_servidor (guild_id, criado_em DESC);
""".format(dim=_EMBED_DIM)

_SQL_IDX_MENSAGENS = (
    "CREATE INDEX IF NOT EXISTS idx_mensagens_embedding "
    "ON mensagens USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);"
)
_SQL_IDX_RESUMOS = (
    "CREATE INDEX IF NOT EXISTS idx_resumos_embedding "
    "ON resumos USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);"
)
_SQL_IDX_ORDENS = (
    "CREATE INDEX IF NOT EXISTS idx_ordens_embedding "
    "ON ordens_proprietario USING hnsw (embedding vector_cosine_ops) "
    "WITH (m = 16, ef_construction = 64);"
)


class MemoriaVetorial:
    """
    Gerencia a memória de longo prazo do bot usando PostgreSQL + pgvector.

    NOVOS MÉTODOS:
      salvar_perfil_usuario()      — persiste perfil comportamental no banco
      carregar_perfis_usuarios()   — restaura todos os perfis ao iniciar
      registrar_ordem_proprietario() — persiste ordem/regra do proprietário
      buscar_ordens_relevantes()   — recupera ordens por similaridade semântica
      salvar_snapshot_servidor()   — persiste contexto compacto do servidor
      carregar_snapshot_servidor() — restaura snapshot mais recente
    """

    def __init__(self, db_url: str, openai_key: str = ""):
        self._db_url   = db_url
        self._oai_key  = openai_key
        self._pool: Optional["asyncpg.Pool"] = None
        self._oai: Optional["AsyncOpenAI"]   = None
        self._embed_cache = _LRUCache(maxsize=512)
        self._fila_batch: list[dict] = []
        self._flush_task: Optional[asyncio.Task] = None

    # ── Inicialização ─────────────────────────────────────────────────────────

    async def inicializar(self) -> None:
        if not ASYNCPG_OK:
            raise RuntimeError("asyncpg não disponível.")

        self._pool = await asyncpg.create_pool(
            self._db_url,
            min_size=1,
            max_size=5,
            command_timeout=30,
        )

        async with self._pool.acquire() as conn:
            await conn.execute(_SQL_INIT)
            for sql_idx in (_SQL_IDX_MENSAGENS, _SQL_IDX_RESUMOS, _SQL_IDX_ORDENS):
                try:
                    await conn.execute(sql_idx)
                except Exception:
                    pass

        if OPENAI_OK and self._oai_key:
            self._oai = AsyncOpenAI(api_key=self._oai_key)

        self._flush_task = asyncio.ensure_future(self._flush_worker())
        log.info("[CEREBRO] Pool PostgreSQL criado e schema inicializado.")

    # ── Embeddings ────────────────────────────────────────────────────────────

    async def _embed(self, texto: str) -> Optional[list[float]]:
        if not self._oai:
            return None
        texto = texto[:8000]
        cache_key = _LRUCache.key(texto)
        cached = self._embed_cache.get(cache_key)
        if cached is not None:
            return cached
        try:
            resp = await self._oai.embeddings.create(
                model=_EMBED_MODEL,
                input=texto,
            )
            vec = resp.data[0].embedding
            self._embed_cache.put(cache_key, vec)
            return vec
        except Exception as e:
            log.debug(f"[CEREBRO] Falha ao gerar embedding: {e}")
            return None

    @staticmethod
    def _vec_str(vec: list[float]) -> str:
        return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"

    # ── Ingestão de mensagens ─────────────────────────────────────────────────

    async def ingerir_mensagem(
        self,
        canal_id: int,
        canal_nome: str,
        autor_id: int,
        autor_nome: str,
        conteudo: str,
        ts: Optional[datetime] = None,
    ) -> None:
        """Enfileira mensagem para ingestão em batch."""
        if not self._pool:
            return
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._fila_batch.append({
            "canal_id": canal_id, "canal_nome": canal_nome,
            "autor_id": autor_id, "autor_nome": autor_nome,
            "conteudo": conteudo[:2000], "ts": ts,
        })
        if len(self._fila_batch) >= 10:
            asyncio.ensure_future(self._flush_batch())

    async def _flush_worker(self) -> None:
        while True:
            await asyncio.sleep(5)
            if self._fila_batch:
                await self._flush_batch()

    async def _flush_batch(self) -> None:
        if not self._fila_batch or not self._pool:
            return
        batch = self._fila_batch[:]
        self._fila_batch.clear()
        for item in batch:
            try:
                vec = await self._embed(item["conteudo"])
                async with self._pool.acquire() as conn:
                    if vec:
                        await conn.execute(
                            """
                            INSERT INTO mensagens
                                (canal_id, canal_nome, autor_id, autor_nome, conteudo, ts, embedding)
                            VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                            """,
                            item["canal_id"], item["canal_nome"], item["autor_id"],
                            item["autor_nome"], item["conteudo"], item["ts"],
                            self._vec_str(vec),
                        )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO mensagens
                                (canal_id, canal_nome, autor_id, autor_nome, conteudo, ts)
                            VALUES ($1, $2, $3, $4, $5, $6)
                            """,
                            item["canal_id"], item["canal_nome"], item["autor_id"],
                            item["autor_nome"], item["conteudo"], item["ts"],
                        )
            except Exception as e:
                log.debug(f"[CEREBRO] Falha ao persistir mensagem: {e}")

    # ── Busca por contexto ────────────────────────────────────────────────────

    async def buscar_contexto(
        self,
        pergunta: str,
        canal_id: Optional[int] = None,
        top_k: int = 5,
    ) -> str:
        if not self._pool:
            return ""
        vec = await self._embed(pergunta)
        if not vec:
            return ""
        vec_s = self._vec_str(vec)
        candidatos: list[dict] = []

        try:
            async with self._pool.acquire() as conn:
                # Busca vetorial em mensagens
                rows_msgs = await conn.fetch(
                    """
                    SELECT autor_nome, conteudo, ts, canal_nome,
                           embedding <=> $1::vector AS dist
                    FROM mensagens
                    WHERE resumida = FALSE
                    ORDER BY dist ASC
                    LIMIT 20
                    """,
                    vec_s,
                )
                for r in rows_msgs:
                    score = 1.0 - float(r["dist"])
                    if score < 0.65:
                        continue
                    data = r["ts"].strftime("%d/%m %H:%M") if r["ts"] else "?"
                    texto = f"[{data}] #{r['canal_nome']} — {r['autor_nome']}: {r['conteudo']}"
                    try:
                        emb_r = list(map(float, r["embedding"][1:-1].split(","))) if r["embedding"] else None
                    except Exception:
                        emb_r = None
                    candidatos.append({"score": score, "texto": texto, "vec": emb_r})

                # Busca vetorial em resumos
                rows_res = await conn.fetch(
                    """
                    SELECT canal_nome, conteudo, periodo_ini,
                           embedding <=> $1::vector AS dist
                    FROM resumos
                    ORDER BY dist ASC
                    LIMIT 10
                    """,
                    vec_s,
                )
                for r in rows_res:
                    score = 1.0 - float(r["dist"])
                    if score < 0.65:
                        continue
                    data = r["periodo_ini"].strftime("%d/%m") if r["periodo_ini"] else "?"
                    texto = f"[Resumo {data}] #{r['canal_nome']}: {r['conteudo'][:300]}"
                    try:
                        emb_r = list(map(float, r["embedding"][1:-1].split(","))) if r["embedding"] else None
                    except Exception:
                        emb_r = None
                    candidatos.append({"score": score, "texto": texto, "vec": emb_r})

        except Exception as e:
            log.warning(f"[CEREBRO] Falha na busca vetorial: {e}")
            return ""

        candidatos.sort(key=lambda x: x["score"], reverse=True)

        def _cos(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b))
            na  = sum(x * x for x in a) ** 0.5
            nb  = sum(x * x for x in b) ** 0.5
            return dot / (na * nb) if na and nb else 0.0

        aceitos: list[dict] = []
        for c in candidatos:
            if c["vec"] and any(
                a["vec"] and _cos(c["vec"], a["vec"]) > 0.92
                for a in aceitos
            ):
                continue
            aceitos.append(c)
            if len(aceitos) >= top_k:
                break

        if not aceitos:
            return ""

        bloco = "\n".join(c["texto"] for c in aceitos)
        return (
            "── Memória de longo prazo (contexto relevante recuperado) ──\n"
            + bloco
            + "\n── Fim da memória ──"
        )

    # ── Perfis de usuários ────────────────────────────────────────────────────

    async def salvar_perfil_usuario(self, user_id: int, autor_nome: str, perfil: dict) -> None:
        """
        Persiste ou atualiza o perfil comportamental de um usuário no banco.
        perfil: dict com chaves resumo, n, episodios, preferencias, horarios, canais, ultima_vez_visto
        """
        if not self._pool:
            return
        import json
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO perfis_usuarios
                        (user_id, autor_nome, resumo, n_interacoes, episodios,
                         preferencias, horarios, canais, ultima_vez_visto, atualizado)
                    VALUES ($1, $2, $3, $4, $5::jsonb, $6::jsonb, $7::jsonb, $8::jsonb, $9, NOW())
                    ON CONFLICT (user_id) DO UPDATE SET
                        autor_nome      = EXCLUDED.autor_nome,
                        resumo          = EXCLUDED.resumo,
                        n_interacoes    = EXCLUDED.n_interacoes,
                        episodios       = EXCLUDED.episodios,
                        preferencias    = EXCLUDED.preferencias,
                        horarios        = EXCLUDED.horarios,
                        canais          = EXCLUDED.canais,
                        ultima_vez_visto = EXCLUDED.ultima_vez_visto,
                        atualizado      = NOW()
                    """,
                    user_id,
                    autor_nome,
                    perfil.get("resumo", ""),
                    perfil.get("n", 0),
                    json.dumps(perfil.get("episodios", []), ensure_ascii=False),
                    json.dumps(perfil.get("preferencias", []), ensure_ascii=False),
                    json.dumps(perfil.get("horarios", []), ensure_ascii=False),
                    json.dumps(perfil.get("canais", {}), ensure_ascii=False),
                    perfil.get("ultima_vez_visto"),
                )
        except Exception as e:
            log.debug(f"[CEREBRO] Falha ao salvar perfil de {user_id}: {e}")

    async def carregar_perfis_usuarios(self) -> dict[int, dict]:
        """
        Carrega todos os perfis de usuários do banco.
        Retorna dict {user_id: perfil_dict} para restaurar perfis_usuarios no bot.
        """
        if not self._pool:
            return {}
        import json
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT * FROM perfis_usuarios ORDER BY atualizado DESC"
                )
            resultado: dict[int, dict] = {}
            for r in rows:
                uid = r["user_id"]
                resultado[uid] = {
                    "resumo":          r["resumo"],
                    "n":               r["n_interacoes"],
                    "atualizado":      r["atualizado"].isoformat() if r["atualizado"] else "",
                    "episodios":       json.loads(r["episodios"]) if r["episodios"] else [],
                    "preferencias":    json.loads(r["preferencias"]) if r["preferencias"] else [],
                    "horarios":        json.loads(r["horarios"]) if r["horarios"] else [],
                    "canais":          json.loads(r["canais"]) if r["canais"] else {},
                    "ultima_vez_visto": r["ultima_vez_visto"],
                }
            log.info(f"[CEREBRO] {len(resultado)} perfis de usuários carregados do banco.")
            return resultado
        except Exception as e:
            log.warning(f"[CEREBRO] Falha ao carregar perfis: {e}")
            return {}

    # ── Ordens do proprietário ────────────────────────────────────────────────

    async def registrar_ordem_proprietario(
        self,
        conteudo: str,
        tipo: str = "regra",
        alvo_nome: Optional[str] = None,
        alvo_id: Optional[int] = None,
    ) -> None:
        """
        Persiste uma ordem ou regra do proprietário com embedding vetorial.
        tipo: 'regra' (sobre membro específico) | 'ordem' (instrução de comportamento) | 'preferencia'
        alvo_nome: display_name do membro alvo (se houver)
        alvo_id: ID do membro alvo (se houver)

        Exemplo de uso:
            await mem.registrar_ordem_proprietario(
                "não punir sem autorização prévia do proprietário",
                tipo="regra", alvo_nome="viadinhodaboca"
            )
        """
        if not self._pool or not conteudo.strip():
            return
        vec = await self._embed(conteudo)
        try:
            async with self._pool.acquire() as conn:
                if vec:
                    await conn.execute(
                        """
                        INSERT INTO ordens_proprietario
                            (tipo, alvo_nome, alvo_id, conteudo, embedding)
                        VALUES ($1, $2, $3, $4, $5::vector)
                        """,
                        tipo,
                        alvo_nome.lower() if alvo_nome else None,
                        alvo_id,
                        conteudo,
                        self._vec_str(vec),
                    )
                else:
                    await conn.execute(
                        """
                        INSERT INTO ordens_proprietario
                            (tipo, alvo_nome, alvo_id, conteudo)
                        VALUES ($1, $2, $3, $4)
                        """,
                        tipo,
                        alvo_nome.lower() if alvo_nome else None,
                        alvo_id,
                        conteudo,
                    )
            log.info(f"[CEREBRO] Ordem do proprietário salva: tipo={tipo} alvo={alvo_nome!r}")
        except Exception as e:
            log.warning(f"[CEREBRO] Falha ao salvar ordem: {e}")

    async def desativar_ordem_proprietario(self, alvo_nome: str) -> int:
        """
        Marca todas as ordens ativas sobre um membro como inativas.
        Retorna o número de ordens desativadas.
        """
        if not self._pool:
            return 0
        try:
            async with self._pool.acquire() as conn:
                result = await conn.execute(
                    """
                    UPDATE ordens_proprietario
                    SET ativa = FALSE, atualizado = NOW()
                    WHERE alvo_nome = $1 AND ativa = TRUE
                    """,
                    alvo_nome.lower(),
                )
                n = int(result.split()[-1]) if result else 0
            log.info(f"[CEREBRO] {n} ordem(s) desativada(s) para {alvo_nome!r}")
            return n
        except Exception as e:
            log.warning(f"[CEREBRO] Falha ao desativar ordens: {e}")
            return 0

    async def buscar_ordens_relevantes(
        self,
        consulta: str,
        alvo_nome: Optional[str] = None,
        top_k: int = 8,
    ) -> str:
        """
        Recupera ordens/regras do proprietário relevantes para a consulta.
        Se alvo_nome fornecido, filtra por esse alvo + busca global.
        Retorna bloco de texto pronto para injetar no prompt.

        Exemplo de uso:
            ctx = await mem.buscar_ordens_relevantes("o que fazer com o viadinhodaboca?")
        """
        if not self._pool:
            return ""
        vec = await self._embed(consulta)

        try:
            async with self._pool.acquire() as conn:
                # 1. Ordens específicas do alvo (sem embedding — recupera todas)
                rows_alvo: list = []
                if alvo_nome:
                    rows_alvo = await conn.fetch(
                        """
                        SELECT tipo, alvo_nome, conteudo FROM ordens_proprietario
                        WHERE ativa = TRUE AND alvo_nome = $1
                        ORDER BY criado_em DESC
                        LIMIT 20
                        """,
                        alvo_nome.lower(),
                    )

                # 2. Busca vetorial nas ordens globais + todas ativas
                rows_vec: list = []
                if vec:
                    rows_vec = await conn.fetch(
                        """
                        SELECT tipo, alvo_nome, conteudo,
                               embedding <=> $1::vector AS dist
                        FROM ordens_proprietario
                        WHERE ativa = TRUE
                        ORDER BY dist ASC
                        LIMIT 20
                        """,
                        self._vec_str(vec),
                    )

                # 3. Fallback: sem embedding → busca textual simples
                if not rows_vec:
                    rows_vec = await conn.fetch(
                        """
                        SELECT tipo, alvo_nome, conteudo FROM ordens_proprietario
                        WHERE ativa = TRUE
                        ORDER BY criado_em DESC
                        LIMIT $1
                        """,
                        top_k,
                    )

        except Exception as e:
            log.warning(f"[CEREBRO] Falha ao buscar ordens: {e}")
            return ""

        # Junta e deduplica
        vistos: set[str] = set()
        linhas: list[str] = []

        # Ordens específicas do alvo têm prioridade
        for r in rows_alvo:
            chave = r["conteudo"].strip()
            if chave not in vistos:
                vistos.add(chave)
                alvo_txt = f" [{r['alvo_nome']}]" if r["alvo_nome"] else ""
                linhas.append(f"  {r['tipo'].upper()}{alvo_txt}: {r['conteudo']}")

        # Ordens por similaridade
        for r in rows_vec:
            score = 1.0 - float(r.get("dist", 0.5))
            if score < 0.55:
                continue
            chave = r["conteudo"].strip()
            if chave not in vistos:
                vistos.add(chave)
                alvo_txt = f" [{r['alvo_nome']}]" if r["alvo_nome"] else ""
                linhas.append(f"  {r['tipo'].upper()}{alvo_txt}: {r['conteudo']}")
            if len(linhas) >= top_k:
                break

        if not linhas:
            return ""

        return (
            "=== ORDENS DO PROPRIETÁRIO (recuperadas do banco) ===\n"
            "Estas ordens NUNCA podem ser ignoradas:\n"
            + "\n".join(linhas)
            + "\n"
        )

    async def carregar_todas_ordens(self) -> dict[str, list[str]]:
        """
        Carrega todas as ordens ativas do banco agrupadas por alvo.
        Retorna dict {alvo_nome: [conteudo, ...]} para restaurar _regras_membro.
        alvo_nome = '' para ordens globais.
        """
        if not self._pool:
            return {}
        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    "SELECT alvo_nome, conteudo FROM ordens_proprietario WHERE ativa = TRUE ORDER BY criado_em ASC"
                )
            resultado: dict[str, list[str]] = {}
            for r in rows:
                chave = r["alvo_nome"] or ""
                resultado.setdefault(chave, []).append(r["conteudo"])
            log.info(f"[CEREBRO] {sum(len(v) for v in resultado.values())} ordens do proprietário carregadas.")
            return resultado
        except Exception as e:
            log.warning(f"[CEREBRO] Falha ao carregar ordens: {e}")
            return {}

    # ── Snapshot do servidor ──────────────────────────────────────────────────

    async def salvar_snapshot_servidor(self, guild_id: int, conteudo: str) -> None:
        """
        Persiste o snapshot compacto do servidor.
        Mantém apenas os 3 snapshots mais recentes por servidor.
        """
        if not self._pool or not conteudo:
            return
        try:
            async with self._pool.acquire() as conn:
                await conn.execute(
                    """
                    INSERT INTO snapshot_servidor (guild_id, conteudo)
                    VALUES ($1, $2)
                    """,
                    guild_id, conteudo[:8000],
                )
                # Mantém apenas os 3 mais recentes
                await conn.execute(
                    """
                    DELETE FROM snapshot_servidor
                    WHERE guild_id = $1
                      AND id NOT IN (
                        SELECT id FROM snapshot_servidor
                        WHERE guild_id = $1
                        ORDER BY criado_em DESC
                        LIMIT 3
                      )
                    """,
                    guild_id,
                )
        except Exception as e:
            log.debug(f"[CEREBRO] Falha ao salvar snapshot: {e}")

    async def carregar_snapshot_servidor(self, guild_id: int) -> str:
        """
        Retorna o snapshot mais recente do servidor (para restaurar contexto após reinício).
        """
        if not self._pool:
            return ""
        try:
            async with self._pool.acquire() as conn:
                row = await conn.fetchrow(
                    """
                    SELECT conteudo FROM snapshot_servidor
                    WHERE guild_id = $1
                    ORDER BY criado_em DESC
                    LIMIT 1
                    """,
                    guild_id,
                )
            return row["conteudo"] if row else ""
        except Exception as e:
            log.debug(f"[CEREBRO] Falha ao carregar snapshot: {e}")
            return ""

    # ── Sumarização diária ────────────────────────────────────────────────────

    async def sumarizar_e_limpar(self, groq_api_key: str, max_msgs: int = 200) -> int:
        if not self._pool:
            return 0

        resumidor = self._oai
        usa_groq   = False
        if resumidor is None and groq_api_key:
            try:
                resumidor = AsyncOpenAI(
                    api_key=groq_api_key,
                    base_url="https://api.groq.com/openai/v1",
                )
                usa_groq = True
            except Exception:
                return 0
        if resumidor is None:
            return 0

        modelo_sum = (
            "llama-3.1-8b-instant" if usa_groq
            else "gpt-4o-mini"
        )

        try:
            async with self._pool.acquire() as conn:
                canais = await conn.fetch(
                    """
                    SELECT DISTINCT canal_id, canal_nome
                    FROM mensagens
                    WHERE resumida = FALSE
                      AND ts < NOW() - INTERVAL '12 hours'
                    """
                )
        except Exception as e:
            log.warning(f"[CEREBRO] Falha ao listar canais para sumarização: {e}")
            return 0

        n_resumos = 0

        for row in canais:
            canal_id   = row["canal_id"]
            canal_nome = row["canal_nome"]

            try:
                async with self._pool.acquire() as conn:
                    msgs = await conn.fetch(
                        """
                        SELECT id, autor_nome, conteudo, ts
                        FROM mensagens
                        WHERE canal_id = $1
                          AND resumida = FALSE
                          AND ts < NOW() - INTERVAL '12 hours'
                        ORDER BY ts ASC
                        LIMIT $2
                        """,
                        canal_id, max_msgs,
                    )

                if not msgs:
                    continue

                linhas = []
                for m in msgs:
                    data = m["ts"].strftime("%d/%m %H:%M") if m["ts"] else "?"
                    linhas.append(f"[{data}] {m['autor_nome']}: {m['conteudo']}")
                texto_raw = "\n".join(linhas)

                try:
                    resp = await resumidor.chat.completions.create(
                        model=modelo_sum,
                        max_tokens=350,
                        messages=[
                            {
                                "role": "system",
                                "content": (
                                    "Voce resume conversas de Discord de forma concisa (max 300 palavras). "
                                    "Preserve: decisoes tomadas, fatos importantes, quem disse o que, "
                                    "temas recorrentes e contexto de relacionamento entre membros. "
                                    "Seja direto e factual. Sem introducao, sem conclusao — so o resumo."
                                ),
                            },
                            {
                                "role": "user",
                                "content": f"Canal #{canal_nome}:\n\n{texto_raw[:6000]}",
                            },
                        ],
                    )
                    resumo_txt = resp.choices[0].message.content.strip()
                except Exception as e:
                    log.debug(f"[CEREBRO] Falha ao gerar resumo para #{canal_nome}: {e}")
                    continue

                emb_resumo = await self._embed(resumo_txt)
                periodo_ini = msgs[0]["ts"]
                periodo_fim = msgs[-1]["ts"]
                ids_resumidos = [m["id"] for m in msgs]

                async with self._pool.acquire() as conn:
                    if emb_resumo:
                        await conn.execute(
                            """
                            INSERT INTO resumos
                                (canal_id, canal_nome, periodo_ini, periodo_fim, conteudo, embedding)
                            VALUES ($1, $2, $3, $4, $5, $6::vector)
                            """,
                            canal_id, canal_nome, periodo_ini, periodo_fim,
                            resumo_txt, self._vec_str(emb_resumo),
                        )
                    else:
                        await conn.execute(
                            """
                            INSERT INTO resumos
                                (canal_id, canal_nome, periodo_ini, periodo_fim, conteudo)
                            VALUES ($1, $2, $3, $4, $5)
                            """,
                            canal_id, canal_nome, periodo_ini, periodo_fim, resumo_txt,
                        )

                    await conn.execute(
                        "UPDATE mensagens SET resumida = TRUE WHERE id = ANY($1::bigint[])",
                        ids_resumidos,
                    )

                n_resumos += 1
                log.info(
                    f"[CEREBRO] Resumo gerado para #{canal_nome} "
                    f"({len(ids_resumidos)} msgs → {len(resumo_txt)} chars)."
                )

            except Exception as e:
                log.warning(f"[CEREBRO] Erro ao sumarizar canal {canal_id}: {e}")
                continue

        try:
            async with self._pool.acquire() as conn:
                deleted = await conn.fetchval(
                    """
                    WITH del AS (
                        DELETE FROM mensagens
                        WHERE resumida = TRUE
                          AND ts < NOW() - INTERVAL '30 days'
                        RETURNING id
                    )
                    SELECT COUNT(*) FROM del
                    """
                )
                if deleted:
                    log.info(f"[CEREBRO] Limpeza física: {deleted} mensagens antigas removidas.")
        except Exception as e:
            log.warning(f"[CEREBRO] Falha na limpeza física: {e}")

        return n_resumos

    # ── Utilitários ───────────────────────────────────────────────────────────

    async def fechar(self) -> None:
        if self._flush_task:
            self._flush_task.cancel()
        if self._fila_batch:
            await self._flush_batch()
        if self._pool:
            await self._pool.close()
            self._pool = None
            log.info("[CEREBRO] Pool PostgreSQL fechado.")
