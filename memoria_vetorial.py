"""
memoria_vetorial.py
===================
Módulo de memória de longo prazo para o bot Discord.
Usa PostgreSQL + pgvector para armazenar e buscar mensagens por similaridade semântica.

Dependências:
    asyncpg>=0.29.0
    openai>=1.0.0          (embeddings via OpenAI text-embedding-3-small)

Variáveis de ambiente necessárias (lidas pelo bot):
    DATABASE_URL       — URL de conexão PostgreSQL (provisionado automaticamente no Railway)
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
# Evita chamadas repetidas à OpenAI para textos idênticos ou muito frequentes.
# Tamanho 512: ocupa ~3 MB de RAM (512 × 1536 floats × 4 bytes).
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


# ── Dimensão do modelo de embedding ──────────────────────────────────────────
_EMBED_MODEL = "text-embedding-3-small"
_EMBED_DIM   = 1536

# ── SQL de inicialização ──────────────────────────────────────────────────────
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

CREATE INDEX IF NOT EXISTS idx_mensagens_canal    ON mensagens (canal_id);
CREATE INDEX IF NOT EXISTS idx_mensagens_ts       ON mensagens (ts DESC);
CREATE INDEX IF NOT EXISTS idx_mensagens_resumida ON mensagens (resumida);
CREATE INDEX IF NOT EXISTS idx_resumos_canal      ON resumos (canal_id);
""".format(dim=_EMBED_DIM)

# HNSW é superior ao IVFFlat para bases pequenas (< 500k rows):
# recall quase perfeito, construção incremental, sem necessidade de VACUUM periódico.
# m=16 (conexões por nó) e ef_construction=64 são defaults seguros.
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


