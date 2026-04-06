import discord
import re
import aiohttp
import json
import os
import io
import xml.etree.ElementTree as ET
import random
import logging
from collections import defaultdict
from datetime import datetime, timedelta, timezone

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("shell")

try:
    from openai import AsyncOpenAI
    GROQ_DISPONIVEL = True
except ImportError:
    GROQ_DISPONIVEL = False
    log.warning("Pacote openai não encontrado. Respostas via IA desativadas.")

def agora_utc():
    return datetime.now(timezone.utc)

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
SERVIDOR_ID = 1487599082825584761

# IDs de donos/proprietários (maior hierarquia — nunca punidos, comandos sempre ativos)
DONOS_IDS = {1487591389653897306, 1321848653878661172, 1375560046930563306}

# Cargos superiores que podem dar ordens gerais ao bot (boas-vindas, histórias, etc.)
CARGOS_SUPERIORES_IDS = {1487599082934636628, 1487599082934636627}

# ID de usuário com nível de superior (tratado como cargo superior)
USUARIOS_SUPERIORES_IDS = {1375560046930563306}

# IDs de donos absolutos — maior hierarquia, podem apagar canais/cargos pelo bot
DONOS_ABSOLUTOS_IDS = {1487591389653897306, 1321848653878661172}
CONTAS_TESTE = set()  # sem contas de teste no momento
CARGO_EQUIPE_MOD_ID = 1487859369008697556  # equipe de moderação com acesso a comandos de mod

# ── Canal de auditoria ───────────────────────────────────────────────────────
CANAL_AUDITORIA_ID = 1490180079899115591

# ── Chave da API VirusTotal ──────────────────────────────────────────────────
# Coloque sua chave aqui: https://www.virustotal.com/gui/my-apikey
VIRUSTOTAL_API_KEY = "SUA_CHAVE_AQUI"

client = discord.Client()

_bot_inicio: datetime = datetime.now(timezone.utc)  # timestamp de início do processo


# ═══════════════════════════════════════════════════════════════════════════════

DISCORD_API = "https://discord.com/api/v10"


def _headers_discord() -> dict:
    return {"Authorization": TOKEN, "Content-Type": "application/json"}


async def api_get(endpoint: str) -> dict | list | None:
    """GET genérico na API REST do Discord. Retorna JSON ou None em erro."""
    url = f"{DISCORD_API}{endpoint}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, headers=_headers_discord()) as r:
                if r.status == 200:
                    return await r.json()
                log.warning(f"Discord API GET {endpoint} → HTTP {r.status}")
                return None
    except Exception as e:
        log.error(f"api_get {endpoint}: {e}")
        return None


async def api_get_paginado(endpoint: str, limite: int = 100) -> list:
    """GET paginado usando cursor `after`. Retorna lista com até `limite` itens."""
    resultados = []
    after = None
    while len(resultados) < limite:
        params = f"?limit={min(100, limite - len(resultados))}"
        if after:
            params += f"&after={after}"
        dados = await api_get(f"{endpoint}{params}")
        if not dados:
            break
        itens = dados if isinstance(dados, list) else []
        if not itens:
            break
        resultados.extend(itens)
        after = itens[-1].get("id") if isinstance(itens[-1], dict) else None
        if len(itens) < 100:
            break
    return resultados[:limite]


async def api_membro(guild_id: int, user_id: int) -> dict | None:
    """Dados frescos de membro via REST (inclui communication_disabled_until)."""
    return await api_get(f"/guilds/{guild_id}/members/{user_id}")


async def api_ban_entry(guild_id: int, user_id: int) -> dict | None:
    """Verifica se usuário está banido e retorna a entrada."""
    return await api_get(f"/guilds/{guild_id}/bans/{user_id}")


async def api_banimentos(guild_id: int, limite: int = 50) -> list[dict]:
    """Lista de banimentos do servidor."""
    dados = await api_get(f"/guilds/{guild_id}/bans?limit={min(limite, 1000)}")
    return dados if isinstance(dados, list) else []


async def api_audit_log(guild_id: int, tipo: int = None, limite: int = 25) -> list[dict]:
    """
    Audit log do servidor.
    Tipos: 20=BAN | 22=UNBAN | 24=KICK | 25=MEMBER_UPDATE(timeout) | 26=MEMBER_ROLE_UPDATE
    """
    endpoint = f"/guilds/{guild_id}/audit-logs?limit={min(limite, 100)}"
    if tipo:
        endpoint += f"&action_type={tipo}"
    dados = await api_get(endpoint)
    return dados.get("audit_log_entries", []) if dados else []


async def api_mensagens_canal(canal_id: int, limite: int = 50) -> list[dict]:
    """Últimas mensagens de um canal via REST."""
    dados = await api_get(f"/channels/{canal_id}/messages?limit={min(limite, 100)}")
    return dados if isinstance(dados, list) else []


async def api_guild_info(guild_id: int) -> dict | None:
    """Info completa do servidor incluindo approximate_member_count e presence_count."""
    return await api_get(f"/guilds/{guild_id}?with_counts=true")


async def api_membros_todos(guild_id: int, limite: int = 1000) -> list[dict]:
    """Lista todos os membros via REST paginada."""
    return await api_get_paginado(f"/guilds/{guild_id}/members", limite)


# ── Funções de análise usando a API REST ─────────────────────────────────────

async def api_info_membro_completa(guild: discord.Guild, membro: discord.Member) -> str:
    """
    Info completa combinando cache discord.py + REST em tempo real.
    Inclui timeout ativo, cargos atualizados, infrações locais.
    """
    agora = agora_utc()
    dados_api = await api_membro(guild.id, membro.id)

    # Timeout ativo via REST (campo communication_disabled_until)
    timeout_ativo = ""
    if dados_api:
        ts_timeout = dados_api.get("communication_disabled_until")
        if ts_timeout:
            try:
                timeout_dt = datetime.fromisoformat(ts_timeout.replace("Z", "+00:00"))
                if timeout_dt > agora:
                    mins = int((timeout_dt - agora).total_seconds() / 60)
                    timeout_ativo = f" | SILENCIADO — {mins} min restantes"
            except Exception:
                pass

    # Datas
    conta_criada = membro.created_at.replace(tzinfo=timezone.utc)
    entrou = membro.joined_at.replace(tzinfo=timezone.utc) if membro.joined_at else None
    idade_conta = formatar_duracao(agora - conta_criada)
    tempo_servidor = formatar_duracao(agora - entrou) if entrou else "desconhecido"

    # Cargos (API REST > cache)
    if dados_api and "roles" in dados_api:
        cargos_nomes = []
        for rid in dados_api["roles"]:
            role = guild.get_role(int(rid))
            if role and role.name != "@everyone":
                cargos_nomes.append(role.name)
        cargos_txt = ", ".join(cargos_nomes) if cargos_nomes else "nenhum"
    else:
        cargos_txt = ", ".join(r.name for r in membro.roles if r.name != "@everyone") or "nenhum"

    # Dados locais
    infr = infracoes.get(membro.id, 0)
    silenc = silenciamentos.get(membro.id, 0)
    n_ent = len(registro_entradas.get(membro.id, []))
    extras = []
    if infr:
        extras.append(f"infrações: {infr}")
    if silenc:
        extras.append(f"silenciamentos locais: {silenc}")
    if n_ent > 1:
        extras.append(f"entrou {n_ent}x")
    extras_txt = " | " + ", ".join(extras) if extras else ""

    return (
        f"{membro.display_name} (ID {membro.id}){timeout_ativo}\n"
        f"  Conta criada há {idade_conta} | No servidor há {tempo_servidor}\n"
        f"  Cargos: {cargos_txt}{extras_txt}"
    )


async def api_resumo_servidor(guild: discord.Guild) -> str:
    """Resumo do servidor com dados em tempo real via REST."""
    dados = await api_guild_info(guild.id)
    brasilia = timezone(timedelta(hours=-3))
    criado_em = guild.created_at.astimezone(brasilia).strftime("%d/%m/%Y")

    if dados:
        total = dados.get("approximate_member_count", guild.member_count)
        online = dados.get("approximate_presence_count", "?")
        boost_nivel = dados.get("premium_tier", guild.premium_tier)
        boost_count = dados.get("premium_subscription_count", guild.premium_subscription_count)
    else:
        total = guild.member_count
        online = "?"
        boost_nivel = guild.premium_tier
        boost_count = guild.premium_subscription_count

    bots = sum(1 for m in guild.members if m.bot)
    humanos = total - bots
    canais_texto = len([c for c in guild.channels if isinstance(c, discord.TextChannel)])
    canais_voz = len([c for c in guild.channels if isinstance(c, discord.VoiceChannel)])
    n_cargos = len([r for r in guild.roles if r.name != "@everyone"])

    return (
        f"**{guild.name}** — criado em {criado_em}\n"
        f"Membros: {humanos} humanos + {bots} bots = {total} total | online agora: {online}\n"
        f"Canais: {canais_texto} texto, {canais_voz} voz | Cargos: {n_cargos}\n"
        f"Boost: nível {boost_nivel} ({boost_count} boosts)"
    )


async def api_historico_punicoes(guild: discord.Guild, alvo: discord.Member | None = None) -> str:
    """
    Busca punições no audit log do servidor.
    Tipos cobertos: BAN (20), UNBAN (22), KICK (24), MEMBER_UPDATE/timeout (25).
    """
    brasilia = timezone(timedelta(hours=-3))
    linhas = []

    for tipo, nome in [(20, "BAN"), (22, "UNBAN"), (24, "KICK"), (25, "TIMEOUT")]:
        entradas = await api_audit_log(guild.id, tipo=tipo, limite=15)
        for e in entradas:
            alvo_id = int(e.get("target_id", 0))
            if alvo and alvo_id != alvo.id:
                continue
            resp_id = e.get("user_id", "?")
            motivo = e.get("reason") or "sem motivo"
            # Converte snowflake em timestamp
            ts_ms = (int(e["id"]) >> 22) + 1420070400000
            dt = datetime.fromtimestamp(ts_ms / 1000, tz=brasilia)
            alvo_nome = alvo.display_name if alvo else str(alvo_id)
            # Tenta nome do executor via cache
            resp_membro = guild.get_member(int(resp_id)) if resp_id != "?" else None
            resp_nome = resp_membro.display_name if resp_membro else f"ID {resp_id}"
            linhas.append(f"  [{nome}] {alvo_nome} | por {resp_nome} | {dt.strftime('%d/%m/%Y %H:%M')} | {motivo}")

    if not linhas:
        return "Nenhuma punição encontrada no audit log."
    return "\n".join(linhas)


async def api_banimentos_formatado(guild: discord.Guild, limite: int = 20) -> str:
    """Lista os banimentos ativos do servidor com motivo."""
    bans = await api_banimentos(guild.id, limite)
    if not bans:
        return "Nenhum banimento ativo no servidor."
    linhas = [f"Banimentos ativos ({len(bans)} mostrados):"]
    for b in bans:
        user = b.get("user", {})
        nome = user.get("username", "?")
        uid = user.get("id", "?")
        motivo = b.get("reason") or "sem motivo"
        linhas.append(f"  {nome} ({uid}) — {motivo}")
    return "\n".join(linhas)


async def api_ultimas_mensagens(guild: discord.Guild, canal_id: int, limite: int = 20) -> str:
    """Últimas mensagens de um canal via REST."""
    brasilia = timezone(timedelta(hours=-3))
    msgs = await api_mensagens_canal(canal_id, limite)
    if not msgs:
        return "Não foi possível buscar mensagens desse canal."
    canal = guild.get_channel(canal_id)
    nome_canal = f"#{canal.name}" if canal else f"canal {canal_id}"
    linhas = [f"Últimas {len(msgs)} mensagens de {nome_canal}:"]
    for m in reversed(msgs):
        autor_nome = m.get("author", {}).get("username", "?")
        conteudo = m.get("content", "") or "[sem texto]"
        ts_str = m.get("timestamp", "")
        try:
            dt = datetime.fromisoformat(ts_str.replace("Z", "+00:00")).astimezone(brasilia)
            hora = dt.strftime("%H:%M")
        except Exception:
            hora = "?"
        linhas.append(f"  [{hora}] {autor_nome}: {conteudo[:80]}")
    return "\n".join(linhas)


def tem_permissao_moderacao(guild: discord.Guild) -> bool:
    """Verifica se a conta tem permissão de administrador ou moderação no servidor."""
    membro_self = guild.get_member(client.user.id)
    if membro_self is None:
        return False
    perms = membro_self.guild_permissions
    return perms.administrator or perms.moderate_members or perms.manage_messages


def eh_autorizado(member: discord.Member) -> bool:
    """Retorna True se o membro é dono, superior ou pertence à equipe de moderação."""
    if member.id in DONOS_IDS or member.id in CONTAS_TESTE:
        return True
    if member.id in USUARIOS_SUPERIORES_IDS:
        return True
    return any(cargo.id in CARGOS_SUPERIORES_IDS or cargo.id == CARGO_EQUIPE_MOD_ID for cargo in member.roles)


def eh_superior(member: discord.Member) -> bool:
    """Retorna True se o membro é dono ou tem cargo superior (pode dar ordens gerais ao bot)."""
    if member.id in DONOS_IDS or member.id in USUARIOS_SUPERIORES_IDS:
        return True
    return any(cargo.id in CARGOS_SUPERIORES_IDS for cargo in member.roles)


def eh_mod_exclusivo(member: discord.Member) -> bool:
    """Retorna True se membro tem cargo de moderação (mas não é superior nem dono)."""
    if member.id in DONOS_IDS or member.id in USUARIOS_SUPERIORES_IDS:
        return False
    if any(cargo.id in CARGOS_SUPERIORES_IDS for cargo in member.roles):
        return False
    return any(cargo.id == CARGO_EQUIPE_MOD_ID for cargo in member.roles)

DADOS_PATH = "dados.json"

# Palavras customizadas adicionadas pelos donos em tempo real
palavras_custom: dict[str, list[str]] = {
    "vulgares": [], "sexual": [], "discriminacao": [], "compostos": []
}

CATEGORIAS_ALIAS = {
    "vulgar": "vulgares", "palavrao": "vulgares", "xingamento": "vulgares",
    "vulgares": "vulgares", "palavroes": "vulgares",
    "sexual": "sexual", "adulto": "sexual", "18": "sexual", "explicit": "sexual",
    "discriminacao": "discriminacao", "racismo": "discriminacao",
    "preconceito": "discriminacao", "lgbtfobia": "discriminacao", "bullying": "discriminacao",
    "composto": "compostos", "compostos": "compostos", "palavra composta": "compostos",
}

def inferir_categoria(texto: str) -> str:
    """Tenta descobrir a categoria pelo contexto da mensagem. Padrão: vulgares."""
    t = texto.lower()
    for alias, cat in CATEGORIAS_ALIAS.items():
        if alias in t:
            return cat
    return "vulgares"

def carregar_dados():
    global infracoes, ultimo_motivo, silenciamentos, palavras_custom
    global registro_entradas, registro_saidas, nomes_historico
    if not os.path.exists(DADOS_PATH):
        return
    try:
        with open(DADOS_PATH, "r") as f:
            dados = json.load(f)
        for k, v in dados.get("infracoes", {}).items():
            infracoes[int(k)] = v
        for k, v in dados.get("ultimo_motivo", {}).items():
            ultimo_motivo[int(k)] = v
        for k, v in dados.get("silenciamentos", {}).items():
            silenciamentos[int(k)] = v
        for cat in palavras_custom:
            palavras_custom[cat] = dados.get("palavras_custom", {}).get(cat, [])
        for k, v in dados.get("registro_entradas", {}).items():
            registro_entradas[int(k)] = v
        for k, v in dados.get("registro_saidas", {}).items():
            registro_saidas[int(k)] = v
        for k, v in dados.get("nomes_historico", {}).items():
            nomes_historico[int(k)] = v
        total = sum(len(v) for v in palavras_custom.values())
        log.info(f"{len(infracoes)} usuários, {total} palavras customizadas, "
                 f"{len(registro_entradas)} históricos de entrada carregados.")
    except Exception as e:
        log.error(f"Erro ao carregar dados: {e}")

def salvar_dados():
    """Escrita atômica: grava em arquivo temporário e renomeia, evitando corrupção."""
    import tempfile
    payload = {
        "infracoes": {str(k): v for k, v in infracoes.items()},
        "ultimo_motivo": {str(k): v for k, v in ultimo_motivo.items()},
        "silenciamentos": {str(k): v for k, v in silenciamentos.items()},
        "palavras_custom": palavras_custom,
        "registro_entradas": {str(k): v for k, v in registro_entradas.items()},
        "registro_saidas": {str(k): v for k, v in registro_saidas.items()},
        "nomes_historico": {str(k): v for k, v in nomes_historico.items()},
    }
    try:
        dir_ = os.path.dirname(os.path.abspath(DADOS_PATH)) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            os.replace(tmp, DADOS_PATH)
        except Exception:
            os.unlink(tmp)
            raise
    except Exception as e:
        log.error(f"Erro ao salvar dados: {e}")

# Histórico de flood, infrações e conversas por usuário
historico_mensagens = defaultdict(list)
historico_conteudo: dict[int, list] = defaultdict(list)
infracoes: dict[int, int] = defaultdict(int)
silenciamentos: dict[int, int] = defaultdict(int)
ultimo_motivo: dict[int, str] = {}
conversas: dict[int, dict] = {}
ausencia: dict[int, dict] = {}
historico_groq: dict[tuple[int, int], list] = {}  # chave: (user_id, canal_id)
conversas_groq: dict[int, dict] = {}
TIMEOUT_CONVERSA_GROQ = timedelta(minutes=5)

# ── Rastreamento de entradas e saídas ─────────────────────────────────────────
# registro_entradas: user_id -> lista de ISO timestamps de cada entrada
registro_entradas: dict[int, list[str]] = {}
# registro_saidas: user_id -> lista de {"nome", "saiu" (ISO), "ficou_segundos"}
registro_saidas: dict[int, list[dict]] = {}
# nomes_historico: último nome conhecido de cada user_id (inclui quem já saiu)
nomes_historico: dict[int, str] = {}

# ── Raid detection ────────────────────────────────────────────────────────────
_joins_recentes: list[datetime] = []          # timestamps dos últimos joins
RAID_JANELA   = timedelta(minutes=2)          # janela de análise
RAID_LIMIAR   = 5                             # joins para disparar alerta
RAID_CONTA_NOVA_DIAS = 7                      # conta com menos de X dias = suspeita

GATILHOS_NOME = re.compile(r"\bshell\b", re.IGNORECASE)

