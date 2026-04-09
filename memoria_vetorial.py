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
import json
import logging
from datetime import datetime, timezone
from typing import Optional

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

        log.info("[CEREBRO] Pool PostgreSQL criado e schema inicializado.")

    # ── Embeddings ────────────────────────────────────────────────────────────

    async def _embed(self, texto: str) -> Optional[list[float]]:
        """Gera embedding via OpenAI. Retorna None se indisponível."""
        if not self._oai:
            return None
        texto = texto[:8000]  # limite de segurança
        try:
            resp = await self._oai.embeddings.create(
                model=_EMBED_MODEL,
                input=texto,
            )
            return resp.data[0].embedding
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
        Persiste uma mensagem do Discord com seu embedding.
        Chamado de forma assíncrona no on_message — falhas são silenciosas.
        """
        if not self._pool:
            return
        if ts is None:
            ts = datetime.now(timezone.utc)

        embedding = await self._embed(conteudo)

        try:
            async with self._pool.acquire() as conn:
                if embedding is not None:
                    await conn.execute(
                        """
                        INSERT INTO mensagens
                            (canal_id, canal_nome, autor_id, autor_nome, conteudo, ts, embedding)
                        VALUES ($1, $2, $3, $4, $5, $6, $7::vector)
                        """,
                        canal_id, canal_nome, autor_id, autor_nome,
                        conteudo[:2000], ts, self._vec_str(embedding),
                    )
                else:
                    # Sem embedding — persiste só texto (busca vetorial não funcionará)
                    await conn.execute(
                        """
                        INSERT INTO mensagens
                            (canal_id, canal_nome, autor_id, autor_nome, conteudo, ts)
                        VALUES ($1, $2, $3, $4, $5, $6)
                        """,
                        canal_id, canal_nome, autor_id, autor_nome,
                        conteudo[:2000], ts,
                    )
        except Exception as e:
            log.debug(f"[CEREBRO] Falha ao ingerir mensagem: {e}")

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
        - limiar 0.60: captura paráfrases e contextos relacionados (0.75 era restritivo demais)
        - Ordenação final por similaridade real (não por tabela)
        - Penalidade temporal: mensagens antigas pesam menos
        - Deduplicação por similaridade entre resultados (sim > 0.92 = duplicata)
        - canal_id opcional: filtra por canal quando fornecido
        Retorna string formatada para injetar no system prompt, ou '' se nada relevante.
        """
        if not self._pool or not self._oai:
            return ""

        embedding = await self._embed(pergunta)
        if embedding is None:
            return ""

        vec = self._vec_str(embedding)

        try:
            async with self._pool.acquire() as conn:
                # Filtro de canal opcional
                canal_filtro_msg = "AND canal_id = $3" if canal_id else ""
                canal_filtro_res = "AND canal_id = $3" if canal_id else ""
                params_msg = [vec, top_k * 3, canal_id] if canal_id else [vec, top_k * 3]
                params_res = [vec, max(3, top_k), canal_id] if canal_id else [vec, max(3, top_k)]

                # Busca em mensagens não-resumidas — recupera mais do que top_k para filtrar depois
                rows_msg = await conn.fetch(
                    f"""
                    SELECT autor_nome, conteudo, ts,
                           1 - (embedding <=> $1::vector) AS sim
                    FROM mensagens
                    WHERE embedding IS NOT NULL
                      AND resumida = FALSE
                      {canal_filtro_msg}
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    *params_msg,
                )

                # Busca em resumos
                rows_res = await conn.fetch(
                    f"""
                    SELECT canal_nome, conteudo, periodo_ini AS ts,
                           1 - (embedding <=> $1::vector) AS sim
                    FROM resumos
                    WHERE embedding IS NOT NULL
                      {canal_filtro_res}
                    ORDER BY embedding <=> $1::vector
                    LIMIT $2
                    """,
                    *params_res,
                )
        except Exception as e:
            log.debug(f"[CEREBRO] Falha na busca vetorial: {e}")
            return ""

        from datetime import timezone as _tz
        agora = datetime.now(_tz.utc)

        # Monta lista unificada com score ajustado por tempo
        candidatos: list[dict] = []

        for r in rows_res:
            sim = float(r["sim"])
            if sim < limiar:
                continue
            ts = r["ts"]
            # Penalidade temporal: -0.01 por semana, máx -0.10
            dias = (agora - ts.replace(tzinfo=_tz.utc)).days if ts else 0
            penalidade = min(0.10, dias / 700)
            score = sim - penalidade
            data = ts.strftime("%d/%m/%Y") if ts else "?"
            candidatos.append({
                "score": score,
                "texto": f"[Resumo #{r['canal_nome']} em {data}]\n{r['conteudo'][:350]}",
            })

        for r in rows_msg:
            sim = float(r["sim"])
            if sim < limiar:
                continue
            ts = r["ts"]
            dias = (agora - ts.replace(tzinfo=_tz.utc)).days if ts else 0
            penalidade = min(0.10, dias / 700)
            score = sim - penalidade
            data = ts.strftime("%d/%m %H:%M") if ts else "?"
            candidatos.append({
                "score": score,
                "texto": f"[{r['autor_nome']} em {data}] {r['conteudo'][:350]}",
            })

        if not candidatos:
            return ""

        # Ordena por score descendente
        candidatos.sort(key=lambda x: x["score"], reverse=True)

        # Deduplicação simples: descarta candidatos cujo texto começa igual (primeiros 60 chars)
        vistos: set[str] = set()
        resultados: list[str] = []
        for c in candidatos:
            chave = c["texto"][:60].lower().strip()
            if chave in vistos:
                continue
            vistos.add(chave)
            resultados.append(c["texto"])
            if len(resultados) >= top_k:
                break

        if not resultados:
            return ""

        bloco = "\n".join(resultados)
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

        return n_resumos

    # ── Utilitários ───────────────────────────────────────────────────────────

    async def fechar(self) -> None:
        """Fecha o pool de conexões (chamar ao desligar o bot, opcional)."""
        if self._pool:
            await self._pool.close()
            self._pool = None
            log.info("[CEREBRO] Pool PostgreSQL fechado.")