class MemoriaVetorial:
    """
    Gerencia a memória de longo prazo do bot usando PostgreSQL + pgvector.

    Uso típico (dentro do on_ready do bot):
        mem = MemoriaVetorial(db_url=DATABASE_URL, openai_key=OPENAI_API_KEY)
        await mem.inicializar()

    Depois, no on_message:
        await mem.ingerir_mensagem(...)

    E no responder_com_groq:
        ctx = await mem.buscar_contexto(pergunta)
    """

    def __init__(self, db_url: str, openai_key: str = ""):
        self._db_url   = db_url
        self._oai_key  = openai_key
        self._pool: Optional["asyncpg.Pool"] = None
        self._oai: Optional["AsyncOpenAI"]   = None
        # Cache LRU: reutiliza embeddings já gerados
        self._embed_cache = _LRUCache(maxsize=512)
        # Fila de batch insert: acumula até 10 mensagens ou 5s antes de persistir
        self._fila_batch: list[dict] = []
        self._flush_task: Optional[asyncio.Task] = None

    # ── Inicialização ─────────────────────────────────────────────────────────

    async def inicializar(self) -> None:
        """Cria o pool de conexões e garante que o schema existe."""
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
            # Tenta criar índices vetoriais (ignora falha se tabela vazia)
            for sql_idx in (_SQL_IDX_MENSAGENS, _SQL_IDX_RESUMOS):
                try:
                    await conn.execute(sql_idx)
                except Exception:
                    pass

        if OPENAI_OK and self._oai_key:
            self._oai = AsyncOpenAI(api_key=self._oai_key)

        # Inicia worker de flush em background
        self._flush_task = asyncio.ensure_future(self._flush_worker())

        log.info("[CEREBRO] Pool PostgreSQL criado e schema inicializado.")

    # ── Embeddings ────────────────────────────────────────────────────────────

    async def _embed(self, texto: str) -> Optional[list[float]]:
        """Gera embedding via OpenAI. Usa cache LRU para evitar chamadas repetidas."""
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
        """Converte lista de floats para string no formato pgvector '[1.0,2.0,...]'."""
        return "[" + ",".join(f"{v:.6f}" for v in vec) + "]"

    # ── Ingestão ──────────────────────────────────────────────────────────────

    async def ingerir_mensagem(
        self,
        canal_id: int,
        canal_nome: str,
        autor_id: int,
        autor_nome: str,
        conteudo: str,
        ts: Optional[datetime] = None,
    ) -> None:
        """
        Enfileira a mensagem para persistência em batch.
        O flush ocorre automaticamente a cada 10 mensagens ou 5 segundos.
        """
        if not self._pool:
            return
        if ts is None:
            ts = datetime.now(timezone.utc)
        self._fila_batch.append({
            "canal_id":   canal_id,
            "canal_nome": canal_nome,
            "autor_id":   autor_id,
            "autor_nome": autor_nome,
            "conteudo":   conteudo[:2000],
            "ts":         ts,
        })
        if len(self._fila_batch) >= 10:
            await self._flush_batch()

    async def _flush_worker(self) -> None:
        """Worker contínuo: faz flush da fila a cada 5 segundos."""
        while True:
            await asyncio.sleep(5)
            if self._fila_batch:
                await self._flush_batch()

    async def _flush_batch(self) -> None:
        """
        Persiste todas as mensagens na fila de uma vez.
        Gera embeddings em paralelo e faz um único executemany no banco.
        """
        if not self._fila_batch:
            return
        lote, self._fila_batch = self._fila_batch, []

        # Gera embeddings em paralelo para todo o lote
        embeddings = await asyncio.gather(
            *[self._embed(m["conteudo"]) for m in lote],
            return_exceptions=True,
        )

        registros_com = []
        registros_sem = []
        for msg, emb in zip(lote, embeddings):
            if isinstance(emb, list):
                registros_com.append((
                    msg["canal_id"], msg["canal_nome"],
                    msg["autor_id"], msg["autor_nome"],
                    msg["conteudo"], msg["ts"],
                    self._vec_str(emb),
                ))
            else:
                registros_sem.append((
                    msg["canal_id"], msg["canal_nome"],
                    msg["autor_id"], msg["autor_nome"],
                    msg["conteudo"], msg["ts"],
                ))

        try:
            async with self._pool.acquire() as conn:
                if registros_com:
                    await conn.executemany(
                        """
                        INSERT INTO mensagens
                            (canal_id, canal_nome, autor_id, autor_nome, conteudo, ts, embedding)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                        """,
                        registros_com,
                    )
                if registros_sem:
                    await conn.executemany(
                        """
                        INSERT INTO mensagens
                            (canal_id, canal_nome, autor_id, autor_nome, conteudo, ts)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        registros_sem,
                    )
            log.debug(f"[CEREBRO] Batch flush: {len(lote)} mensagens persistidas.")
        except Exception as e:
            log.warning(f"[CEREBRO] Falha no batch flush: {e}")

    # ── Busca ─────────────────────────────────────────────────────────────────

    async def buscar_contexto(
        self,
        pergunta: str,
        top_k: int = 5,
        limiar: float = 0.60,
        canal_id: Optional[int] = None,
    ) -> str:
        """
        Busca as mensagens/resumos mais relevantes para a pergunta.
        - UNION ALL numa única conexão (era dois SELECTs separados)
        - Deduplicação por similaridade vetorial real (cosseno > 0.92)
        - Penalidade temporal: mensagens antigas pesam menos
        - limiar 0.60: captura paráfrases e contextos relacionados
        Retorna string formatada para injetar no system prompt, ou '' se nada relevante.
        """
        if not self._pool or not self._oai:
            return ""

        embedding = await self._embed(pergunta)
        if embedding is None:
            return ""

        vec = self._vec_str(embedding)
        canal_filtro = "AND canal_id = $3" if canal_id else ""
        params = [vec, top_k * 3, canal_id] if canal_id else [vec, top_k * 3]

        try:
            async with self._pool.acquire() as conn:
                rows = await conn.fetch(
                    f"""
                    SELECT fonte, autor_label, conteudo, ts,
                           1 - (embedding <=> $1::vector) AS sim,
                           embedding::text AS emb_txt
                    FROM (
                        SELECT 'msg'   AS fonte,
                               autor_nome AS autor_label,
                               conteudo, ts, embedding
                        FROM mensagens
                        WHERE embedding IS NOT NULL
                          AND resumida = FALSE
                          {canal_filtro}
                        ORDER BY embedding <=> $1::vector
                        LIMIT $2

                        UNION ALL

                        SELECT 'resumo' AS fonte,
                               canal_nome AS autor_label,
                               conteudo, periodo_ini AS ts, embedding
                        FROM resumos
                        WHERE embedding IS NOT NULL
                          {canal_filtro}
                        ORDER BY embedding <=> $1::vector
                        LIMIT $2
                    ) sub
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    *params,
                )
        except Exception as e:
            log.debug(f"[CEREBRO] Falha na busca vetorial: {e}")
            return ""

        agora = datetime.now(timezone.utc)

        # Monta candidatos com score ajustado por tempo
        candidatos: list[dict] = []
        for r in rows:
            sim = float(r["sim"])
            if sim < limiar:
                continue
            ts = r["ts"]
            dias = (agora - ts.replace(tzinfo=timezone.utc)).days if ts else 0
            penalidade = min(0.10, dias / 700)
            score = sim - penalidade

            if r["fonte"] == "resumo":
                data = ts.strftime("%d/%m/%Y") if ts else "?"
                texto = f"[Resumo #{r['autor_label']} em {data}]\n{r['conteudo'][:350]}"
            else:
                data = ts.strftime("%d/%m %H:%M") if ts else "?"
                texto = f"[{r['autor_label']} em {data}] {r['conteudo'][:350]}"

            # Parseia vetor para deduplicação real
            try:
                emb_vec = [float(x) for x in r["emb_txt"].strip("[]").split(",")]
            except Exception:
                emb_vec = None

            candidatos.append({"score": score, "texto": texto, "vec": emb_vec})

        if not candidatos:
            return ""

        candidatos.sort(key=lambda x: x["score"], reverse=True)

        # Deduplicação por similaridade vetorial (cosseno > 0.92 = duplicata)
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
                continue  # duplicata vetorial — descarta
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

    # ── Sumarização diária ────────────────────────────────────────────────────

    async def sumarizar_e_limpar(self, groq_api_key: str, max_msgs: int = 200) -> int:
        """
        Sumariza lotes de mensagens antigas por canal e as marca como resumidas.
        Retorna o número de resumos gerados.
        Chamado uma vez por dia pela task _task_sumarizacao_diaria.
        """
        if not self._pool:
            return 0

        # Usa a mesma base (openai) se disponível, senão tenta groq
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
                # Busca canais que têm mensagens não resumidas (mais antigas que 48h)
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

                # Monta texto para sumarização
                linhas = []
                for m in msgs:
                    data = m["ts"].strftime("%d/%m %H:%M") if m["ts"] else "?"
                    linhas.append(f"[{data}] {m['autor_nome']}: {m['conteudo']}")
                texto_raw = "\n".join(linhas)

                # Chama IA para resumir
                # 6 000 chars ≈ 1 500 tokens — cobre bem 200 mensagens de canal ativo
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
                                "content": (
                                    f"Canal #{canal_nome}:\n\n{texto_raw[:6000]}"
                                ),
                            },
                        ],
                    )
                    resumo_txt = resp.choices[0].message.content.strip()
                except Exception as e:
                    log.debug(f"[CEREBRO] Falha ao gerar resumo para #{canal_nome}: {e}")
                    continue

                # Embedding do resumo
                emb_resumo = await self._embed(resumo_txt)

                periodo_ini = msgs[0]["ts"]
                periodo_fim = msgs[-1]["ts"]
                ids_resumidos = [m["id"] for m in msgs]

                async with self._pool.acquire() as conn:
                    # Insere resumo
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

                    # Marca mensagens como resumidas
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

        # Limpeza física: remove mensagens resumidas com mais de 30 dias
        # Mantém apenas os resumos — impede crescimento infinito da tabela
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
        """Fecha o pool de conexões (chamar ao desligar o bot, opcional)."""
        if self._flush_task:
            self._flush_task.cancel()
        if self._fila_batch:
            await self._flush_batch()  # drena o que sobrou antes de fechar
        if self._pool:
            await self._pool.close()
            self._pool = None
            log.info("[CEREBRO] Pool PostgreSQL fechado.")