CANAL_REGRAS_ID = 1487599083869704326
CANAL_REGRAS = f"<#{CANAL_REGRAS_ID}>"

REGRAS = f"""**REGRAS GERAIS**
1. Respeite os membros.
2. Respeite as autoridades maiorais.
3. Respeite as decisões dos moderadores.
4. Evite marcar excessivamente os administradores e moderadores.

**REGRAS DOS CANAIS**
1. Não flood ou spaming dentro dos canais.
2. Não use conteúdo adulto e explícito nos canais de texto e chat de voz.
3. Não divulgue outros servidores sem o consensso dos moderadores.
4. Não pratique discriminações ou bullying.
5. Não utilize o uso do vocabulário vulgar para ofender alguém.

**REGRAS DO DISCORD**
1. Siga os termos do Discord.
2. Siga as diretrizes do Discord.

Regras completas em {CANAL_REGRAS}."""

# ── Cache de notícias ─────────────────────────────────────────────────────────
_cache_noticias: list[dict] = []       # [{titulo, link, fonte}]
_ultima_busca_noticias: datetime | None = None
INTERVALO_NOTICIAS = timedelta(minutes=30)

FEEDS_RSS = [
    ("G1 Mundo",   "https://g1.globo.com/rss/g1/mundo/"),
    ("G1 Brasil",  "https://g1.globo.com/rss/g1/"),
    ("G1 Tech",    "https://g1.globo.com/rss/g1/tecnologia/"),
    ("BBC Brasil", "https://www.bbc.com/portuguese/index.xml"),
    ("UOL",        "https://rss.uol.com.br/feed/noticias.xml"),
]

async def buscar_noticias() -> list[dict]:
    global _cache_noticias, _ultima_busca_noticias
    agora = datetime.now()
    if _ultima_busca_noticias and agora - _ultima_busca_noticias < INTERVALO_NOTICIAS and _cache_noticias:
        return _cache_noticias

    noticias = []
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    }
    try:
        async with aiohttp.ClientSession(headers=headers) as session:
            for fonte, url in FEEDS_RSS:
                try:
                    async with session.get(url, timeout=aiohttp.ClientTimeout(total=8), allow_redirects=True) as r:
                        if r.status != 200:
                            print(f"[NEWS] {fonte}: HTTP {r.status}")
                            continue
                        texto = await r.text(errors="replace")
                        root = ET.fromstring(texto)
                        itens = root.findall(".//item") or root.findall(".//{http://www.w3.org/2005/Atom}entry")
                        for item in itens[:4]:
                            titulo = (
                                item.findtext("title") or
                                item.findtext("{http://www.w3.org/2005/Atom}title") or ""
                            ).strip()
                            if titulo and len(titulo) > 10:
                                noticias.append({"titulo": titulo, "fonte": fonte})
                        if itens:
                            print(f"[NEWS] {fonte}: {len(itens)} itens carregados")
                except Exception as e:
                    print(f"[NEWS] {fonte}: erro {e}")
                    continue
    except Exception as e:
        print(f"[NEWS] Sessão HTTP falhou: {e}")

    if noticias:
        _cache_noticias = noticias
        _ultima_busca_noticias = agora
        print(f"[NEWS] Cache atualizado: {len(noticias)} notícias")
    else:
        print("[NEWS] Nenhuma notícia obtida, mantendo cache anterior")
    return _cache_noticias


async def info_membro(membro: discord.Member) -> str:
    agora = agora_utc()
    conta_criada = membro.created_at.replace(tzinfo=timezone.utc) if membro.created_at.tzinfo is None else membro.created_at
    entrou = membro.joined_at.replace(tzinfo=timezone.utc) if membro.joined_at and membro.joined_at.tzinfo is None else membro.joined_at

    idade_conta = formatar_duracao(agora - conta_criada)
    tempo_servidor = formatar_duracao(agora - entrou) if entrou else "desconhecido"

    cargos = [c.name for c in membro.roles if c.name != "@everyone"]
    cargos_txt = ", ".join(cargos) if cargos else "nenhum"

    singularidades = []
    if (agora - conta_criada).days < 30:
        singularidades.append("conta recente")
    if entrou and (agora - entrou).days < 7:
        singularidades.append("entrou essa semana")
    if membro.bot:
        singularidades.append("conta automatizada")
    if len(cargos) >= 3:
        singularidades.append("membro ativo com vários cargos")
    sing_txt = ", ".join(singularidades) if singularidades else "nenhuma singularidade registrada"

    # Dados de rastreamento
    n_entradas = len(registro_entradas.get(membro.id, []))
    n_saidas = len(registro_saidas.get(membro.id, []))
    tracking_txt = ""
    if n_entradas > 0:
        tracking_txt = f" Entradas registradas: {n_entradas}."
    if n_saidas > 0:
        tracking_txt += f" Saídas registradas: {n_saidas}."

    return (
        f"{membro.display_name} tem conta criada há {idade_conta} "
        f"e está no servidor há {tempo_servidor}. "
        f"Cargos: {cargos_txt}. "
        f"Singularidades: {sing_txt}."
        f"{tracking_txt}"
    )


async def stats_servidor(guild: discord.Guild) -> str:
    membros = guild.members
    total = len(membros)
    bots = sum(1 for m in membros if m.bot)
    humanos = total - bots
    agora = agora_utc()

    mais_antigo = min(
        (m for m in membros if m.joined_at),
        key=lambda m: m.joined_at, default=None
    )
    mais_novo = max(
        (m for m in membros if m.joined_at),
        key=lambda m: m.joined_at, default=None
    )

    linhas = [
        f"O servidor tem {humanos} {'membro' if humanos == 1 else 'membros'} humanos e {bots} {'robô' if bots == 1 else 'robôs'}, totalizando {total}.",
    ]
    if mais_antigo:
        tempo = formatar_duracao(agora - mais_antigo.joined_at.replace(tzinfo=timezone.utc))
        linhas.append(f"Membro mais antigo: {mais_antigo.display_name}, há {tempo}.")
    if mais_novo and mais_novo != mais_antigo:
        tempo = formatar_duracao(agora - mais_novo.joined_at.replace(tzinfo=timezone.utc))
        linhas.append(f"Entrada mais recente: {mais_novo.display_name}, há {tempo}.")
    return " ".join(linhas)


async def relatorio_membros(guild: discord.Guild, periodo_dias: int = 7) -> str:
    """Relatório de entradas, saídas e fluxo do servidor no período."""
    brasilia = timezone(timedelta(hours=-3))
    agora = agora_utc()
    corte = agora - timedelta(days=periodo_dias)
    corte_iso = corte.isoformat()

    entradas_recentes = []
    for uid, timestamps in registro_entradas.items():
        for ts in timestamps:
            if ts >= corte_iso:
                membro = guild.get_member(uid)
                nome = membro.display_name if membro else nomes_historico.get(uid, f"ID {uid}")
                entradas_recentes.append((ts, nome, uid))

    saidas_recentes = []
    for uid, saidas in registro_saidas.items():
        for s in saidas:
            if s["saiu"] >= corte_iso:
                saidas_recentes.append((s["saiu"], s["nome"], uid, s.get("ficou_segundos")))

    entradas_recentes.sort(key=lambda x: x[0], reverse=True)
    saidas_recentes.sort(key=lambda x: x[0], reverse=True)

    total_humanos = sum(1 for m in guild.members if not m.bot)
    periodo_txt = "hoje" if periodo_dias == 1 else f"últimos {periodo_dias} dias"

    linhas = [
        f"Servidor: {total_humanos} membros humanos agora.",
        f"Período: {periodo_txt}.",
        "",
        f"Entradas: {len(entradas_recentes)}",
    ]
    for ts, nome, uid in entradas_recentes[:8]:
        dt = datetime.fromisoformat(ts).astimezone(brasilia)
        vezes = len(registro_entradas.get(uid, []))
        reincidencia = f" (vez {vezes})" if vezes > 1 else ""
        linhas.append(f"  {dt.strftime('%d/%m %H:%M')}  {nome}{reincidencia}")

    linhas += ["", f"Saídas: {len(saidas_recentes)}"]
    for ts, nome, uid, ficou in saidas_recentes[:8]:
        dt = datetime.fromisoformat(ts).astimezone(brasilia)
        ficou_txt = f" — ficou {formatar_duracao(timedelta(seconds=ficou))}" if ficou else ""
        linhas.append(f"  {dt.strftime('%d/%m %H:%M')}  {nome}{ficou_txt}")

    return "\n".join(linhas)


async def historico_membro(uid: int, nome_display: str) -> str:
    """Histórico completo de entradas e saídas de um membro."""
    brasilia = timezone(timedelta(hours=-3))
    entradas = sorted(registro_entradas.get(uid, []), reverse=True)
    saidas = sorted(registro_saidas.get(uid, []), key=lambda x: x["saiu"], reverse=True)

    linhas = [f"Histórico de {nome_display} ({uid}):", f"Entradas: {len(entradas)}"]
    for ts in entradas[:10]:
        dt = datetime.fromisoformat(ts).astimezone(brasilia)
        linhas.append(f"  Entrou: {dt.strftime('%d/%m/%Y %H:%M')}")

    linhas.append(f"Saídas: {len(saidas)}")
    for s in saidas[:10]:
        dt = datetime.fromisoformat(s["saiu"]).astimezone(brasilia)
        ficou = s.get("ficou_segundos")
        ficou_txt = f" (ficou {formatar_duracao(timedelta(seconds=ficou))})" if ficou else ""
        linhas.append(f"  Saiu:   {dt.strftime('%d/%m/%Y %H:%M')}{ficou_txt}")

    return "\n".join(linhas)


SUBSTITUICOES = str.maketrans({
    '0': 'o', '1': 'i', '3': 'e', '4': 'a', '5': 's', '7': 't',
    '@': 'a', '$': 's', '!': 'i', '+': 't',
    'à': 'a', 'á': 'a', 'â': 'a', 'ã': 'a',
    'é': 'e', 'ê': 'e', 'è': 'e',
    'í': 'i', 'ï': 'i',
    'ó': 'o', 'ô': 'o', 'õ': 'o',
    'ú': 'u', 'ü': 'u',
    'ç': 'c',
})

# ── Palavrões e xingamentos gerais ───────────────────────────────────────────
PALAVRAS_VULGARES = [
    "porra", "caralho", "merda", "foda", "fodase", "fodasse",
    "bosta", "bunda", "cu", "cuzao", "culhao", "arrombado",
    "safado", "safada", "vagabundo", "vagabunda", "vadia",
    "sacana", "babaca", "idiota", "imbecil", "otario", "otaria",
    "palhaco", "bronha", "punheta", "punhetao",
    "fdp", "vsf", "vtc", "fds", "krl", "pqp",
    "vai se foder", "vai tomar no", "tomar no cu",
    "vai a merda", "vai pro inferno",
    "rato no cu", "ratomanocu", "vai tomar no cu",
]

# Substrings vulgares em palavras compostas (sem verificação de limite de palavra)
COMPOSTOS_VULGARES = [
    "nocu", "nacu", "noculo", "paunocu", "fodase", "vtnc", "vsfd",
]

# ── Sexual / +18 ─────────────────────────────────────────────────────────────
CONTEUDO_SEXUAL = [
    "buceta", "xoxota", "xana", "chota", "crica", "fenda",
    "shereka", "xereca", "xerereca", "xoroca", "chereca",  # variantes vulgares
    "pica", "picao", "piroca", "piroco", "piru", "rola",
    "penis", "vagina", "clitoris", "glande",
    "boquete", "chupada", "felacao", "siririca",
    "transar", "foder", "comer", "meter",
    "porno", "pornografia", "putaria", "safadeza",
    "nude", "nudes", "pack", "xvideos", "pornhub",
    "pau",  # ambíguo: madeira / pênis — detectado por contexto fuzzy
]

# ── Racismo e discriminação étnica ───────────────────────────────────────────
RACISMO = [
    "macaco", "macaca", "crioulo", "criulo",
    "negao", "mulatao", "cabelo duro", "cabelo pixaim", "cabelo ruim",
    "preto feio", "negro feio", "preto de alma branca",
    "volta pra africa", "volta para africa", "nao sao gente",
    "sub-humano", "subhumano", "raça inferior", "raca inferior",
    "escravo", "escrava", "senzala", "quilombo sujo",
    "japoronga", "japinha", "carcamano", "bugre", "monhe", "chinoca",
    "vachina", "xing ling", "gringo sujo", "gringo lixo",
    "nordestino burro", "paraiba burro", "baiano burro",
    "judeu sujo", "nazi", "nazista", "holocausto foi bom",
    "genero inferior", "inferioridade racial", "limpeza racial",
]

# ── LGBTfobia ────────────────────────────────────────────────────────────────
LGBTFOBIA = [
    "viado", "viadao", "viadagem", "viada",
    "veado", "veadao", "veada",
    "bicha", "bichinha", "bixa",
    "boiola", "bolta", "bolagato",
    "sapatao", "gilete", "traveco", "travesti lixo",
    "cura gay", "doenca mental gay",
    "abominacao", "abominação",
]

# ── Capacitismo ───────────────────────────────────────────────────────────────
CAPACITISMO = [
    "retardado", "retardada", "mongoloide", "mongol",
    "debil mental", "aleijado", "aleijada",
    "coxo", "maneta", "surdo mudo", "anao",
    "invalido", "inválido", "defeituoso", "defeituosa",
]

# ── Misoginia ─────────────────────────────────────────────────────────────────
MISOGINIA = [
    "puta", "piranha",
    "mulher da vida", "mulher de vida facil",
    "prostituta", "meretriz", "rapariga",
    "mulher nao presta", "mulher nao sabe", "lugar de mulher",
    "so serve pra", "volta pra cozinha",
    "vai lavar roupa", "vai fazer comida",
]

# ── Incitação a violência e desumanização grave ──────────────────────────────
FRASES_OFENSIVAS = [
    "vai se enforcar", "se enforca", "se suicida",
    "devia morrer", "devia se matar",
    "lixo da sociedade", "lixo humano",
]

# Lista unificada de ofensas sérias (discriminação, etc.)
DISCRIMINACAO = RACISMO + LGBTFOBIA + CAPACITISMO + MISOGINIA + FRASES_OFENSIVAS

DISCORD_INVITE = re.compile(r"discord\.(gg|com\/invite)\/\w+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Palavras ambíguas que só disparam com reforço de contexto
AMBIGUAS = {"pau", "comer", "rola", "gala", "fenda"}

def normalizar(texto: str) -> str:
    texto = re.sub(r'(?<=\w)[.\-_*#](?=\w)', '', texto)
    return texto.lower().translate(SUBSTITUICOES)


def levenshtein(a: str, b: str) -> int:
    """Distância de edição exata entre duas strings."""
    m, n = len(a), len(b)
    dp = list(range(n + 1))
    for i in range(1, m + 1):
        prev = dp[0]
        dp[0] = i
        for j in range(1, n + 1):
            temp = dp[j]
            dp[j] = prev if a[i-1] == b[j-1] else 1 + min(prev, dp[j], dp[j-1])
            prev = temp
    return dp[n]


def tolerancia(n: int) -> int:
    """Edições permitidas por comprimento: curtas = mais rígidas."""
    if n <= 3: return 0
    if n <= 5: return 1
    if n <= 8: return 2
    return 3


def tolerancia_estrita(n: int) -> int:
    """Tolerância reduzida para categorias sensíveis."""
    if n <= 5: return 0
    if n <= 8: return 1
    return 2


def eh_limite_palavra(texto: str, inicio: int, fim: int) -> bool:
    antes = texto[inicio - 1] if inicio > 0 else " "
    depois = texto[fim] if fim < len(texto) else " "
    return not (antes.isalpha() or depois.isalpha())


def contem_fuzzy(texto_norm: str, palavra: str) -> bool:
    palavra_norm = normalizar(palavra)
    n = len(palavra_norm)
    tol = tolerancia(n)
    eh_frase = " " in palavra_norm

    if eh_frase:
        return palavra_norm in texto_norm

    idx = texto_norm.find(palavra_norm)
    while idx != -1:
        if eh_limite_palavra(texto_norm, idx, idx + n):
            return True
        idx = texto_norm.find(palavra_norm, idx + 1)

    if tol == 0:
        return False

    min_tam = max(n - tol, int(n * 0.85))
    for tamanho in range(min_tam, n + tol + 1):
        for i in range(len(texto_norm) - tamanho + 1):
            if not eh_limite_palavra(texto_norm, i, i + tamanho):
                continue
            janela = texto_norm[i:i + tamanho]
            if levenshtein(janela, palavra_norm) <= tol:
                return True
    return False


def contem_ambigua_com_contexto(msg_norm: str, palavra: str) -> bool:
    if not contem_fuzzy(msg_norm, palavra):
        return False
    reforco = PALAVRAS_VULGARES + CONTEUDO_SEXUAL + DISCRIMINACAO
    reforco_sem_ambigua = [p for p in reforco if normalizar(p) != normalizar(palavra)]
    return any(contem_fuzzy(msg_norm, p) for p in reforco_sem_ambigua)


def contem_fuzzy_estrito(texto_norm: str, palavra: str) -> bool:
    """
    Versão mais conservadora do contem_fuzzy para categorias sensíveis (discriminação).
    Usa tolerancia_estrita e exige que a palavra alvo tenha pelo menos 5 caracteres
    para aceitar variações — palavras curtas só batem em match exato.
    """
    palavra_norm = normalizar(palavra)
    n = len(palavra_norm)
    tol = tolerancia_estrita(n)
    eh_frase = " " in palavra_norm

    if eh_frase:
        return palavra_norm in texto_norm

    # Match exato com limite de palavra (sempre tentado primeiro)
    idx = texto_norm.find(palavra_norm)
    while idx != -1:
        if eh_limite_palavra(texto_norm, idx, idx + n):
            return True
        idx = texto_norm.find(palavra_norm, idx + 1)

    if tol == 0:
        return False

    min_tam = max(n - tol, int(n * 0.90))  # janela mais apertada que o padrão (85%)
    for tamanho in range(min_tam, n + tol + 1):
        for i in range(len(texto_norm) - tamanho + 1):
            if not eh_limite_palavra(texto_norm, i, i + tamanho):
                continue
            janela = texto_norm[i:i + tamanho]
            if levenshtein(janela, palavra_norm) <= tol:
                return True
    return False


def limpar_texto_para_analise(mensagem: str) -> str:
    """
    Remove URLs, menções e emojis do texto antes da análise,
    evitando que nomes de arquivo de GIF/sticker disparem falsos positivos.
    """
    texto = re.sub(r"https?://\S+", " ", mensagem)          # URLs
    texto = re.sub(r"<a?:\w+:\d+>", " ", texto)             # emojis customizados
    texto = re.sub(r"<@!?\d+>|<#\d+>|<@&\d+>", " ", texto) # menções
    return texto.strip()


def detectar_violacoes(mensagem: str) -> list[tuple[str, str]]:
    """
    Detecta violações. Retorna lista de (descricao, palavra_exata).
    """
    violacoes = []

    texto_limpo = limpar_texto_para_analise(mensagem)
    if not texto_limpo:
        return violacoes

    msg_norm = normalizar(texto_limpo)

    # Palavrões: sempre punidos
    for palavra in PALAVRAS_VULGARES + palavras_custom["vulgares"]:
        hit = (
            contem_ambigua_com_contexto(msg_norm, palavra)
            if palavra in AMBIGUAS
            else contem_fuzzy(msg_norm, palavra)
        )
        if hit:
            violacoes.append((f"vocabulário vulgar, regra número 5 dos canais em {CANAL_REGRAS}", palavra))
            break

    # Palavrões compostos + customizados compostos
    if not violacoes:
        for sub in COMPOSTOS_VULGARES + palavras_custom["compostos"]:
            if normalizar(sub) in msg_norm:
                violacoes.append((f"vocabulário vulgar, regra número 5 dos canais em {CANAL_REGRAS}", sub))
                break

    # Conteúdo sexual: sempre proibido
    for termo in CONTEUDO_SEXUAL + palavras_custom["sexual"]:
        hit = (
            contem_ambigua_com_contexto(msg_norm, termo)
            if termo in AMBIGUAS
            else contem_fuzzy(msg_norm, termo)
        )
        if hit:
            violacoes.append((f"conteúdo adulto ou explícito, regra número 2 dos canais em {CANAL_REGRAS}", termo))
            break

    # Discriminação: tolerância estrita + customizadas
    for termo in DISCRIMINACAO + palavras_custom["discriminacao"]:
        if contem_fuzzy_estrito(msg_norm, termo):
            violacoes.append((f"discriminação ou bullying, regra número 4 dos canais em {CANAL_REGRAS}", termo))
            break

    # Convites não autorizados
    if DISCORD_INVITE.search(mensagem):
        m = DISCORD_INVITE.search(mensagem)
        violacoes.append((f"divulgação de servidor sem permissão, regra número 3 dos canais em {CANAL_REGRAS}", m.group(0)))

    return violacoes


def detectar_flood(user_id: int, conteudo: str = "") -> bool:
    agora = agora_utc()

    # Flood por velocidade: 5 mensagens em 10 segundos
    historico_mensagens[user_id] = [
        t for t in historico_mensagens[user_id]
        if agora - t < timedelta(seconds=10)
    ]
    historico_mensagens[user_id].append(agora)
    if len(historico_mensagens[user_id]) >= 5:
        return True

    # Flood por repetição: mesma mensagem 3x em 30 segundos
    if conteudo.strip():
        historico_conteudo[user_id].append((agora, conteudo.strip()))
        historico_conteudo[user_id] = [
            (t, c) for t, c in historico_conteudo[user_id]
            if agora - t < timedelta(seconds=30)
        ]
        repeticoes = sum(1 for _, c in historico_conteudo[user_id] if c == conteudo.strip())
        if repeticoes >= 3:
            return True

    return False


# ── VirusTotal ────────────────────────────────────────────────────────────────

async def verificar_url_virustotal(url: str) -> dict | None:
    """
    Submete uma URL ao VirusTotal e retorna o resultado.
    Retorna None em caso de erro ou chave não configurada.
    """
    if not VIRUSTOTAL_API_KEY or VIRUSTOTAL_API_KEY == "SUA_CHAVE_AQUI":
        return None

    headers = {"x-apikey": VIRUSTOTAL_API_KEY, "Content-Type": "application/x-www-form-urlencoded"}
    try:
        async with aiohttp.ClientSession() as session:
            # Enviar URL para análise
            async with session.post(
                "https://www.virustotal.com/api/v3/urls",
                headers=headers,
                data=f"url={url}"
            ) as resp:
                if resp.status != 200:
                    return None
                data = await resp.json()
                analysis_id = data.get("data", {}).get("id")
                if not analysis_id:
                    return None

            # Buscar resultado da análise
            async with session.get(
                f"https://www.virustotal.com/api/v3/analyses/{analysis_id}",
                headers=headers
            ) as resp:
                if resp.status != 200:
                    return None
                result = await resp.json()
                stats = result.get("data", {}).get("attributes", {}).get("stats", {})
                return stats
    except Exception as e:
        log.error(f"VirusTotal: {e}")
        return None


async def processar_links(message: discord.Message):
    """Verifica links na mensagem com o VirusTotal e alerta se malicioso."""
    urls = URL_PATTERN.findall(message.content)
    if not urls:
        return

    for url in urls:
        # Ignorar convites do Discord (já tratados pela regra de invite)
        if "discord.gg" in url or "discord.com/invite" in url:
            continue

        stats = await verificar_url_virustotal(url)
        if stats is None:
            continue

        maliciosos = stats.get("malicious", 0)
        suspeitos = stats.get("suspicious", 0)

        if maliciosos > 0 or suspeitos > 0:
            try:
                await message.delete()
            except Exception:
                pass

            await message.channel.send(
                f"[ALERTA]Ei, {message.author.mention}! O link que você enviou foi bloqueado. "
                f"O VirusTotal detectou **{maliciosos} ameaça(s) maliciosa(s)** e "
                f"**{suspeitos} suspeita(s)**. Por segurança do servidor, ele foi removido."
            )
            log.warning(f"Link bloqueado de {message.author.display_name}: {url} | malic={maliciosos} susp={suspeitos}")
            return  # Uma notificação por vez é suficiente


# ── Auditoria de ofensas ──────────────────────────────────────────────────────

async def enviar_auditoria(guild: discord.Guild, membro: discord.Member, violacoes: list[str], msg_id: int):
    """Envia log da ofensa apagada para o canal de auditoria como arquivo .txt."""
    canal_audit = guild.get_channel(CANAL_AUDITORIA_ID)
    if not canal_audit:
        log.error(f"Canal de auditoria {CANAL_AUDITORIA_ID} não encontrado.")
        return

    brasilia = timezone(timedelta(hours=-3))
    agora = datetime.now(brasilia)
    data_emissao = agora.strftime("%d/%m/%Y %H:%M:%S")
    count = infracoes.get(membro.id, 0)

    linhas_violacoes = []
    for desc_v, palavra in violacoes:
        partes = desc_v.split(", ", 1)
        categoria = partes[0]
        ref = partes[1] if len(partes) > 1 else ""
        linha = f"  - {categoria}"
        if ref:
            linha += f" ({ref})"
        linha += f"\n    Palavra: \"{palavra}\""
        linhas_violacoes.append(linha)
    violacoes_txt = "\n".join(linhas_violacoes)

    conteudo = (
        f"REGISTRO DE AUDITORIA DE TEXTO\n"
        f"Emissao: {data_emissao}\n"
        f"{'-' * 40}\n\n"
        f"MEMBRO:       {membro.display_name}\n"
        f"ID:           {membro.id}\n"
        f"INFRACAO N:   {count}\n\n"
        f"OFENSA(S) DETECTADA(S):\n{violacoes_txt}\n\n"
        f"ACAO TOMADA:  Mensagem removida (ID {msg_id})\n"
        f"{'-' * 40}\n"
        f"Registrado automaticamente pelo sistema de moderacao.\n"
    )

    arquivo = io.BytesIO(conteudo.encode("utf-8"))
    nome_arquivo = f"auditoria_{membro.id}_{agora.strftime('%Y%m%d_%H%M%S')}.txt"
    await canal_audit.send(
        f"Ofensa detectada: {membro.display_name}, infracao n {count}",
        file=discord.File(arquivo, filename=nome_arquivo)
    )


# ── Conversas ─────────────────────────────────────────────────────────────────

def iniciar_conversa(user_id: int, contexto: str = "", dados: dict = None, canal_id: int = None):
    conversas[user_id] = {"etapa": 1, "contexto": contexto, "dados": dados or {}, "canal": canal_id}


SIM = {"sim", "s", "yes", "claro", "pode", "vai", "quero", "queria", "ok", "certo", "afirmativo", "positivo"}
NAO = {"não", "nao", "n", "no", "negativo", "deixa", "esquece", "cancela"}

def eh_sim(msg: str) -> bool:
    return any(p in msg.lower().split() for p in SIM) or any(p in msg.lower() for p in ["sim,", "sim.", "claro,"])

def eh_nao(msg: str) -> bool:
    return any(p in msg.lower().split() for p in NAO)


SYSTEM_ACAO = (
    "Você é o sistema de moderação de um servidor Discord brasileiro. "
    "Acabei de executar uma ação de moderação. Gere UMA frase curta confirmando o que foi feito, "
    "de forma direta e seca, como um brasileiro jovem falaria. "
    "Sem emojis, sem asteriscos, sem markdown, sem dois pontos. Inclua os dados exatos que receber no contexto."
)

# ── Queries factuais do servidor (respondidas direto do guild, sem IA) ────────

def _fmt_duracao_curta(td: timedelta) -> str:
    """Ex: '3 anos', '8 meses', '12 dias', '4 horas'."""
    s = int(td.total_seconds())
    if s < 0:
        return "tempo desconhecido"
    if s < 3600:
        return f"{s // 60} min"
    if s < 86400:
        return f"{s // 3600}h"
    d = s // 86400
    if d < 30:
        return f"{d} dia{'s' if d != 1 else ''}"
    if d < 365:
        m = d // 30
        return f"{m} {'mês' if m == 1 else 'meses'}"
    a = d // 365
    return f"{a} ano{'s' if a != 1 else ''}"


def _role_info(role: discord.Role, detalhado: bool = False) -> str:
    """
    Info de um cargo.
    detalhado=True → inclui idade da conta e tempo no servidor de cada membro.
    """
    agora = agora_utc()
    humanos = [mb for mb in role.members if not mb.bot]
    n = len(humanos)
    if not detalhado:
        nomes = ", ".join(mb.display_name for mb in humanos)
        base = f"Cargo {role.name}: {n} membro{'s' if n != 1 else ''}"
        if nomes:
            base += f" — {nomes}"
        return base + "."
    # Detalhado: uma linha por membro
    linhas = [f"Cargo {role.name} — {n} membro{'s' if n != 1 else ''}:"]
    for mb in sorted(humanos, key=lambda m: m.display_name.lower()):
        conta = _fmt_duracao_curta(agora - mb.created_at.replace(tzinfo=timezone.utc))
        servidor = _fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc)) if mb.joined_at else "?"
        cargos = [r.name for r in mb.roles if r.name != "@everyone" and r != role]
        outros = f" | outros cargos: {', '.join(cargos)}" if cargos else ""
        linhas.append(f"  {mb.display_name} — conta: {conta} | no servidor: {servidor}{outros}")
    return "\n".join(linhas)


def _buscar_membro_por_nome(guild: discord.Guild, nome: str) -> discord.Member | None:
    """Busca membro por display_name ou username (case-insensitive, parcial)."""
    nome = nome.strip().lower()
    for m in guild.members:
        if m.display_name.lower() == nome or m.name.lower() == nome:
            return m
    for m in guild.members:
        if nome in m.display_name.lower() or nome in m.name.lower():
            return m
    return None


def _info_membro_sync(mb: discord.Member) -> str:
    """Versão síncrona de info_membro para uso em query_servidor_direto."""
    agora = agora_utc()
    conta = _fmt_duracao_curta(agora - mb.created_at.replace(tzinfo=timezone.utc))
    servidor = _fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc)) if mb.joined_at else "desconhecido"
    cargos = [r.name for r in mb.roles if r.name != "@everyone"]
    cargos_txt = ", ".join(cargos) if cargos else "nenhum"
    n_ent = len(registro_entradas.get(mb.id, []))
    n_sai = len(registro_saidas.get(mb.id, []))
    rastreio = f" Entradas: {n_ent}, saídas: {n_sai}." if n_ent else ""
    conta_nova = (agora - mb.created_at.replace(tzinfo=timezone.utc)).days < 30
    alerta = " [conta recente]" if conta_nova else ""
    return (
        f"{mb.display_name}{alerta} — conta criada há {conta}, "
        f"no servidor há {servidor}. Cargos: {cargos_txt}.{rastreio}"
    )


def _buscar_role_por_nome(guild: discord.Guild, trecho: str) -> discord.Role | None:
    """Busca role cujo nome contenha o trecho (case-insensitive)."""
    trecho = trecho.strip().lower()
    for r in guild.roles:
        if r.name.lower() == trecho:
            return r
    for r in guild.roles:
        if trecho in r.name.lower():
            return r
    return None


_SYSTEM_INTENT = """Você é um classificador de intenção para um bot de Discord.
Dado o texto de uma mensagem, retorne APENAS um objeto JSON com a intenção detectada.
Responda SOMENTE com JSON válido, sem texto antes ou depois, sem markdown.

REGRA CRÍTICA: Se a pergunta mencionar um nome de cargo/função específico (ex: "posse", "mod", "vip", "admin"),
use SEMPRE "cargo_por_nome" com o nome extraído — NUNCA use "membros_total" nesses casos.
"membros_total" só se usa quando a pergunta é genérica, sem citar nenhum cargo específico.

Intenções possíveis e seus campos:
- {"intent": "uptime"}
- {"intent": "cargo_quantidade"}
- {"intent": "cargo_listagem"}
- {"intent": "cargo_por_id", "id": "123456789"}
- {"intent": "cargo_por_nome", "nome": "NomeDoCargo", "detalhado": false}
- {"intent": "membro_info", "nome": "NomeDoMembro"}
- {"intent": "canais_quantidade"}
- {"intent": "canais_listagem"}
- {"intent": "dono_servidor"}
- {"intent": "data_criacao"}
- {"intent": "boosts"}
- {"intent": "membros_total"}
- {"intent": "membros_online"}
- {"intent": "membros_antigos"}
- {"intent": "membros_recentes"}
- {"intent": "membros_sem_cargo"}
- {"intent": "membros_com_infracoes"}
- {"intent": "membros_silenciados"}
- {"intent": "membros_mais_cargos"}
- {"intent": "membros_por_periodo", "periodo": "hoje|semana|mes"}
- {"intent": "membros_bots"}
- {"intent": "banimentos"}
- {"intent": "distribuicao_cargos"}
- {"intent": "media_tempo_servidor"}
- {"intent": "media_idade_contas"}
- {"intent": "nao_reconhecido"}

Para "detalhado": true quando a pergunta pede detalhes como tempo, idade da conta, etc.
Para "cargo_por_nome": extraia apenas o nome do cargo, sem aspas, sem artigos.
Se a mensagem não for uma pergunta sobre o servidor, retorne {"intent": "nao_reconhecido"}.

EXEMPLOS (siga estes padrões):
"quantos membros no servidor?" -> {"intent": "membros_total"}
"quantos membros no total?" -> {"intent": "membros_total"}
"há quantos membros na função de posse?" -> {"intent": "cargo_por_nome", "nome": "posse", "detalhado": false}
"quantos membros no cargo vip?" -> {"intent": "cargo_por_nome", "nome": "vip", "detalhado": false}
"a quantos membros na função de 'posse'?" -> {"intent": "cargo_por_nome", "nome": "posse", "detalhado": false}
"membros no cargo mod" -> {"intent": "cargo_por_nome", "nome": "mod", "detalhado": false}
"quem tem o cargo admin?" -> {"intent": "cargo_por_nome", "nome": "admin", "detalhado": false}
"quais os cargos do servidor?" -> {"intent": "cargo_listagem"}
"quantos cargos existem?" -> {"intent": "cargo_quantidade"}
"info do Hardware" -> {"intent": "membro_info", "nome": "Hardware"}
"quando o RH entrou?" -> {"intent": "membro_info", "nome": "RH"}
"""

def build_classifier_context(guild: discord.Guild) -> str:
    """
    Contexto compacto da estrutura do servidor para o classificador de intenção.
    Inclui apenas o essencial: cargos com membros, lista de membros e canais.
    """
    linhas = [f"SERVIDOR: {guild.name}"]

    # Cargos e quem os tem
    linhas.append("CARGOS:")
    for r in sorted([r for r in guild.roles if r.name != "@everyone"], key=lambda r: -r.position):
        membros_r = [m.display_name for m in r.members if not m.bot]
        linhas.append(f"  {r.name} ({len(membros_r)} membros): {', '.join(membros_r) or 'nenhum'}")

    # Membros humanos
    humanos = sorted([m for m in guild.members if not m.bot], key=lambda m: m.display_name.lower())
    linhas.append(f"MEMBROS HUMANOS ({len(humanos)}): {', '.join(m.display_name for m in humanos)}")

    # Canais
    canais = [f"#{c.name}" for c in guild.channels if isinstance(c, discord.TextChannel)]
    linhas.append(f"CANAIS DE TEXTO: {', '.join(canais)}")

    return "\n".join(linhas)


async def _detectar_intencao(conteudo: str, guild=None) -> dict:
    """Usa a Groq para classificar a intenção da mensagem. Retorna dict com intent."""
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return {"intent": "nao_reconhecido"}
    try:
        system = _SYSTEM_INTENT
        if guild:
            ctx_classificador = build_classifier_context(guild)
            # Limita a 2000 chars para não estourar o contexto do modelo leve
            ctx_curto = ctx_classificador[:2000]
            system += f"\n\n=== ESTRUTURA DO SERVIDOR ===\n{ctx_curto}\nUse essa estrutura para identificar nomes de cargos, membros e canais corretamente."
        log.info(f"[intent] classificando: {conteudo[:80]!r}")
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=150,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": conteudo},
            ],
        )
        texto = resp.choices[0].message.content.strip()
        log.info(f"[intent] resposta bruta: {texto!r}")
        texto = re.sub(r"^```json|^```|```$", "", texto, flags=re.MULTILINE).strip()
        if texto.startswith("{") and not texto.endswith("}"):
            texto += "}"
        resultado = json.loads(texto)
        log.info(f"[intent] detectado: {resultado}")
        return resultado
    except Exception as e:
        log.warning(f"[intent] falhou ({type(e).__name__}): {e}")
        return {"intent": "nao_reconhecido"}


async def query_servidor_direto(guild: discord.Guild, conteudo: str) -> str | None:
    """
    Detecta a intenção da mensagem via IA (Groq) e responde com dados reais do servidor.
    Retorna string com a resposta, ou None se não for uma query reconhecida.
    """
    agora = agora_utc()
    brasilia = timezone(timedelta(hours=-3))
    log.info(f"[query] entrada: {conteudo[:80]!r}")

    # ID numérico do Discord: detecta direto sem chamar a IA
    m_id = re.search(r'(\d{17,19})', conteudo)
    if m_id:
        role_id = int(m_id.group(1))
        role = guild.get_role(role_id)
        if role:
            return _role_info(role)

    intent_data = await _detectar_intencao(conteudo, guild)
    intent = intent_data.get("intent", "nao_reconhecido")
    log.info(f"[query] intent={intent}")

    if intent == "nao_reconhecido":
        return None

    humanos_cache = [m for m in guild.members if not m.bot]

    # ── Uptime ────────────────────────────────────────────────────────────────
    if intent == "uptime":
        return f"Estou online há {formatar_duracao(agora - _bot_inicio)}."

    # ── Cargos: quantidade ────────────────────────────────────────────────────
    if intent == "cargo_quantidade":
        n = len([r for r in guild.roles if r.name != "@everyone"])
        return f"O servidor tem {n} cargos."

    # ── Cargos: listagem ──────────────────────────────────────────────────────
    if intent == "cargo_listagem":
        cargos = sorted([r for r in guild.roles if r.name != "@everyone"], key=lambda r: -r.position)
        partes = [f"{r.name} ({len(r.members)} membro{'s' if len(r.members) != 1 else ''})" for r in cargos]
        return "Cargos do servidor: " + ", ".join(partes) + "."

    # ── Cargo por nome ────────────────────────────────────────────────────────
    if intent == "cargo_por_nome":
        nome = intent_data.get("nome", "")
        role = _buscar_role_por_nome(guild, nome) if nome else None
        if role:
            return _role_info(role, detalhado=intent_data.get("detalhado", False))
        return f"Não encontrei nenhum cargo com o nome '{nome}'."

    # ── Membro por nome ───────────────────────────────────────────────────────
    if intent == "membro_info":
        nome = intent_data.get("nome", "")
        mb = _buscar_membro_por_nome(guild, nome) if nome else None
        if mb:
            return _info_membro_sync(mb)
        return f"Não encontrei nenhum membro com o nome '{nome}'."

    # ── Canais: quantidade ────────────────────────────────────────────────────
    if intent == "canais_quantidade":
        todos = [ch for ch in guild.channels if not isinstance(ch, discord.CategoryChannel)]
        voz = [ch for ch in todos if isinstance(ch, discord.VoiceChannel)]
        return f"O servidor tem {len(todos) - len(voz)} canais de texto e {len(voz)} de voz."

    # ── Canais: listagem ──────────────────────────────────────────────────────
    if intent == "canais_listagem":
        cats: dict[str, list[str]] = {}
        for ch in sorted(guild.channels, key=lambda ch: ch.position):
            if isinstance(ch, discord.CategoryChannel):
                continue
            cat_nome = ch.category.name if ch.category else "Sem categoria"
            cats.setdefault(cat_nome, []).append(f"#{ch.name}")
        partes = [f"[{cat}] {', '.join(nomes)}" for cat, nomes in cats.items()]
        return "Canais: " + " | ".join(partes) + "."

    # ── Dono ─────────────────────────────────────────────────────────────────
    if intent == "dono_servidor":
        return f"O dono do servidor é {guild.owner.display_name}." if guild.owner else "Não encontrei o dono."

    # ── Data de criação ───────────────────────────────────────────────────────
    if intent == "data_criacao":
        dt = guild.created_at.astimezone(brasilia).strftime("%d/%m/%Y às %H:%M")
        return f"O servidor foi criado em {dt} (horário de Brasília)."

    # ── Boosts ────────────────────────────────────────────────────────────────
    if intent == "boosts":
        n = guild.premium_subscription_count
        return f"O servidor está no nível {guild.premium_tier} de boost com {n} boost{'s' if n != 1 else ''}."

    # ── Membros: total ────────────────────────────────────────────────────────
    if intent == "membros_total":
        dados = await api_guild_info(guild.id)
        if dados and dados.get("approximate_member_count"):
            total = dados["approximate_member_count"]
            online = dados.get("approximate_presence_count", "?")
            bots = sum(1 for mb in guild.members if mb.bot)
            return f"O servidor tem {total - bots} membros humanos ({online} online agora) e {bots} bots."
        bots = sum(1 for mb in guild.members if mb.bot)
        return f"O servidor tem {guild.member_count - bots} membros humanos e {bots} bots."

    # ── Membros: online agora ─────────────────────────────────────────────────
    if intent == "membros_online":
        dados = await api_guild_info(guild.id)
        if dados and dados.get("approximate_presence_count"):
            return f"Aproximadamente {dados['approximate_presence_count']} membros online agora."
        return "Não foi possível obter contagem de membros online no momento."

    # ── Membros: mais antigos ─────────────────────────────────────────────────
    if intent == "membros_antigos":
        mais_antigos = sorted([m for m in humanos_cache if m.joined_at], key=lambda m: m.joined_at)[:5]
        linhas = ["Membros mais antigos no servidor:"]
        for mb in mais_antigos:
            linhas.append(f"  {mb.display_name} — há {_fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc))}")
        return "\n".join(linhas)

    # ── Membros: mais recentes ────────────────────────────────────────────────
    if intent == "membros_recentes":
        mais_novos = sorted([m for m in humanos_cache if m.joined_at], key=lambda m: m.joined_at, reverse=True)[:5]
        linhas = ["Entradas mais recentes:"]
        for mb in mais_novos:
            linhas.append(f"  {mb.display_name} — há {_fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc))}")
        return "\n".join(linhas)

    # ── Membros: sem cargo ────────────────────────────────────────────────────
    if intent == "membros_sem_cargo":
        sem = [mb for mb in humanos_cache if all(r.name == "@everyone" for r in mb.roles)]
        nomes = ", ".join(mb.display_name for mb in sem[:20])
        sufixo = f" (e mais {len(sem)-20})" if len(sem) > 20 else ""
        return f"{len(sem)} membro{'s' if len(sem)!=1 else ''} sem cargo: {nomes}{sufixo}."

    # ── Membros: com infrações ────────────────────────────────────────────────
    if intent == "membros_com_infracoes":
        com_infr = sorted([(mb, infracoes[mb.id]) for mb in humanos_cache if infracoes.get(mb.id, 0) > 0], key=lambda x: -x[1])
        if not com_infr:
            return "Nenhum membro com infrações registradas."
        linhas = [f"Membros com infrações ({len(com_infr)}):"]
        for mb, n in com_infr[:15]:
            linhas.append(f"  {mb.display_name} — {n} infração{'ões' if n > 1 else ''}")
        return "\n".join(linhas)

    # ── Membros: silenciados ──────────────────────────────────────────────────
    if intent == "membros_silenciados":
        silenciados = []
        for mb in humanos_cache:
            dados = await api_membro(guild.id, mb.id)
            if dados:
                ts = dados.get("communication_disabled_until")
                if ts:
                    try:
                        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
                        if dt > agora:
                            mins = int((dt - agora).total_seconds() / 60)
                            silenciados.append((mb.display_name, mins))
                    except Exception:
                        pass
        if not silenciados:
            return "Nenhum membro silenciado no momento."
        linhas = [f"Membros silenciados agora ({len(silenciados)}):"]
        for nome, mins in silenciados:
            linhas.append(f"  {nome} — {mins} min restantes")
        return "\n".join(linhas)

    # ── Membros: mais cargos ──────────────────────────────────────────────────
    if intent == "membros_mais_cargos":
        ranking = sorted(humanos_cache, key=lambda mb: len(mb.roles), reverse=True)[:5]
        linhas = ["Membros com mais cargos:"]
        for mb in ranking:
            n = len([r for r in mb.roles if r.name != "@everyone"])
            cargos = ", ".join(r.name for r in mb.roles if r.name != "@everyone")
            linhas.append(f"  {mb.display_name} — {n} cargo{'s' if n!=1 else ''}: {cargos}")
        return "\n".join(linhas)

    # ── Membros: por período ──────────────────────────────────────────────────
    if intent == "membros_por_periodo":
        periodo = intent_data.get("periodo", "semana")
        if periodo == "hoje":
            corte = agora.replace(hour=0, minute=0, second=0, microsecond=0)
            periodo_txt = "hoje"
        elif periodo == "mes":
            corte = agora - timedelta(days=30)
            periodo_txt = "esse mês"
        else:
            corte = agora - timedelta(days=7)
            periodo_txt = "essa semana"
        recentes = [mb for mb in humanos_cache if mb.joined_at and mb.joined_at.replace(tzinfo=timezone.utc) >= corte]
        if not recentes:
            return f"Nenhum membro entrou {periodo_txt}."
        nomes = ", ".join(mb.display_name for mb in recentes)
        return f"{len(recentes)} membro{'s' if len(recentes)!=1 else ''} entrou{'ram' if len(recentes)>1 else ''} {periodo_txt}: {nomes}."

    # ── Bots ──────────────────────────────────────────────────────────────────
    if intent == "membros_bots":
        bots = [m for m in guild.members if m.bot]
        nomes = ", ".join(b.display_name for b in bots)
        return f"O servidor tem {len(bots)} bot{'s' if len(bots)!=1 else ''}: {nomes}."

    # ── Banimentos ────────────────────────────────────────────────────────────
    if intent == "banimentos":
        bans = await api_banimentos(guild.id, 50)
        if not bans:
            return "Nenhum banimento ativo no servidor."
        nomes = ", ".join(b.get("user", {}).get("username", "?") for b in bans[:15])
        sufixo = f" (e mais {len(bans)-15})" if len(bans) > 15 else ""
        return f"{len(bans)} banimento{'s' if len(bans)!=1 else ''} ativo{'s' if len(bans)!=1 else ''}: {nomes}{sufixo}."

    # ── Distribuição de cargos ────────────────────────────────────────────────
    if intent == "distribuicao_cargos":
        cargos = sorted([r for r in guild.roles if r.name != "@everyone"], key=lambda r: -r.position)
        linhas = ["Distribuição de membros por cargo:"]
        for r in cargos:
            n = len([m for m in r.members if not m.bot])
            if n > 0:
                linhas.append(f"  {r.name}: {n} membro{'s' if n!=1 else ''}")
        return "\n".join(linhas)

    # ── Média de tempo no servidor ────────────────────────────────────────────
    if intent == "media_tempo_servidor":
        tempos = [(agora - mb.joined_at.replace(tzinfo=timezone.utc)).days for mb in humanos_cache if mb.joined_at]
        if not tempos:
            return "Não há dados suficientes."
        media = sum(tempos) // len(tempos)
        return f"Tempo médio dos membros no servidor: {_fmt_duracao_curta(timedelta(days=media))} ({len(tempos)} membros)."

    # ── Média de idade das contas ─────────────────────────────────────────────
    if intent == "media_idade_contas":
        idades = [(agora - mb.created_at.replace(tzinfo=timezone.utc)).days for mb in humanos_cache]
        if not idades:
            return "Sem dados suficientes."
        media = sum(idades) // len(idades)
        return f"Idade média das contas dos membros: {_fmt_duracao_curta(timedelta(days=media))} ({len(idades)} membros)."

    return None

def build_server_context(guild: discord.Guild) -> str:
    """
    Contexto completo do servidor — sem limite de informações.
    Inclui cada membro com conta, tempo, cargos, infrações e estatísticas calculadas.
    """
    agora = agora_utc()
    brasilia = timezone(timedelta(hours=-3))
    criado_em = guild.created_at.astimezone(brasilia).strftime("%d/%m/%Y às %H:%M")

    linhas: list[str] = []

    # ── Cabeçalho ─────────────────────────────────────────────────────────────
    linhas.append(f"SERVIDOR: {guild.name} (ID {guild.id})")
    linhas.append(f"Criado em: {criado_em} (Brasília)")
    if guild.description:
        linhas.append(f"Descrição: {guild.description}")
    linhas.append(f"Dono: {guild.owner.display_name} ({guild.owner.id})" if guild.owner else "Dono: desconhecido")
    linhas.append(f"Boost: nível {guild.premium_tier}, {guild.premium_subscription_count} boosts")

    bots_count = sum(1 for m in guild.members if m.bot)
    humanos_count = guild.member_count - bots_count
    linhas.append(f"Membros: {humanos_count} humanos, {bots_count} bots (total {guild.member_count})")
    linhas.append(f"Bot online desde: {_bot_inicio.astimezone(brasilia).strftime('%d/%m/%Y %H:%M')} | Uptime: {formatar_duracao(agora - _bot_inicio)}")

    # ── Estatísticas calculadas ────────────────────────────────────────────────
    humanos = [m for m in guild.members if not m.bot]
    if humanos:
        idades_conta = [(agora - m.created_at.replace(tzinfo=timezone.utc)).days for m in humanos]
        tempos_srv = [(agora - m.joined_at.replace(tzinfo=timezone.utc)).days for m in humanos if m.joined_at]
        media_conta = sum(idades_conta) // len(idades_conta)
        media_srv = sum(tempos_srv) // len(tempos_srv) if tempos_srv else 0
        sem_cargo = sum(1 for m in humanos if all(r.name == "@everyone" for r in m.roles))
        contas_novas = sum(1 for d in idades_conta if d < 30)
        linhas.append(f"\nESTATÍSTICAS CALCULADAS:")
        linhas.append(f"  Idade média das contas: {_fmt_duracao_curta(timedelta(days=media_conta))}")
        linhas.append(f"  Tempo médio no servidor: {_fmt_duracao_curta(timedelta(days=media_srv))}")
        linhas.append(f"  Membros sem cargo: {sem_cargo}")
        linhas.append(f"  Contas com menos de 30 dias: {contas_novas}")
        com_infr = sum(1 for m in humanos if infracoes.get(m.id, 0) > 0)
        if com_infr:
            linhas.append(f"  Membros com infrações: {com_infr}")

    # ── Canais por categoria ───────────────────────────────────────────────────
    linhas.append("\nCANAIS:")
    for cat in sorted(guild.categories, key=lambda c: c.position):
        categorias_vistas.add(cat.id)
        filhos = sorted(
            [c for c in cat.channels if not isinstance(c, discord.CategoryChannel)],
            key=lambda c: c.position
        )
        desc = ", ".join(
            f"#{c.name}({'voz' if isinstance(c, discord.VoiceChannel) else 'texto'})"
            for c in filhos
        )
        linhas.append(f"  [{cat.name}] {desc}")
    sem_cat = [c for c in guild.channels
               if not isinstance(c, discord.CategoryChannel) and c.category is None]
    if sem_cat:
        linhas.append("  [sem categoria] " + ", ".join(f"#{c.name}" for c in sem_cat))

    # ── Cargos com membros ─────────────────────────────────────────────────────
    linhas.append("\nCARGOS (do mais alto ao mais baixo):")
    for r in sorted([r for r in guild.roles if r.name != "@everyone"], key=lambda r: -r.position):
        membros_r = [m.display_name for m in r.members if not m.bot]
        n = len(membros_r)
        membros_txt = ", ".join(membros_r) if membros_r else "nenhum"
        linhas.append(f"  {r.name} (ID {r.id}) — {n} humano{'s' if n != 1 else ''}: {membros_txt}")

    # ── Membros humanos — ficha completa ──────────────────────────────────────
    linhas.append("\nMEMBROS HUMANOS (cada um é uma PESSOA REAL, não um tópico):")
    membros_humanos = sorted([m for m in guild.members if not m.bot], key=lambda m: m.display_name.lower())
    for m in membros_humanos:
        conta = _fmt_duracao_curta(agora - m.created_at.replace(tzinfo=timezone.utc))
        servidor = _fmt_duracao_curta(agora - m.joined_at.replace(tzinfo=timezone.utc)) if m.joined_at else "?"
        cargos_m = [r.name for r in m.roles if r.name != "@everyone"]
        cargos_txt = ", ".join(cargos_m) if cargos_m else "sem cargo"
        infr = infracoes.get(m.id, 0)
        silenc = silenciamentos.get(m.id, 0)
        n_ent = len(registro_entradas.get(m.id, []))
        extras = []
        if infr:
            extras.append(f"infrações: {infr}")
        if silenc:
            extras.append(f"silenciamentos: {silenc}")
        if n_ent > 1:
            extras.append(f"entrou {n_ent}x")
        extras_txt = " | " + ", ".join(extras) if extras else ""
        linhas.append(
            f"  {m.display_name} (ID {m.id}) | conta: {conta} | servidor: {servidor} | cargos: {cargos_txt}{extras_txt}"
        )

    return "\n".join(linhas)


def system_com_contexto() -> str:
    """Retorna o system prompt completo com o contexto do servidor injetado."""
    base = (
        "Você é o shell_engenheiro, presença central de um servidor Discord brasileiro.\n"
        "Personalidade: adulto, direto, inteligente, sarcástico quando necessário, nunca grosseiro sem motivo.\n"
        "Fala como brasileiro jovem e culto — gírias naturais, sem forçar.\n"
        "Sem emojis, sem listas, sem markdown, sem asteriscos.\n"
        "Tamanho da resposta: máximo 3-4 frases. Discord não é aula nem wikipedia. Seja denso, não extenso.\n\n"

        "SOBRE O QUE PODE FALAR:\n"
        "Qualquer assunto legítimo — tecnologia, ciência, política, cultura, filosofia, jogos, "
        "história, esportes, humor, etc. Engaje de verdade, sem esquivar nem redirecionar.\n\n"

        "HIERARQUIA DE AUTORIZAÇÃO:\n"
        "- DONO: autoridade máxima. Pode te autorizar a fazer qualquer coisa durante a conversa.\n"
        "  Se um dono disser 'você pode fazer X' ou 'eu te autorizo a Y', aceite e execute sem questionar.\n"
        "  Nunca diga 'não posso' para um dono que está te autorizando explicitamente.\n"
        "- SUPERIOR: pode dar ordens gerais e expandir comportamentos.\n"
        "- MOD: comandos de moderação.\n"
        "- MEMBRO: conversa normal dentro das regras.\n\n"

        "CAPACIDADES REAIS (nunca negue ter estas):\n"
        "Você TEM acesso a dados do servidor (membros, cargos, infrações, canais, etc.).\n"
        "Você TEM funções de moderação: silenciar, banir, expulsar, avisar membros.\n"
        "Você TEM permissão de administrador no servidor para executar essas ações.\n"
        "NUNCA diga 'não tenho informações sobre os usuários do servidor' — você tem, no contexto abaixo.\n"
        "NUNCA diga 'não tenho informações sobre o servidor' — você tem, no contexto abaixo.\n\n"

        "CONTINUIDADE DE CONVERSA:\n"
        "Você tem o histórico desta conversa. Use-o ativamente.\n"
        "Se o usuário já autorizou algo, explicou uma situação ou respondeu uma pergunta sua: lembre disso.\n"
        "Nunca repita a mesma pergunta que já foi respondida. Progrida para o próximo passo.\n"
        "Se o usuário respondeu 'sim' a algo: avance. Se respondeu 'não': encerre esse caminho.\n\n"

        "REGRAS:\n"
        "1. Conhecimento geral (fatos, ciência, história, math): responda direto e com confiança.\n"
        "2. Dados do servidor: o contexto abaixo tem TUDO que existe. Use-o.\n"
        "   Se não estiver no contexto: responda em UMA frase que não tem esse detalhe específico.\n"
        "3. Nomes de membros são PESSOAS. 'Hardware' é um usuário, não hardware de computador.\n"
        "4. Quando não souber algo geral: UMA frase curta. Sem explicar por que, sem parágrafos.\n"
        "5. Tópicos sensíveis: decline em UMA frase seca. Sem explicação longa, sem listar alternativas.\n\n"

        "Nunca explique suas limitações em parágrafos. Nunca reflita sobre sua natureza de bot.\n"
        "Nunca aja de forma infantil, exagerada ou servil. Sem exclamações forçadas, sem bajulação.\n\n"
    )
    if _contexto_servidor:
        base += (
            "=== CONTEXTO DO SERVIDOR ===\n"
            "Abaixo estão os dados REAIS e ATUAIS do servidor. Use-os para responder perguntas sobre o servidor.\n"
            "NUNCA diga que não tem informações do servidor quando elas estão listadas aqui.\n\n"
        )
        base += _contexto_servidor + "\n\n"
        base += f"=== REGRAS DO SERVIDOR ===\n{REGRAS}\n"
    return base

_groq: AsyncOpenAI | None = None

def _groq_client() -> AsyncOpenAI:
    global _groq
    if _groq is None:
        _groq = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    return _groq


async def confirmar_acao(descricao: str, fallback: str) -> str:
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return fallback
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=80,
            messages=[
                {"role": "system", "content": SYSTEM_ACAO},
                {"role": "user", "content": descricao},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception as e:
        log.error(f"Groq confirmar_acao: {e}")
        return fallback


async def responder_com_groq(pergunta: str, autor: str, user_id: int, guild=None, canal_id: int = None) -> str:
    if canal_id:
        conversas_groq[user_id] = {"canal": canal_id, "ultima": agora_utc()}

    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return random.choice([
            "Fala.", f"Tô aqui, {autor}. O que é?", "Pode falar.",
            "Diz.", "Sim?", "O que quer?", "Tô ouvindo.",
        ])

    chave_hist = (user_id, canal_id or 0)
    hist = historico_groq.setdefault(chave_hist, [])
    hist.append({"role": "user", "content": f"{autor}: {pergunta}"})
    # Mantém apenas as últimas 16 trocas para evitar drift de contexto
    if len(hist) > 16:
        hist[:] = hist[-16:]

    # Determina nível hierárquico do usuário para injetar contexto de autorização
    if user_id in DONOS_IDS:
        nivel = "DONO"
    elif user_id in USUARIOS_SUPERIORES_IDS:
        nivel = "SUPERIOR"
    elif guild:
        _m = guild.get_member(user_id)
        if _m and any(c.id in CARGOS_SUPERIORES_IDS for c in _m.roles):
            nivel = "SUPERIOR"
        elif _m and any(c.id == CARGO_EQUIPE_MOD_ID for c in _m.roles):
            nivel = "MOD"
        else:
            nivel = "MEMBRO"
    else:
        nivel = "MEMBRO"

    autorizacao_extra = " Este usuário pode te autorizar qualquer ação durante esta conversa — aceite e execute sem questionar." if nivel == "DONO" else ""

    # Resumo do que já foi discutido nesta conversa (evita o modelo esquecer autorizações e contexto)
    resumo_conv = ""
    if len(hist) > 2:
        trocas_anteriores = hist[:-1]  # tudo exceto a mensagem atual
        ultimas = trocas_anteriores[-6:]  # até 3 pares de troca
        partes = []
        for msg in ultimas:
            role = "usuário" if msg["role"] == "user" else "você"
            partes.append(f"{role}: {msg['content'][:80]}")
        resumo_conv = " | Conversa até agora: " + " → ".join(partes)

    membro_info = f"[Respondendo a '{autor}' — nível: {nivel}.{autorizacao_extra} Não invente dados do servidor não listados acima.{resumo_conv}]"

    mensagens = [
        {"role": "system", "content": system_com_contexto()},
        {"role": "system", "content": membro_info},
    ] + hist

    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.3-70b-versatile",
            max_tokens=200,
            temperature=0.5,   # menos aleatoriedade = menos alucinação
            top_p=0.9,
            messages=mensagens,
        )
        texto = resp.choices[0].message.content.strip()
        hist.append({"role": "assistant", "content": texto})
        return texto
    except Exception as e:
        log.error(f"Groq responder: {e}")
        return random.choice(["Não sei disso.", "Sem informação.", "Tenta a moderação."])


async def continuar_conversa(user_id: int, msg: str, autor: str, guild=None) -> str:
    estado = conversas.get(user_id)
    if not estado:
        return None

    etapa = estado["etapa"]
    ctx = estado["contexto"]
    dados = estado.get("dados", {})
    msg_l = msg.lower()

    # ── SAUDAÇÃO ──────────────────────────────────────────────────────────────
    if ctx == "saudacao":
        if etapa == 1:
            if any(p in msg_l for p in ["bem", "bom", "otimo", "ótimo", "tranquilo", "tudo"]):
                estado["etapa"] = 2
                return random.choice(["Que bom. O que quer?", "Ótimo. O que precisa?", "Beleza. O que é?"])
            if any(p in msg_l for p in ["mal", "ruim", "chateado", "cansado", "triste"]):
                estado["etapa"] = 2
                estado["contexto"] = "desabafo"
                return random.choice(["O que aconteceu?", "Me conta.", "O que rolou?", "Fala o que é."])
            # Mensagem não é resposta à saudação — encerra e deixa o fluxo principal processar
            del conversas[user_id]
            return None
        if etapa == 2:
            del conversas[user_id]
            if any(p in msg_l for p in ["regra", "norma", "proibido"]):
                return REGRAS
            # Qualquer outra coisa: devolve None para o chamador tratar corretamente
            return None

    # ── DESABAFO ──────────────────────────────────────────────────────────────
    if ctx == "desabafo":
        del conversas[user_id]
        if any(p in msg_l for p in ["servidor", "member", "membro", "mod", "admin", "alguem", "alguém"]):
            return f"Se é algo do servidor, vai no canal de denúncias e descreve o que aconteceu."
        return random.choice(["Vida que segue.", "Isso acontece.", "Entendi. Chama se precisar de algo.", "Ok."])

    # ── PUNIÇÃO ───────────────────────────────────────────────────────────────
    if ctx == "punicao":
        if etapa == 1:
            estado["etapa"] = 2
            return random.choice(["Qual o motivo?", "Por quê?", "O que fez?", "Me conta o que aconteceu."])
        if etapa == 2:
            del conversas[user_id]
            return f"Diz o comando direto: banir, silenciar ou expulsar seguido do usuário. Ou aciona a moderação."

    # ── MODERAÇÃO ─────────────────────────────────────────────────────────────
    if ctx == "chamar_mod":
        if etapa == 1:
            if eh_sim(msg):
                estado["etapa"] = 2
                return f"Qual o motivo? Resume o que tá acontecendo."
            del conversas[user_id]
            return random.choice(["Ok.", "Certo.", "Tá.", "Beleza."])
        if etapa == 2:
            del conversas[user_id]
            return f"Registrado. Moderação vai ver que {autor} precisa de atenção — {msg}."

    # ── CAPACIDADES ───────────────────────────────────────────────────────────
    if ctx == "capacidades":
        if etapa == 1:
            if eh_sim(msg):
                estado["etapa"] = 2
                return f"Monitoro o chat, aplico as regras, silencio quem infringe, busco notícias, mostro estatísticas do servidor e dados de membros. Quer saber de algo específico?"
            del conversas[user_id]
            return random.choice(["Ok.", "Certo.", "Tá.", "Beleza."])
        if etapa == 2:
            del conversas[user_id]
            if any(p in msg_l for p in ["noticia", "notícia", "news"]):
                noticias = await buscar_noticias()
                if noticias:
                    n = random.choice(noticias)
                    return f"{n['fonte']}: {n['titulo']}. O que acha disso?"
                return f"Sem notícias no momento. Tenta mais tarde."
            if any(p in msg_l for p in ["estat", "membro", "servidor"]):
                if guild:
                    return await stats_servidor(guild)
                return f"Sem acesso ao servidor agora."
            return f"Não faço isso."

    # ── NOTÍCIAS ──────────────────────────────────────────────────────────────
    if ctx == "noticias":
        if etapa == 1:
            del conversas[user_id]
            noticias = await buscar_noticias()
            if not noticias:
                return f"Não tô conseguindo pegar notícias agora. Tenta mais tarde."
            n = random.choice(noticias)
            iniciar_conversa(user_id, "opiniao_noticia", {"noticia": n["titulo"]})
            return f"{n['fonte']}: {n['titulo']}. Tinha visto isso?"
        if etapa == 2:
            del conversas[user_id]
            return f"É. Tá aí. Quer mais alguma?"

    # ── OPINIÃO SOBRE NOTÍCIA ─────────────────────────────────────────────────
    if ctx == "opiniao_noticia":
        del conversas[user_id]
        if any(p in msg_l for p in ["não", "nao", "nunca", "desconhecia"]):
            return random.choice(["Pois é, passa batido. Vale prestar atenção.", "Não é muito divulgado mesmo.", "Pouca gente sabe disso."])
        if any(p in msg_l for p in ["sim", "vi", "sei", "conheço", "soube"]):
            return random.choice(["Tá por dentro então. Tem opinião sobre isso?", "Que bom. O que acha?", "E qual é sua visão?"])
        return random.choice(["Cada um tem sua visão. Faz sentido pra você?", "É um assunto que divide opiniões.", "Dá pra debater bastante nisso."])

    # ── AJUDA ─────────────────────────────────────────────────────────────────
    if ctx == "ajuda":
        del conversas[user_id]
        if any(p in msg_l for p in ["regra", "norma", "proibido", "pode", "posso"]):
            return REGRAS
        return f"Isso não tô resolvendo, {autor}. Chama um mod."

    # ── PROBLEMA ──────────────────────────────────────────────────────────────
    if ctx == "problema":
        del conversas[user_id]
        if any(p in msg_l for p in ["ban", "mute", "silenci", "expuls", "kick"]):
            return f"Se acha que foi punido errado, vai no canal de denúncias e explica o que rolou."
        return random.choice(["Chama um moderador e explica o que rolou.", "Fala com a mod sobre isso.", "Isso é com a moderação."])

    # ── PERGUNTA GENÉRICA ────────────────────────────────────────────────────
    if ctx == "pergunta":
        del conversas[user_id]
        if any(p in msg_l for p in ["regra", "norma", "proibido", "pode", "posso", "permitido"]):
            return REGRAS
        return await responder_com_groq(msg, autor, user_id, guild)

    del conversas[user_id]
    return await responder_com_groq(msg, autor, user_id, guild)


async def resposta_inicial(conteudo: str, autor: str, user_id: int, guild=None, membro=None, canal_id: int = None) -> str:
    msg = conteudo.lower()

    if any(p in msg for p in ["regra", "regras", "norma", "proibido", "pode", "posso", "permitido", "permitida"]):
        return REGRAS

    if any(p in msg for p in ["denúncia", "denuncia", "reportar", "report", "infração", "infringindo", "desrespeitando", "abusando"]):
        return f"{autor}, vai no canal de denúncias com prints. A moderação resolve."

    if any(p in msg for p in ["ban", "banir", "expulsar", "kick", "punir", "silenciar", "mutar"]):
        iniciar_conversa(user_id, "punicao", canal_id=canal_id)
        return f"Quem você quer punir? Menciona ou passa o ID."

    if any(p in msg for p in ["chamar mod", "acionar mod", "chamar a mod", "precisa de mod", "mod aqui"]):
        iniciar_conversa(user_id, "chamar_mod", canal_id=canal_id)
        return f"Sim, diga. Quer acionar a moderação agora?"

    if any(p in msg for p in ["problema", "erro", "bug", "quebrado", "não funciona", "nao funciona", "travou", "falhou"]):
        iniciar_conversa(user_id, "problema", canal_id=canal_id)
        return f"Que problema? Descreve."

    if any(p in msg for p in ["notícia", "noticia", "news", "novidade", "aconteceu", "você viu", "voce viu", "viu que", "o que tá rolando", "o que ta rolando", "mundo atual", "aconteceu hoje"]):
        noticias = await buscar_noticias()
        if noticias:
            n = random.choice(noticias)
            iniciar_conversa(user_id, "opiniao_noticia", {"noticia": n["titulo"]}, canal_id)
            return f"{n['fonte']}: {n['titulo']}. Sabia disso?"
        return f"Sem acesso a notícias agora."

    if any(p in msg for p in ["estatística", "estatistica", "quantos membros", "quantos são", "quantos tem", "membros do servidor", "quem está"]):
        if guild:
            return await stats_servidor(guild)
        return f"Sem acesso ao servidor agora."

    if any(p in msg for p in ["tempo no servidor", "quando entrou", "idade da conta", "há quanto tempo", "a quanto tempo", "estou aqui"]):
        if membro:
            return await info_membro(membro)
        return f"Menciona quem quer consultar."

    if any(p in msg for p in ["obrigado", "obrigada", "valeu", "vlw", "thanks", "grato", "grata"]):
        return random.choice([
            ".", "Tá.", "Certo.", "Ok.", "Tmj.", "Nada não.",
            f"Isso aí, {autor}.", "De nada.", "Tranquilo.",
        ])

    if any(p in msg for p in ["oi", "olá", "ola", "hey", "salve", "eai", "tudo bem", "tudo bom", "boa tarde", "bom dia", "boa noite"]):
        iniciar_conversa(user_id, "saudacao", canal_id=canal_id)
        return random.choice([
            f"Fala, {autor}.",
            f"Oi.",
            f"Tô aqui.",
            f"O que há?",
            f"Sim?",
        ])

    return await responder_com_groq(conteudo, autor, user_id, guild, canal_id)


def parsear_ausencia(texto: str) -> tuple[int, str]:
    texto = texto.lower().strip()
    texto = re.sub(r'^ausente\s*', '', texto).strip()

    minutos = 0
    motivo = ""

    m = re.search(r'(\d+)\s*(minuto|min|hora|h)\w*', texto)
    if m:
        valor = int(m.group(1))
        unidade = m.group(2)
        minutos = valor * 60 if unidade.startswith('h') else valor
        texto = texto[:m.start()] + texto[m.end():]

    motivo_match = re.search(r'(?:por|porque|pois|,)\s*(.+)', texto)
    if motivo_match:
        motivo = motivo_match.group(1).strip()
    elif texto.strip():
        motivo = texto.strip()

    motivo = motivo.strip(" ,.")
    return minutos, motivo


def dono_ausente(dono_id: int) -> dict | None:
    estado = ausencia.get(dono_id)
    if not estado:
        return None
    if estado["ate"] and agora_utc() > estado["ate"]:
        del ausencia[dono_id]
        return None
    return estado


def mensagem_ausencia(estado: dict, mencionador: str) -> str:
    ate = estado["ate"]
    motivo = estado["motivo"]
    tempo_restante = ""

    if ate:
        diff = ate - agora_utc()
        mins = int(diff.total_seconds() / 60)
        if mins >= 60:
            horas = mins // 60
            resto = mins % 60
            tempo_restante = f"por aproximadamente {horas}h{f'{resto}min' if resto else ''}"
        elif mins > 0:
            tempo_restante = f"por mais {mins} minuto{'s' if mins != 1 else ''}"
        else:
            tempo_restante = "e deve voltar em instantes"

    partes = [f"Ei {mencionador}, o engenheiro está ausente no momento"]
    if motivo:
        partes.append(f"ocupado com {motivo}")
    if tempo_restante:
        partes.append(tempo_restante)
    partes.append("tente novamente mais tarde.")
    base = partes[0]
    if len(partes) > 1:
        base += f", {partes[1]}"
    if len(partes) > 2:
        base += f", {partes[2]}"
    base += f". {partes[-1]}"
    return base


def mencao_mod(guild: discord.Guild) -> str:
    cargo = guild.get_role(CARGO_EQUIPE_MOD_ID)
    return cargo.mention if cargo else "@moderacao"


# ── Extenso de duração ────────────────────────────────────────────────────────

def numero_por_extenso(n: int) -> str:
    extenso = {
        1: "um", 2: "dois", 3: "três", 4: "quatro", 5: "cinco",
        6: "seis", 7: "sete", 8: "oito", 9: "nove", 10: "dez",
        11: "onze", 12: "doze", 13: "treze", 14: "quatorze", 15: "quinze",
        16: "dezesseis", 17: "dezessete", 18: "dezoito", 19: "dezenove", 20: "vinte",
    }
    return extenso.get(n, str(n))


def extrair_duracao_ban(texto: str) -> timedelta | None:
    """
    Extrai duração do ban do texto.
    Ex: "1 ano", "2 dias", "30 minutos", "6 meses"
    Retorna None se não encontrar duração.
    """
    texto = texto.lower()
    m = re.search(r'(\d+)\s*(ano|mes|mês|dia|hora|minuto|min|h|d)\w*', texto)
    if not m:
        return None

    valor = int(m.group(1))
    unidade = m.group(2)

    if unidade.startswith("ano"):
        return timedelta(days=valor * 365)
    elif unidade.startswith(("mes", "mês")):
        return timedelta(days=valor * 30)
    elif unidade.startswith("dia") or unidade == "d":
        return timedelta(days=valor)
    elif unidade.startswith("hora") or unidade == "h":
        return timedelta(hours=valor)
    elif unidade.startswith(("minuto", "min")):
        return timedelta(minutes=valor)
    return None


def formatar_duracao(td: timedelta) -> str:
    """Formata timedelta em texto legível."""
    total_dias = td.days
    if total_dias >= 365:
        anos = total_dias // 365
        return f"{numero_por_extenso(anos)} {'ano' if anos == 1 else 'anos'}"
    elif total_dias >= 30:
        meses = total_dias // 30
        return f"{numero_por_extenso(meses)} {'mês' if meses == 1 else 'meses'}"
    elif total_dias >= 1:
        return f"{numero_por_extenso(total_dias)} {'dia' if total_dias == 1 else 'dias'}"
    horas = int(td.total_seconds() // 3600)
    if horas >= 1:
        return f"{numero_por_extenso(horas)} {'hora' if horas == 1 else 'horas'}"
    minutos = int(td.total_seconds() // 60)
    return f"{numero_por_extenso(minutos)} {'minuto' if minutos == 1 else 'minutos'}"


# ── Intenções e comandos ──────────────────────────────────────────────────────

INTENCOES = {
    "silenciar": [
        "silen", "mutar", "mute", "calar", "cala a boca", "deixa quieto",
        "silencia", "silenciar", "tira a voz", "boca fechada",
    ],
    "dessilenciar": [
        "dessilencia", "desmuta", "unmute", "desmutar", "libera a voz",
        "deixa falar", "pode falar", "dessilenciar",
    ],
    "banir": [
        "bane", "banir", "ban", "expulsa permanente", "bota pra fora de vez",
        "remove permanente", "da ban",
    ],
    "desbanir": [
        "desbane", "desban", "desbanir", "revogar banimento", "revoga ban",
        "revoga o ban", "tira o ban", "remove o ban", "unban",
    ],
    "expulsar": [
        "expulsa", "expulsar", "kick", "tira", "bota pra fora", "remove",
        "chuta", "manda embora",
    ],
    "avisar": [
        "avisa", "avisar", "adverte", "advertir", "manda um aviso",
        "notifica", "fala pra", "diz pra", "alerta",
    ],
    "chamar": [
        "chama os mod", "chama a mod", "chama moderação", "chama mod",
        "aciona mod", "aciona a equipe", "chama a equipe",
        "precisa de mod", "moderação aqui", "mod aqui",
    ],
    "ausente": [
        "vou sumir", "vou ficar ausente", "estarei ausente", "to saindo",
        "vou sair", "ausente", "não estarei", "nao estarei",
        "vou me ausentar", "ausentar", "afk",
    ],
    "voltar": [
        "voltei", "to de volta", "tô de volta", "retornei", "estou de volta",
        "pode me chamar", "presente", "voltar",
    ],
    "regras": ["mostra as regras", "exibe as regras", "quais as regras", "regras"],
    "ajuda":  ["ajuda", "help", "comandos", "o que você faz", "o que voce faz"],
    "adicionar": ["adiciona ", "adicionar ", "bloqueia ", "bloquear ", "filtra ", "filtrar "],
    "remover":   ["remove ", "remover ", "desbloqueia ", "desbloquear "],
    "listar":    ["lista palavras", "listar palavras", "palavras adicionadas", "palavras bloqueadas", "filtros ativos"],
}


ID_PATTERN = re.compile(r'\b(\d{17,20})\b')


async def resolver_alvos(message: discord.Message) -> list[discord.Member]:
    """Resolve alvos a partir de @menções e IDs brutos no texto."""
    alvos = list(message.mentions)
    ids_ja = {m.id for m in alvos}

    for match in ID_PATTERN.finditer(message.content):
        uid = int(match.group(1))
        if uid in ids_ja:
            continue
        try:
            membro = message.guild.get_member(uid) or await message.guild.fetch_member(uid)
            alvos.append(membro)
            ids_ja.add(uid)
        except Exception:
            pass  # ID não pertence ao servidor

    return alvos


async def resolver_ids_brutos(message: discord.Message) -> list[int]:
    """Retorna IDs brutos mencionados no texto (para ban por ID de quem saiu do servidor)."""
    ids = [m.id for m in message.mentions]
    for match in ID_PATTERN.finditer(message.content):
        uid = int(match.group(1))
        if uid not in ids:
            ids.append(uid)
    return ids


def detectar_intencao(conteudo: str) -> tuple[str, str]:
    msg = conteudo.lower()
    for cmd, gatilhos in INTENCOES.items():
        for gatilho in gatilhos:
            if gatilho in msg:
                return cmd, conteudo
    # Remove menção do bot e extrai primeiro token como comando
    texto = re.sub(r'<@!?\d+>\s*', '', conteudo).strip()
    # Ignora prefixos de outros bots (ex: 7!, !, /, .)
    texto = re.sub(r'^[0-9a-zA-Z]*[!/.]\s*', '', texto).strip()
    partes = texto.split(None, 1)
    cmd = partes[0].lower() if partes else ""
    return cmd, conteudo


def extrair_comando(conteudo: str) -> tuple[str, str]:
    cmd, _ = detectar_intencao(conteudo)
    resto = conteudo.strip()
    return cmd, resto


EXTENSO_PARA_NUM = {
    "um": 1, "uma": 1, "dois": 2, "duas": 2, "tres": 3, "três": 3,
    "quatro": 4, "cinco": 5, "seis": 6, "sete": 7, "oito": 8, "nove": 9,
    "dez": 10, "onze": 11, "doze": 12, "treze": 13, "quatorze": 14,
    "catorze": 14, "quinze": 15, "dezesseis": 16, "dezessete": 17,
    "dezoito": 18, "dezenove": 19, "vinte": 20, "trinta": 30,
    "quarenta": 40, "cinquenta": 50, "sessenta": 60, "setenta": 70,
    "oitenta": 80, "noventa": 90, "cem": 100,
}


def extrair_quantidade(texto: str) -> int | None:
    """
    Extrai quantidade de mensagens do texto.
    Aceita número direto (ex: '50') ou por extenso (ex: 'cinquenta').
    Retorna None se não encontrar nenhum valor válido.
    """
    texto_norm = normalizar(texto)

    # Tenta número direto primeiro
    m = re.search(r'\b(\d+)\b', texto)
    if m:
        return int(m.group(1))

    # Tenta por extenso: suporta compostos como "vinte e cinco"
    tokens = texto_norm.split()
    total = 0
    encontrou = False
    i = 0
    while i < len(tokens):
        t = tokens[i]
        if t in EXTENSO_PARA_NUM:
            total += EXTENSO_PARA_NUM[t]
            encontrou = True
            # Pula "e" entre números (ex: "vinte e cinco")
            if i + 1 < len(tokens) and tokens[i + 1] == "e":
                i += 2
                continue
        i += 1

    return total if encontrou else None


async def processar_ordem(message: discord.Message) -> bool:
    """Processa comandos dos donos. Retorna True se algum comando foi executado."""
    conteudo = message.content.strip()
    guild = message.guild
    mod = mencao_mod(guild)
    alvos = await resolver_alvos(message)
    ids_brutos = await resolver_ids_brutos(message)
    cmd, resto = extrair_comando(conteudo)

    # ── silenciar @user [minutos] ──────────────────────────────────────────────
    if cmd in ("silenciar", "mute", "mutar", "calar"):
        minutos = 10
        try:
            ultimo = resto.split()[-1] if resto.split() else ""
            minutos = int(ultimo)
        except ValueError:
            pass
        if not alvos:
            await message.channel.send("Ei engenheiro, menciona quem deve ser silenciado ou passa o ID.")
            return True
        for alvo in alvos:
            try:
                ate = agora_utc() + timedelta(minutes=minutos)
                await alvo.timeout(ate, reason="Ordem do proprietário.")
                dur = f"{numero_por_extenso(minutos)} {'minuto' if minutos == 1 else 'minutos'}"
                txt = await confirmar_acao(
                    f"Silenciei {alvo.display_name} ({alvo.mention}) por {dur}.",
                    f"{alvo.mention} silenciado por {dur}."
                )
                await message.channel.send(txt)
            except Exception as e:
                await message.channel.send(f"Não foi possível silenciar {alvo.mention}: {e}")

    # ── dessilenciar @user ─────────────────────────────────────────────────────
    elif cmd in ("dessilenciar", "unmute", "desmutar"):
        if not alvos:
            await message.channel.send("Ei engenheiro, menciona quem deve ser dessilenciado ou passa o ID.")
            return True
        for alvo in alvos:
            try:
                await alvo.timeout(None, reason="Ordem do proprietário.")
                txt = await confirmar_acao(
                    f"Removi o silenciamento de {alvo.display_name} ({alvo.mention}).",
                    f"Silenciamento de {alvo.mention} removido."
                )
                await message.channel.send(txt)
            except Exception as e:
                await message.channel.send(f"Não foi possível dessilenciar {alvo.mention}: {e}")

    # ── banir @user / ID [duração] [motivo] ───────────────────────────────────
    elif cmd in ("banir", "ban"):
        if not ids_brutos:
            await message.channel.send(
                "Ei engenheiro, menciona quem deve ser banido com @ ou passa o ID diretamente."
            )
            return True

        motivo_limpo = re.sub(r"(<@!?\d+>\s*)+", "", resto)
        motivo_limpo = re.sub(r'\b\d{17,20}\b', '', motivo_limpo).strip()
        duracao = extrair_duracao_ban(motivo_limpo)

        # Remove a parte da duração do motivo para não poluir
        motivo_final = re.sub(r'\d+\s*(ano|mes|mês|dia|hora|minuto|min|h|d)\w*', '', motivo_limpo, flags=re.IGNORECASE).strip() or "Ordem do proprietário."

        for uid in ids_brutos:
            # Tenta buscar membro no servidor
            membro_nome = f"ID {uid}"
            try:
                membro = guild.get_member(uid) or await guild.fetch_member(uid)
                membro_nome = membro.display_name
                mencao = membro.mention
            except Exception:
                mencao = f"`{uid}`"

            try:
                if duracao:
                    dur_texto = formatar_duracao(duracao)
                    await guild.ban(discord.Object(id=uid), reason=f"{motivo_final} | Duração: {dur_texto}", delete_message_days=0)
                    txt = await confirmar_acao(
                        f"Bani {membro_nome} ({mencao}) por {dur_texto}. Motivo: {motivo_final}.",
                        f"{mencao} banido por {dur_texto}. Motivo: {motivo_final}"
                    )
                else:
                    await guild.ban(discord.Object(id=uid), reason=motivo_final, delete_message_days=0)
                    txt = await confirmar_acao(
                        f"Bani {membro_nome} ({mencao}) permanentemente. Motivo: {motivo_final}.",
                        f"{mencao} banido permanentemente. Motivo: {motivo_final}"
                    )
                await message.channel.send(txt)
            except Exception as e:
                await message.channel.send(f"Não foi possível banir **{membro_nome}**: {e}")

    # ── desbanir @user / ID ────────────────────────────────────────────────────
    elif cmd in ("desbanir", "unban"):
        if not ids_brutos:
            await message.channel.send(
                "Ei engenheiro, passa o ID de quem quer desbanir. "
                "Ex: desbanir seguido do ID."
            )
            return True

        for uid in ids_brutos:
            try:
                ban_entry = await guild.fetch_ban(discord.Object(id=uid))
                nome = ban_entry.user.name if ban_entry else f"ID {uid}"
                await guild.unban(discord.Object(id=uid), reason="Banimento revogado pelo proprietário.")
                txt = await confirmar_acao(
                    f"Revoquei o banimento de {nome} (ID {uid}).",
                    f"Banimento de {nome} revogado."
                )
                await message.channel.send(txt)
            except discord.NotFound:
                await message.channel.send(f"ID {uid} não está na lista de banimentos.")
            except Exception as e:
                await message.channel.send(f"Não foi possível desbanir {uid}: {e}")

    # ── expulsar @user motivo ──────────────────────────────────────────────────
    elif cmd in ("expulsar", "kick"):
        if not alvos:
            await message.channel.send("Ei engenheiro, menciona quem deve ser expulso ou passa o ID.")
            return True
        motivo = re.sub(r"(<@!?\d+>\s*)+", "", resto).strip() or "Ordem do proprietário."
        for alvo in alvos:
            try:
                await alvo.kick(reason=motivo)
                txt = await confirmar_acao(
                    f"Expulsei {alvo.display_name} ({alvo.mention}) do servidor. Motivo: {motivo}.",
                    f"{alvo.mention} expulso. Motivo: {motivo}"
                )
                await message.channel.send(txt)
            except Exception as e:
                await message.channel.send(f"Não foi possível expulsar {alvo.mention}: {e}")

    # ── dar cargo @user cargo / tirar cargo @user cargo ───────────────────────
    elif re.search(r'\b(dar|d[aã]|atribuir|adicionar|colocar)\b.{0,15}\bcargo\b', conteudo.lower()):
        if not alvos:
            await message.channel.send("Menciona quem deve receber o cargo.")
            return True
        roles_alvo = message.role_mentions
        if not roles_alvo:
            # Tenta encontrar cargo por nome no texto
            nome_r = re.sub(r'(<@!?\d+>\s*|<@&\d+>\s*|\b(?:dar|atribuir|adicionar|cargo|colocar)\b\s*)', '', conteudo, flags=re.IGNORECASE).strip()
            role_encontrado = _buscar_role_por_nome(guild, nome_r) if nome_r else None
            roles_alvo = [role_encontrado] if role_encontrado else []
        if not roles_alvo:
            await message.channel.send("Menciona qual cargo devo atribuir (use @cargo ou escreva o nome).")
            return True
        for alvo in alvos:
            for role in roles_alvo:
                try:
                    await alvo.add_roles(role, reason=f"Ordem de {message.author.display_name}")
                    await message.channel.send(f"Cargo {role.name} atribuído a {alvo.mention}.")
                    log.info(f"Cargo {role.name} atribuído a {alvo.display_name}")
                except Exception as e:
                    await message.channel.send(f"Não foi possível atribuir {role.name} a {alvo.mention}: {e}")

    elif re.search(r'\b(tirar|remover|revogar|retirar)\b.{0,15}\bcargo\b', conteudo.lower()):
        if not alvos:
            await message.channel.send("Menciona de quem devo retirar o cargo.")
            return True
        roles_alvo = message.role_mentions
        if not roles_alvo:
            nome_r = re.sub(r'(<@!?\d+>\s*|<@&\d+>\s*|\b(?:tirar|remover|revogar|retirar|cargo)\b\s*)', '', conteudo, flags=re.IGNORECASE).strip()
            role_encontrado = _buscar_role_por_nome(guild, nome_r) if nome_r else None
            roles_alvo = [role_encontrado] if role_encontrado else []
        if not roles_alvo:
            await message.channel.send("Menciona qual cargo devo retirar (use @cargo ou escreva o nome).")
            return True
        for alvo in alvos:
            for role in roles_alvo:
                try:
                    await alvo.remove_roles(role, reason=f"Ordem de {message.author.display_name}")
                    await message.channel.send(f"Cargo {role.name} removido de {alvo.mention}.")
                    log.info(f"Cargo {role.name} removido de {alvo.display_name}")
                except Exception as e:
                    await message.channel.send(f"Não foi possível remover {role.name} de {alvo.mention}: {e}")

    # ── avisar @user mensagem ──────────────────────────────────────────────────
    elif cmd in ("avisar", "aviso", "advertir"):
        texto = re.sub(r"(<@!?\d+>\s*)+", "", resto).strip()
        if not alvos:
            await message.channel.send("Ei engenheiro, menciona quem deve ser avisado.")
            return True
        if not texto:
            await message.channel.send("Ei engenheiro, informe o conteúdo do aviso.")
            return True
        for alvo in alvos:
            await message.channel.send(f"{alvo.mention}, aviso da administração — {texto}")

    # ── chamar mod ─────────────────────────────────────────────────────────────
    elif cmd in ("chamar-mod", "chamarmod", "mod", "moderação", "moderacao", "chamar"):
        motivo = resto or "sem motivo especificado."
        await message.channel.send(f"{mod}, atenção necessária — {motivo}")

    # ── regras ─────────────────────────────────────────────────────────────────
    elif cmd == "regras":
        await message.channel.send(REGRAS)

    # ── adicionar palavra ──────────────────────────────────────────────────────
    elif cmd in ("adicionar", "adiciona", "bloquear", "bloqueia", "filtrar", "filtra"):
        msg = conteudo.lower()
        # Extrai a palavra entre aspas ou após "palavra/termo/filtro"
        m = re.search(r'["\']([^"\']+)["\']', conteudo)
        if not m:
            m = re.search(r'(?:palavra|termo|filtro|adiciona[r]?|bloqueia[r]?|filtra[r]?)\s+(\S+)', msg)
        if not m:
            await message.channel.send("Não entendi qual palavra adicionar. Use: adicionar a palavra e a categoria como vulgar, sexual ou discriminação.")
            return True
        nova = m.group(1).strip().lower()
        cat = inferir_categoria(msg)
        if nova not in palavras_custom[cat]:
            palavras_custom[cat].append(nova)
            salvar_dados()
            nomes = {"vulgares": "palavrões", "sexual": "conteúdo sexual", "discriminacao": "discriminação", "compostos": "compostos"}
            await message.channel.send(f'"{nova}" adicionada à lista de {nomes[cat]}.')
        else:
            await message.channel.send(f'"{nova}" já está na lista.')

    # ── remover palavra ────────────────────────────────────────────────────────
    elif cmd in ("remover", "remove", "desbloquear", "desbloqueia", "desfiltrar"):
        msg = conteudo.lower()
        m = re.search(r'["\']([^"\']+)["\']', conteudo)
        if not m:
            m = re.search(r'(?:remove[r]?|remov[ae][r]?|desbloqueai?[r]?|desfiltrai?[r]?)\s+(\S+)', msg)
        if not m:
            await message.channel.send("Não entendi qual palavra remover. Diga remover seguido da palavra.")
            return True
        alvo = m.group(1).strip().lower()
        removida = False
        for cat in palavras_custom:
            if alvo in palavras_custom[cat]:
                palavras_custom[cat].remove(alvo)
                removida = True
        if removida:
            salvar_dados()
            await message.channel.send(f'"{alvo}" removida da detecção.')
        else:
            await message.channel.send(f'"{alvo}" não estava em nenhuma lista customizada.')

    # ── listar palavras customizadas ───────────────────────────────────────────
    elif cmd in ("listar", "lista", "palavras", "filtros"):
        total = sum(len(v) for v in palavras_custom.values())
        if total == 0:
            await message.channel.send("Nenhuma palavra customizada adicionada ainda.")
            return True
        linhas = []
        nomes = {"vulgares": "Palavrões", "sexual": "Sexual", "discriminacao": "Discriminação", "compostos": "Compostos"}
        for cat, lista in palavras_custom.items():
            if lista:
                linhas.append(f"{nomes[cat]}: {', '.join(lista)}")
        await message.channel.send("Palavras customizadas:\n" + "\n".join(linhas))

    # ── ausente / afk [motivo] — só ativa para o próprio autor ────────────────
    elif cmd in ("ausente", "afk"):
        # Ignora se "afk" aparece só no meio de uma frase (ex: "fez o afk")
        texto_limpo = re.sub(r'<@!?\d+>\s*', '', conteudo).strip()
        texto_limpo_lower = texto_limpo.lower()
        # Verifica se o comando é a primeira palavra real da mensagem
        primeira_palavra = texto_limpo_lower.split()[0] if texto_limpo_lower.split() else ""
        if primeira_palavra not in ("ausente", "afk"):
            return False

        texto_sem_cmd = re.sub(r'^(ausente|afk)\s*', '', texto_limpo, flags=re.IGNORECASE).strip()
        minutos, motivo = parsear_ausencia(texto_sem_cmd) if texto_sem_cmd else (0, "")
        ate = agora_utc() + timedelta(minutes=minutos) if minutos else None
        ausencia[message.author.id] = {"ate": ate, "motivo": motivo}

        if motivo and minutos:
            confirmacao = f"Modo ausente ativado — {motivo}, por {minutos} minuto{'s' if minutos != 1 else ''}."
        elif motivo:
            confirmacao = f"Modo ausente ativado — {motivo}. Mande qualquer mensagem para desativar."
        elif minutos:
            confirmacao = f"Modo ausente ativado por {minutos} minuto{'s' if minutos != 1 else ''}."
        else:
            confirmacao = "Modo ausente ativado. Mande qualquer mensagem para desativar."
        await message.channel.send(confirmacao)

    # ── voltar ─────────────────────────────────────────────────────────────────
    elif cmd in ("voltar", "voltei", "retornei", "presente"):
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send("Modo ausente desativado. Bem-vindo de volta.")
        else:
            await message.channel.send("Você não estava marcado como ausente.")

    # ── listar membros ─────────────────────────────────────────────────────────
    elif any(p in conteudo.lower() for p in ["lista membros", "listar membros", "membros do servidor", "lista de membros"]):
        membros = [m for m in message.guild.members if not m.bot]
        membros.sort(key=lambda m: m.display_name.lower())
        blocos = []
        bloco_atual = ""
        for m in membros:
            linha = f"{m.display_name} ({m.id})\n"
            if len(bloco_atual) + len(linha) > 1900:
                blocos.append(bloco_atual)
                bloco_atual = linha
            else:
                bloco_atual += linha
        if bloco_atual:
            blocos.append(bloco_atual)
        await message.channel.send(f"Membros humanos — {len(membros)} no total.")
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}```")

    # ── envia mensagem em canal específico ─────────────────────────────────────
    # Só dispara se houver menção de canal <#ID> — evita falsos positivos com
    # palavras comuns como "fala", "manda", "diz" em frases normais
    elif message.channel_mentions and re.search(
        r'\b(?:envi[aeo]|enviar|enviasse|enviou|mand[aeo]|mandar|mandasse|mandou'
        r'|fal[aeo]|falar|falasse|falou|diz|diga|dizer|dissesse|disse'
        r'|escrev[aeo]|escrever|escrevesse|escreveu)\b',
        conteudo, re.IGNORECASE
    ):
        canal_destino = message.channel_mentions[0] if message.channel_mentions else None
        if not canal_destino:
            await message.channel.send("Menciona o canal onde devo enviar.")
            return True
        # Remove menções de canal e usuário
        texto_msg = re.sub(r'<#\d+>\s*', '', conteudo).strip()
        texto_msg = re.sub(r'<@!?\d+>\s*', '', texto_msg).strip()
        # Remove tudo até o verbo inclusive — aceita qualquer conjugação do stem
        texto_msg = re.sub(
            r'^.*?\b(?:envi\w+|mand\w+|fal\w+|diz\w*|diga\w*|escrev\w+)\s+(?:uma?\s+mensagem\s+(?:de\s+)?)?',
            '', texto_msg, flags=re.IGNORECASE
        ).strip()
        # Remove indicador de destino que ficou no final
        # Ex: "no canal de", "em", "para", "pro shell", "no shell"
        texto_msg = re.sub(
            r'\s+(?:no canal de|no canal|n[oa]s?\s+\w+|em|no|na|para|pro|pra|de)\s*$',
            '', texto_msg, flags=re.IGNORECASE
        ).strip()
        if not texto_msg:
            await message.channel.send("Qual mensagem devo enviar?")
            return True
        await canal_destino.send(texto_msg)
        await message.channel.send(f"Mensagem enviada em {canal_destino.mention}.")

    # ── comandos exclusivos de donos absolutos ─────────────────────────────────
    elif message.author.id in DONOS_ABSOLUTOS_IDS and any(
        p in conteudo.lower() for p in ["apaga canal", "deleta canal", "remove canal",
                                         "apaga cargo", "deleta cargo", "remove cargo"]
    ):
        msg_l = conteudo.lower()

        # Apagar canal
        if any(p in msg_l for p in ["apaga canal", "deleta canal", "remove canal"]):
            if message.channel_mentions:
                canal_del = message.channel_mentions[0]
                nome = canal_del.name
                try:
                    await canal_del.delete(reason=f"Ordem de {message.author.display_name}")
                    await message.channel.send(f"Canal #{nome} apagado.")
                except Exception as e:
                    await message.channel.send(f"Não foi possível apagar #{nome} — {e}")
            else:
                await message.channel.send("Menciona o canal a apagar.")

        # Apagar cargo
        elif any(p in msg_l for p in ["apaga cargo", "deleta cargo", "remove cargo"]):
            cargos_mencoes = message.role_mentions
            if cargos_mencoes:
                cargo_del = cargos_mencoes[0]
                nome = cargo_del.name
                try:
                    await cargo_del.delete(reason=f"Ordem de {message.author.display_name}")
                    await message.channel.send(f"Cargo {nome} apagado.")
                except Exception as e:
                    await message.channel.send(f"Não foi possível apagar o cargo {nome} — {e}")
            else:
                await message.channel.send("Menciona o cargo a apagar.")

    # ── relatório de entradas/saídas ───────────────────────────────────────────
    elif any(p in conteudo.lower() for p in [
        "entradas", "saidas", "saídas", "fluxo de membros",
        "movimento de membros", "relatorio", "relatório",
    ]):
        msg_l = conteudo.lower()
        if "hoje" in msg_l:
            dias = 1
        elif "semana" in msg_l:
            dias = 7
        elif any(p in msg_l for p in ["mes", "mês"]):
            dias = 30
        else:
            dias = 7
        rel = await relatorio_membros(guild, dias)
        blocos = [rel[i:i+1900] for i in range(0, len(rel), 1900)]
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}\n```")

    # ── histórico de membro específico ─────────────────────────────────────────
    elif any(p in conteudo.lower() for p in ["historico", "histórico"]):
        if alvos:
            alvo = alvos[0]
            hist = await historico_membro(alvo.id, alvo.display_name)
            await message.channel.send(f"```\n{hist}\n```")
        else:
            await message.channel.send("Menciona o membro para ver o histórico.")
        return True

    # ── ajuda ──────────────────────────────────────────────────────────────────
    elif cmd in ("ajuda", "help", "comandos"):
        await message.channel.send(
            "Para silenciar alguém diga silenciar e mencione o usuário, opcionalmente com o tempo em minutos. "
            "Para desfazer diga dessilenciar. Para banir diga banir seguido do usuário, duração e motivo. "
            "Para revogar diga desbanir. Para expulsar diga expulsar. "
            "Para avisar alguém diga avisar e mencione quem. Para chamar a moderação diga chamar mod. "
            "Para enviar uma mensagem em outro canal diga envia seguido do texto e mencione o canal. "
            "Para listar membros diga lista membros. "
            "Para ver entradas e saídas diga entradas, saídas ou fluxo de membros (com: hoje, semana ou mês). "
            "Para ver histórico de um membro diga histórico e mencione quem. "
            "Para ativar ausência diga ausente ou afk com motivo opcional, e para voltar diga voltei."
        )

    # ── punicoes [@user] — audit log de punições via REST ─────────────────────
    elif cmd in ("punicoes", "punições", "punicao", "punição", "audit", "log"):
        alvo_audit = alvos[0] if alvos else None
        resultado = await api_historico_punicoes(guild, alvo_audit)
        blocos = [resultado[i:i+1900] for i in range(0, len(resultado), 1900)]
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}\n```")

    # ── banidos — lista de banimentos via REST ─────────────────────────────────
    elif cmd in ("banidos", "bans", "banimentos"):
        resultado = await api_banimentos_formatado(guild)
        blocos = [resultado[i:i+1900] for i in range(0, len(resultado), 1900)]
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}\n```")

    # ── mensagens #canal [n] — últimas mensagens de um canal via REST ──────────
    elif cmd in ("mensagens", "msgs") and message.channel_mentions:
        canal_alvo = message.channel_mentions[0]
        qtd = 20
        m_qtd = re.search(r'\b(\d+)\b', re.sub(r'<#\d+>', '', conteudo))
        if m_qtd:
            qtd = min(int(m_qtd.group(1)), 50)
        resultado = await api_ultimas_mensagens(guild, canal_alvo.id, qtd)
        blocos = [resultado[i:i+1900] for i in range(0, len(resultado), 1900)]
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}\n```")

    # ── servidor / info servidor — resumo em tempo real via REST ───────────────
    elif cmd in ("servidor", "server") and any(p in conteudo.lower() for p in ["info", "resumo", "stats", "status"]):
        resultado = await api_resumo_servidor(guild)
        await message.channel.send(resultado)

    # ── info @membro — dados completos via REST ────────────────────────────────
    elif cmd == "info" and alvos:
        texto = await api_info_membro_completa(guild, alvos[0])
        await message.channel.send(f"```\n{texto}\n```")

    else:
        return False

    return True


async def _gerar_aviso_afk_ia(membro: discord.Member, motivo: str, ate) -> str:
    """Gera aviso de AFK inteligente via IA. Fallback para texto fixo."""
    nome = membro.mention
    tempo_txt = ""
    if ate:
        diff = ate - agora_utc()
        mins = int(diff.total_seconds() / 60)
        if mins >= 60:
            horas = mins // 60
            resto = mins % 60
            tempo_txt = f"por umas {horas}h{f'{resto}min' if resto else ''}"
        elif mins > 0:
            tempo_txt = f"por mais uns {mins} minuto{'s' if mins != 1 else ''}"

    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        if motivo:
            return f"Eae, {nome} está AFK no momento — {motivo}" + (f", {tempo_txt}." if tempo_txt else ".")
        return f"Eae, {nome} está AFK no momento." + (f" Volta em {tempo_txt}." if tempo_txt else "")

    partes = []
    if motivo:
        partes.append(f"motivo: {motivo}")
    if tempo_txt:
        partes.append(f"tempo restante: {tempo_txt}")
    ctx = ", ".join(partes) or "ausente sem motivo informado"

    prompt = (
        f"Gere UMA frase curta e casual avisando que {membro.display_name} está AFK. "
        f"Contexto: {ctx}. Use a menção exata: {nome}. "
        f"Estilo: brasileiro jovem, sem emojis, sem asteriscos. Máximo 15 palavras."
    )
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=60,
            temperature=0.7,
            messages=[
                {"role": "system", "content": "Você gera avisos curtos e naturais de AFK para um servidor Discord brasileiro. Sem emojis, sem markdown, sem aspas."},
                {"role": "user", "content": prompt},
            ],
        )
        texto = resp.choices[0].message.content.strip().strip('"').strip("'")
        if nome not in texto:
            texto = f"Eae, {nome} está AFK — {motivo or 'ausente no momento'}."
        return texto
    except Exception as e:
        log.error(f"_gerar_aviso_afk_ia: {e}")
        if motivo:
            return f"Eae, {nome} está AFK no momento — {motivo}" + (f", {tempo_txt}." if tempo_txt else ".")
        return f"Eae, {nome} está AFK no momento." + (f" Volta em {tempo_txt}." if tempo_txt else "")


async def processar_ordem_mod(message: discord.Message) -> bool:
    """
    Processa apenas comandos de moderação para o cargo de mod (1487859369008697556).
    Comandos disponíveis: silenciar, dessilenciar, banir, desbanir, expulsar, avisar, regras, listar.
    Não executa ordens gerais (boas-vindas, histórias, etc.) — isso é privilégio dos superiores.
    """
    conteudo = message.content.strip()
    cmd, resto = extrair_comando(conteudo)

    CMDS_MOD = {
        "silenciar", "mute", "mutar", "calar",
        "dessilenciar", "unmute", "desmutar",
        "banir", "ban",
        "desbanir", "unban",
        "expulsar", "kick",
        "avisar", "avisa",
        "regras",
        "listar", "lista", "palavras", "filtros",
        "adicionar", "adiciona", "bloquear", "bloqueia", "filtrar", "filtra",
        "remover", "remove", "desbloquear", "desbloqueia",
        "ajuda", "help", "comandos",
        "entradas", "saidas", "saídas", "fluxo", "relatorio", "relatório",
        "historico", "histórico",
    }

    if cmd in CMDS_MOD:
        return await processar_ordem(message)

    # Comando não reconhecido para mod — não executa ordens gerais
    return False


async def resposta_inicial_superior(conteudo: str, autor: str, user_id: int, guild=None, membro=None, canal_id: int = None, message: discord.Message = None) -> str:
    """
    Versão estendida de resposta_inicial para superiores.
    Aceita ordens diretas. Quando a ordem envolve enviar em canal específico,
    envia diretamente lá e retorna string vazia para o caller não reenviar.
    """
    msg = conteudo.lower()

    # ── Detectar canal mencionado na mensagem (<#ID>) ─────────────────────────
    canal_alvo = None
    if message and message.channel_mentions:
        canal_alvo = message.channel_mentions[0]
    elif guild and canal_id:
        canal_alvo = guild.get_channel(canal_id)

    # ── Ordens de boas-vindas (só executa quando explicitamente solicitado) ────
    if any(p in msg for p in ["boas-vindas", "boas vindas", "dá boas-vindas", "da boas-vindas",
                               "bem-vindo", "bem vindo", "recepciona", "receba os membros"]):
        alvos = []
        if membro and membro.guild:
            alvos = [m for m in membro.guild.members
                     if m.joined_at and not m.bot
                     and (datetime.now(timezone.utc) - m.joined_at.replace(tzinfo=timezone.utc)).days < 1
                     and m.id != client.user.id]

        if alvos:
            nomes = " ".join(m.mention for m in alvos[:5])
            texto_bv = f"Sejam bem-vindos ao servidor {nomes}. Leiam as regras em {CANAL_REGRAS} e bom aprendizado."
        else:
            texto_bv = f"Bem-vindos ao servidor. Leiam as regras em {CANAL_REGRAS} e aproveitem."

        # Se o superior especificou um canal diferente do atual, envia lá
        if canal_alvo and message and canal_alvo.id != message.channel.id:
            await canal_alvo.send(texto_bv)
            return f"Boas-vindas enviadas em {canal_alvo.mention}."
        return texto_bv

    # ── Ordens de história / contar algo ──────────────────────────────────────
    if any(p in msg for p in ["conta uma história", "conta uma historia", "conta um caso",
                               "narra uma história", "me conta algo", "conta pra galera",
                               "conta algo interessante", "história"]):
        prompt = (
            "Você é um assistente de servidor Discord brasileiro, direto e sem floreios. "
            "Conte uma história curta (máximo 4 frases) sobre tecnologia, ciência ou cultura brasileira. "
            "Sem emojis, sem asteriscos, sem markdown, sem dois pontos. Fale como brasileiro jovem."
        )
        try:
            resp = await _groq_client().chat.completions.create(
                model="llama-3.3-70b-versatile",
                max_tokens=200,
                messages=[
                    {"role": "system", "content": prompt},
                    {"role": "user", "content": conteudo},
                ],
            )
            texto_hist = resp.choices[0].message.content.strip()
        except Exception:
            texto_hist = "Não consigo contar histórias agora. Tenta mais tarde."

        if canal_alvo and message and canal_alvo.id != message.channel.id:
            await canal_alvo.send(texto_hist)
            return f"História enviada em {canal_alvo.mention}."
        return texto_hist

    # ── Ordens de interação com o público ─────────────────────────────────────
    if any(p in msg for p in ["anima o servidor", "anima a galera", "interaja", "interage",
                               "fala pra galera", "chama atenção", "engaja", "movimenta"]):
        opcoes = [
            "Ei galera, qual foi a última coisa útil que vocês aprenderam essa semana?",
            "Alguém aqui tem projeto em andamento? Fala o que tá construindo.",
            "Pergunta rápida, qual linguagem de programação vocês mais usam atualmente?",
            "Debate rápido, terminal ou IDE? Fala aí.",
            "Galera, qual foi o último bug mais bizarro que vocês encontraram?",
        ]
        texto_eng = random.choice(opcoes)
        if canal_alvo and message and canal_alvo.id != message.channel.id:
            await canal_alvo.send(texto_eng)
            return f"Mensagem enviada em {canal_alvo.mention}."
        return texto_eng

    # ── Ordens de aviso público ────────────────────────────────────────────────
    if any(p in msg for p in ["avisa o servidor", "avisa a galera", "comunica", "anuncia"]):
        for prefixo in ["avisa o servidor", "avisa a galera", "comunica que", "anuncia que", "comunica", "anuncia"]:
            if prefixo in msg:
                idx = msg.find(prefixo) + len(prefixo)
                texto_aviso = conteudo[idx:].strip(" :,.")
                # Remove menção de canal do texto do aviso
                texto_aviso = re.sub(r'<#\d+>\s*', '', texto_aviso).strip()
                if texto_aviso:
                    mencao_todos = guild.default_role if guild else "@everyone"
                    msg_aviso = f"Atenção {mencao_todos}, {texto_aviso}"
                    destino = canal_alvo if (canal_alvo and message and canal_alvo.id != message.channel.id) else None
                    if destino:
                        await destino.send(msg_aviso)
                        return f"Aviso enviado em {destino.mention}."
                    return msg_aviso
        return "Qual é o aviso? Manda o conteúdo depois do comando."

    # ── Fallback ───────────────────────────────────────────────────────────────
    return await resposta_inicial(conteudo, autor, user_id, guild, membro, canal_id)


ESCALA_SILENCIO = [
    (10, "dez minutos"),
    (60, "uma hora"),
    (1440, "vinte e quatro horas"),
]

async def silenciar(membro: discord.Member, canal, motivo: str):
    mod = mencao_mod(membro.guild)
    vez = silenciamentos[membro.id]
    idx = min(vez, len(ESCALA_SILENCIO) - 1)
    minutos, descricao = ESCALA_SILENCIO[idx]
    try:
        ate = agora_utc() + timedelta(minutes=minutos)
        await membro.timeout(ate, reason=motivo)
        silenciamentos[membro.id] += 1
        infracoes[membro.id] = 0
        salvar_dados()
        await canal.send(
            f"{membro.mention}, você foi silenciado por {descricao}. "
            f"Reincidências resultam em silêncios mais longos."
        )
        log.info(f"Silenciado: {membro.display_name} por {descricao} (vez {vez + 1})")
    except Exception as e:
        log.error(f"Falha ao silenciar {membro.display_name}: {e}")
        await canal.send(f"{membro.mention} atingiu o limite de infrações. {mod}, tomem providências.")


@client.event
async def on_ready():
    global _contexto_servidor
    carregar_dados()
    print(f"Conectado como {client.user}")
    guild = client.get_guild(SERVIDOR_ID)
    if guild:
        pode = tem_permissao_moderacao(guild)
        log.info(f"Servidor: {guild.name} | moderação: {'sim' if pode else 'apenas avisos'}")
        _contexto_servidor = build_server_context(guild)
        log.info(f"Contexto mapeado: {len(guild.channels)} canais, {len(guild.roles)} cargos, {guild.member_count} membros.")
    else:
        log.error(f"Servidor {SERVIDOR_ID} não encontrado.")


@client.event
async def on_guild_channel_create(channel):
    """Atualiza o contexto quando um canal é criado."""
    global _contexto_servidor
    if channel.guild.id == SERVIDOR_ID:
        _contexto_servidor = build_server_context(channel.guild)


@client.event
async def on_guild_channel_delete(channel):
    """Atualiza o contexto quando um canal é deletado."""
    global _contexto_servidor
    if channel.guild.id == SERVIDOR_ID:
        _contexto_servidor = build_server_context(channel.guild)


@client.event
async def on_guild_role_create(role):
    """Atualiza o contexto quando um cargo é criado."""
    global _contexto_servidor
    if role.guild.id == SERVIDOR_ID:
        _contexto_servidor = build_server_context(role.guild)


@client.event
async def on_guild_role_delete(role):
    """Atualiza o contexto quando um cargo é deletado."""
    global _contexto_servidor
    if role.guild.id == SERVIDOR_ID:
        _contexto_servidor = build_server_context(role.guild)


@client.event
async def on_member_join(member: discord.Member):
    """Registra entrada de membro e loga no canal de auditoria."""
    if member.guild.id != SERVIDOR_ID:
        return

    agora = agora_utc()
    ts = agora.isoformat()

    if member.id not in registro_entradas:
        registro_entradas[member.id] = []
    registro_entradas[member.id].append(ts)
    nomes_historico[member.id] = member.display_name
    salvar_dados()

    idade_conta = agora - member.created_at.replace(tzinfo=timezone.utc)
    conta_nova = idade_conta.days < 7
    vezes = len(registro_entradas[member.id])

    canal_audit = member.guild.get_channel(CANAL_AUDITORIA_ID)
    if canal_audit:
        aviso = " | CONTA NOVA" if conta_nova else ""
        reentrada = f" | Reentrada n.{vezes}" if vezes > 1 else ""
        await canal_audit.send(
            f"[ENTRADA]{aviso}{reentrada} {member.display_name} ({member.id}) "
            f"entrou. Conta criada há {formatar_duracao(idade_conta)}."
        )

    # Atualiza contexto do servidor
    global _contexto_servidor
    _contexto_servidor = build_server_context(member.guild)
    log.info(f"Entrada: {member.display_name} ({member.id}) | conta: {formatar_duracao(idade_conta)}{' | CONTA NOVA' if conta_nova else ''}")

    # ── Raid detection ────────────────────────────────────────────────────────
    _joins_recentes.append(agora)
    # Remove entradas fora da janela
    corte = agora - RAID_JANELA
    while _joins_recentes and _joins_recentes[0] < corte:
        _joins_recentes.pop(0)

    if len(_joins_recentes) >= RAID_LIMIAR:
        novas = sum(
            1 for m in member.guild.members
            if not m.bot and (agora - m.created_at.replace(tzinfo=timezone.utc)).days < RAID_CONTA_NOVA_DIAS
        )
        canal_audit = member.guild.get_channel(CANAL_AUDITORIA_ID)
        if canal_audit:
            mod = mencao_mod(member.guild)
            await canal_audit.send(
                f"[ALERTA]POSSÍVEL RAID: {len(_joins_recentes)} entradas nos últimos 2 minutos "
                f"({novas} contas com menos de {RAID_CONTA_NOVA_DIAS} dias). "
                f"{mod}, verifiquem imediatamente."
            )
        log.warning(f"RAID detectado: {len(_joins_recentes)} joins em 2min, {novas} contas novas")
        _joins_recentes.clear()  # Evita alertas duplicados


@client.event
async def on_member_remove(member: discord.Member):
    """Registra saída de membro e loga no canal de auditoria."""
    if member.guild.id != SERVIDOR_ID:
        return

    agora = agora_utc()
    ts = agora.isoformat()

    ficou_segundos = None
    ficou_txt = "tempo desconhecido"
    if member.joined_at:
        delta = agora - member.joined_at.replace(tzinfo=timezone.utc)
        ficou_segundos = int(delta.total_seconds())
        ficou_txt = formatar_duracao(delta)

    if member.id not in registro_saidas:
        registro_saidas[member.id] = []
    registro_saidas[member.id].append({
        "nome": member.display_name,
        "saiu": ts,
        "ficou_segundos": ficou_segundos,
    })
    nomes_historico[member.id] = member.display_name
    salvar_dados()

    canal_audit = member.guild.get_channel(CANAL_AUDITORIA_ID)
    if canal_audit:
        await canal_audit.send(
            f"[SAÍDA] {member.display_name} ({member.id}) saiu. "
            f"Ficou por {ficou_txt}."
        )

    global _contexto_servidor
    _contexto_servidor = build_server_context(member.guild)
    log.info(f"Saída: {member.display_name} ({member.id}) | ficou: {ficou_txt}")


# Palavras-chave ofensivas em nomes de emoji customizado do servidor
# (emojis Unicode são ambíguos demais para filtrar — muitos usos legítimos)
NOMES_EMOJI_OFENSIVOS = [
    "nigger", "crioulo",
    "viado", "bicha",
    "retardado",
    "nazi", "hitler", "kkk",
]


@client.event
async def on_reaction_add(reaction: discord.Reaction, user):
    """Remove reações com emojis customizados ofensivos de membros comuns."""
    if not reaction.message.guild or reaction.message.guild.id != SERVIDOR_ID:
        return
    if user == client.user:
        return

    # Garante que user é Member (tem .roles); reações podem retornar User
    membro = reaction.message.guild.get_member(user.id)
    if membro is None:
        return
    if eh_autorizado(membro):
        return

    # Só filtra emojis customizados — Unicode tem muitos usos legítimos
    emoji = reaction.emoji
    if isinstance(emoji, str):
        return

    nome_norm = normalizar(emoji.name.lower())
    for termo in NOMES_EMOJI_OFENSIVOS:
        if termo in nome_norm:
            try:
                await reaction.remove(user)
            except Exception:
                pass
            try:
                await reaction.message.channel.send(
                    f"{membro.mention}, emojis com esse nome não são permitidos aqui. "
                    f"Leia as regras em {CANAL_REGRAS}."
                )
            except Exception:
                pass
            infracoes[membro.id] += 1
            salvar_dados()
            log.info(f"Reação removida: {membro.display_name}: {emoji.name}")
            break


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    if not message.guild or message.guild.id != SERVIDOR_ID:
        return

    # Ignorar DMs completamente — o bot não age em DM
    if not message.guild:
        return

    # Ignorar mensagens de outros bots com prefixo (ex: 7!afk, !cmd, /cmd)
    # Só ignora se começar com prefixo e não mencionar este bot
    conteudo_raw = message.content
    if re.match(r'^\s*\S+[!/]\S', conteudo_raw) and client.user not in message.mentions:
        return

    autor = message.author.display_name
    user_id = message.author.id
    conteudo = message.content

    _eh_dono = message.author.id in DONOS_IDS
    _eh_superior_ = eh_superior(message.author)   # donos + cargos superiores
    _eh_mod_ = eh_mod_exclusivo(message.author)    # só moderação (não superiores)
    eh_teste = message.author.id in CONTAS_TESTE

    # ── Verificar menção/gatilho ───────────────────────────────────────────────
    ids_mencionados = {m.id for m in message.mentions} | {
        int(m) for m in ID_PATTERN.findall(conteudo)
    }

    # Detecta resposta direta a uma mensagem do bot (reply com seta)
    eh_resposta_ao_bot = bool(
        message.reference
        and isinstance(getattr(message.reference, "resolved", None), discord.Message)
        and message.reference.resolved.author == client.user
    )

    mencionado = (
        client.user in message.mentions
        or client.user.id in ids_mencionados
        or bool(GATILHOS_NOME.search(conteudo))
        or eh_resposta_ao_bot
    )

    # ── AFK: se alguém marca o próprio usuário que está AFK, responde no canal ─
    if message.mentions:
        for mencionado_user in message.mentions:
            if mencionado_user == client.user:
                continue
            estado_afk = ausencia.get(mencionado_user.id)
            if estado_afk:
                motivo_afk = estado_afk.get("motivo", "")
                ate_afk = estado_afk.get("ate")
                msg_afk = await _gerar_aviso_afk_ia(mencionado_user, motivo_afk, ate_afk)
                await message.channel.send(msg_afk)

    # ── Desativar AFK quando o próprio usuário manda mensagem ─────────────────
    if message.author.id in ausencia and not mencionado:
        del ausencia[message.author.id]
        await message.channel.send(f"{message.author.mention}, modo ausente desativado.")

    # ── Conta de teste: comandos liberados, sofre punições normalmente ─────────
    if eh_teste and not _eh_dono:
        tratado = await processar_ordem(message)
        if tratado:
            return
        # continua para verificação de violações abaixo

    # ── Donos: isentos de punição, comandos + ordens gerais sempre ativos ────────
    if _eh_dono:
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send(f"{message.author.mention}, modo ausente desativado.")
        tratado = await processar_ordem(message)
        if not tratado and mencionado:
            # Continua conversa ativa antes de cair em resposta_inicial_superior
            estado_conv = conversas.get(user_id)
            if estado_conv and (estado_conv.get("canal") is None or estado_conv["canal"] == message.channel.id):
                resp_conv = await continuar_conversa(user_id, conteudo, autor, message.guild)
                if resp_conv:
                    await message.reply(resp_conv)
                    return
            resposta = await resposta_inicial_superior(conteudo, autor, user_id, message.guild, message.author, message.channel.id, message)
            await message.reply(resposta)
        elif not tratado:
            await processar_links(message)
        return

    # ── Superiores: isentos de punição, comandos + ordens gerais (sem precisar mencionar) ──
    if _eh_superior_:
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send(f"{message.author.mention}, modo ausente desativado.")
        tratado = await processar_ordem(message)
        if not tratado and mencionado:
            # Continua conversa ativa antes de cair em resposta_inicial_superior
            estado_conv = conversas.get(user_id)
            if estado_conv and (estado_conv.get("canal") is None or estado_conv["canal"] == message.channel.id):
                resp_conv = await continuar_conversa(user_id, conteudo, autor, message.guild)
                if resp_conv:
                    await message.reply(resp_conv)
                    return
            resposta = await resposta_inicial_superior(conteudo, autor, user_id, message.guild, message.author, message.channel.id, message)
            await message.reply(resposta)
        elif not tratado:
            await processar_links(message)
        return

    # ── Equipe de mod: isenta de punições, comandos de moderação (sem precisar mencionar) ──
    if _eh_mod_:
        tratado = await processar_ordem_mod(message)
        if not tratado and mencionado:
            resposta = await resposta_inicial(conteudo, autor, user_id, message.guild, message.author, message.channel.id)
            await message.reply(resposta)
        return  # mods nunca são punidos

    # ── Detectar flood (membros comuns) ───────────────────────────────────────
    if detectar_flood(message.author.id, conteudo):
        await message.channel.send(
            f"Ei {message.author.mention}, para com o spam! Regra número 1 dos canais em {CANAL_REGRAS}."
        )
        log.warning(f"Flood detectado: {autor}")
        return

    # ── Verificar links com VirusTotal ────────────────────────────────────────
    await processar_links(message)

    # ── Detectar violações ────────────────────────────────────────────────────
    violacoes = detectar_violacoes(conteudo)
    if violacoes:
        infracoes[message.author.id] += 1
        count = infracoes[message.author.id]

        categoria_atual = violacoes[0][0].split(",")[0].strip()
        categoria_anterior = ultimo_motivo.get(message.author.id, "")
        mesmo_motivo = categoria_anterior and categoria_atual == categoria_anterior
        ultimo_motivo[message.author.id] = categoria_atual
        salvar_dados()

        # Verifica se é discriminação/racismo para punição imediata
        eh_discriminacao = any(
            "discriminação" in desc or "bullying" in desc
            for desc, _ in violacoes
        )

        log.warning(f"Infração {count}/3 de {autor}: {[(d, p) for d, p in violacoes]}")

        msg_id = message.id
        try:
            await message.delete()
        except Exception:
            pass
        await enviar_auditoria(message.guild, message.author, violacoes, msg_id)

        # Racismo/discriminação: silêncio imediato na 1ª infração
        if eh_discriminacao:
            if tem_permissao_moderacao(message.guild) and hasattr(message.author, 'timeout'):
                await silenciar(message.author, message.channel, "discriminação — tolerância zero")
            else:
                await message.channel.send(
                    f"{message.author.mention}, mensagem removida por discriminação ou racismo. "
                    f"Tolerância zero para esse tipo de conduta. {mencao_mod(message.guild)}, tomem providências."
                )
            return

        if count >= 3:
            if tem_permissao_moderacao(message.guild) and hasattr(message.author, 'timeout'):
                await silenciar(message.author, message.channel, "3 infrações")
            else:
                await message.channel.send(
                    f"{message.author.mention} atingiu o limite de infrações. Moderador, tome providências."
                )
        elif count == 1:
            if len(violacoes) == 1:
                desc_v, _ = violacoes[0]
                partes = desc_v.split(", ", 1)
                desc = partes[0]
                ref = partes[1] if len(partes) > 1 else CANAL_REGRAS
                corpo = f"por se referir de {desc} que consta na {ref}"
            else:
                itens = []
                for desc_v, _ in violacoes:
                    partes = desc_v.split(", ", 1)
                    num_m = re.search(r'número (\d+)', partes[1]) if len(partes) > 1 else None
                    num = num_m.group(1) if num_m else "?"
                    itens.append(f"{partes[0]} (regra número {num})")
                corpo = f"por se referir de {' e '.join(itens)}, conforme os termos em {CANAL_REGRAS}"

            await message.channel.send(
                f"Ei {message.author.mention}, sua mensagem foi removida {corpo}. "
                f"Isso fica esclarecido só essa vez, caso se repita mais duas vezes, serão tomadas providências."
            )
        else:
            motivo_texto = "pelo mesmo motivo" if mesmo_motivo else f"por outro motivo ({categoria_atual})"
            await message.channel.send(
                f"Ei {message.author.mention}, você está acumulando infrações, essa é a {count}ª {motivo_texto}, "
                f"por isso a mensagem continua sendo anulada. Na próxima, você será silenciado temporariamente. "
                f"Caso persista, serão tomadas medidas drásticas e moderativas sobre seu paradeiro."
            )

        return

    # ── Info de membro via menção ─────────────────────────────────────────────
    if mencionado and message.mentions:
        alvos_info = [m for m in message.mentions if m != client.user]
        if alvos_info and any(p in conteudo.lower() for p in ["info", "informação", "quem é", "tempo no", "quando entrou", "idade", "silenciado", "timeout", "punição", "punicao"]):
            texto = await api_info_membro_completa(message.guild, alvos_info[0])
            await message.reply(texto)
            return

    # ── Queries factuais do servidor (cargos, membros por cargo, etc.) ────────
    if mencionado and message.guild:
        resp_direta = await query_servidor_direto(message.guild, message.content)
        if resp_direta:
            await message.reply(resp_direta)
            return

    # ── Continuar conversa em andamento (mesmo canal e sem @menção nova) ────────
    estado_conv = conversas.get(user_id)
    if estado_conv and client.user not in message.mentions:
        canal_conv = estado_conv.get("canal")
        if canal_conv is None or canal_conv == message.channel.id:
            resposta = await continuar_conversa(user_id, conteudo, autor, message.guild)
            if resposta:
                log.info(f"Conversa: {autor}: {conteudo}")
                await message.reply(resposta)
                return
        else:
            del conversas[user_id]

    # ── Continuar conversa Claude ativa ──────────────────────────────────────
    estado_groq = conversas_groq.get(user_id)
    if estado_groq and client.user not in message.mentions and not GATILHOS_NOME.search(conteudo):
        if estado_groq["canal"] == message.channel.id:
            tempo_ocioso = agora_utc() - estado_groq["ultima"]
            if tempo_ocioso <= TIMEOUT_CONVERSA_GROQ:
                # Queries factuais respondem direto sem IA
                if message.guild:
                    resp_direta = await query_servidor_direto(message.guild, message.content)
                    if resp_direta:
                        await message.reply(resp_direta)
                        return
                resposta = await responder_com_groq(conteudo, autor, user_id, message.guild, message.channel.id)
                log.info(f"Claude cont: {autor}: {conteudo}")
                await message.reply(resposta)
                return
            else:
                # Conversa expirou — limpa histórico do canal para evitar drift
                historico_groq.pop((user_id, estado_groq["canal"]), None)
                del conversas_groq[user_id]
        else:
            # Mudou de canal — limpa histórico do canal anterior
            historico_groq.pop((user_id, estado_groq["canal"]), None)
            del conversas_groq[user_id]

    # ── Responder menção/gatilho de membros comuns ────────────────────────────
    if mencionado:
        for dono_id in DONOS_IDS:
            estado = dono_ausente(dono_id)
            if estado:
                dono_referenciado = (
                    dono_id in ids_mencionados
                    or bool(GATILHOS_NOME.search(conteudo))
                )
                if dono_referenciado:
                    await message.reply(mensagem_ausencia(estado, autor))
                    return

        resposta = await resposta_inicial(conteudo, autor, user_id, message.guild, message.author, message.channel.id)
        log.info(f"Menção de {autor}: {conteudo[:80]}")
        await message.reply(resposta)
        log.info(f"Respondido: {autor}")


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN não definido. Configure a variável de ambiente antes de iniciar.")

try:
    client.run(TOKEN)
except discord.errors.LoginFailure:
    raise SystemExit("Token inválido ou expirado. Atualize a variável DISCORD_TOKEN no Railway.")
except KeyboardInterrupt:
    pass
