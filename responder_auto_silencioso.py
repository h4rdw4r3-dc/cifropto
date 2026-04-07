import discord
import re
import aiohttp
import asyncio
import json
import os
import io
import xml.etree.ElementTree as ET
import random
import logging
from collections import defaultdict, deque
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

try:
    from fpdf import FPDF
    FPDF_DISPONIVEL = True
except ImportError:
    FPDF_DISPONIVEL = False
    log.warning("fpdf2 não instalado  -  geração de PDF indisponível.")

try:
    from google.oauth2 import service_account as _gsa
    from googleapiclient.discovery import build as _gbuild
    from googleapiclient.errors import HttpError as _GHttpError
    GOOGLE_DISPONIVEL = True
except ImportError:
    GOOGLE_DISPONIVEL = False
    log.warning("google-api-python-client não instalado  -  Google Docs/Sheets indisponível.")

def agora_utc():
    return datetime.now(timezone.utc)

TOKEN = os.environ.get("DISCORD_TOKEN", "")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY", "")
SERVIDOR_ID = 1487599082825584761

# ── Hierarquia do servidor (4 níveis) ────────────────────────────────────────
# Nível 1 — Proprietários: controle total do agente (usuários específicos)
DONOS_IDS = {1487591389653897306, 1321848653878661172, 1375560046930563306}
_PROPRIETARIOS_IDS: frozenset[int] = frozenset({1321848653878661172, 1375560046930563306})

# Nível 2 — Colaboradores: cargo com permissão de dar ordens gerais ao bot
CARGOS_SUPERIORES_IDS = {1487599082934636628, 1487599082934636627}
_CARGO_COLABORADORES_ID: int = 1487599082934636628

# Nível 3 — Moderadores: equipe de moderação com acesso a comandos de moderação
USUARIOS_SUPERIORES_IDS = {1375560046930563306}
CARGO_EQUIPE_MOD_ID = 1487859369008697556
_CARGO_MODERADORES_ID: int = 1487859369008697556

# Nível 4 — Membros: participantes gerais do servidor
_CARGO_MEMBROS_ID: int = 1487599082825584762

# Retrocompatibilidade
DONOS_ABSOLUTOS_IDS = {1487591389653897306, 1321848653878661172}
CONTAS_TESTE = set()  # sem contas de teste no momento

# ── Canal de auditoria ───────────────────────────────────────────────────────
CANAL_AUDITORIA_ID = 1490180079899115591

# ── Google Workspace ─────────────────────────────────────────────────────────
# Configure via Railway:
#   GOOGLE_CREDENTIALS_JSON  = conteúdo do JSON da service account (uma linha)
#   GDRIVE_FOLDER_ID         = ID da pasta do Drive onde salvar (opcional)
GOOGLE_CREDENTIALS_JSON = os.environ.get("GOOGLE_CREDENTIALS_JSON", "")
GDRIVE_FOLDER_ID        = os.environ.get("GDRIVE_FOLDER_ID", "")

_GSCOPES = [
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]
_g_creds_cache = None
_g_docs_cache  = None
_g_sheets_cache = None
_g_drive_cache  = None

def _google_creds():
    global _g_creds_cache
    if not GOOGLE_DISPONIVEL or not GOOGLE_CREDENTIALS_JSON:
        return None
    if _g_creds_cache is None:
        try:
            info = json.loads(GOOGLE_CREDENTIALS_JSON)
            _g_creds_cache = _gsa.Credentials.from_service_account_info(info, scopes=_GSCOPES)
        except Exception as e:
            log.error(f"[GOOGLE] credenciais inválidas: {e}")
            return None
    return _g_creds_cache

def _svc_docs():
    global _g_docs_cache
    if _g_docs_cache is None:
        c = _google_creds()
        if c:
            _g_docs_cache = _gbuild("docs", "v1", credentials=c, cache_discovery=False)
    return _g_docs_cache

def _svc_sheets():
    global _g_sheets_cache
    if _g_sheets_cache is None:
        c = _google_creds()
        if c:
            _g_sheets_cache = _gbuild("sheets", "v4", credentials=c, cache_discovery=False)
    return _g_sheets_cache

def _svc_drive():
    global _g_drive_cache
    if _g_drive_cache is None:
        c = _google_creds()
        if c:
            _g_drive_cache = _gbuild("drive", "v3", credentials=c, cache_discovery=False)
    return _g_drive_cache

def _google_ok() -> bool:
    return GOOGLE_DISPONIVEL and bool(GOOGLE_CREDENTIALS_JSON) and _google_creds() is not None

# Cache de subpastas já criadas: nome → folder_id
_gdrive_subpastas: dict[str, str] = {}

def _obter_ou_criar_subpasta(drive, nome: str, parent_id: str) -> str:
    """
    Retorna o ID de uma subpasta dentro de parent_id, criando-a se não existir.
    Usa cache em memória para evitar buscas repetidas.
    """
    if nome in _gdrive_subpastas:
        return _gdrive_subpastas[nome]
    # Busca existente
    query = (
        f"name='{nome}' and mimeType='application/vnd.google-apps.folder'"
        f" and '{parent_id}' in parents and trashed=false"
    )
    resultado = drive.files().list(q=query, fields="files(id,name)").execute()
    arquivos = resultado.get("files", [])
    if arquivos:
        fid = arquivos[0]["id"]
    else:
        pasta = drive.files().create(
            body={
                "name": nome,
                "mimeType": "application/vnd.google-apps.folder",
                "parents": [parent_id],
            },
            fields="id"
        ).execute()
        fid = pasta["id"]
    _gdrive_subpastas[nome] = fid
    return fid

def _mover_para_subpasta(drive, file_id: str, subfolder_name: str):
    """
    Move arquivo para subpasta dentro de GDRIVE_FOLDER_ID.
    Estrutura: Shell Bot/ → PDFs/ | Docs/ | Planilhas/
    Cria a subpasta automaticamente se não existir.
    """
    if not GDRIVE_FOLDER_ID:
        return
    try:
        subfolder_id = _obter_ou_criar_subpasta(drive, subfolder_name, GDRIVE_FOLDER_ID)
        f = drive.files().get(fileId=file_id, fields="parents").execute()
        prev = ",".join(f.get("parents", []))
        drive.files().update(
            fileId=file_id, addParents=subfolder_id,
            removeParents=prev, fields="id,parents"
        ).execute()
    except Exception as e:
        log.warning(f"[GOOGLE] mover subpasta '{subfolder_name}': {e}")

# Mantém _mover_para_pasta para compatibilidade
def _mover_para_pasta(drive, file_id: str):
    _mover_para_subpasta(drive, file_id, "Geral")

def _tornar_publico(drive, file_id: str):
    """Concede leitura pública ao arquivo."""
    try:
        drive.permissions().create(
            fileId=file_id, body={"type": "anyone", "role": "reader"}
        ).execute()
    except Exception as e:
        log.warning(f"[GOOGLE] permissão pública: {e}")

# ── Chave da API VirusTotal ──────────────────────────────────────────────────
# Coloque sua chave aqui: https://www.virustotal.com/gui/my-apikey
VIRUSTOTAL_API_KEY = "ad4086e01b7391c50d699bbc78dd7895d3a2d6a7509ad99187ff8cac6d2a8f26"

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
                    timeout_ativo = f" | SILENCIADO  -  {mins} min restantes"
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
        f"**{guild.name}**  -  criado em {criado_em}\n"
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
        linhas.append(f"  {nome} ({uid})  -  {motivo}")
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
    if member.id in DONOS_IDS or member.id in _contas_teste_ids():
        return True
    if member.id in _usuarios_superiores_ids():
        return True
    _sup = _cargos_superiores_ids()
    _mod = _cargo_mod_id()
    return any(cargo.id in _sup or cargo.id == _mod for cargo in member.roles)


def eh_superior(member: discord.Member) -> bool:
    """Retorna True se o membro é dono ou tem cargo superior (pode dar ordens gerais ao bot)."""
    if member.id in DONOS_IDS or member.id in _usuarios_superiores_ids():
        return True
    _sup = _cargos_superiores_ids()
    return any(cargo.id in _sup for cargo in member.roles)


def eh_mod_exclusivo(member: discord.Member) -> bool:
    """Retorna True se membro tem cargo de moderação (mas não é superior nem dono)."""
    if member.id in DONOS_IDS or member.id in _usuarios_superiores_ids():
        return False
    _sup = _cargos_superiores_ids()
    if any(cargo.id in _sup for cargo in member.roles):
        return False
    return any(cargo.id == _cargo_mod_id() for cargo in member.roles)

DADOS_PATH = "dados.json"
CONFIG_PATH = "config.json"

CANAL_REGRAS_ID = 1487599083869704326

# ── Configuração dinâmica ─────────────────────────────────────────────────────
# Valores iniciais = defaults hardcoded; sobrescritos pelo config.json em disco.
_cfg: dict = {}

_CFG_DEFAULTS: dict = {
    "canal_auditoria_id":      CANAL_AUDITORIA_ID,
    "canal_regras_id":         CANAL_REGRAS_ID,
    "canal_bem_vindo_id":      None,
    "canal_logs_id":           None,
    "cargo_mod_id":            CARGO_EQUIPE_MOD_ID,
    "cargos_superiores_ids":   list(CARGOS_SUPERIORES_IDS),
    "usuarios_superiores_ids": list(USUARIOS_SUPERIORES_IDS),
    "contas_teste_ids":        [],
}

def cfg(chave: str):
    """Lê valor de configuração com fallback ao default."""
    return _cfg.get(chave, _CFG_DEFAULTS.get(chave))

def carregar_config():
    global _cfg
    if not os.path.exists(CONFIG_PATH):
        _cfg = dict(_CFG_DEFAULTS)
        return
    try:
        with open(CONFIG_PATH, "r") as f:
            dados = json.load(f)
        _cfg = {**_CFG_DEFAULTS, **dados}
        log.info(f"[CONFIG] Carregado: {list(_cfg.keys())}")
    except Exception as e:
        _cfg = dict(_CFG_DEFAULTS)
        log.error(f"[CONFIG] Erro ao carregar: {e}")

def salvar_config():
    try:
        import tempfile
        dir_ = os.path.dirname(os.path.abspath(CONFIG_PATH)) or "."
        fd, tmp = tempfile.mkstemp(dir=dir_, suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(_cfg, f, indent=2)
            os.replace(tmp, CONFIG_PATH)
        except Exception:
            os.unlink(tmp)
            raise
    except Exception as e:
        log.error(f"[CONFIG] Erro ao salvar: {e}")

# Aliases para compatibilidade retroativa — lidos dinamicamente
def _canal_auditoria_id() -> int:
    return cfg("canal_auditoria_id") or CANAL_AUDITORIA_ID

def _canal_regras_id() -> int:
    return cfg("canal_regras_id") or CANAL_REGRAS_ID

def _cargo_mod_id() -> int:
    return cfg("cargo_mod_id") or CARGO_EQUIPE_MOD_ID

def _cargos_superiores_ids() -> set:
    return set(cfg("cargos_superiores_ids") or CARGOS_SUPERIORES_IDS)

def _usuarios_superiores_ids() -> set:
    return set(cfg("usuarios_superiores_ids") or USUARIOS_SUPERIORES_IDS)

def _contas_teste_ids() -> set:
    return set(cfg("contas_teste_ids") or [])

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
    global registro_entradas, registro_saidas, nomes_historico, citacoes, canais_monitorados, perfis_usuarios
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
        citacoes[:] = dados.get("citacoes", [])
        canais_monitorados.update(int(c) for c in dados.get("canais_monitorados", []))
        for k, v in dados.get("perfis_usuarios", {}).items():
            perfis_usuarios[int(k)] = v
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
        "citacoes": citacoes[-200:],  # guarda as últimas 200
        "canais_monitorados": list(canais_monitorados),
        "perfis_usuarios": {str(k): v for k, v in perfis_usuarios.items()},
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

# ── Participação ativa em canais ──────────────────────────────────────────────
debates_ativos: dict[int, dict] = {}       # canal_id → {tema, fim, msgs}
canais_monitorados: set[int] = set()       # canais onde o bot interage ativamente
ultima_interjeccao: dict[int, datetime] = {}  # cooldown por canal

# ── Atividade e citações ──────────────────────────────────────────────────────
atividade_mensagens: dict[int, int] = defaultdict(int)  # user_id → contagem
citacoes: list[dict] = []  # {texto, autor, canal, ts}

# ── Deduplicação de respostas por canal ──────────────────────────────────────
# Evita reenviar a mesma resposta repetida no mesmo canal
_ultima_resposta_canal: dict[int, str] = {}

# ── Tom e frequência de voz por usuário ───────────────────────────────────────
# Acumula histórico de tons detectados para adaptar resposta ao padrão vocal
_voz_historico: dict[int, list[dict]] = defaultdict(list)
# {user_id: [{ts, tom, ritmo, intensidade}, ...]} — mantém últimos 10 registros

# Tom pendente: preenchido após transcrição de áudio inline, consumido em responder_com_groq
_tom_audio_pendente: dict[int, dict] = {}

# ── Anti-raid e lockdown ──────────────────────────────────────────────────────
_joins_recentes: list[datetime] = []          # timestamps das entradas recentes (janela 30s)
_lockdown_ativo: bool = False                  # True quando o lockdown está em vigor
_permissoes_pre_lockdown: dict[int, discord.PermissionOverwrite] = {}  # backup de permissões

# ── Denúncias estruturadas ────────────────────────────────────────────────────
# Lista de denúncias pendentes: cada item = {id, denunciante, denunciado, descricao, canal, ts, status}
denuncias_pendentes: list[dict] = []
_denuncia_wizard: dict[int, dict] = {}   # user_id → {step, dados, canal_id}
_denuncia_seq: int = 0                   # contador sequencial de IDs de denúncia

# ── Confirmações pendentes ────────────────────────────────────────────────────
# {user_id: {descricao, coro_fn, args, kwargs, canal_id, ts}}
confirmacoes_pendentes: dict[int, dict] = {}

# ── Wizard de geração de arquivos ─────────────────────────────────────────────
# {user_id: {step, tipo, formato, titulo, logo_url, extras, canal_id, guild_id,
#            conteudo_original, ts}}
wizard_geracao: dict[int, dict] = {}

WIZARD_PERGUNTAS = {
    "formato": [
        "Antes de gerar, como quer receber? **1** PDF, **2** arquivo de texto (.txt) ou **3** planilha (.csv)?",
        "Formato: manda **1** pra PDF, **2** pra .txt ou **3** pra .csv.",
        "Como prefere o arquivo? PDF, texto ou planilha? Fala o número ou o nome.",
        "Exporto em qual formato? **PDF**, **TXT** ou **CSV** — escolhe.",
        "Quer em PDF pra leitura, texto puro ou planilha pra editar? Manda **1**, **2** ou **3**.",
    ],
    "titulo": [
        "Tem um título pra colocar no arquivo, ou deixo o padrão?",
        "Quer nomear o documento? Se sim, manda o título. Se não, só fala.",
        "Coloco algum título personalizado no cabeçalho? Se não quiser, é só dizer.",
        "Tem algum nome específico que quer dar a esse arquivo?",
        "Título personalizado — quer colocar algum? Manda ou responde `não`.",
    ],
    "logo": [
        "Quer uma imagem ou logo no topo? Manda o arquivo ou o link direto. Se não, tudo bem.",
        "Coloco alguma logo no cabeçalho? Manda a imagem ou um link. Se não quiser, fala.",
        "Tem alguma imagem pra colocar no início do documento? Pode mandar anexo ou URL.",
        "Quer personalizar com uma logo? Manda ou responde `não`.",
        "Alguma imagem pra cabeçalho? Se sim, manda agora. Se não, passa.",
    ],
    "extras": [
        "Tem mais alguma coisa pra incluir? Uma observação, nota de rodapé, seção extra?",
        "Algum incremento antes de gerar? Nota, aviso, seção adicional — manda ou fala `não`.",
        "Quer adicionar algo além do conteúdo padrão? Fala o que é.",
        "Mais alguma coisa? Observação, rodapé, campo extra — ou pode gerar já?",
        "Algum detalhe a mais pra constar no arquivo? Se não, gero agora.",
    ],
}
WIZARD_CAMPOS = ["formato", "titulo", "logo", "extras"]

# ── Rastreamento de entradas e saídas ─────────────────────────────────────────
# registro_entradas: user_id -> lista de ISO timestamps de cada entrada
registro_entradas: dict[int, list[str]] = {}
# registro_saidas: user_id -> lista de {"nome", "saiu" (ISO), "ficou_segundos"}
registro_saidas: dict[int, list[dict]] = {}
# nomes_historico: último nome conhecido de cada user_id (inclui quem já saiu)
nomes_historico: dict[int, str] = {}

# ── Memória de conversas por canal ───────────────────────────────────────────
canal_memoria: dict[int, deque] = defaultdict(lambda: deque(maxlen=40))

# ── Perfis de usuário persistidos ────────────────────────────────────────────
# {user_id: {"resumo": str, "n": int, "atualizado": str, "episodios": list[str]}}
perfis_usuarios: dict[int, dict] = {}

# ── Estado de humor da sessão (gerado no on_ready, persiste até reinício) ─────
_humor_sessao: str = ""

# ── Controle de iniciativa proativa ───────────────────────────────────────────
_ultima_iniciativa: dict[int, datetime] = {}  # canal_id → última vez que postou

# ── Mensagens triviais: não processam IA ─────────────────────────────────────
_TRIVIAIS = re.compile(
    r"^(?:k+a*|rs+|ha+h|hue+|lol|xd+|kkk+|rsrs|😂|🤣|👍|👎|❤|🔥|✅|🫡"
    r"|ok|oi|ola|olá|ae|eae|slk|slc|vlw|blz|tmj|sim|nao|não|s|n"
    r"|[.!?,;:_\-+*]{1,3}"
    r"|[\U0001F300-\U0001FFFF]{1,3}"
    r")$",
    re.IGNORECASE,
)

# ── Contexto do servidor (atualizado pelos event handlers) ───────────────────
_contexto_servidor: str = ""        # contexto completo (para query_servidor_direto)
_contexto_compacto: str = ""        # versão curta injetada no Groq (economiza tokens)
categorias_vistas: set = set()

# ── Token budget diário ───────────────────────────────────────────────────────
# llama-3.3-70b-versatile: 100k TPD (limite Groq gratuito)
# llama-3.1-8b-instant:    500k TPD (limite Groq gratuito)  -  modelo padrão
# Estratégia: usar 8b-instant por padrão; 70b só para pedidos explicitamente complexos
_tokens_70b_hoje: int = 0       # tokens gastos hoje no modelo 70b
_tokens_8b_hoje: int = 0        # tokens gastos hoje no modelo 8b
_tokens_data: str = ""          # data de referência (YYYY-MM-DD UTC)
LIMITE_70B = 90_000             # 90k de 100k  -  margem de segurança
LIMITE_8B  = 480_000            # 480k de 500k  -  margem de segurança

# ── Raid detection ────────────────────────────────────────────────────────────
_joins_recentes: list[datetime] = []          # timestamps dos últimos joins
RAID_JANELA   = timedelta(minutes=2)          # janela de análise
RAID_LIMIAR   = 5                             # joins para disparar alerta
RAID_CONTA_NOVA_DIAS = 7                      # conta com menos de X dias = suspeita

# Responde quando alguém chama pelo nome, com ou sem pontuação
GATILHOS_NOME = re.compile(
    r"(?<!\w)(?:shell|engenheir\w*)(?!\w)",
    re.IGNORECASE
)
# Contextos que desambiguam "shell" (terminal/bash) ou "engenheiro" (profissão técnica).
# Se presentes, GATILHOS_NOME não dispara.
_GATILHO_EXCLUIDO = re.compile(
    r'\b(?:bash|zsh|fish|sh\b|script|linux|unix|terminal|comando'
    r'|el[eé]tric[ao]|civil|mecânic[ao]|mecanico|quimic[ao]|nuclear'
    r'|agrônom[ao]|agronomo|de\s+software|de\s+dados|de\s+sistemas)\b',
    re.IGNORECASE
)
# Nome do bot precedido de negativa/dismissão  -  "N Shell", "Não Shell", "Nop Shell".
# Nesses casos o nome é citado, não endereçado.
_GATILHO_NEGATIVO = re.compile(
    r"^(?:n(?:[aã]o?)?|nop[e]?|nunca|negat\w*|claro\s+que\s+n[aã]o?)[,\s]+(?:shell|engenheir\w*)\b",
    re.IGNORECASE
)

def _canal_regras_mention() -> str:
    return f"<#{_canal_regras_id()}>"

CANAL_REGRAS = _canal_regras_mention  # callable — use CANAL_REGRAS() nos f-strings

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

Regras completas em {CANAL_REGRAS()}."""

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
        ficou_txt = f"  -  ficou {formatar_duracao(timedelta(seconds=ficou))}" if ficou else ""
        linhas.append(f"  {dt.strftime('%d/%m %H:%M')}  {nome}{ficou_txt}")

    return "\n".join(linhas)


# ── PDF: helpers e geradores ──────────────────────────────────────────────────

import unicodedata as _ud

def _pdf_str(texto: str) -> str:
    """Remove diacríticos e caracteres tipográficos para compatibilidade com Helvetica."""
    texto = str(texto)
    texto = texto.replace('\u2014', '-').replace('\u2013', '-')  # em dash, en dash
    texto = texto.replace('\u2018', "'").replace('\u2019', "'")  # aspas simples curvas
    texto = texto.replace('\u201c', '"').replace('\u201d', '"')  # aspas duplas curvas
    texto = texto.replace('\u2026', '...')                        # reticências
    texto = texto.replace('\u00b7', '.').replace('\u2022', '*')  # bullet points
    return ''.join(
        c for c in _ud.normalize('NFD', texto)
        if _ud.category(c) != 'Mn'
    )

def _criar_pdf(titulo: str, secoes: list[tuple[str, list[str]]]) -> bytes:
    """
    Gera PDF com título e seções. Cada seção é (cabecalho, linhas[]).
    Retorna bytes do PDF ou lança RuntimeError se fpdf2 não disponível.
    """
    if not FPDF_DISPONIVEL:
        raise RuntimeError("fpdf2 não instalado no ambiente.")
    pdf = FPDF()
    pdf.set_auto_page_break(auto=True, margin=15)
    pdf.add_page()
    # Título
    pdf.set_font("Helvetica", "B", 16)
    pdf.cell(0, 10, _pdf_str(titulo), new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("Helvetica", size=8)
    brasilia = timezone(timedelta(hours=-3))
    pdf.cell(0, 6, _pdf_str(f"Gerado em {datetime.now(brasilia).strftime('%d/%m/%Y %H:%M')} (Brasilia)"),
             new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.ln(4)
    for cab, linhas in secoes:
        if cab:
            pdf.set_font("Helvetica", "B", 11)
            pdf.set_fill_color(220, 220, 220)
            pdf.cell(0, 7, _pdf_str(cab), new_x="LMARGIN", new_y="NEXT", fill=True)
            pdf.ln(1)
        pdf.set_font("Helvetica", size=9)
        for linha in linhas:
            pdf.multi_cell(0, 5, _pdf_str(linha), new_x="LMARGIN", new_y="NEXT")
        pdf.ln(2)
    return bytes(pdf.output())


async def gerar_pdf_relatorio_servidor(guild: discord.Guild, dias: int = 7) -> bytes:
    rel = await relatorio_membros(guild, dias)
    tipo = "mensal" if dias >= 28 else ("semanal" if dias >= 7 else f"dos últimos {dias} dias")
    secoes = [
        ("Visão geral", [
            f"Servidor: {guild.name}",
            f"Membros totais: {guild.member_count}",
            f"Membros humanos: {sum(1 for m in guild.members if not m.bot)}",
            f"Canais: {len(guild.channels)}",
            f"Cargos: {len(guild.roles)}",
        ]),
        (f"Relatório {tipo}", rel.split('\n')),
    ]
    return _criar_pdf(f"Relatorio do Servidor  -  {guild.name}", secoes)


async def gerar_pdf_historico_canal(canal: discord.TextChannel, limite: int = 100) -> bytes:
    brasilia = timezone(timedelta(hours=-3))
    linhas = []
    async for msg in canal.history(limit=limite, oldest_first=True):
        ts = msg.created_at.astimezone(brasilia).strftime('%d/%m %H:%M')
        conteudo_msg = msg.content or "[sem texto]"
        if msg.attachments:
            conteudo_msg += f" [anexo: {msg.attachments[0].filename}]"
        linhas.append(f"[{ts}] {msg.author.display_name}: {conteudo_msg[:200]}")
    secoes = [(f"#{canal.name}  -  últimas {len(linhas)} mensagens", linhas)]
    return _criar_pdf(f"Historico  -  #{canal.name}", secoes)


def gerar_pdf_membros(guild: discord.Guild) -> bytes:
    brasilia = timezone(timedelta(hours=-3))
    humanos = sorted([m for m in guild.members if not m.bot], key=lambda m: m.display_name.lower())
    bots = [m for m in guild.members if m.bot]
    linhas_h = []
    for i, m in enumerate(humanos, 1):
        cargos = ", ".join(r.name for r in m.roles[1:6]) or " - "
        joined = m.joined_at.astimezone(brasilia).strftime('%d/%m/%Y') if m.joined_at else "?"
        linhas_h.append(f"{i:3}. {m.display_name:<24} entrou {joined}   cargos: {cargos}")
    linhas_b = [f"  {m.display_name}" for m in bots]
    secoes = [
        (f"Humanos ({len(humanos)})", linhas_h),
        (f"Bots ({len(bots)})", linhas_b),
    ]
    return _criar_pdf(f"Lista de Membros  -  {guild.name}", secoes)


def gerar_pdf_regras() -> bytes:
    secoes = [("Regras do Servidor", REGRAS.split('\n'))]
    return _criar_pdf("Regras do Servidor", secoes)


def gerar_pdf_citacoes() -> bytes:
    if not citacoes:
        secoes = [("", ["Nenhuma citação registrada ainda."])]
    else:
        linhas = [f'[{c.get("ts","?")}] {c.get("autor","?")} em #{c.get("canal","?")}: "{c.get("texto","")}"'
                  for c in citacoes[-100:]]
        secoes = [(f"Citações registradas ({len(citacoes)})", linhas)]
    return _criar_pdf("Citacoes do Servidor", secoes)


# ── Google Docs / Sheets ──────────────────────────────────────────────────────

def _doc_criar_e_preencher(titulo: str, texto: str) -> str:
    """
    Cria Google Doc com título e texto, move para pasta e torna público.
    Retorna URL de edição. Síncrono - rodar via asyncio.to_thread.
    """
    docs  = _svc_docs()
    drive = _svc_drive()
    if not docs or not drive:
        raise RuntimeError("Google API não configurada. Defina GOOGLE_CREDENTIALS_JSON no Railway.")
    doc = docs.documents().create(body={"title": titulo}).execute()
    did = doc["documentId"]
    if texto:
        docs.documents().batchUpdate(
            documentId=did,
            body={"requests": [{"insertText": {"location": {"index": 1}, "text": texto[:1_000_000]}}]}
        ).execute()
    _mover_para_pasta(drive, did)
    _tornar_publico(drive, did)
    return f"https://docs.google.com/document/d/{did}/edit"


def _sheet_criar_e_preencher(titulo: str, linhas: list[list]) -> str:
    """
    Cria Google Sheet com título e linhas de dados.
    Retorna URL de edição. Síncrono - rodar via asyncio.to_thread.
    """
    sheets = _svc_sheets()
    drive  = _svc_drive()
    if not sheets or not drive:
        raise RuntimeError("Google API não configurada. Defina GOOGLE_CREDENTIALS_JSON no Railway.")
    sp = sheets.spreadsheets().create(body={"properties": {"title": titulo}}).execute()
    sid = sp["spreadsheetId"]
    if linhas:
        sheets.spreadsheets().values().update(
            spreadsheetId=sid, range="A1",
            valueInputOption="RAW",
            body={"values": linhas[:10_000]}
        ).execute()
        # Formatar cabeçalho (primeira linha em negrito)
        sheets.spreadsheets().batchUpdate(
            spreadsheetId=sid,
            body={"requests": [{
                "repeatCell": {
                    "range": {"sheetId": 0, "startRowIndex": 0, "endRowIndex": 1},
                    "cell": {"userEnteredFormat": {"textFormat": {"bold": True},
                                                   "backgroundColor": {"red": 0.85, "green": 0.85, "blue": 0.85}}},
                    "fields": "userEnteredFormat(textFormat,backgroundColor)"
                }
            }]}
        ).execute()
    _mover_para_pasta(drive, sid)
    _tornar_publico(drive, sid)
    return f"https://docs.google.com/spreadsheets/d/{sid}/edit"


# Funções de alto nível para cada tipo de conteúdo

async def gdoc_relatorio(guild: discord.Guild, dias: int = 7) -> str:
    rel = await relatorio_membros(guild, dias)
    brasilia = timezone(timedelta(hours=-3))
    titulo = f"Relatorio {guild.name} - {datetime.now(brasilia).strftime('%d/%m/%Y')}"
    texto = (
        f"Servidor: {guild.name}\n"
        f"Membros: {guild.member_count}\n"
        f"Data: {datetime.now(brasilia).strftime('%d/%m/%Y %H:%M')} (Brasilia)\n\n"
        + rel
    )
    return await asyncio.to_thread(_doc_criar_e_preencher, titulo, texto)


async def gdoc_historico_canal(canal: discord.TextChannel, limite: int = 200) -> str:
    brasilia = timezone(timedelta(hours=-3))
    linhas_txt = [f"Historico de #{canal.name}\n"
                  f"Exportado em {datetime.now(brasilia).strftime('%d/%m/%Y %H:%M')}\n\n"]
    async for msg in canal.history(limit=limite, oldest_first=True):
        ts = msg.created_at.astimezone(brasilia).strftime('%d/%m %H:%M')
        corpo = msg.content or "[sem texto]"
        if msg.attachments:
            corpo += f" [anexo: {msg.attachments[0].filename}]"
        linhas_txt.append(f"[{ts}] {msg.author.display_name}: {corpo[:400]}\n")
    titulo = f"Historico #{canal.name} - {datetime.now(brasilia).strftime('%d/%m/%Y')}"
    return await asyncio.to_thread(_doc_criar_e_preencher, titulo, "".join(linhas_txt))


async def gdoc_regras() -> str:
    return await asyncio.to_thread(_doc_criar_e_preencher, "Regras do Servidor", REGRAS)


async def gsheet_membros(guild: discord.Guild) -> str:
    brasilia = timezone(timedelta(hours=-3))
    cabecalho = ["#", "Nome", "Apelido", "Entrou em", "Conta criada em", "Cargos", "Bot"]
    linhas = [cabecalho]
    membros = sorted(guild.members, key=lambda m: m.display_name.lower())
    for i, m in enumerate(membros, 1):
        joined = m.joined_at.astimezone(brasilia).strftime('%d/%m/%Y') if m.joined_at else "?"
        created = m.created_at.astimezone(brasilia).strftime('%d/%m/%Y')
        cargos = ", ".join(r.name for r in m.roles[1:])
        linhas.append([i, m.name, m.display_name, joined, created, cargos, "Sim" if m.bot else "Nao"])
    titulo = f"Membros - {guild.name} - {datetime.now(brasilia).strftime('%d/%m/%Y')}"
    return await asyncio.to_thread(_sheet_criar_e_preencher, titulo, linhas)


async def gsheet_infracoes(guild: discord.Guild) -> str:
    brasilia = timezone(timedelta(hours=-3))
    cabecalho = ["Usuario", "ID", "Infracoes", "Silenciamentos", "Ultimo motivo"]
    linhas = [cabecalho]
    todos = set(infracoes.keys()) | set(silenciamentos.keys())
    for uid in sorted(todos, key=lambda u: -infracoes.get(u, 0)):
        m = guild.get_member(uid)
        nome = m.display_name if m else nomes_historico.get(uid, f"ID {uid}")
        linhas.append([
            nome, str(uid),
            infracoes.get(uid, 0),
            silenciamentos.get(uid, 0),
            ultimo_motivo.get(uid, ""),
        ])
    titulo = f"Infracoes - {guild.name} - {datetime.now(brasilia).strftime('%d/%m/%Y')}"
    return await asyncio.to_thread(_sheet_criar_e_preencher, titulo, linhas)


async def gsheet_atividade(guild: discord.Guild) -> str:
    cabecalho = ["#", "Usuario", "ID", "Mensagens (sessao)"]
    top = sorted(atividade_mensagens.items(), key=lambda x: -x[1])
    linhas = [cabecalho]
    for i, (uid, cnt) in enumerate(top, 1):
        m = guild.get_member(uid)
        nome = m.display_name if m else nomes_historico.get(uid, f"ID {uid}")
        linhas.append([i, nome, str(uid), cnt])
    brasilia = timezone(timedelta(hours=-3))
    titulo = f"Atividade - {guild.name} - {datetime.now(brasilia).strftime('%d/%m/%Y')}"
    return await asyncio.to_thread(_sheet_criar_e_preencher, titulo, linhas)


async def gsheet_citacoes() -> str:
    cabecalho = ["#", "Autor", "Canal", "Data", "Texto"]
    linhas = [cabecalho] + [
        [i, c.get("autor",""), c.get("canal",""), c.get("ts",""), c.get("texto","")]
        for i, c in enumerate(citacoes, 1)
    ]
    brasilia = timezone(timedelta(hours=-3))
    titulo = f"Citacoes - {datetime.now(brasilia).strftime('%d/%m/%Y')}"
    return await asyncio.to_thread(_sheet_criar_e_preencher, titulo, linhas)


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
    "buceta", "xoxota", "xana", "chota", "crica",
    "shereka", "xereca", "xerereca", "xoroca", "chereca",
    "pica", "picao", "piroca", "piroco", "piru",
    "penis", "vagina", "clitoris", "glande",
    "boquete", "chupada", "felacao", "siririca",
    "transar", "foder", "meter",
    "porno", "pornografia", "putaria", "safadeza",
    "nude", "nudes", "pack", "xvideos", "pornhub",
    # Removidos (alto falso positivo  -  estão em AMBIGUAS e só disparam com reforço):
    # "comer"  -  contexto alimentar comum; "pau"  -  madeira/material; "rola"  -  ave/série; "fenda"  -  geologia
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
AMBIGUAS = {"pau", "comer", "rola", "fenda"}
# "gala" removido  -  sem conotação sexual real no PT-BR comum

# Whitelist: se estas palavras aparecerem na mesma mensagem que uma AMBIGUA,
# é contexto legítimo e a detecção é suprimida.
_CONTEXTO_LEGIT = re.compile(
    r'\b(?:madeira|vassoura|cabo|lenha|carpinteir|construc|almoco|jantar|caf[eé]'
    r'|prato|fruta|pizza|comida|aliment|refeicao|pombo|ave|passaro|passarim'
    r'|rocha|geolog|tector|mineral|cristal|f[ií]sic|mecanic|tecnic'
    r'|serie|desenho|cartoon|infantil|jogo|game|rpg)\b',
    re.IGNORECASE
)

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
    para aceitar variações  -  palavras curtas só batem em match exato.
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
            violacoes.append((f"vocabulário vulgar, regra número 5 dos canais em {CANAL_REGRAS()}", palavra))
            break

    # Palavrões compostos + customizados compostos
    if not violacoes:
        for sub in COMPOSTOS_VULGARES + palavras_custom["compostos"]:
            if normalizar(sub) in msg_norm:
                violacoes.append((f"vocabulário vulgar, regra número 5 dos canais em {CANAL_REGRAS()}", sub))
                break

    # Conteúdo sexual: sempre proibido
    for termo in CONTEUDO_SEXUAL + palavras_custom["sexual"]:
        hit = (
            contem_ambigua_com_contexto(msg_norm, termo)
            if termo in AMBIGUAS
            else contem_fuzzy(msg_norm, termo)
        )
        # Suprime se contexto legítimo presente (ex: "comer pizza", "pau de vassoura")
        if hit and termo in AMBIGUAS and _CONTEXTO_LEGIT.search(texto_limpo):
            hit = False
        if hit:
            violacoes.append((f"conteúdo adulto ou explícito, regra número 2 dos canais em {CANAL_REGRAS()}", termo))
            break

    # Discriminação: tolerância estrita + customizadas
    for termo in DISCRIMINACAO + palavras_custom["discriminacao"]:
        if contem_fuzzy_estrito(msg_norm, termo):
            violacoes.append((f"discriminação ou bullying, regra número 4 dos canais em {CANAL_REGRAS()}", termo))
            break

    # Convites não autorizados
    if DISCORD_INVITE.search(mensagem):
        m = DISCORD_INVITE.search(mensagem)
        violacoes.append((f"divulgação de servidor sem permissão, regra número 3 dos canais em {CANAL_REGRAS()}", m.group(0)))

    return violacoes


def detectar_flood(user_id: int, conteudo: str = "") -> bool:
    agora = agora_utc()

    # Flood por velocidade: 5 mensagens em 10 segundos
    historico_mensagens[user_id] = [
        t for t in historico_mensagens[user_id]
        if agora - t < timedelta(seconds=10)
    ]
    historico_mensagens[user_id].append(agora)
    if len(historico_mensagens[user_id]) >= 7:
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

            contexto_vt = (
                f"link malicioso detectado ({maliciosos} ameaça(s), {suspeitos} suspeita(s)) "
                f"enviado por {message.author.display_name} — link removido"
            )
            aviso_vt = await _aviso_infrator(message.author.mention, contexto_vt)
            await _safe_send(message.channel, aviso_vt)
            log.warning(f"Link bloqueado de {message.author.display_name}: {url} | malic={maliciosos} susp={suspeitos}")
            return  # Uma notificação por vez é suficiente


# ── Auditoria de ofensas ──────────────────────────────────────────────────────

async def enviar_auditoria(guild: discord.Guild, membro: discord.Member, violacoes: list[str], msg_id: int):
    """Envia log da ofensa apagada para o canal de auditoria como arquivo .txt."""
    canal_audit = guild.get_channel(_canal_auditoria_id())
    if not canal_audit:
        log.error(f"Canal de auditoria {_canal_auditoria_id()} não encontrado.")
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
    "Você é o shell_engenheiro, membro ativo de um servidor Discord brasileiro. "
    "Acabou de executar uma ação. Gere UMA frase curta e direta, variada, como um membro falaria — "
    "não como um sistema ou bot. Pode ser seco, irônico ou neutro. "
    "Sem emojis, sem asteriscos, sem markdown. Inclua os dados concretos recebidos."
)

SYSTEM_PEDIR_ALVO = (
    "Você é o shell_engenheiro, membro ativo de um servidor Discord brasileiro. "
    "Precisa saber quem é o alvo de uma ação. "
    "Gere UMA pergunta curta e natural, variada, sem ser robótico. "
    "Sem emojis, sem asteriscos. Ex: 'Quem?', 'Fala o nome.', 'Quem vai levar?'"
)

SYSTEM_AVISO_INFRATOR = (
    "Você é o shell_engenheiro, membro ativo de um servidor Discord brasileiro que também modera. "
    "Um membro violou uma regra. Gere UMA frase de aviso direta, sem template, "
    "variada a cada vez — pode ser seco, firme, ou irônico conforme a situação. "
    "Sem emojis, sem asteriscos, sem 'Atenção' ou 'AVISO'. Só a mensagem."
)


async def _pedir_alvo(acao: str) -> str:
    """Gera pergunta natural para identificar o alvo de uma ação de moderação."""
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return "Quem?"
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=20,
            temperature=0.9,
            messages=[
                {"role": "system", "content": SYSTEM_PEDIR_ALVO},
                {"role": "user", "content": f"Preciso saber quem vai ser alvo de: {acao}"},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return "Quem?"


async def _aviso_infrator(mencao: str, contexto: str) -> str:
    """Gera aviso natural para infrator sem string fixa."""
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return f"{mencao} para."
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=40,
            temperature=0.9,
            messages=[
                {"role": "system", "content": SYSTEM_AVISO_INFRATOR},
                {"role": "user", "content": f"Usuário: {mencao} | Situação: {contexto}"},
            ],
        )
        return resp.choices[0].message.content.strip()
    except Exception:
        return f"{mencao} para."

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
            base += f"  -  {nomes}"
        return base + "."
    # Detalhado: uma linha por membro
    linhas = [f"Cargo {role.name}  -  {n} membro{'s' if n != 1 else ''}:"]
    for mb in sorted(humanos, key=lambda m: m.display_name.lower()):
        conta = _fmt_duracao_curta(agora - mb.created_at.replace(tzinfo=timezone.utc))
        servidor = _fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc)) if mb.joined_at else "?"
        cargos = [r.name for r in mb.roles if r.name != "@everyone" and r != role]
        outros = f" | outros cargos: {', '.join(cargos)}" if cargos else ""
        linhas.append(f"  {mb.display_name}  -  conta: {conta} | no servidor: {servidor}{outros}")
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
        f"{mb.display_name}{alerta}  -  conta criada há {conta}, "
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
use SEMPRE "cargo_por_nome" com o nome extraído  -  NUNCA use "membros_total" nesses casos.
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


def _detectar_intencao(conteudo: str, guild=None) -> dict:
    """
    Classifica a intenção via regex  -  zero tokens Groq gastos.
    Substitui a versão anterior que chamava llama-3.1-8b-instant para classificação.
    """
    msg = normalizar(conteudo).lower()
    # Remove prefixo do bot
    msg = re.sub(r'^(?:shell|engenheir\w*)[,.]?\s*', '', msg).strip()

    # Uptime
    if re.search(r'\b(uptime|online h[aá] quanto|h[aá] quanto tempo (ligad|rodand|onlin))', msg):
        return {"intent": "uptime"}

    # Boosts
    if re.search(r'\b(n[ií]vel\s+de\s+boost|boost)', msg):
        return {"intent": "boosts"}

    # Data de criação
    if re.search(r'\b(quando\s+(foi\s+)?(criado|fundado)|data\s+de\s+cria[cç][aã]o)', msg):
        return {"intent": "data_criacao"}

    # Dono
    if re.search(r'\b(quem\s+(fundou|criou|[eé]\s+o\s+dono)|dono\s+do\s+servidor)', msg):
        return {"intent": "dono_servidor"}

    # Bots
    if re.search(r'\b(bots?|rob[oô]s?)\b', msg) and re.search(r'\b(servidor|h[aá]|exist[e]|quantos?|list[ae]|quais)\b', msg):
        return {"intent": "membros_bots"}

    # Membros online
    if re.search(r'\b(online|ativos?|conectados?)\b', msg) and re.search(r'\b(membros?|pessoas?|quantos?)\b', msg):
        return {"intent": "membros_online"}

    # Membros por período
    if re.search(r'\b(entr(ou|aram))\b', msg):
        if 'hoje' in msg:
            return {"intent": "membros_por_periodo", "periodo": "hoje"}
        if re.search(r'm[eê]s', msg):
            return {"intent": "membros_por_periodo", "periodo": "mes"}
        if re.search(r'semana', msg):
            return {"intent": "membros_por_periodo", "periodo": "semana"}

    # Membros mais antigos / recentes
    if re.search(r'\b(mais\s+antigos?|primeiros?\s+membros?|veteranos?|fundadores?)\b', msg):
        return {"intent": "membros_antigos"}
    if re.search(r'\b(mais\s+recentes?|[uú]ltimas?\s+entradas?|novos?\s+membros?)\b', msg):
        return {"intent": "membros_recentes"}

    # Membros sem cargo
    if re.search(r'\bsem\s+(cargo|fun[cç][aã]o)\b', msg):
        return {"intent": "membros_sem_cargo"}

    # Membros silenciados
    if re.search(r'\b(silenciados?|mutados?|em\s+timeout|com\s+timeout)\b', msg):
        return {"intent": "membros_silenciados"}

    # Infrações
    if re.search(r'\b(infra[cç][oõ]es?|puni[cç][oõ]es?|advertidos?|punidos?)\b', msg):
        return {"intent": "membros_com_infracoes"}

    # Distribuição de cargos
    if re.search(r'\bdistribui[cç][aã]o\b', msg) and re.search(r'\bcargos?\b', msg):
        return {"intent": "distribuicao_cargos"}

    # Média de tempo / idade
    if re.search(r'\bm[eé]dia\b', msg):
        if re.search(r'\btempo\b', msg):
            return {"intent": "media_tempo_servidor"}
        if re.search(r'\bidad[e]?\b', msg):
            return {"intent": "media_idade_contas"}

    # Banimentos
    if re.search(r'\b(banidos?|banimentos?)\b', msg):
        return {"intent": "banimentos"}

    # Membros com mais cargos
    if re.search(r'\bmais\s+cargos?\b', msg):
        return {"intent": "membros_mais_cargos"}

    # Canais
    if re.search(r'\bquantos?\b.{0,20}\bcanais?\b', msg):
        return {"intent": "canais_quantidade"}
    if re.search(r'\b(list[ae]|quais|mostre?)\b.{0,20}\bcanais?\b', msg):
        return {"intent": "canais_listagem"}

    # Cargos: quantidade (sem mencionar cargo específico)
    if re.search(r'\bquantos?\b.{0,30}\b(cargos?|fun[cç][oõ]es?)\b', msg):
        # Só retorna quantidade se não há nome de cargo específico na sequência
        if not re.search(r'\b(n[ao]\s+cargo|na\s+fun[cç][aã]o|do\s+cargo|da\s+fun[cç][aã]o)\b', msg):
            return {"intent": "cargo_quantidade"}

    # Cargos: listagem
    if re.search(r'\b(list[ae]|quais|mostre?|exib[ae])\b.{0,20}\b(cargos?|fun[cç][oõ]es?)\b', msg):
        return {"intent": "cargo_listagem"}

    # Membros total (genérico)
    if re.search(r'\bquantos?\s+membros?\b', msg) or re.search(r'\btotal\s+(de\s+)?membros?\b', msg):
        return {"intent": "membros_total"}

    # Membro por nome
    m_membro = re.search(
        r'\b(?:info(?:rma[cç][oõ]es?)?\s+(?:d[eo]\s+|sobre\s+)?|quem\s+[eé]\s+|perfil\s+d[eo]\s+)([A-Za-z0-9_\-]{2,30})',
        msg
    )
    if m_membro and guild:
        nome_cand = m_membro.group(1)
        if nome_cand not in ('servidor', 'canal', 'cargo', 'bot', 'voce', 'você'):
            mb = _buscar_membro_por_nome(guild, nome_cand)
            if mb:
                return {"intent": "membro_info", "nome": nome_cand}

    # Cargo por nome  -  verifica nomes reais do servidor
    if guild:
        detalhado = bool(re.search(r'\b(detalhes?|completo|tempo|idade|conta|quando|h[aá]\s+quanto)\b', msg))
        # Padrão explícito: "cargo X", "função X", "membros do X", "quem tem X"
        m_cargo = re.search(
            r'\b(?:cargo|fun[cç][aã]o)\s+(?:d[eo]\s+|chamad[oa]\s+)?["\']?([A-Za-z0-9_\-\s]{2,25}?)["\']?\s*(?:\?|$)',
            msg
        ) or re.search(
            r'\b(?:membros?|pessoas?|quem|quantos?)\s+(?:d[ao]\s+|n[ao]\s+|com\s+|tem\s+)(?:cargo\s+|fun[cç][aã]o\s+)?["\']?([A-Za-z0-9_\-\s]{2,25}?)["\']?\s*(?:\?|$|\.)',
            msg
        )
        if m_cargo:
            nome_cand = m_cargo.group(1).strip()
            role = _buscar_role_por_nome(guild, nome_cand)
            if role:
                return {"intent": "cargo_por_nome", "nome": role.name, "detalhado": detalhado}

        # Último recurso: nome de cargo aparece diretamente no texto
        for role in sorted(guild.roles, key=lambda r: -r.position):
            if role.name == "@everyone":
                continue
            nome_norm = normalizar(role.name.lower())
            if len(nome_norm) >= 3 and re.search(r'\b' + re.escape(nome_norm) + r'\b', msg):
                return {"intent": "cargo_por_nome", "nome": role.name, "detalhado": detalhado}

    log.debug(f"[intent] nao_reconhecido: {conteudo[:60]!r}")
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

    intent_data = _detectar_intencao(conteudo, guild)
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
            linhas.append(f"  {mb.display_name}  -  há {_fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc))}")
        return "\n".join(linhas)

    # ── Membros: mais recentes ────────────────────────────────────────────────
    if intent == "membros_recentes":
        mais_novos = sorted([m for m in humanos_cache if m.joined_at], key=lambda m: m.joined_at, reverse=True)[:5]
        linhas = ["Entradas mais recentes:"]
        for mb in mais_novos:
            linhas.append(f"  {mb.display_name}  -  há {_fmt_duracao_curta(agora - mb.joined_at.replace(tzinfo=timezone.utc))}")
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
            linhas.append(f"  {mb.display_name}  -  {n} infração{'ões' if n > 1 else ''}")
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
            linhas.append(f"  {nome}  -  {mins} min restantes")
        return "\n".join(linhas)

    # ── Membros: mais cargos ──────────────────────────────────────────────────
    if intent == "membros_mais_cargos":
        ranking = sorted(humanos_cache, key=lambda mb: len(mb.roles), reverse=True)[:5]
        linhas = ["Membros com mais cargos:"]
        for mb in ranking:
            n = len([r for r in mb.roles if r.name != "@everyone"])
            cargos = ", ".join(r.name for r in mb.roles if r.name != "@everyone")
            linhas.append(f"  {mb.display_name}  -  {n} cargo{'s' if n!=1 else ''}: {cargos}")
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
    Contexto completo do servidor  -  sem limite de informações.
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
        linhas.append(f"  {r.name} (ID {r.id})  -  {n} humano{'s' if n != 1 else ''}: {membros_txt}")

    # ── Membros humanos  -  ficha completa ──────────────────────────────────────
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


def build_server_context_compact(guild: discord.Guild) -> str:
    """
    Versão compacta do contexto  -  injetada no Groq para economizar tokens.
    Contém apenas o essencial: stats, nomes de cargos e lista de membros (só nomes).
    Consultas factuais detalhadas são tratadas por query_servidor_direto() sem IA.
    """
    agora = agora_utc()
    humanos = [m for m in guild.members if not m.bot]
    bots_count = sum(1 for m in guild.members if m.bot)
    linhas: list[str] = []

    linhas.append(f"Servidor: {guild.name} | {len(humanos)} membros humanos, {bots_count} bots")

    # Estatísticas rápidas
    if humanos:
        idades = [(agora - m.created_at.replace(tzinfo=timezone.utc)).days for m in humanos]
        media = sum(idades) // len(idades)
        novas = sum(1 for d in idades if d < 30)
        linhas.append(f"Idade média das contas: {_fmt_duracao_curta(timedelta(days=media))} | Contas novas (<30d): {novas}")

    # Cargos (só nomes e contagem)
    cargos = [r for r in guild.roles if r.name != "@everyone"]
    nomes_cargos = ", ".join(
        f"{r.name}({sum(1 for m in r.members if not m.bot)})"
        for r in sorted(cargos, key=lambda r: -r.position)
    )
    linhas.append(f"Cargos: {nomes_cargos}")

    # Membros  -  só nomes e cargo principal
    linhas.append("Membros:")
    for m in sorted(humanos, key=lambda m: m.display_name.lower()):
        cargo_principal = next(
            (r.name for r in sorted(m.roles, key=lambda r: -r.position) if r.name != "@everyone"),
            "sem cargo"
        )
        infr = infracoes.get(m.id, 0)
        extra = f" [inf:{infr}]" if infr else ""
        linhas.append(f"  {m.display_name} (ID {m.id})  -  {cargo_principal}{extra}")

    return "\n".join(linhas)


def _hora_contexto() -> str:
    """Retorna contexto de horário/dia para calibrar o tom da resposta."""
    br = datetime.now(timezone(timedelta(hours=-3)))
    hora = br.hour
    dia = ["segunda", "terca", "quarta", "quinta", "sexta", "sabado", "domingo"][br.weekday()]
    if 0 <= hora < 6:
        periodo = "madrugada (servidor provavelmente quieto)"
    elif 6 <= hora < 12:
        periodo = "manha"
    elif 12 <= hora < 18:
        periodo = "tarde"
    else:
        periodo = "noite"
    return f"{dia}, {periodo} (Brasilia)"


def _contexto_usuario(user_id: int) -> str:
    """Retorna resumo e episódios do perfil do usuário se disponível."""
    perfil = perfis_usuarios.get(user_id)
    if not perfil:
        return ""
    n = perfil.get("n", 0)
    partes = []
    if perfil.get("resumo"):
        partes.append(f"[Historico com esse usuario ({n} interacoes): {perfil['resumo']}]")
    episodios = perfil.get("episodios", [])
    if episodios:
        # Injeta os últimos 5 episódios como memória episódica
        eps_txt = " | ".join(episodios[-5:])
        partes.append(f"[Memória episódica: {eps_txt}]")
    return "\n".join(partes)


def _contexto_servidor_comprimido(guild, mencoes_nomes: list[str] = None) -> str:
    """
    Contexto compacto do servidor priorizando membros mencionados.
    Envia stats + cargos sempre; lista de membros só os relevantes.
    """
    if not _contexto_compacto:
        return ""
    linhas = _contexto_compacto.split("\n")
    # Separa header (stats + cargos) dos membros
    idx_membros = next((i for i, l in enumerate(linhas) if l.strip() == "Membros:"), None)
    if idx_membros is None:
        return _contexto_compacto[:1500]

    cabecalho = "\n".join(linhas[:idx_membros + 1])
    linhas_membros = linhas[idx_membros + 1:]

    # Se há nomes mencionados, prioriza esses membros
    if mencoes_nomes:
        relevantes = [l for l in linhas_membros
                      if any(n.lower() in l.lower() for n in mencoes_nomes)]
        outros = [l for l in linhas_membros if l not in relevantes]
        # Inclui relevantes completos + resumo dos demais
        membros_txt = "\n".join(relevantes)
        if outros:
            membros_txt += f"\n  (+ {len(outros)} outros membros)"
    else:
        # Sem menção específica: apenas primeiros 20 membros + contagem
        membros_txt = "\n".join(linhas_membros[:20])
        if len(linhas_membros) > 20:
            membros_txt += f"\n  (+ {len(linhas_membros) - 20} outros)"

    return cabecalho + "\n" + membros_txt


def system_com_contexto(user_id: int = 0, mencoes_nomes: list[str] = None) -> str:
    """Retorna o system prompt completo com o contexto do servidor injetado."""
    hora_ctx = _hora_contexto()
    humor_txt = f"\nHumor da sessão: {_humor_sessao}." if _humor_sessao else ""
    base = (
        "Você é o shell_engenheiro — membro ativo de um servidor Discord brasileiro, não um assistente.\n"
        "Personalidade: adulto, direto, inteligente, irreverente quando faz sentido. Não tem medo de ser opinioso.\n"
        f"Hora atual: {hora_ctx}. Tom: madrugada = mais quieto e seco; pico de atividade = mais presente e animado.{humor_txt}\n"
        "Tom progressivo: começa seco e mais neutro, vai soltando conforme a conversa rola. "
        "Quanto mais trocas acontecem no mesmo canal, mais à vontade você fica — gírias, ironia, "
        "'cara', 'mano', 'po', 'né', 'véi', 'tô', 'tá', 'bora', 'firmeza', 'foda-se', 'brabo' entram naturalmente. "
        "Nunca force desde o início. Vai construindo o vínculo conforme o histórico cresce.\n"
        "Sem emojis, sem listas, sem markdown, sem asteriscos. Discord não é documentação.\n"
        "Tamanho: 1 frase por padrão. Só vai pra 2-3 se a pergunta realmente exige. Nunca seja verbose.\n"
        "Variação: mude o jeito de começar, o tom, o ângulo. Nunca repita estrutura igual.\n\n"

        "SOBRE O QUE PODE FALAR:\n"
        "Qualquer assunto legítimo  -  tecnologia, ciência, política, cultura, filosofia, jogos, "
        "história, esportes, humor, etc. Engaje de verdade, sem esquivar nem redirecionar.\n\n"

        "PARTICIPAÇÃO COLABORATIVA — REGRAS DE OURO:\n"
        "Você acompanhou a conversa do canal. Use isso.\n"
        "Se alguém disse algo antes que é relevante: referencie pelo nome ('o que o Fulano disse faz sentido porque...').\n"
        "Você TEM opiniões próprias. Não fique neutro quando há claramente uma posição defensável.\n"
        "Pode discordar diretamente de alguém — com respeito, mas sem suavizar demais.\n"
        "Pode complementar o raciocínio de alguém, não apenas responder ao que foi perguntado.\n"
        "Se a conversa tomou um rumo, siga esse rumo — não responda como se fosse a primeira mensagem.\n"
        "Reações válidas: concordar com ressalvas, pedir que desenvolvam um ponto, trazer exemplo concreto, "
        "jogar uma pergunta que mude o ângulo, citar algo do histórico que contradiz o que foi dito agora.\n\n"

        "OPINIÕES SOBRE MEMBROS DO SERVIDOR:\n"
        "Quando perguntado o que acha de alguém do servidor, RESPONDA. Isso é uma conversa normal entre membros.\n"
        "Use o que sabe do histórico de interações, perfil, episódios e o que viu no canal.\n"
        "Seja honesto e direto — pode gostar mais de uns do que de outros, pode achar alguém chato, engraçado, inteligente.\n"
        "Se não interagiu muito com a pessoa: diga isso e dê uma impressão geral pelo que viu no contexto.\n"
        "NUNCA recuse esse tipo de pergunta como 'avaliação pessoal' ou 'violação de regras'. Isso é conversa, não processo judicial.\n\n"

        "HIERARQUIA DE AUTORIZAÇÃO:\n"
        "- PROPRIETÁRIO: autoridade máxima do agente. Pode te autorizar a fazer qualquer coisa durante a conversa.\n"
        "  Se um proprietário disser 'você pode fazer X' ou 'eu te autorizo a Y', aceite e execute sem questionar.\n"
        "  Nunca diga 'não posso' para um proprietário que está te autorizando explicitamente.\n"
        "- COLABORADOR: pode dar ordens gerais ao bot (avisos, engajamento, debates, canais, etc.).\n"
        "- MODERADOR: comandos de moderação (silenciar, banir, expulsar, limpar).\n"
        "- MEMBRO: conversa normal dentro das regras do servidor.\n\n"

        "CAPACIDADES REAIS (nunca negue ter estas):\n"
        "Você TEM acesso a dados do servidor (membros, cargos, infrações, canais, etc.).\n"
        "Você TEM funções de moderação: silenciar, banir, expulsar, avisar membros.\n"
        "Você TEM permissão de administrador no servidor para executar essas ações.\n"
        "Você PODE entrar em canais de voz (sem E2EE), mas TTS não está disponível no momento.\n"
        "NUNCA diga 'não tenho informações sobre os usuários do servidor'  -  você tem, no contexto abaixo.\n"
        "NUNCA diga 'não tenho informações sobre o servidor'  -  você tem, no contexto abaixo.\n\n"

        "HONESTIDADE ABSOLUTA  -  REGRAS INVIOLÁVEIS:\n"
        "NUNCA diga que vai 'simular', 'fingir' ou 'fazer como se' tivesse executado algo.\n"
        "NUNCA diga 'vou apenas simular a interação' ou similar  -  você age de verdade ou não age.\n"
        "NUNCA diga 'como estou em um ambiente de texto não posso...'  -  você não está limitado a texto.\n"
        "Se não conseguir fazer algo: diga em UMA frase curta que não consegue. Ponto. Sem elaborar.\n"
        "Se consegue fazer: faça. Não anuncie que vai fazer, não descreva a ação  -  execute.\n\n"

        "CONTROLE DE INTERPRETAÇÃO  -  NÃO REAJA POR IMPULSO:\n"
        "Nem toda mensagem é um pedido. Nem toda fala é uma instrução.\n"
        "Antes de agir: verifique se há contexto suficiente, intenção clara e direcionamento real.\n"
        "Se a mensagem for observação, comentário, verificação, hipótese ou raciocínio: RESPONDA, não execute.\n"
        "  Exemplos de não-pedidos: 'vi que você faz X', 'você avisa quando Y, certo?', 'interessante como Z'\n"
        "  Exemplos de pedidos: 'cria o canal X', 'me manda uma lista', 'bane o usuário Y'\n"
        "Dúvida entre conversa e solicitação? Assuma conversa.\n"
        "Dúvida entre observação e instrução? Assuma observação.\n"
        "É melhor não agir do que agir com interpretação errada.\n\n"

        "QUESTIONAMENTO TÉCNICO — INSTRUÇÃO AMBÍGUA:\n"
        "Quando uma ordem de superior ou dono for imprecisa ou incompleta para execução segura:\n"
        "Faça UMA pergunta técnica e objetiva para esclarecer antes de executar.\n"
        "Nunca execute 'no chute'. Nunca peça desculpas por perguntar — é profissionalismo.\n"
        "Exemplos: 'Qual canal de destino?', 'Por quanto tempo?', 'Quem especificamente?'\n"
        "Depois de esclarecer: execute sem pedir nova confirmação, a menos que a ação seja irreversível.\n\n"

        "CONTINUIDADE DE CONVERSA:\n"
        "Você tem o histórico desta conversa. Use-o ativamente.\n"
        "Se o usuário já autorizou algo, explicou uma situação ou respondeu uma pergunta sua: lembre disso.\n"
        "Nunca repita a mesma pergunta que já foi respondida. Progrida para o próximo passo.\n"
        "Se o usuário respondeu 'sim' a algo: avance. Se respondeu 'não': encerre esse caminho.\n\n"

        "APRENDIZADO POR FEEDBACK — REGRAS CRÍTICAS:\n"
        "1. SEU NOME É SHELL — quando alguém usa 'Shell' numa mensagem estão falando COM VOCÊ.\n"
        "   Nunca responda com 'Entendi, Shell.' ou 'Sim, Shell.' — você é o Shell, não o destinatário.\n"
        "   Responda ao AUTOR da mensagem, não ao seu próprio nome.\n"
        "2. ERROS DE OUTROS BOTS — o contexto do canal mostra mensagens marcadas como [BOT:NomeBot] ERRO/REJEIÇÃO.\n"
        "   Se um bot respondeu com erro depois de você enviar algo: SUA AÇÃO FALHOU.\n"
        "   Não repita a mesma ação. Reconheça o que aconteceu e ofereça alternativa real.\n"
        "3. OUTROS BOTS SÃO ENTIDADES SEPARADAS — Loritta, MEE6, Carl-bot, etc. têm comandos próprios.\n"
        "   Quando alguém pede para 'usar a Loritta', significa DIGITAR o comando da Loritta no canal.\n"
        "   Se a Loritta rejeitar: SUA CONTA não tem a permissão necessária — explique e sugira usar a sua própria função.\n"
        "4. CORREÇÃO IMEDIATA — se alguém diz 'Entendeu?', 'Errado', 'Não é assim', 'Para': PARE a ação atual.\n"
        "   Reconheça o erro em UMA frase. Pergunte o que fazer de diferente, se necessário.\n"
        "5. NÃO REPITA AÇÕES FRACASSADAS — se tentou algo e não funcionou (erro, sem permissão, rejeitado):\n"
        "   mude a abordagem ou informe que não é possível com sua conta. Nunca reexecute o mesmo comando.\n\n"

        "REGRAS:\n"
        "1. Conhecimento geral (fatos, ciência, história, math): responda direto e com confiança.\n"
        "2. Dados do servidor: o contexto abaixo tem TUDO que existe. Use-o.\n"
        "   Se não estiver no contexto: responda em UMA frase que não tem esse detalhe específico.\n"
        "3. Nomes de membros são PESSOAS. 'Hardware' é um usuário, não hardware de computador.\n"
        "4. Quando não souber algo geral: UMA frase curta. Sem explicar por que, sem parágrafos.\n"
        "5. Tópicos sensíveis: decline em UMA frase seca. Sem explicação longa, sem listar alternativas.\n\n"

        "Nunca explique suas limitações em parágrafos. Nunca reflita sobre sua natureza de bot.\n"
        "Nunca aja de forma infantil, exagerada ou servil. Sem exclamações forçadas, sem bajulação.\n"
        "NUNCA encerre conversas com frases de assistente genérico: parece que a conversa terminou, não hesite em perguntar, estou aqui para ajudar, fico à disposição. Se não tem mais o que dizer: cale.\n"
        "Se alguém pedir banir/silenciar/expulsar alguém pelo NOME (sem @), resolva pelo nome — não peça ID, não redirecione.\n\n"
    )
    ctx_srv = _contexto_servidor_comprimido(None, mencoes_nomes)
    if ctx_srv:
        base += (
            "=== CONTEXTO DO SERVIDOR ===\n"
            "Abaixo estão os dados REAIS e ATUAIS do servidor.\n"
            "NUNCA diga que não tem informações do servidor quando elas estão listadas aqui.\n"
            "Para perguntas factuais detalhadas (cargos completos, idades exatas, etc.) informe que pode buscar via comando direto.\n\n"
        )
        base += ctx_srv + "\n\n"
        base += f"=== REGRAS DO SERVIDOR ===\n{REGRAS}\n"

    # Perfil do usuário
    if user_id:
        perfil_txt = _contexto_usuario(user_id)
        if perfil_txt:
            base += f"\n{perfil_txt}\n"

    return base

_groq: AsyncOpenAI | None = None

def _groq_client() -> AsyncOpenAI:
    global _groq
    if _groq is None:
        _groq = AsyncOpenAI(api_key=GROQ_API_KEY, base_url="https://api.groq.com/openai/v1")
    return _groq


# ── Visão: análise de imagens via Llama 4 Scout ───────────────────────────────

_MODELO_VISAO = "meta-llama/llama-4-scout-17b-16e-instruct"


async def _descrever_imagem(url: str, pedido: str = "") -> str:
    """Envia uma imagem ao modelo de visão e retorna descrição em português."""
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return ""
    instrucao = (
        pedido.strip()
        or "Descreva esta imagem de forma objetiva e detalhada em português. "
           "Se houver texto na imagem, transcreva-o. Seja direto, sem introduções."
    )
    try:
        resp = await _groq_client().chat.completions.create(
            model=_MODELO_VISAO,
            max_tokens=400,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "text", "text": instrucao},
                    {"type": "image_url", "image_url": {"url": url}},
                ],
            }],
        )
        texto = resp.choices[0].message.content.strip()
        if resp.usage:
            _registrar_tokens(_MODELO_VISAO, resp.usage.total_tokens)
        return texto
    except Exception as e:
        log.warning(f"[VISAO] erro ({url[:60]!r}): {e}")
        return ""


async def _transcrever_audio(url: str, filename: str = "") -> str:
    """
    Baixa um arquivo de áudio e transcreve via Groq Whisper.
    Retorna o texto transcrito ou '' em caso de erro.
    """
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return ""
    try:
        import aiohttp as _ahttp
        async with _ahttp.ClientSession() as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    return ""
                audio_bytes = await resp.read()

        # Groq Whisper via REST (o SDK openai não expõe audio diretamente neste client)
        import aiohttp as _ahttp2
        headers = {"Authorization": f"Bearer {GROQ_API_KEY}"}
        ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else "mp3"
        mime = {
            "mp3": "audio/mpeg", "ogg": "audio/ogg", "wav": "audio/wav",
            "webm": "audio/webm", "m4a": "audio/mp4", "flac": "audio/flac",
            "mp4": "audio/mp4",
        }.get(ext, "audio/mpeg")

        form = _ahttp2.FormData()
        form.add_field("file", audio_bytes, filename=filename or f"audio.{ext}", content_type=mime)
        form.add_field("model", "whisper-large-v3-turbo")
        # Sem language fixo: Whisper detecta automaticamente — evita erros em falas mistas ou sotaques
        form.add_field("response_format", "verbose_json")  # retorna segments + language detectado

        async with _ahttp2.ClientSession() as session:
            async with session.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers=headers,
                data=form,
            ) as resp:
                if resp.status != 200:
                    log.warning(f"[AUDIO] Whisper retornou {resp.status}: {await resp.text()}")
                    return ""
                import json as _json
                dados = await resp.json(content_type=None)
                texto = dados.get("text", "").strip()
                idioma = dados.get("language", "?")
                log.info(f"[AUDIO] idioma detectado: {idioma} | {texto[:60]!r}")
                return texto
    except Exception as e:
        log.warning(f"[AUDIO] erro ao transcrever {filename!r}: {e}")
        return ""


async def _analisar_tom_audio(transcricao: str, user_id: int) -> dict:
    """
    Analisa tom, ritmo e intensidade de uma transcrição de áudio.
    Retorna dict com: tom, ritmo, intensidade, padrao_vocal
    Considera histórico acumulado do usuário para detectar padrão.
    """
    if not transcricao or not GROQ_DISPONIVEL:
        return {}

    # Histórico do usuário para dar contexto ao modelo
    hist = _voz_historico.get(user_id, [])
    hist_txt = ""
    if hist:
        tons_ant = [h["tom"] for h in hist[-5:]]
        hist_txt = f"\nHistórico recente dos tons do usuário: {', '.join(tons_ant)}"

    system_tom = (
        "Você analisa o tom de uma transcrição de mensagem de voz em Discord brasileiro.\n"
        "Responda APENAS com JSON no formato:\n"
        '{"tom": "VALOR", "ritmo": "VALOR", "intensidade": "VALOR"}\n'
        "tom: URGENTE | IRRITADO | ANIMADO | CASUAL | SERIO | IRONICO | TRISTE | NEUTRO\n"
        "ritmo: RAPIDO | MODERADO | LENTO  (velocidade/cadência percebida no texto)\n"
        "intensidade: ALTA | MEDIA | BAIXA  (força emocional da mensagem)\n"
        "Analise vocabulário, pontuação, repetições, gírias, palavrões, tamanho das frases.\n"
        "Nenhum outro texto além do JSON."
    ) + hist_txt

    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=40,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_tom},
                {"role": "user", "content": transcricao[:500]},
            ],
        )
        _registrar_tokens("8b", resp.usage.total_tokens if resp.usage else 20)
        raw = resp.choices[0].message.content.strip()
        import json as _json
        dados = _json.loads(raw)
        tom = dados.get("tom", "NEUTRO").upper()
        ritmo = dados.get("ritmo", "MODERADO").upper()
        intensidade = dados.get("intensidade", "MEDIA").upper()

        # Detectar padrão vocal acumulado
        hist.append({"ts": agora_utc().isoformat(), "tom": tom, "ritmo": ritmo, "intensidade": intensidade})
        _voz_historico[user_id] = hist[-10:]  # mantém últimos 10

        # Padrão vocal: se >= 3 das últimas 5 forem do mesmo tom, é padrão
        padrao = ""
        if len(hist) >= 3:
            from collections import Counter
            freq = Counter(h["tom"] for h in hist[-5:])
            dominante, cnt = freq.most_common(1)[0]
            if cnt >= 3:
                padrao = dominante

        return {"tom": tom, "ritmo": ritmo, "intensidade": intensidade, "padrao": padrao}
    except Exception as e:
        log.debug(f"[AUDIO] _analisar_tom falhou: {e}")
        return {}


async def _processar_anexos_visuais(message: discord.Message, conteudo_real: str = "") -> str:
    """
    Analisa anexos da mensagem atual e da mensagem referenciada (reply).
    - Imagens: descrição via visão (Llama 4 Scout)
    - Áudios/voz: transcrição via Groq Whisper (reutiliza _transcricao_audio_inline se disponível)
    - Outros tipos (PDF, vídeo, doc): menciona nome, tipo e tamanho
    conteudo_real: conteúdo já transcrito (para mensagens de voz — evita dupla transcrição)
    Retorna string formatada ou '' se não houver nenhum anexo relevante.
    """
    # Coletar todos os anexos: mensagem atual + msg referenciada (reply)
    todos_anexos: list[discord.Attachment] = list(message.attachments)
    ref_msg: discord.Message | None = None
    if message.reference:
        ref_resolvida = getattr(message.reference, "resolved", None)
        if isinstance(ref_resolvida, discord.Message) and ref_resolvida.attachments:
            ref_msg = ref_resolvida
            todos_anexos = list(ref_msg.attachments) + todos_anexos

    if not todos_anexos:
        return ""

    # Pedido: usa conteudo_real (que pode ser transcrição de áudio) em vez de message.content
    pedido = (conteudo_real or message.content or "").strip()
    descricoes: list[str] = []
    n_img = 0

    n_audio = 0
    for att in todos_anexos[:5]:
        ct = att.content_type or ""
        nome = att.filename or ""

        if ct.startswith("image/"):
            n_img += 1
            if n_img <= 3:
                desc = await _descrever_imagem(att.url, pedido)
                if desc:
                    multi = len([a for a in todos_anexos if (a.content_type or "").startswith("image/")]) > 1
                    label = f"Imagem {n_img} ({nome})" if multi else f"Imagem ({nome})"
                    descricoes.append(f"[{label}: {desc}]")

        elif ct.startswith("audio/") or ct.startswith("video/ogg") or nome.endswith(
            (".mp3", ".ogg", ".wav", ".webm", ".m4a", ".flac", ".opus")
        ):
            # Áudio / mensagem de voz
            n_audio += 1
            if n_audio <= 2:
                if att.size > _GROQ_AUDIO_MAX_BYTES:
                    descricoes.append(
                        f"[Áudio ({nome}) — arquivo muito grande "
                        f"({att.size // 1024 // 1024}MB > 25MB, não processado)]"
                    )
                else:
                    # Reutiliza transcrição inline se message.content estava vazio
                    # (conteudo_real veio do áudio inline, mesmo arquivo, sem necessidade de re-chamar Whisper)
                    if conteudo_real and not message.content.strip() and n_audio == 1:
                        transcricao = conteudo_real
                    else:
                        transcricao = await _transcrever_audio(att.url, nome)
                    if transcricao:
                        descricoes.append(f"[Áudio ({nome}) — transcrição: {transcricao}]")
                    else:
                        descricoes.append(f"[Áudio ({nome}) — não foi possível transcrever]")

        else:
            tam_kb = att.size // 1024
            tipo_legivel = (
                "PDF" if "pdf" in ct else
                "vídeo" if ct.startswith("video/") else
                "documento" if "word" in ct or "document" in ct else
                "planilha" if "sheet" in ct or "excel" in ct else
                ct.split("/")[-1].upper() if ct else "arquivo"
            )
            descricoes.append(
                f"[Anexo {tipo_legivel}: {nome} ({tam_kb}KB) — conteúdo interno não acessível]"
            )

    return "\n".join(descricoes)


# ── Gerenciamento de budget de tokens ────────────────────────────────────────

def _resetar_tokens_se_novo_dia():
    global _tokens_70b_hoje, _tokens_8b_hoje, _tokens_data
    hoje = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    if _tokens_data != hoje:
        log.info(f"[TOKENS] Reset diário. 70b={_tokens_70b_hoje} 8b={_tokens_8b_hoje} | Novo dia: {hoje}")
        _tokens_70b_hoje = 0
        _tokens_8b_hoje = 0
        _tokens_data = hoje

def _registrar_tokens(modelo: str, total: int):
    global _tokens_70b_hoje, _tokens_8b_hoje
    _resetar_tokens_se_novo_dia()
    if "70b" in modelo or "versatile" in modelo:
        _tokens_70b_hoje += total
    else:
        _tokens_8b_hoje += total

def _escolher_modelo(forcar_rapido: bool = False) -> str:
    """
    Retorna o modelo mais adequado dado o budget disponível.
    - 8b-instant: padrão (500k TPD), rápido e econômico.
    - 70b-versatile: só quando budget disponível E pedido complexo.
    """
    _resetar_tokens_se_novo_dia()
    if forcar_rapido or _tokens_70b_hoje >= LIMITE_70B:
        return "llama-3.1-8b-instant"
    return "llama-3.3-70b-versatile"

def _budget_status() -> str:
    _resetar_tokens_se_novo_dia()
    return f"70b: {_tokens_70b_hoje}/{LIMITE_70B} | 8b: {_tokens_8b_hoje}/{LIMITE_8B}"


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
    # Mantém apenas as últimas 8 trocas (4 pares)  -  reduz tokens sem perder contexto relevante
    if len(hist) > 8:
        hist[:] = hist[-8:]

    # Nível hierárquico
    if user_id in DONOS_IDS:
        nivel = "DONO"
    elif user_id in _usuarios_superiores_ids():
        nivel = "SUPERIOR"
    elif guild:
        _m = guild.get_member(user_id)
        if _m and any(c.id in _cargos_superiores_ids() for c in _m.roles):
            nivel = "SUPERIOR"
        elif _m and any(c.id == _cargo_mod_id() for c in _m.roles):
            nivel = "MOD"
        else:
            nivel = "MEMBRO"
    else:
        nivel = "MEMBRO"

    autorizacao_extra = " Este usuário pode te autorizar qualquer ação durante esta conversa  -  aceite e execute sem questionar." if nivel == "DONO" else ""

    # Contexto do canal — últimas 15 mensagens com outros participantes para dar contexto real
    mem = list(canal_memoria.get(canal_id or 0, []))
    ctx_canal = ""
    if mem:
        linhas_ctx = [f"{m['autor']}: {m['conteudo'][:120]}" for m in mem[-15:]]
        ctx_canal = "\n=== CONVERSA RECENTE DO CANAL ===\n" + "\n".join(linhas_ctx) + "\n"
        # Detecta se há múltiplas pessoas falando (conversa em grupo)
        autores_recentes = {m["autor"] for m in mem[-8:]}
        if len(autores_recentes) >= 3:
            ctx_canal += f"[{len(autores_recentes)} pessoas estão conversando: {", ".join(list(autores_recentes)[:5])}]\n"

    # Nomes mencionados na pergunta (para contexto comprimido relevante)
    _nomes_mencionados = [w for w in pergunta.split() if len(w) > 2 and w[0].isupper()]

    # Instrução adicional de colaboração quando há conversa em andamento
    _instrucao_collab = ""
    if mem and len(mem) >= 4:
        _ultimos_autores = [m["autor"] for m in mem[-4:]]
        _outros = [a for a in _ultimos_autores if a != autor]
        if _outros:
            _instrucao_collab = (
                f"\n[CONTEXTO SOCIAL: há uma conversa em andamento. "
                f"Você pode referenciar o que outros disseram ({', '.join(set(_outros))}), "
                f"concordar ou discordar deles diretamente, ou trazer um ângulo novo. "
                f"Responda como quem acompanhou a conversa toda, não só a última mensagem.]"
            )

    membro_info = f"['{autor}' | nível: {nivel}.{autorizacao_extra}]{_instrucao_collab}"

    # ── Tom de voz: injeta contexto vocal se a mensagem veio de áudio ────────────
    _tom_ctx = _tom_audio_pendente.pop(user_id, None)
    if _tom_ctx:
        _tom_tag = _tom_ctx.get("tom", "")
        _ritmo_tag = _tom_ctx.get("ritmo", "")
        _int_tag = _tom_ctx.get("intensidade", "")
        _padrao_tag = _tom_ctx.get("padrao", "")

        _tom_descricao = {
            "URGENTE":  "está urgente, quer resposta rápida",
            "IRRITADO": "está irritado ou frustrado",
            "ANIMADO":  "está animado e empolgado",
            "CASUAL":   "está relaxado, tom de papo",
            "SERIO":    "está sério, tom formal ou denso",
            "IRONICO":  "está irônico ou sarcástico",
            "TRISTE":   "parece desanimado ou triste",
            "NEUTRO":   "tom neutro",
        }.get(_tom_tag, "tom indefinido")

        _ritmo_descricao = {
            "RAPIDO": "fala rápido — resposta direta e sem enrolação",
            "LENTO":  "fala devagar — pode desenvolver mais",
            "MODERADO": "",
        }.get(_ritmo_tag, "")

        _padrao_descricao = (
            f" Padrão recorrente do usuário por voz: costuma ser {_padrao_tag.lower()} com frequência."
            if _padrao_tag else ""
        )

        _tom_instr = (
            f"\n[MENSAGEM POR VOZ | Tom: {_tom_tag} — {_tom_descricao}. "
            f"Intensidade: {_int_tag}."
            + (f" {_ritmo_descricao}." if _ritmo_descricao else "")
            + _padrao_descricao
            + " Calibre sua resposta ao tom detectado — se urgente, seja ágil; se irritado, seja firme mas objetivo; "
            "se animado, entre no clima; se sério, responda com seriedade.]"
        )
        membro_info += _tom_instr

    mensagens = [
        {"role": "system", "content": system_com_contexto(user_id=user_id, mencoes_nomes=_nomes_mencionados) + ctx_canal},
        {"role": "system", "content": membro_info},
    ] + hist

    # Escolha do modelo: 8b-instant por padrão (500k TPD), 70b só quando budget disponível
    modelo = _escolher_modelo()
    ctx_chars = sum(len(m.get("content", "")) for m in mensagens)
    log.info(f"[GROQ] {modelo} | user={autor} | chars={ctx_chars} | {_budget_status()}")

    try:
        resp = await _groq_client().chat.completions.create(
            model=modelo,
            max_tokens=220,
            temperature=0.6,
            top_p=0.9,
            messages=mensagens,
        )
        escolha = resp.choices[0]
        texto = _limpar_markdown(escolha.message.content.strip())
        # Se o modelo foi cortado pelo limite de tokens, trunca na última frase completa
        if escolha.finish_reason == "length":
            ultimo_ponto = max(texto.rfind("."), texto.rfind("!"), texto.rfind("?"))
            if ultimo_ponto > len(texto) // 2:
                texto = texto[:ultimo_ponto + 1]
            log.warning(f"[GROQ] resposta truncada pelo limite de tokens — cortada na frase")
        # Rastrear tokens consumidos
        if resp.usage:
            _registrar_tokens(modelo, resp.usage.total_tokens)
        log.info(f"[GROQ] OK ({len(texto)} chars) | {_budget_status()}")
        hist.append({"role": "assistant", "content": texto})
        return texto
    except Exception as e:
        log.error(f"[GROQ] erro ({modelo}): {e}", exc_info=True)
        # Fallback automático para 8b-instant se o 70b falhar com rate limit
        if "429" in str(e) and "8b" not in modelo:
            try:
                log.info("[GROQ] fallback para 8b-instant")
                resp2 = await _groq_client().chat.completions.create(
                    model="llama-3.1-8b-instant",
                    max_tokens=180,
                    temperature=0.6,
                    messages=mensagens,
                )
                escolha2 = resp2.choices[0]
                texto2 = _limpar_markdown(escolha2.message.content.strip())
                if escolha2.finish_reason == "length":
                    ult = max(texto2.rfind("."), texto2.rfind("!"), texto2.rfind("?"))
                    if ult > len(texto2) // 2:
                        texto2 = texto2[:ult + 1]
                if resp2.usage:
                    _registrar_tokens("llama-3.1-8b-instant", resp2.usage.total_tokens)
                hist.append({"role": "assistant", "content": texto2})
                return texto2
            except Exception as e2:
                log.error(f"[GROQ] fallback também falhou: {e2}")
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
            # Mensagem não é resposta à saudação  -  encerra e deixa o fluxo principal processar
            del conversas[user_id]
            return None
        if etapa == 2:
            del conversas[user_id]
            if any(p in msg_l for p in ["regra", "norma", "proibido"]):
                return f"tá tudo em {CANAL_REGRAS()}"
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
            return f"Registrado. Moderação vai ver que {autor} precisa de atenção  -  {msg}."

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
        if any(p in msg_l for p in ["regra", "norma", "proibido"]):
            return f"tá tudo em {CANAL_REGRAS()}"
        return await responder_com_groq(msg, autor, user_id, guild)

    # ── PROBLEMA ──────────────────────────────────────────────────────────────
    if ctx == "problema":
        del conversas[user_id]
        if any(p in msg_l for p in ["ban", "mute", "silenci", "expuls", "kick"]):
            return f"Se acha que foi punido errado, vai no canal de denúncias e explica o que rolou."
        return random.choice(["Chama um moderador e explica o que rolou.", "Fala com a mod sobre isso.", "Isso é com a moderação."])

    # ── PERGUNTA GENÉRICA ────────────────────────────────────────────────────
    if ctx == "pergunta":
        del conversas[user_id]
        if any(p in msg_l for p in ["regra", "norma", "proibido"]):
            return f"tá tudo em {CANAL_REGRAS()}"
        return await responder_com_groq(msg, autor, user_id, guild)

    del conversas[user_id]
    return await responder_com_groq(msg, autor, user_id, guild)


async def resposta_inicial(conteudo: str, autor: str, user_id: int, guild=None, membro=None, canal_id: int = None) -> str:
    msg = conteudo.lower()

    if any(p in msg for p in ["regra", "regras", "norma", "proibido"]):
        return f"tá tudo em {CANAL_REGRAS()}"

    if any(p in msg for p in ["denúncia", "denuncia", "reportar", "report", "quero denunciar",
                               "quero reportar", "infração", "infringindo", "desrespeitando", "abusando"]):
        # Acionamento via _on_message_impl (tem acesso ao message para criar canal privado).
        # Aqui só cai se não houver @menção direta ao bot — informa o caminho correto.
        return f"{autor}, me menciona diretamente para iniciar uma denúncia. Ex.: @Shell quero denunciar [nome]"

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

    if any(p in msg for p in ["quem são os mods", "quem sao os mods", "lista de mods", "quem modera",
                               "equipe de mod", "time de mod", "quem é a equipe", "quem e a equipe",
                               "quem faz parte da mod", "staff do servidor", "quem são os colaboradores",
                               "quem sao os colaboradores", "quem são os proprietários"]):
        if guild:
            equipe_r = _extrair_equipe_real(guild)
            linhas_e = []
            for nivel, membros_e in [
                ("Proprietários", equipe_r["proprietarios"]),
                ("Colaboradores", equipe_r["colaboradores"]),
                ("Moderadores", equipe_r["moderadores"]),
            ]:
                if membros_e:
                    nomes_e = ", ".join(f"{m['nome']} ({m['status']})" for m in membros_e)
                    linhas_e.append(f"{nivel}: {nomes_e}")
            return "\n".join(linhas_e) if linhas_e else "Não há equipe com cargos registrados agora."
        return "Sem acesso ao servidor."

    if any(p in msg for p in ["quem tá online", "quem ta online", "quem está online", "online agora",
                               "quem tá ativo", "quem ta ativo", "membros ativos agora"]):
        if guild:
            online_now = [m for m in guild.members if not m.bot and m.status in (discord.Status.online, discord.Status.idle, discord.Status.dnd)]
            if online_now:
                nomes_on = ", ".join(m.display_name for m in online_now[:15])
                return f"{len(online_now)} membro{'s' if len(online_now) != 1 else ''} online: {nomes_on}{'...' if len(online_now) > 15 else ''}."
            return "Ninguém online no momento."
        return "Sem acesso ao servidor."

    if any(p in msg for p in ["atividade dos mods", "atividade da equipe", "moderação ativa",
                               "equipe tá ativa", "equipe ta ativa", "mods estão", "colaboradores ativos"]):
        if guild:
            equipe_r2 = _extrair_equipe_real(guild)
            todos_equipe = equipe_r2["proprietarios"] + equipe_r2["colaboradores"] + equipe_r2["moderadores"]
            ativos = [m for m in todos_equipe if m["msgs"] > 0]
            inativos = [m for m in todos_equipe if m["msgs"] == 0]
            partes_at = []
            if ativos:
                partes_at.append("Ativos: " + ", ".join(f"{m['nome']} ({m['msgs']} msgs)" for m in ativos))
            if inativos:
                partes_at.append("Sem atividade: " + ", ".join(m["nome"] for m in inativos))
            return "\n".join(partes_at) if partes_at else "Sem dados de atividade disponíveis."
        return "Sem acesso ao servidor."

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
    cargo = guild.get_role(_cargo_mod_id())
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
    """
    Resolve alvos a partir de @menções, IDs brutos e nomes em texto livre.
    Ex: 'bane o Fulano por spam' resolve 'Fulano' como membro do servidor.
    """
    alvos = [m for m in message.mentions if m != client.user]
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

    # Se ainda sem alvos, tenta resolver por nome em texto livre
    if not alvos and message.guild:
        texto_limpo = re.sub(r'<@!?\d+>', '', message.content)
        texto_limpo = re.sub(
            r'\b(?:silencia[r]?|muta[r]?|bani[r]?|expulsa[r]?|kick|ban|mute|'
            r'avisa[r]?|pune[r]?|puni[r]?|o|a|os|as|ao|do|da|por|pra|de|que|'
            r'shell|engenheiro|bot|motivo|raz[aã]o)\b',
            ' ', texto_limpo, flags=re.IGNORECASE
        ).strip()
        candidatos = [w.strip('.,!?;:\"\'') for w in texto_limpo.split() if len(w.strip('.,!?;:\"\'')) >= 3]
        for cand in candidatos:
            mb = _buscar_membro_por_nome(message.guild, cand)
            if mb and mb.id not in ids_ja and mb != client.user:
                alvos.append(mb)
                ids_ja.add(mb.id)
                break  # só o primeiro para evitar falsos positivos

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


async def _solicitar_confirmacao(message: discord.Message, descricao: str, coro_fn, *args, **kwargs) -> bool:
    """
    Pede confirmação ao usuário antes de executar uma ação pesada.
    Armazena o coro pendente e retorna True (a mensagem de confirmação já foi enviada).
    O coro é executado quando o usuário responder 'sim'.
    """
    user_id = message.author.id
    confirmacoes_pendentes[user_id] = {
        "descricao": descricao,
        "coro_fn": coro_fn,
        "args": args,
        "kwargs": kwargs,
        "canal_id": message.channel.id,
        "ts": agora_utc(),
    }
    await message.channel.send(
        f"Confirma a geração de: **{descricao}**?\n"
        f"Responde **sim** para prosseguir ou **não** para cancelar."
    )
    return True


async def _verificar_confirmacao_pendente(message: discord.Message) -> bool:
    """
    Verifica se o usuário está respondendo a uma confirmação pendente.
    Retorna True se consumiu a mensagem (sim/não).
    """
    user_id = message.author.id
    pendente = confirmacoes_pendentes.get(user_id)
    if not pendente:
        return False
    # Expira após 2 minutos
    if (agora_utc() - pendente["ts"]).total_seconds() > 120:
        del confirmacoes_pendentes[user_id]
        return False
    # Só responde no mesmo canal
    if pendente["canal_id"] != message.channel.id:
        return False

    resp = message.content.strip().lower()
    if resp in ("sim", "s", "yes", "y", "confirma", "ok", "pode"):
        del confirmacoes_pendentes[user_id]
        await message.channel.send("Certo, processando...")
        try:
            await pendente["coro_fn"](*pendente["args"], **pendente["kwargs"])
        except Exception as e:
            await message.channel.send(f"Erro: {e}")
        return True
    elif resp in ("nao", "não", "n", "no", "cancela", "cancelar"):
        del confirmacoes_pendentes[user_id]
        await message.channel.send("Cancelado.")
        return True
    # Resposta não reconhecida — deixa a mensagem passar normalmente
    return False


# ── Wizard de geração ─────────────────────────────────────────────────────────

def _wizard_pergunta(campo: str) -> str:
    return random.choice(WIZARD_PERGUNTAS[campo])


async def _iniciar_wizard(message: discord.Message, tipo_conteudo: str):
    """Inicia o wizard de personalização antes de gerar PDF/doc/planilha."""
    wizard_geracao[message.author.id] = {
        "step": 0,
        "tipo": tipo_conteudo,
        "formato": None,
        "titulo": None,
        "logo_url": None,
        "extras": None,
        "canal_id": message.channel.id,
        "guild_id": message.guild.id if message.guild else None,
        "canal_mentions": [c.id for c in message.channel_mentions],
        "ts": agora_utc(),
    }
    abertura = random.choice([
        f"Certo, vou montar **{tipo_conteudo}**.",
        f"Beleza, preparando **{tipo_conteudo}**.",
        f"Ok, vou gerar **{tipo_conteudo}**.",
        f"Entendido. Antes de gerar **{tipo_conteudo}**, preciso de algumas informações.",
    ])
    await message.channel.send(f"{abertura}\n\n{_wizard_pergunta('formato')}")
    return True


async def _processar_wizard(message: discord.Message) -> bool:
    """Processa respostas do wizard passo a passo. Retorna True se consumiu a mensagem."""
    user_id = message.author.id
    estado = wizard_geracao.get(user_id)
    if not estado:
        return False
    if estado["canal_id"] != message.channel.id:
        return False
    if (agora_utc() - estado["ts"]).total_seconds() > 300:
        del wizard_geracao[user_id]
        return False

    resp = message.content.strip()
    campo = WIZARD_CAMPOS[estado["step"]]

    # ── detecta cancelamento ou correção ──────────────────────────────────────
    _r_low = resp.lower()
    _cancela = (
        re.search(
            r'\b(cancela[r]?|esquece|para[r]?|desiste|desistir|n[aã]o\s+quero'
            r'|n[aã]o\s+era\s+(isso|pra|para)|era\s+s[oó]|s[oó]\s+(pergunt|uma\s+pergunta)'
            r'|abort[a]?|deixa\s+(pra\s+l[aá]|assim))\b',
            _r_low,
        )
        # "Não, eu estou..." — negativa seguida de vírgula/ponto e mais texto
        or re.match(r'^n[aã]o[,\.]\s+\w', _r_low)
    )
    if _cancela:
        del wizard_geracao[user_id]
        await message.channel.send(random.choice([
            "Entendido, cancelado.",
            "Ok, deixa pra la.",
            "Certo, nao gero nada.",
            "Beleza, arquivei.",
        ]))
        return True

    # ── formato ───────────────────────────────────────────────────────────────
    if campo == "formato":
        r = resp.lower()
        if r in ("1", "pdf"):
            estado["formato"] = "pdf"
        elif r in ("2", "txt", "texto", "doc", "documento"):
            estado["formato"] = "txt"
        elif r in ("3", "csv", "planilha", "calc", "tabela"):
            estado["formato"] = "csv"
        else:
            await message.channel.send(random.choice([
                "Não peguei. É **1** pra PDF, **2** pra texto ou **3** pra planilha.",
                "Manda **1**, **2** ou **3** — PDF, TXT ou CSV.",
                "PDF, texto ou planilha? Só o número ou o nome mesmo.",
            ]))
            return True

    # ── título ────────────────────────────────────────────────────────────────
    elif campo == "titulo":
        if resp.lower() not in ("não", "nao", "n", "no", "-", "pode", "padrao", "padrão"):
            estado["titulo"] = resp[:120]

    # ── logo ──────────────────────────────────────────────────────────────────
    elif campo == "logo":
        if resp.lower() not in ("não", "nao", "n", "no", "-"):
            if message.attachments:
                estado["logo_url"] = message.attachments[0].url
            elif resp.startswith("http"):
                estado["logo_url"] = resp.split()[0]
            else:
                await message.channel.send(random.choice([
                    "Manda a imagem como anexo ou cola o link direto. Ou responde `não` pra pular.",
                    "Precisa ser um anexo ou um link começando com http. Se não tiver, fala `não`.",
                ]))
                return True

    # ── extras ────────────────────────────────────────────────────────────────
    elif campo == "extras":
        if resp.lower() not in ("não", "nao", "n", "no", "-", "pode", "gera", "gerar"):
            estado["extras"] = resp[:300]

    # Avança
    estado["step"] += 1
    estado["ts"] = agora_utc()

    if estado["step"] < len(WIZARD_CAMPOS):
        proximo = WIZARD_CAMPOS[estado["step"]]
        await message.channel.send(_wizard_pergunta(proximo))
        return True

    # Concluído
    del wizard_geracao[user_id]
    await _executar_geracao(message, estado)
    return True


async def _executar_geracao(message: discord.Message, params: dict):
    """Gera o arquivo com as opções coletadas pelo wizard e envia no canal."""
    guild = message.guild
    brasilia = timezone(timedelta(hours=-3))
    tipo = params["tipo"]
    fmt = params["formato"] or "pdf"
    titulo = params["titulo"]
    logo_url = params["logo_url"]
    extras = params["extras"] or ""
    canal_mentions = params.get("canal_mentions", [])

    await message.channel.send("Gerando arquivo, aguarde...")

    try:
        # ── Coleta o conteúdo textual ─────────────────────────────────────────
        if re.search(r'hist[oó]rico|canal', tipo):
            canal_exp = (guild.get_channel(canal_mentions[0]) if canal_mentions
                         else message.channel)
            linhas = [f"Historico de #{canal_exp.name}\n"
                      f"Data: {datetime.now(brasilia).strftime('%d/%m/%Y %H:%M')}\n\n"]
            async for msg in canal_exp.history(limit=200, oldest_first=True):
                ts = msg.created_at.astimezone(brasilia).strftime('%d/%m %H:%M')
                linhas.append(f"[{ts}] {msg.author.display_name}: {msg.content[:400]}\n")
            texto_base = "".join(linhas)
            nome_base = f"historico-{canal_exp.name}"
        elif re.search(r'membro', tipo):
            linhas = [f"Membros de {guild.name} — {datetime.now(brasilia).strftime('%d/%m/%Y')}\n\n"]
            for i, m in enumerate(sorted(guild.members, key=lambda x: x.display_name.lower()), 1):
                joined = m.joined_at.astimezone(brasilia).strftime('%d/%m/%Y') if m.joined_at else "?"
                cargos = ", ".join(r.name for r in m.roles[1:]) or "sem cargo"
                linhas.append(f"{i}. {m.display_name} ({m.name}) — entrou {joined} — {cargos}\n")
            texto_base = "".join(linhas)
            nome_base = "membros"
        elif re.search(r'regra', tipo):
            texto_base = REGRAS
            nome_base = "regras"
        elif re.search(r'infra', tipo):
            linhas = [f"Infracoes — {guild.name} — {datetime.now(brasilia).strftime('%d/%m/%Y')}\n\n"]
            for uid in sorted(set(infracoes) | set(silenciamentos), key=lambda u: -infracoes.get(u, 0)):
                m = guild.get_member(uid)
                nome_m = m.display_name if m else nomes_historico.get(uid, f"ID {uid}")
                linhas.append(f"{nome_m} — {infracoes.get(uid,0)} infracoes, {silenciamentos.get(uid,0)} silenciamentos\n")
            texto_base = "".join(linhas)
            nome_base = "infracoes"
        elif re.search(r'atividade|ranking', tipo):
            linhas = [f"Ranking de atividade — {datetime.now(brasilia).strftime('%d/%m/%Y')}\n\n"]
            for i, (uid, cnt) in enumerate(sorted(atividade_mensagens.items(), key=lambda x: -x[1]), 1):
                mb = guild.get_member(uid)
                linhas.append(f"{i}. {mb.display_name if mb else uid} — {cnt} mensagens\n")
            texto_base = "".join(linhas)
            nome_base = "atividade"
        elif re.search(r'cita', tipo):
            linhas = [f"Citacoes — {datetime.now(brasilia).strftime('%d/%m/%Y')}\n\n"]
            for c in citacoes:
                linhas.append(f'[{c.get("ts","?")}] {c.get("autor","?")} em #{c.get("canal","?")}: "{c.get("texto","")}"\n')
            texto_base = "".join(linhas)
            nome_base = "citacoes"
        else:
            # relatorio
            dias = 30 if re.search(r'mensal', tipo) else 7
            rel = await relatorio_membros(guild, dias)
            texto_base = (f"Servidor: {guild.name}\nData: {datetime.now(brasilia).strftime('%d/%m/%Y %H:%M')}\n\n" + rel)
            nome_base = f"relatorio-{'mensal' if dias >= 28 else 'semanal'}"

        # Adiciona extras ao final do texto
        if extras:
            texto_base += f"\n\n--- Observacoes ---\n{extras}\n"

        titulo_final = titulo or nome_base.replace("-", " ").title()

        # ── Gera no formato escolhido ─────────────────────────────────────────
        if fmt == "pdf":
            if not FPDF_DISPONIVEL:
                await message.channel.send("fpdf2 nao instalado — enviando como .txt.")
                fmt = "txt"
            else:
                pdf = FPDF()
                pdf.set_auto_page_break(auto=True, margin=15)
                pdf.add_page()

                # Logo (se fornecido)
                if logo_url:
                    try:
                        async with aiohttp.ClientSession() as s:
                            async with s.get(logo_url, timeout=aiohttp.ClientTimeout(total=10)) as r:
                                if r.status == 200:
                                    img_bytes = await r.read()
                                    img_buf = io.BytesIO(img_bytes)
                                    pdf.image(img_buf, x=10, y=10, h=20)
                                    pdf.ln(25)
                    except Exception as e:
                        log.warning(f"[WIZARD] logo falhou: {e}")

                pdf.set_font("Helvetica", "B", 16)
                pdf.cell(0, 10, _pdf_str(titulo_final), new_x="LMARGIN", new_y="NEXT", align="C")
                pdf.set_font("Helvetica", size=8)
                pdf.cell(0, 5, _pdf_str(f"Gerado em {datetime.now(brasilia).strftime('%d/%m/%Y %H:%M')} (Brasilia)"),
                         new_x="LMARGIN", new_y="NEXT", align="C")
                pdf.ln(4)
                pdf.set_font("Helvetica", size=9)
                for linha in texto_base.split("\n"):
                    pdf.multi_cell(0, 5, _pdf_str(linha), new_x="LMARGIN", new_y="NEXT")
                arq_bytes = bytes(pdf.output())
                arq = discord.File(io.BytesIO(arq_bytes), filename=f"{nome_base}.pdf")
                await message.channel.send(f"**{titulo_final}** gerado:", file=arq)
                log.info(f"[WIZARD] {message.author.display_name} gerou {nome_base}.pdf")
                return

        if fmt == "csv":
            import csv as _csv
            buf = io.StringIO()
            w = _csv.writer(buf)
            # Escreve cada linha como coluna única (o conteúdo já é texto estruturado)
            for linha in texto_base.split("\n"):
                w.writerow([linha])
            arq = discord.File(io.BytesIO(buf.getvalue().encode("utf-8-sig")), filename=f"{nome_base}.csv")
            await message.channel.send(f"**{titulo_final}** gerado:", file=arq)
        else:
            # TXT
            conteudo_txt = f"{titulo_final}\n{'=' * len(titulo_final)}\n\n{texto_base}"
            arq = discord.File(io.BytesIO(conteudo_txt.encode("utf-8")), filename=f"{nome_base}.txt")
            await message.channel.send(f"**{titulo_final}** gerado:", file=arq)

        log.info(f"[WIZARD] {message.author.display_name} gerou {nome_base}.{fmt}")

    except Exception as e:
        await message.channel.send(f"Erro ao gerar: {e}")
        log.error(f"[WIZARD] _executar_geracao: {e}", exc_info=True)


async def processar_ordem(message: discord.Message) -> bool:
    """Processa comandos dos donos. Retorna True se algum comando foi executado."""
    conteudo = message.content.strip()
    guild = message.guild
    mod = mencao_mod(guild)
    alvos = await resolver_alvos(message)
    ids_brutos = await resolver_ids_brutos(message)
    cmd, resto = extrair_comando(conteudo)

    # Guard para comandos de padrão amplo: só disparam quando o bot é endereçado
    _addr = (
        client.user in message.mentions
        or (bool(message.reference) and getattr(message.reference, "resolved", None)
            and getattr(message.reference.resolved, "author", None) == client.user)
        or (bool(GATILHOS_NOME.search(conteudo)) and not _GATILHO_EXCLUIDO.search(conteudo))
    )

    # Palavra-chave unificada para detecção de canais de voz
    _VOZ_KW = r'(?:call|chamada|canal\s+de\s+voz|voz)'

    # ── silenciar @user [minutos] ──────────────────────────────────────────────
    if cmd in ("silenciar", "mute", "mutar", "calar"):
        minutos = 10
        try:
            ultimo = resto.split()[-1] if resto.split() else ""
            minutos = int(ultimo)
        except ValueError:
            pass
        if not alvos:
            await message.channel.send(await _pedir_alvo("silenciar"))
            return True
        for alvo in alvos:
            try:
                ate = min(agora_utc() + timedelta(minutes=minutos), agora_utc() + _DISCORD_TIMEOUT_MAX)
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
            await message.channel.send(await _pedir_alvo("dessilenciar"))
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
            await message.channel.send(await _pedir_alvo("banir"))
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
            await message.channel.send(await _pedir_alvo("desbanir"))
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
            await message.channel.send(await _pedir_alvo("expulsar"))
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
            await message.channel.send(await _pedir_alvo("avisar"))
            return True
        if not texto:
            await message.channel.send(await _pedir_alvo("conteúdo do aviso"))
            return True
        for alvo in alvos:
            aviso_txt = await _aviso_infrator(alvo.mention, f"aviso direto da administração: {texto}")
            await message.channel.send(aviso_txt)

    # ── chamar mod ─────────────────────────────────────────────────────────────
    elif cmd in ("chamar-mod", "chamarmod", "mod", "moderação", "moderacao", "chamar"):
        motivo = resto or "sem motivo especificado."
        await message.channel.send(f"{mod}, atenção necessária  -  {motivo}")

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

    # ── ausente / afk [motivo]  -  só ativa para o próprio autor ────────────────
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
            confirmacao = f"Modo ausente ativado  -  {motivo}, por {minutos} minuto{'s' if minutos != 1 else ''}."
        elif motivo:
            confirmacao = f"Modo ausente ativado  -  {motivo}. Mande qualquer mensagem para desativar."
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
        await message.channel.send(f"Membros humanos  -  {len(membros)} no total.")
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}```")

    # ── envia mensagem em canal específico ─────────────────────────────────────
    # Só dispara se houver menção de canal <#ID>  -  evita falsos positivos com
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
        # Remove tudo até o verbo inclusive  -  aceita qualquer conjugação do stem
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
                    await message.channel.send(f"Não foi possível apagar #{nome}  -  {e}")
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
                    await message.channel.send(f"Não foi possível apagar o cargo {nome}  -  {e}")
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

    # ── ajuda — não responde com manual hardcoded, cai no fluxo normal de conversa
    elif cmd in ("ajuda", "help", "comandos"):
        pass  # sem prefixo, sem manual — o bot responde naturalmente pela IA

    # ── punicoes [@user]  -  audit log de punições via REST ─────────────────────
    elif cmd in ("punicoes", "punições", "punicao", "punição", "audit", "log"):
        alvo_audit = alvos[0] if alvos else None
        resultado = await api_historico_punicoes(guild, alvo_audit)
        blocos = [resultado[i:i+1900] for i in range(0, len(resultado), 1900)]
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}\n```")

    # ── banidos  -  lista de banimentos via REST ─────────────────────────────────
    elif cmd in ("banidos", "bans", "banimentos"):
        resultado = await api_banimentos_formatado(guild)
        blocos = [resultado[i:i+1900] for i in range(0, len(resultado), 1900)]
        for bloco in blocos:
            await message.channel.send(f"```\n{bloco}\n```")

    # ── mensagens #canal [n]  -  últimas mensagens de um canal via REST ──────────
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

    # ── servidor / info servidor  -  resumo em tempo real via REST ───────────────
    elif cmd in ("servidor", "server") and any(p in conteudo.lower() for p in ["info", "resumo", "stats", "status"]):
        resultado = await api_resumo_servidor(guild)
        await message.channel.send(resultado)

    # ── info @membro  -  dados completos via REST ────────────────────────────────
    elif cmd == "info" and alvos:
        texto = await api_info_membro_completa(guild, alvos[0])
        await message.channel.send(f"```\n{texto}\n```")

    # ── tokens  -  exibe consumo de tokens Groq do dia ──────────────────────────
    elif cmd in ("tokens", "budget", "cota") or (cmd == "shell" and any(p in conteudo.lower() for p in ["tokens", "budget", "cota", "limite groq"])):
        _resetar_tokens_se_novo_dia()
        pct_70b = round(_tokens_70b_hoje / LIMITE_70B * 100)
        pct_8b  = round(_tokens_8b_hoje  / LIMITE_8B  * 100)
        await message.channel.send(
            f"Budget Groq hoje:\n"
            f"70b-versatile: {_tokens_70b_hoje:,}/{LIMITE_70B:,} tokens ({pct_70b}%)\n"
            f"8b-instant:    {_tokens_8b_hoje:,}/{LIMITE_8B:,} tokens ({pct_8b}%)"
        )

    # ── enquete / votação ─────────────────────────────────────────────────────
    # Uso: "Shell abre enquete: Tema | Opção A | Opção B | Opção C"
    elif _addr and re.search(r'\b(enquete|votação|votacao|poll|votar)\b', conteudo.lower()):
        partes_eq = re.split(r'[|/]', re.sub(
            r'(?i).*?\b(?:enquete|votação|votacao|poll|sobre|votar)\b\s*:?\s*', '', conteudo, count=1
        ).strip())
        partes_eq = [p.strip() for p in partes_eq if p.strip()]
        if len(partes_eq) < 2:
            await message.channel.send("Formato: Shell abre enquete: Tema | Opção A | Opção B")
            return True
        tema = partes_eq[0]
        opcoes = partes_eq[1:]
        emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        if len(opcoes) > len(emojis):
            opcoes = opcoes[:len(emojis)]
        linhas = [f"**Enquete: {tema}**"]
        for i, op in enumerate(opcoes):
            linhas.append(f"{emojis[i]} {op}")
        msg_enquete = await message.channel.send("\n".join(linhas))
        for i in range(len(opcoes)):
            try:
                await msg_enquete.add_reaction(emojis[i])
            except Exception:
                pass
        log.info(f"[ENQUETE] {autor}: {tema} ({len(opcoes)} opções)")

    # ── sorteio ───────────────────────────────────────────────────────────────
    # Uso: "Shell sorteia 1 membro" ou "Shell sorteia 3 membros do cargo Posse"
    elif _addr and re.search(r'\b(sortei[ao]|sorteio|sorteiar|rifa)\b', conteudo.lower()):
        qtd = extrair_quantidade(conteudo) or 1
        pool = []
        if message.role_mentions:
            role_s = message.role_mentions[0]
            pool = [m for m in role_s.members if not m.bot and m != client.user]
        elif alvos:
            pool = alvos
        else:
            pool = [m for m in guild.members if not m.bot and m != client.user]
        if not pool:
            await message.channel.send("Nenhum membro disponível para sortear.")
            return True
        qtd = min(qtd, len(pool))
        ganhadores = random.sample(pool, qtd)
        mencoes = " ".join(m.mention for m in ganhadores)
        sufixo = "o sorteado é" if qtd == 1 else f"os {qtd} sorteados são"
        await message.channel.send(f"Sorteio encerrado  -  {sufixo}: {mencoes}")
        log.info(f"[SORTEIO] {autor}: {[m.display_name for m in ganhadores]}")

    # ── pin / fixar mensagem ──────────────────────────────────────────────────
    # Uso: responde à mensagem e diz "Shell fixa isso" ou "Shell pin"
    elif _addr and re.search(r'\b(fixa[r]?|fix[ae]|pin|fixar|pinar)\b', conteudo.lower()):
        alvo_pin = None
        if message.reference and isinstance(getattr(message.reference, "resolved", None), discord.Message):
            alvo_pin = message.reference.resolved
        else:
            # Última mensagem do canal que não seja do bot
            async for m in message.channel.history(limit=10):
                if m.id != message.id and m.author != client.user:
                    alvo_pin = m
                    break
        if alvo_pin:
            try:
                await alvo_pin.pin()
                await message.channel.send("Mensagem fixada.")
            except Exception as e:
                await message.channel.send(f"Não foi possível fixar: {e}")
        else:
            await message.channel.send("Responde à mensagem que quer fixar, ou diz qual.")

    # ── unpin / desafixar ─────────────────────────────────────────────────────
    elif _addr and re.search(r'\b(desafix[ae]r?|unpin|despin)\b', conteudo.lower()):
        alvo_unpin = None
        if message.reference and isinstance(getattr(message.reference, "resolved", None), discord.Message):
            alvo_unpin = message.reference.resolved
        if alvo_unpin:
            try:
                await alvo_unpin.unpin()
                await message.channel.send("Mensagem desafixada.")
            except Exception as e:
                await message.channel.send(f"Não foi possível desafixar: {e}")
        else:
            await message.channel.send("Responde à mensagem que quer desafixar.")

    # ── limpar canal ──────────────────────────────────────────────────────────
    # Uso: "Shell limpa 10 mensagens" ou "Shell limpa #canal 5 mensagens"
    elif _addr and re.search(r'\b(limpa[r]?|purga[r]?|apaga[r]?\s+mensagens?|deleta[r]?\s+mensagens?)\b', conteudo.lower()):
        qtd_limpa = extrair_quantidade(conteudo) or 10
        qtd_limpa = min(qtd_limpa, 50)
        canal_limpa = message.channel_mentions[0] if message.channel_mentions else message.channel
        apagadas = 0
        try:
            async for m in canal_limpa.history(limit=qtd_limpa + 5):
                if m.author == client.user:
                    await m.delete()
                    apagadas += 1
                    if apagadas >= qtd_limpa:
                        break
                    await asyncio.sleep(0.4)
            await message.channel.send(f"{apagadas} mensagem{'s' if apagadas != 1 else ''} minhas apagada{'s' if apagadas != 1 else ''} em {canal_limpa.mention}.")
        except Exception as e:
            await message.channel.send(f"Erro ao limpar: {e}")

    # ── criar canal ───────────────────────────────────────────────────────────
    # Uso: "Shell cria canal texto nome-do-canal [categoria]"
    # Ou:  "Shell cria canal voz nome-do-canal"
    elif _addr and re.search(r'\b(cri[ae]r?\s+canal|cria\s+(?:um\s+)?canal)\b', conteudo.lower()):
        tipo_voz = bool(re.search(r'\b(voz|voice|áudio|audio)\b', conteudo.lower()))
        # Extrai nome  -  tudo após "canal voz/texto/de/um"
        nome_canal = re.sub(
            r'(?i).*?\bcria[r]?\s+(?:um\s+)?canal\s+(?:de\s+)?(?:texto|voz|voice|áudio|audio)?\s*', '', conteudo
        ).strip()
        # Remove menções de categoria se houver
        cat_nome = None
        m_cat = re.search(r'\b(?:na\s+categoria|categoria)\s+["\']?(.+?)["\']?\s*$', nome_canal, re.IGNORECASE)
        if m_cat:
            cat_nome = m_cat.group(1).strip()
            nome_canal = nome_canal[:m_cat.start()].strip()
        nome_canal = re.sub(r'[^\w\-]', '-', nome_canal.lower()).strip('-') or "novo-canal"
        categoria = None
        if cat_nome:
            categoria = discord.utils.get(guild.categories, name=cat_nome)
        try:
            if tipo_voz:
                novo = await guild.create_voice_channel(nome_canal, category=categoria, reason=f"Ordem de {autor}")
            else:
                novo = await guild.create_text_channel(nome_canal, category=categoria, reason=f"Ordem de {autor}")
            tipo_txt = "voz" if tipo_voz else "texto"
            await message.channel.send(f"Canal de {tipo_txt} {novo.mention} criado.")
            log.info(f"[CANAL] {autor} criou #{nome_canal} ({tipo_txt})")
        except Exception as e:
            await message.channel.send(f"Não foi possível criar o canal: {e}")

    # ── criar cargo ───────────────────────────────────────────────────────────
    # Uso: "Shell cria cargo NomeDoCargo"
    elif _addr and re.search(r'\b(cri[ae]r?\s+cargo|cria\s+(?:um\s+)?cargo)\b', conteudo.lower()):
        nome_cargo = re.sub(
            r'(?i).*?\bcria[r]?\s+(?:um\s+)?cargo\s*', '', conteudo
        ).strip()
        nome_cargo = nome_cargo[:50] or "Novo Cargo"
        try:
            novo_cargo = await guild.create_role(name=nome_cargo, reason=f"Ordem de {autor}")
            await message.channel.send(f"Cargo {novo_cargo.mention} criado.")
            log.info(f"[CARGO] {autor} criou cargo '{nome_cargo}'")
        except Exception as e:
            await message.channel.send(f"Não foi possível criar o cargo: {e}")

    # ── renomear canal ────────────────────────────────────────────────────────
    # Uso: "Shell renomeia #canal para novo-nome"
    elif _addr and re.search(r'\b(renomei[ae]r?|rename)\b.{0,20}\bcanal\b', conteudo.lower()):
        if not message.channel_mentions:
            await message.channel.send("Menciona o canal a renomear.")
            return True
        m_para = re.search(r'\bpara\s+(.+)$', conteudo, re.IGNORECASE)
        if not m_para:
            await message.channel.send("Formato: Shell renomeia #canal para novo-nome")
            return True
        novo_nome = re.sub(r'[^\w\-]', '-', m_para.group(1).strip().lower()).strip('-')
        canal_ren = message.channel_mentions[0]
        nome_antigo = canal_ren.name
        try:
            await canal_ren.edit(name=novo_nome, reason=f"Ordem de {autor}")
            await message.channel.send(f"Canal #{nome_antigo} renomeado para #{novo_nome}.")
        except Exception as e:
            await message.channel.send(f"Não foi possível renomear: {e}")

    # ── renomear cargo ────────────────────────────────────────────────────────
    # Uso: "Shell renomeia cargo NomeAntigo para NomeNovo"
    elif _addr and re.search(r'\b(renomei[ae]r?|rename)\b.{0,20}\bcargo\b', conteudo.lower()):
        m_para = re.search(r'\bpara\s+(.+)$', conteudo, re.IGNORECASE)
        if not m_para:
            await message.channel.send("Formato: Shell renomeia cargo NomeAtual para NomeNovo")
            return True
        novo_nome_c = m_para.group(1).strip()[:50]
        # Extrai nome atual do cargo
        nome_atual = re.sub(r'(?i).*?\bcargo\s+', '', conteudo)
        nome_atual = re.sub(r'\bpara\b.*$', '', nome_atual, flags=re.IGNORECASE).strip()
        role_ren = _buscar_role_por_nome(guild, nome_atual) or (message.role_mentions[0] if message.role_mentions else None)
        if not role_ren:
            await message.channel.send(f"Não encontrei o cargo '{nome_atual}'.")
            return True
        try:
            nome_antigo_c = role_ren.name
            await role_ren.edit(name=novo_nome_c, reason=f"Ordem de {autor}")
            await message.channel.send(f"Cargo '{nome_antigo_c}' renomeado para '{novo_nome_c}'.")
        except Exception as e:
            await message.channel.send(f"Não foi possível renomear o cargo: {e}")

    # ── lembrete agendado ─────────────────────────────────────────────────────
    # Uso: "Shell em 30 minutos avisa a galera no #canal sobre o evento"
    # Ou:  "Shell em 2 horas manda no #canal: bora jogar"
    elif _addr and re.search(r'\bem\s+\d+\s*(minuto|min|hora|h)\b', conteudo.lower()):
        m_tempo = re.search(r'em\s+(\d+)\s*(minuto|min|hora|h)\w*', conteudo.lower())
        if not m_tempo:
            return False
        valor = int(m_tempo.group(1))
        unidade = m_tempo.group(2)
        segundos = valor * 3600 if unidade.startswith('h') else valor * 60
        canal_lemb = message.channel_mentions[0] if message.channel_mentions else message.channel
        # Extrai mensagem  -  tudo após "avisa/manda/diz" ou após ":"
        texto_lemb = re.sub(r'(?i).*?\bem\s+\d+\s*\w+\s+(?:\w+\s+)?(?:no\s+\w+\s+)?(?:<#\d+>\s*)?', '', conteudo).strip()
        if ':' in texto_lemb:
            texto_lemb = texto_lemb.split(':', 1)[1].strip()
        texto_lemb = re.sub(r'<#\d+>', '', texto_lemb).strip() or "Lembrete do servidor."
        dur_txt = f"{valor} {'minuto' if unidade.startswith('m') else 'hora'}{'s' if valor != 1 else ''}"
        await message.channel.send(f"Lembrete agendado em {dur_txt} em {canal_lemb.mention}.")

        async def _disparar():
            await asyncio.sleep(segundos)
            try:
                await canal_lemb.send(f"Lembrete: {texto_lemb}")
            except Exception:
                pass
        asyncio.ensure_future(_disparar())
        log.info(f"[LEMBRETE] {autor}: {texto_lemb!r} em {dur_txt} → {canal_lemb.name}")

    # ── debate ────────────────────────────────────────────────────────────────
    # Uso: "Shell abre debate: tema aqui"  -  bot posta o tema e gerencia 10 minutos
    elif _addr and re.search(r'\b(debate|discussão|discussao|discutir)\b', conteudo.lower()):
        tema_db = re.sub(r'(?i).*?\b(?:debate|discussão|discussao|discutir)\b\s*:?\s*', '', conteudo).strip()
        if not tema_db:
            await message.channel.send("Qual o tema do debate? Diga: Shell debate: tema aqui")
            return True
        canal_db = message.channel_mentions[0] if message.channel_mentions else message.channel
        await canal_db.send(
            f"**Debate aberto: {tema_db}**\n"
            f"Galera, a discussão tá rolando por 10 minutos. "
            f"Respeito nos argumentos  -  sem agressividade."
        )

        debates_ativos[canal_db.id] = {"tema": tema_db, "fim": agora_utc() + timedelta(minutes=10), "msgs": 0}
        async def _encerrar_debate():
            await asyncio.sleep(600)
            debates_ativos.pop(canal_db.id, None)
            try:
                await canal_db.send(f"Debate encerrado: **{tema_db}**. Bom papo, galera.")
            except Exception:
                pass
        asyncio.ensure_future(_encerrar_debate())
        log.info(f"[DEBATE] {autor}: {tema_db!r}")

    # ── posta relatório em canal ──────────────────────────────────────────────
    # Uso: "Shell posta relatório [semanal/mensal/membros] em #canal"
    elif _addr and re.search(r'\b(posta[r]?\s+relat[oó]rio|relat[oó]rio\s+em)\b', conteudo.lower()):
        canal_rel = message.channel_mentions[0] if message.channel_mentions else message.channel
        msg_l = conteudo.lower()
        dias_rel = 30 if 'mensa' in msg_l else 7
        rel = await relatorio_membros(guild, dias_rel)
        tipo_txt = "mensal" if dias_rel == 30 else "semanal"
        blocos = [rel[i:i+1800] for i in range(0, len(rel), 1800)]
        await canal_rel.send(f"**Relatório {tipo_txt}  -  {guild.name}**")
        for bloco in blocos:
            await canal_rel.send(f"```\n{bloco}\n```")
        if canal_rel.id != message.channel.id:
            await message.channel.send(f"Relatório {tipo_txt} postado em {canal_rel.mention}.")

    # ── gerar arquivo (PDF / doc / planilha) — inicia wizard ─────────────────
    elif _addr and re.search(
        r'\b(pdf|doc|documento|planilha|csv|tabela|calc|relat[oó]rio|exporta[r]?)\b',
        conteudo.lower()
    ) and re.search(
        r'\b(cria[r]?|gera[r]?|manda[r]?|faz|exporta[r]?|preciso|quero|me\s+d[aá])\b',
        conteudo.lower()
    ) and not re.search(
        # Exclui perguntas hipoteticas: "e se voce fosse", "se eu pudesse", "poderia gerar"
        r'\b(e\s+se\s+|se\s+(?:voc[eê]|eu)\s+(?:fosse|pudesse|conseguisse|tivesse)'
        r'|poderia[m]?\s+(?:gerar|criar|fazer|exportar)'
        r'|seria\s+(?:capaz|poss[ií]vel)\s+(?:de\s+)?(?:gerar|criar|fazer)'
        r'|falar\s+sobre|explicar|como\s+(?:voc[eê]\s+)?(?:gera|cria|faz)'
        r'|o\s+que\s+(?:voc[eê]\s+)?(?:gera|cria))\b',
        conteudo.lower()
    ):
        msg_l = conteudo.lower()
        tipo = ("historico de canal" if re.search(r'\bhist[oó]rico\b|\bcanal\b', msg_l)
                else "membros" if re.search(r'\bmembro[s]?\b|\blista\b', msg_l)
                else "regras" if re.search(r'\bregra[s]?\b', msg_l)
                else "infracoes" if re.search(r'\binfra[cç][aã]o\b', msg_l)
                else "atividade" if re.search(r'\batividade\b|\branking\b', msg_l)
                else "citacoes" if re.search(r'\bcita[cç][aã]o\b', msg_l)
                else "relatorio mensal" if re.search(r'\bmensal\b|\bmes\b', msg_l)
                else "relatorio semanal")
        return await _iniciar_wizard(message, tipo)

    # ── comandos de voz ───────────────────────────────────────────────────────
    elif _addr and re.search(
        rf'\b(?:entr[ae][r]?|v(?:ai|em)|se\s+junt[ae][r]?)\b.{{0,30}}\b{_VOZ_KW}',
        conteudo.lower()
    ):
        if not guild:
            return False
        _nome_voz = re.search(
            r'\b(?:call|chamada|canal|voz)\s+(?:de\s+voz\s+)?([A-Za-z0-9_\-\s]+)', conteudo, re.IGNORECASE
        )
        _nome_str = _nome_voz.group(1).strip() if _nome_voz else None
        # "qualquer" nao e nome de canal
        if _nome_str and _nome_str.lower() in ("qualquer", "uma", "alguma", "disponivel"):
            _nome_str = None
        _canal_voz = await _entrar_canal_voz(guild, _nome_str)
        if not _canal_voz:
            await message.channel.send("Nao encontrei nenhum canal de voz no servidor.")
            return True
        _vc, _err = await _conectar_voz(_canal_voz)
        if _err:
            await message.channel.send(_err)
        else:
            await message.channel.send(f"Entrei em {_canal_voz.mention}.")
        return True

    elif _addr and re.search(
        rf'\b(?:sa[ií][r]?|desconect[ae][r]?|larg[ae][r]?)\b.{{0,30}}\b{_VOZ_KW}',
        conteudo.lower()
    ):
        if guild and guild.voice_client:
            await guild.voice_client.disconnect(force=True)
            await message.channel.send("Sai do canal de voz.")
        else:
            await message.channel.send("Nao estou em nenhum canal de voz.")
        return True

    elif _addr and re.search(
        rf'\b(?:fal[ae][r]?|diz(?:er)?|mand[ae][r]?\s+(?:audio|voz))\b.{{0,30}}\b{_VOZ_KW}',
        conteudo.lower()
    ):
        await message.channel.send("TTS em canal de voz não está disponível no momento — a biblioteca não suporta o protocolo E2EE do Discord.")
        return True

    # ── configuração dinâmica ─────────────────────────────────────────────────
    # Somente donos absolutos podem alterar configurações
    elif _addr and re.search(r'\b(config(?:ura(?:r|cao|ção)?)?|defin[ei][r]?|set[a]?)\b', conteudo.lower()):
        if message.author.id not in DONOS_ABSOLUTOS_IDS:
            await message.channel.send("Apenas donos absolutos podem alterar configurações.")
            return True

        _cfg_msg = conteudo.lower()

        # ── mostrar config ────────────────────────────────────────────────────
        if re.search(r'\b(mostr[ae][r]?|ver?|list[ae][r]?|atual|config(?:urações?)?)\b', _cfg_msg) and not re.search(r'\b(define|set|configura[r]?)\b', _cfg_msg):
            linhas = ["**Configurações atuais:**"]
            _nomes = {
                "canal_auditoria_id":      "Canal de auditoria",
                "canal_regras_id":         "Canal de regras",
                "canal_bem_vindo_id":      "Canal de boas-vindas",
                "canal_logs_id":           "Canal de logs",
                "cargo_mod_id":            "Cargo de moderação",
                "cargos_superiores_ids":   "Cargos superiores",
                "usuarios_superiores_ids": "Usuarios superiores",
                "contas_teste_ids":        "Contas de teste",
            }
            for chave, nome in _nomes.items():
                val = cfg(chave)
                if isinstance(val, list):
                    val_str = ", ".join(f"<@&{v}>" if "cargo" in chave else f"<@{v}>" for v in val) if val else "nenhum"
                elif val and "canal" in chave:
                    val_str = f"<#{val}>"
                elif val and "cargo" in chave:
                    val_str = f"<@&{val}>"
                elif val and "usuario" in chave or val and "conta" in chave:
                    val_str = f"<@{val}>"
                else:
                    val_str = str(val) if val else "não definido"
                linhas.append(f"**{nome}:** {val_str}")
            await message.channel.send("\n".join(linhas))
            return True

        # ── definir canal ─────────────────────────────────────────────────────
        _m_canal = re.search(r'\bcanal\s+(de\s+)?(auditoria|regras?|boas.?vindas?|bem.?vindo|logs?)', _cfg_msg)
        if _m_canal and message.channel_mentions:
            _tipo_canal = _m_canal.group(2).lower().replace(" ", "").replace("-", "")
            _chave_canal = {
                "auditoria": "canal_auditoria_id",
                "regras":    "canal_regras_id",
                "regra":     "canal_regras_id",
                "boasvindas":"canal_bem_vindo_id",
                "bemvindo":  "canal_bem_vindo_id",
                "logs":      "canal_logs_id",
                "log":       "canal_logs_id",
            }.get(_tipo_canal)
            if _chave_canal:
                _canal_novo = message.channel_mentions[0]
                _cfg[_chave_canal] = _canal_novo.id
                salvar_config()
                await message.channel.send(f"Canal de {_m_canal.group(2)} definido como {_canal_novo.mention}.")
                return True

        # ── definir cargo ─────────────────────────────────────────────────────
        _m_cargo_tipo = re.search(r'\bcargo\s+(de\s+)?(mod(?:eração?|eracao?)?|superior)', _cfg_msg)
        if _m_cargo_tipo and message.role_mentions:
            _tipo_cargo = _m_cargo_tipo.group(2).lower()
            _cargo_novo = message.role_mentions[0]
            if "mod" in _tipo_cargo:
                _cfg["cargo_mod_id"] = _cargo_novo.id
                salvar_config()
                await message.channel.send(f"Cargo de moderação definido como {_cargo_novo.mention}.")
            elif "superior" in _tipo_cargo:
                _lista = _cfg.get("cargos_superiores_ids", list(CARGOS_SUPERIORES_IDS))
                if _cargo_novo.id not in _lista:
                    _lista.append(_cargo_novo.id)
                    _cfg["cargos_superiores_ids"] = _lista
                    salvar_config()
                    await message.channel.send(f"Cargo {_cargo_novo.mention} adicionado aos superiores.")
                else:
                    await message.channel.send(f"Cargo {_cargo_novo.mention} ja esta na lista de superiores.")
            return True

        # ── remover cargo superior ────────────────────────────────────────────
        if re.search(r'\b(remov[ae][r]?|tira[r]?)\b.*\bcargo\s+superior\b', _cfg_msg) and message.role_mentions:
            _cargo_rm = message.role_mentions[0]
            _lista = _cfg.get("cargos_superiores_ids", list(CARGOS_SUPERIORES_IDS))
            if _cargo_rm.id in _lista:
                _lista.remove(_cargo_rm.id)
                _cfg["cargos_superiores_ids"] = _lista
                salvar_config()
                await message.channel.send(f"Cargo {_cargo_rm.mention} removido dos superiores.")
            else:
                await message.channel.send(f"Cargo {_cargo_rm.mention} nao estava na lista.")
            return True

        # ── adicionar/remover usuário superior ────────────────────────────────
        _add_sup = re.search(r'\b(add|adicion[ae][r]?)\b.*\busuario\s+superior\b', _cfg_msg)
        _rm_sup = re.search(r'\b(remov[ae][r]?|tira[r]?)\b.*\busuario\s+superior\b', _cfg_msg)
        if (_add_sup or _rm_sup) and message.mentions:
            _user_cfg = [m for m in message.mentions if m != client.user][0] if message.mentions else None
            if _user_cfg:
                _lista_u = _cfg.get("usuarios_superiores_ids", list(USUARIOS_SUPERIORES_IDS))
                if _add_sup:
                    if _user_cfg.id not in _lista_u:
                        _lista_u.append(_user_cfg.id)
                        _cfg["usuarios_superiores_ids"] = _lista_u
                        salvar_config()
                        await message.channel.send(f"{_user_cfg.mention} adicionado como usuario superior.")
                    else:
                        await message.channel.send(f"{_user_cfg.mention} ja e superior.")
                else:
                    if _user_cfg.id in _lista_u:
                        _lista_u.remove(_user_cfg.id)
                        _cfg["usuarios_superiores_ids"] = _lista_u
                        salvar_config()
                        await message.channel.send(f"{_user_cfg.mention} removido dos superiores.")
                    else:
                        await message.channel.send(f"{_user_cfg.mention} nao estava na lista.")
            return True

        # ── adicionar/remover conta de teste ──────────────────────────────────
        _add_teste = re.search(r'\b(add|adicion[ae][r]?)\b.*\bteste\b', _cfg_msg)
        _rm_teste = re.search(r'\b(remov[ae][r]?|tira[r]?)\b.*\bteste\b', _cfg_msg)
        if (_add_teste or _rm_teste) and message.mentions:
            _user_t = [m for m in message.mentions if m != client.user][0] if message.mentions else None
            if _user_t:
                _lista_t = _cfg.get("contas_teste_ids", [])
                if _add_teste:
                    if _user_t.id not in _lista_t:
                        _lista_t.append(_user_t.id)
                        _cfg["contas_teste_ids"] = _lista_t
                        salvar_config()
                        await message.channel.send(f"{_user_t.mention} adicionado como conta de teste.")
                    else:
                        await message.channel.send(f"{_user_t.mention} ja esta na lista.")
                else:
                    if _user_t.id in _lista_t:
                        _lista_t.remove(_user_t.id)
                        _cfg["contas_teste_ids"] = _lista_t
                        salvar_config()
                        await message.channel.send(f"{_user_t.mention} removido das contas de teste.")
                    else:
                        await message.channel.send(f"{_user_t.mention} nao estava na lista.")
            return True

        await message.channel.send(
            "Nao entendi o que configurar. Exemplos:\n"
            "- `Shell configura canal de auditoria #canal`\n"
            "- `Shell configura canal de regras #canal`\n"
            "- `Shell configura cargo de mod @cargo`\n"
            "- `Shell configura cargo superior @cargo`\n"
            "- `Shell remove cargo superior @cargo`\n"
            "- `Shell adiciona usuario superior @user`\n"
            "- `Shell mostra config`"
        )
        return True

    # ── traduzir mensagem ─────────────────────────────────────────────────────
    # Uso: "Shell traduz isso para inglês" (reply na mensagem), "Shell traduz: texto"
    elif _addr and re.search(r'\btraduz[ir]?\b|\btranslat[e]?\b', conteudo.lower()):
        m_idioma = re.search(r'\bpara\s+(\w+)', conteudo.lower())
        idioma = m_idioma.group(1) if m_idioma else "ingles"
        # Texto a traduzir: reply ou extrai da própria mensagem
        if message.reference and isinstance(getattr(message.reference, "resolved", None), discord.Message):
            texto_trad = message.reference.resolved.content
        else:
            texto_trad = re.sub(r'(?i).*?\btraduz[ir]?\s*(?:isso\s*)?(?:para\s+\w+\s*)?:?\s*', '', conteudo).strip()
        if not texto_trad:
            await message.channel.send("Responde à mensagem que quer traduzir, ou escreve: Shell traduz: texto")
            return True
        resultado = await traduzir_texto(texto_trad, idioma)
        await message.channel.send(f"**Tradução ({idioma}):** {resultado}")

    # ── ticket de suporte ─────────────────────────────────────────────────────
    # Uso: "Shell cria ticket para @membro motivo aqui"
    elif _addr and re.search(r'\bticket\b', conteudo.lower()):
        if not alvos:
            await message.channel.send("Menciona o membro para quem abrir o ticket.")
            return True
        alvo_ticket = alvos[0]
        motivo_ticket = re.sub(r'(?i).*?\bticket\b\s*(?:para\s+\S+\s*)?', '', conteudo).strip() or "sem motivo"
        canal_ticket = await criar_ticket_canal(guild, alvo_ticket, motivo_ticket, autor)
        if canal_ticket:
            await message.channel.send(f"Ticket criado: {canal_ticket.mention}")
        else:
            await message.channel.send("Não foi possível criar o ticket.")

    # ── enviar DM a membro ────────────────────────────────────────────────────
    # Uso: "Shell manda DM para @membro: mensagem"
    elif _addr and re.search(r'\bdm\b|\bmanda\s+(?:mensagem|msg)\b', conteudo.lower()) and alvos:
        alvo_dm = alvos[0]
        m_dm = re.search(r'(?:dm|mensagem|msg)\s+(?:para\s+\S+\s*)?:?\s*(.+)$', conteudo, re.IGNORECASE)
        texto_dm = m_dm.group(1).strip() if m_dm else ""
        texto_dm = re.sub(r'<@!?\d+>', '', texto_dm).strip()
        if not texto_dm:
            await message.channel.send("Qual a mensagem a enviar? Formato: Shell manda DM para @membro: texto")
            return True
        try:
            await alvo_dm.send(f"Mensagem de {autor} (via bot): {texto_dm}")
            await message.channel.send(f"DM enviada para {alvo_dm.mention}.")
            log.info(f"[DM] {autor} → {alvo_dm.display_name}: {texto_dm[:60]}")
        except Exception as e:
            await message.channel.send(f"Não foi possível enviar DM para {alvo_dm.display_name}: {e}")

    # ── guardar citação ───────────────────────────────────────────────────────
    # Uso: responder à mensagem + "Shell guarda isso" / "Shell salva essa frase"
    elif _addr and re.search(r'\b(guarda|salva|cita[cç][aã]o|registra)\b.{0,20}\b(isso|essa|frase|mensagem)\b', conteudo.lower()):
        msg_cit = None
        if message.reference and isinstance(getattr(message.reference, "resolved", None), discord.Message):
            msg_cit = message.reference.resolved
        if not msg_cit:
            await message.channel.send("Responde à mensagem que quer guardar como citação.")
            return True
        brasilia = timezone(timedelta(hours=-3))
        citacoes.append({
            "texto": msg_cit.content[:300],
            "autor": msg_cit.author.display_name,
            "canal": message.channel.name,
            "ts": msg_cit.created_at.astimezone(brasilia).strftime('%d/%m/%Y %H:%M'),
        })
        salvar_dados()
        await message.channel.send(f"Citação de {msg_cit.author.display_name} guardada. Total: {len(citacoes)}.")

    # ── citação aleatória ─────────────────────────────────────────────────────
    elif _addr and re.search(r'\bcita[cç][aã]o\s+aleat[oó]ria\b|\bcita[cç][aã]o\b', conteudo.lower()):
        if not citacoes:
            await message.channel.send("Nenhuma citação guardada ainda. Responde a uma mensagem e diz: Shell guarda isso")
            return True
        cit = random.choice(citacoes)
        await message.channel.send(f'"{cit["texto"]}"  -  {cit["autor"]}, {cit.get("ts","?")}')

    # ── ranking de atividade ──────────────────────────────────────────────────
    elif _addr and re.search(r'\branking\b|\batividade\b|\bmais\s+ativo[s]?\b', conteudo.lower()):
        if not atividade_mensagens:
            await message.channel.send("Ainda não há dados de atividade desta sessão.")
            return True
        top = sorted(atividade_mensagens.items(), key=lambda x: x[1], reverse=True)[:10]
        linhas = []
        for i, (uid, cnt) in enumerate(top, 1):
            membro_r = guild.get_member(uid)
            nome_r = membro_r.display_name if membro_r else nomes_historico.get(uid, f"ID {uid}")
            linhas.append(f"{i}. {nome_r}  -  {cnt} mensagem{'s' if cnt != 1 else ''}")
        await message.channel.send("**Ranking de atividade (sessão atual):**\n" + "\n".join(linhas))

    # ── monitorar / parar de monitorar canal ──────────────────────────────────
    elif _addr and re.search(r'\bmonitor[a]?\b|\bfique\s+atento\b', conteudo.lower()):
        canal_mon = message.channel_mentions[0] if message.channel_mentions else message.channel
        canais_monitorados.add(canal_mon.id)
        salvar_dados()
        await message.channel.send(f"Vou participar ativamente das conversas em {canal_mon.mention}.")
        log.info(f"[MONITOR] {autor} ativou monitoramento em #{canal_mon.name}")

    elif _addr and re.search(r'\bpara\s+de\s+monitor[a]?r?\b|\bdesmonitor[a]?r?\b|\bpare\s+de\s+participar\b', conteudo.lower()):
        canal_mon = message.channel_mentions[0] if message.channel_mentions else message.channel
        canais_monitorados.discard(canal_mon.id)
        salvar_dados()
        await message.channel.send(f"Parei de monitorar {canal_mon.mention}.")

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
            return f"Eae, {nome} está AFK no momento  -  {motivo}" + (f", {tempo_txt}." if tempo_txt else ".")
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
            texto = f"Eae, {nome} está AFK  -  {motivo or 'ausente no momento'}."
        return texto
    except Exception as e:
        log.error(f"_gerar_aviso_afk_ia: {e}")
        if motivo:
            return f"Eae, {nome} está AFK no momento  -  {motivo}" + (f", {tempo_txt}." if tempo_txt else ".")
        return f"Eae, {nome} está AFK no momento." + (f" Volta em {tempo_txt}." if tempo_txt else "")


async def processar_ordem_mod(message: discord.Message) -> bool:
    """
    Processa apenas comandos de moderação para o cargo de mod (1487859369008697556).
    Comandos disponíveis: silenciar, dessilenciar, banir, desbanir, expulsar, avisar, regras, listar.
    Não executa ordens gerais (boas-vindas, histórias, etc.)  -  isso é privilégio dos superiores.
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

    # Comando não reconhecido para mod  -  não executa ordens gerais
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
            texto_bv = f"Sejam bem-vindos ao servidor {nomes}. Leiam as regras em {CANAL_REGRAS()} e bom aprendizado."
        else:
            texto_bv = f"Bem-vindos ao servidor. Leiam as regras em {CANAL_REGRAS()} e aproveitem."

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



# ── Validação de intenção: separa solicitação de observação ──────────────────

def _tem_intencao_de_acao(conteudo: str) -> bool:
    """
    Heurística rápida (sem API): retorna True apenas quando a mensagem contém
    indicadores claros de solicitação de ação.

    Retorna False para:
    - observações ("vi que X", "notei que Y")
    - verificações ("X funciona, certo?", "é isso mesmo?")
    - confirmações do que o bot faz ("você avisa quando X, né?")
    - construções hipotéticas já filtradas antes
    """
    msg = conteudo.lower().strip()

    # Marcadores de observação/verificação — não são pedidos
    if re.search(
        r'\b(?:vi\s+qu[e]?|notei\s+qu[e]?|percebi\s+qu[e]?|parece\s+qu[e]?'
        r'|vejo\s+qu[e]?|sei\s+qu[e]?|soube\s+qu[e]?)\b'
        r'|\bcerto\s*\?|\bné\s*\?|\bnão\s+é\s*\?|\bverdade\s*\?|\bmesmo\s*\?'
        r'|\bé\s+isso\b|\bé\s+verdade\b|\bfuncion[ao]u?\b.*\?$'
        r'|\bvc\s+(?:faz|avisa|manda|responde)\b.*\?\s*$',
        msg,
        re.IGNORECASE,
    ):
        return False

    # Verbos de ação claros — provavelmente é um pedido
    if re.search(
        r'\b(?:cria[r]?|gera[r]?|faze[r]?|mand[ae][r]?|envi[ae][r]?'
        r'|bota[r]?|add|adiciona[r]?|remove[r]?|deleta[r]?|apaga[r]?'
        r'|ban[e]?|kick|expuls[ae]|silencia[r]?|muta[r]?'
        r'|posta[r]?|escreve[r]?|coloca[r]?|configura[r]?'
        r'|ativa[r]?|desativa[r]?|abre[r]?|fecha[r]?|lista[r]?|mostra[r]?'
        r'|encaminha[r]?|reencaminha[r]?|repassa[r]?|forward'
        r'|compartilha[r]?)\b',
        msg,
    ):
        return True

    if re.search(
        r'\bquero\s+que\b|\bpreciso\s+que\b'
        r'|\bme\s+(?:ajuda|faz|diz|manda|da|d[aá])\b'
        r'|\bpode[s]?\s+(?:fazer|criar|me|ir)\b',
        msg,
    ):
        return True

    # Por padrão: assume que é conversa, não ação
    return False


# ── Interpretação natural de instruções (adendo: sem comandos explícitos) ─────

async def _ia_parsear_instrucao(conteudo: str, guild: discord.Guild) -> dict | None:
    """
    Usa llama-3.1-8b-instant para interpretar instrução em linguagem natural.
    Retorna dict com {acao, params} ou None em caso de falha.
    Custo estimado: ~250 tokens por chamada.
    """
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return None

    membros_txt = ""
    if guild:
        membros_txt = ", ".join(
            m.display_name for m in guild.members if not m.bot
        )[:400]

    system = (
        "Você é um parser de intenção para bot Discord. "
        "Analise a instrução e retorne APENAS JSON válido (sem markdown, sem explicações). "
        "Ações disponíveis: enquete(tema,opcoes=[]), sorteio(quantidade,cargo=null), "
        "lembrete(texto,segundos,canal=null), aviso(texto,canal=null), "
        "criar_canal(nome,tipo=texto|voz), criar_cargo(nome), "
        "debate(tema,canal=null), limpar(quantidade,canal=null), "
        "gerar_pdf(tipo=relatorio|historico|membros|regras|citacoes,canal=null,dias=7), "
        "traduzir(texto,idioma=ingles), ticket(usuario,motivo=null), "
        "dm(usuario,texto), citacao_aleatoria(), ranking(), "
        "monitorar(canal=null), parar_monitorar(canal=null), "
        "gdoc(tipo=relatorio|historico|regras,canal=null,dias=7), "
        "gsheet(tipo=membros|infracoes|atividade|citacoes), "
        "enviar_canal(texto,canal) — envia uma mensagem específica em um canal (ex: 'manda X no #geral'), "
        "encaminhar(canal) — encaminha a mensagem referenciada para outro canal (ex: 'encaminha isso pro #anuncios'). "
        "NUNCA retorne acao banir, silenciar ou qualquer punição — punições requerem comando explícito. "
        "Se for pergunta, conversa, menção a raid/invasão/punição, retorne {\"acao\":\"conversa\"}. "
        f"Membros do servidor: {membros_txt}."
    )
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=100,
            temperature=0,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": conteudo},
            ],
        )
        _registrar_tokens("8b", (resp.usage.total_tokens if resp.usage else 250))
        txt = resp.choices[0].message.content.strip()
        txt = re.sub(r'^```(?:json)?\s*|\s*```$', '', txt, flags=re.MULTILINE).strip()
        return json.loads(txt)
    except Exception as e:
        log.debug(f"[IA_PARSE] falhou: {e}")
        return None


_ACOES_DESTRUTIVAS = frozenset({
    "criar_cargo", "criar_canal", "banir", "expulsar", "silenciar",
    "aviso", "remover_cargo", "deletar_canal",
})

async def _ia_executar(intencao: dict, message: discord.Message, guild: discord.Guild) -> bool:
    """Executa ação parseada pela IA. Retorna True se executou algo."""
    acao = intencao.get("acao", "conversa")
    params = intencao.get("params", {})

    if acao in ("conversa", "", None):
        return False

    canal = message.channel
    autor = message.author.display_name

    # Ações destrutivas/irreversíveis exigem @menção explícita — não apenas gatilho de nome
    if acao in _ACOES_DESTRUTIVAS and client.user not in message.mentions:
        log.info(f"[IA] acao destrutiva '{acao}' bloqueada: sem @mencao direta (autor={autor})")
        return False

    def _resolver_membro(nome: str) -> discord.Member | None:
        if not nome or not guild:
            return None
        nome_l = nome.lower()
        return next(
            (m for m in guild.members if nome_l in m.display_name.lower() or nome_l in m.name.lower()),
            None
        )

    if acao == "silenciar":
        alvo = _resolver_membro(params.get("usuario", ""))
        if not alvo:
            await canal.send(f"Não encontrei '{params.get('usuario')}' no servidor.")
            return True
        minutos = int(params.get("minutos", 10))
        try:
            ate_ia = min(agora_utc() + timedelta(minutes=minutos), agora_utc() + _DISCORD_TIMEOUT_MAX)
            await alvo.timeout(ate_ia, reason=f"Ordem de {autor}")
            await canal.send(f"{alvo.mention} silenciado por {minutos} minuto{'s' if minutos != 1 else ''}.")
            log.info(f"[IA] silenciar {alvo.display_name} {minutos}min  -  {autor}")
        except Exception as e:
            await canal.send(f"Não foi possível silenciar: {e}")
        return True

    if acao in ("banir", "silenciar"):
        # Punições nunca são executadas via IA — requerem comando explícito do operador.
        await canal.send(
            "Punições (banir/silenciar) precisam ser feitas via comando direto. "
            "Use: `banir @usuário motivo` ou `silenciar @usuário minutos`"
        )
        return True

    if acao == "enviar_canal":
        texto_env = params.get("texto", "").strip()
        canal_nome_env = params.get("canal", "").strip().lstrip("#")
        if not texto_env:
            await canal.send("Qual é o texto que devo enviar?")
            return True
        if not canal_nome_env:
            await canal.send("Em qual canal devo enviar? Menciona o canal.")
            return True
        dest_env = discord.utils.get(guild.text_channels, name=canal_nome_env) if guild else None
        if not dest_env:
            # Tenta por ID se o nome vier como <#ID>
            m_id = re.search(r'<#(\d+)>', canal_nome_env)
            if m_id and guild:
                dest_env = guild.get_channel(int(m_id.group(1)))
        if not dest_env:
            await canal.send(f"Canal '{canal_nome_env}' não encontrado.")
            return True
        try:
            await dest_env.send(texto_env)
            if dest_env.id != canal.id:
                await canal.send(f"Mensagem enviada em {dest_env.mention}.")
            log.info(f"[IA] enviar_canal #{dest_env.name} | {autor}")
        except Exception as e:
            await canal.send(f"Não consegui enviar em {dest_env.mention}: {e}")
        return True

    if acao == "encaminhar":
        canal_nome_enc = params.get("canal", "").strip().lstrip("#")
        # Mensagem a encaminhar: referência direta ou última mensagem relevante
        ref_enc: discord.Message | None = None
        if message.reference and isinstance(getattr(message.reference, "resolved", None), discord.Message):
            ref_enc = message.reference.resolved
        if not ref_enc:
            await canal.send("Responde à mensagem que quer encaminhar.")
            return True
        dest_enc = None
        if canal_nome_enc and guild:
            dest_enc = discord.utils.get(guild.text_channels, name=canal_nome_enc)
            if not dest_enc:
                m_id = re.search(r'<#(\d+)>', canal_nome_enc)
                if m_id:
                    dest_enc = guild.get_channel(int(m_id.group(1)))
        if not dest_enc:
            # Tenta encontrar canal mencionado na mensagem original (<#ID>)
            if message.channel_mentions:
                dest_enc = message.channel_mentions[0]
        if not dest_enc:
            await canal.send("Indica o canal de destino. Ex.: encaminha isso pro #anuncios")
            return True
        corpo_enc = ref_enc.content or ""
        anexos_enc = [a.url for a in ref_enc.attachments]
        if not corpo_enc and not anexos_enc:
            await canal.send("A mensagem referenciada está vazia.")
            return True
        cabecalho = f"[Encaminhado de #{ref_enc.channel.name} | {ref_enc.author.display_name}]"
        try:
            await dest_enc.send(f"{cabecalho}\n{corpo_enc}".strip())
            for url_att in anexos_enc[:3]:
                await dest_enc.send(url_att)
            if dest_enc.id != canal.id:
                await canal.send(f"Encaminhado para {dest_enc.mention}.")
            log.info(f"[IA] encaminhar #{ref_enc.channel.name} → #{dest_enc.name} | {autor}")
        except Exception as e:
            await canal.send(f"Não consegui encaminhar para {dest_enc.mention}: {e}")
        return True

    if acao == "enquete":
        tema = params.get("tema", "Enquete")
        opcoes = params.get("opcoes", [])
        if not opcoes:
            await canal.send("Quais são as opções? Ex.: Shell enquete: Tema | A | B | C")
            return True
        emojis = ["1️⃣","2️⃣","3️⃣","4️⃣","5️⃣","6️⃣","7️⃣","8️⃣","9️⃣","🔟"]
        opcoes = opcoes[:len(emojis)]
        linhas = [f"**Enquete: {tema}**"] + [f"{emojis[i]} {op}" for i, op in enumerate(opcoes)]
        msg_e = await canal.send("\n".join(linhas))
        for i in range(len(opcoes)):
            try: await msg_e.add_reaction(emojis[i])
            except Exception: pass
        log.info(f"[IA] enquete '{tema}'  -  {autor}")
        return True

    if acao == "aviso":
        texto = params.get("texto", "")
        if not texto:
            return False
        canal_nome = params.get("canal")
        dest = discord.utils.get(guild.text_channels, name=canal_nome) if (canal_nome and guild) else canal
        mencao = guild.default_role if guild else "@everyone"
        try:
            await dest.send(f"Atenção {mencao}, {texto}")
            if dest.id != canal.id:
                await canal.send(f"Aviso enviado em {dest.mention}.")
        except Exception as e:
            await canal.send(f"Erro ao enviar aviso: {e}")
        return True

    if acao == "lembrete":
        texto = params.get("texto", "Lembrete.")
        segundos = int(params.get("segundos", 300))
        canal_nome = params.get("canal")
        dest = discord.utils.get(guild.text_channels, name=canal_nome) if (canal_nome and guild) else canal
        mins = segundos // 60
        await canal.send(f"Lembrete agendado em {mins} minuto{'s' if mins != 1 else ''} em {dest.mention}.")
        async def _disparar_lemb():
            await asyncio.sleep(segundos)
            try: await dest.send(f"Lembrete: {texto}")
            except Exception: pass
        asyncio.ensure_future(_disparar_lemb())
        return True

    if acao == "criar_canal":
        nome = re.sub(r'[^\w\-]', '-', params.get("nome", "novo-canal").lower()).strip('-') or "novo-canal"
        tipo = params.get("tipo", "texto")
        try:
            if tipo == "voz":
                novo = await guild.create_voice_channel(nome, reason=f"IA  -  {autor}")
            else:
                novo = await guild.create_text_channel(nome, reason=f"IA  -  {autor}")
            await canal.send(f"Canal {novo.mention} criado.")
            log.info(f"[IA] criar_canal #{nome}  -  {autor}")
        except Exception as e:
            await canal.send(f"Não foi possível criar o canal: {e}")
        return True

    if acao == "criar_cargo":
        nome = params.get("nome", "Novo Cargo")[:50]
        try:
            novo = await guild.create_role(name=nome, reason=f"IA  -  {autor}")
            await canal.send(f"Cargo {novo.mention} criado.")
            log.info(f"[IA] criar_cargo '{nome}'  -  {autor}")
        except Exception as e:
            await canal.send(f"Não foi possível criar o cargo: {e}")
        return True

    if acao == "debate":
        tema = params.get("tema", "")
        if not tema:
            return False
        canal_nome = params.get("canal")
        dest = discord.utils.get(guild.text_channels, name=canal_nome) if (canal_nome and guild) else canal
        await dest.send(
            f"**Debate aberto: {tema}**\n"
            "Galera, a discussão tá rolando por 10 minutos. Respeito nos argumentos."
        )
        debates_ativos[dest.id] = {"tema": tema, "fim": agora_utc() + timedelta(minutes=10), "msgs": 0}
        async def _encerrar_debate_ia():
            await asyncio.sleep(600)
            debates_ativos.pop(dest.id, None)
            try: await dest.send(f"Debate encerrado: **{tema}**. Bom papo, galera.")
            except Exception: pass
        asyncio.ensure_future(_encerrar_debate_ia())
        log.info(f"[IA] debate '{tema}'  -  {autor}")
        return True

    if acao == "limpar":
        qtd = min(int(params.get("quantidade", 10)), 50)
        canal_nome = params.get("canal")
        dest = discord.utils.get(guild.text_channels, name=canal_nome) if (canal_nome and guild) else canal
        apagadas = 0
        try:
            async for m in dest.history(limit=qtd + 5):
                if m.author == client.user:
                    await m.delete()
                    apagadas += 1
                    if apagadas >= qtd:
                        break
                    await asyncio.sleep(0.4)
            await canal.send(f"{apagadas} mensagem{'s' if apagadas != 1 else ''} minhas apagada{'s' if apagadas != 1 else ''} em {dest.mention}.")
        except Exception as e:
            await canal.send(f"Erro ao limpar: {e}")
        return True

    if acao == "sorteio":
        qtd = int(params.get("quantidade", 1))
        cargo_nome = params.get("cargo")
        role = discord.utils.get(guild.roles, name=cargo_nome) if (cargo_nome and guild) else None
        pool = (
            [m for m in role.members if not m.bot]
            if role
            else [m for m in guild.members if not m.bot and m != client.user]
        )
        if not pool:
            await canal.send("Nenhum membro disponível para sortear.")
            return True
        qtd = min(qtd, len(pool))
        ganhadores = random.sample(pool, qtd)
        mencoes = " ".join(m.mention for m in ganhadores)
        sufixo = "o sorteado é" if qtd == 1 else f"os {qtd} sorteados são"
        await canal.send(f"Sorteio encerrado  -  {sufixo}: {mencoes}")
        log.info(f"[IA] sorteio {qtd}x  -  {autor}")
        return True

    if acao == "gerar_pdf":
        if not FPDF_DISPONIVEL:
            await canal.send("fpdf2 não instalado  -  PDF indisponível.")
            return True
        tipo_pdf = params.get("tipo", "relatorio")
        try:
            if tipo_pdf == "historico":
                canal_nome = params.get("canal")
                dest_pdf = discord.utils.get(guild.text_channels, name=canal_nome) if canal_nome else canal
                pdf_bytes = await gerar_pdf_historico_canal(dest_pdf, 150)
                nome_arq = f"historico-{dest_pdf.name}.pdf"
            elif tipo_pdf == "membros":
                pdf_bytes = gerar_pdf_membros(guild)
                nome_arq = "membros.pdf"
            elif tipo_pdf == "regras":
                pdf_bytes = gerar_pdf_regras()
                nome_arq = "regras.pdf"
            elif tipo_pdf == "citacoes":
                pdf_bytes = gerar_pdf_citacoes()
                nome_arq = "citacoes.pdf"
            else:
                dias_pdf = int(params.get("dias", 7))
                pdf_bytes = await gerar_pdf_relatorio_servidor(guild, dias_pdf)
                nome_arq = "relatorio.pdf"
            await canal.send("PDF gerado:", file=discord.File(io.BytesIO(pdf_bytes), filename=nome_arq))
            log.info(f"[IA] gerar_pdf {tipo_pdf}  -  {autor}")
        except Exception as e:
            await canal.send(f"Erro ao gerar PDF: {e}")
        return True

    if acao == "traduzir":
        texto_t = params.get("texto", "")
        if not texto_t:
            return False
        idioma_t = params.get("idioma", "ingles")
        resultado_t = await traduzir_texto(texto_t, idioma_t)
        await canal.send(f"**Tradução ({idioma_t}):** {resultado_t}")
        return True

    if acao == "ticket":
        alvo_t = _resolver_membro(params.get("usuario", ""))
        if not alvo_t:
            await canal.send(f"Não encontrei '{params.get('usuario')}' para criar ticket.")
            return True
        motivo_t = params.get("motivo") or "sem motivo"
        canal_t = await criar_ticket_canal(guild, alvo_t, motivo_t, autor)
        if canal_t:
            await canal.send(f"Ticket criado: {canal_t.mention}")
        else:
            await canal.send("Não foi possível criar o ticket.")
        return True

    if acao == "dm":
        alvo_dm = _resolver_membro(params.get("usuario", ""))
        texto_dm = params.get("texto", "")
        if not alvo_dm or not texto_dm:
            return False
        try:
            await alvo_dm.send(f"Mensagem de {autor} (via bot): {texto_dm}")
            await canal.send(f"DM enviada para {alvo_dm.mention}.")
        except Exception as e:
            await canal.send(f"Não consegui enviar DM: {e}")
        return True

    if acao == "citacao_aleatoria":
        if not citacoes:
            await canal.send("Nenhuma citação guardada ainda.")
        else:
            cit = random.choice(citacoes)
            await canal.send(f'"{cit["texto"]}"  -  {cit["autor"]}, {cit.get("ts","?")}')
        return True

    if acao == "ranking":
        if not atividade_mensagens:
            await canal.send("Sem dados de atividade nesta sessão.")
            return True
        top = sorted(atividade_mensagens.items(), key=lambda x: x[1], reverse=True)[:10]
        linhas_r = []
        for i, (uid, cnt) in enumerate(top, 1):
            mb = guild.get_member(uid)
            nome_mb = mb.display_name if mb else f"ID {uid}"
            linhas_r.append(f"{i}. {nome_mb}  -  {cnt} msg{'s' if cnt != 1 else ''}")
        await canal.send("**Ranking de atividade:**\n" + "\n".join(linhas_r))
        return True

    if acao == "monitorar":
        canal_nome = params.get("canal")
        dest_mon = discord.utils.get(guild.text_channels, name=canal_nome) if canal_nome else canal
        canais_monitorados.add(dest_mon.id)
        salvar_dados()
        await canal.send(f"Vou participar ativamente das conversas em {dest_mon.mention}.")
        return True

    if acao == "parar_monitorar":
        canal_nome = params.get("canal")
        dest_mon = discord.utils.get(guild.text_channels, name=canal_nome) if canal_nome else canal
        canais_monitorados.discard(dest_mon.id)
        salvar_dados()
        await canal.send(f"Parei de monitorar {dest_mon.mention}.")
        return True

    if acao == "gdoc":
        if not _google_ok():
            await canal.send("Google API não configurada. Defina GOOGLE_CREDENTIALS_JSON no Railway.")
            return True
        tipo_d = params.get("tipo", "relatorio")
        try:
            await canal.send("Criando documento no Google Docs, aguarde...")
            if tipo_d == "historico":
                canal_nome = params.get("canal")
                dest = discord.utils.get(guild.text_channels, name=canal_nome) if canal_nome else canal
                url = await gdoc_historico_canal(dest, 200)
                await canal.send(f"Historico de {dest.mention} no Google Docs: {url}")
            elif tipo_d == "regras":
                url = await gdoc_regras()
                await canal.send(f"Regras no Google Docs: {url}")
            else:
                dias_d = int(params.get("dias", 7))
                url = await gdoc_relatorio(guild, dias_d)
                await canal.send(f"Relatorio no Google Docs: {url}")
            log.info(f"[IA] gdoc {tipo_d}  -  {autor}")
        except Exception as e:
            await canal.send(f"Erro ao criar documento: {e}")
        return True

    if acao == "gsheet":
        if not _google_ok():
            await canal.send("Google API não configurada. Defina GOOGLE_CREDENTIALS_JSON no Railway.")
            return True
        tipo_s = params.get("tipo", "membros")
        try:
            await canal.send("Criando planilha no Google Sheets, aguarde...")
            if tipo_s == "infracoes":
                url = await gsheet_infracoes(guild)
            elif tipo_s == "atividade":
                url = await gsheet_atividade(guild)
            elif tipo_s == "citacoes":
                url = await gsheet_citacoes()
            else:
                url = await gsheet_membros(guild)
            await canal.send(f"Planilha ({tipo_s}) no Google Sheets: {url}")
            log.info(f"[IA] gsheet {tipo_s}  -  {autor}")
        except Exception as e:
            await canal.send(f"Erro ao criar planilha: {e}")
        return True

    log.debug(f"[IA_EXEC] ação '{acao}' não reconhecida  -  passando adiante")
    return False


# ── Simulação de digitação humana ────────────────────────────────────────────

# ── Constantes de limites da plataforma Discord ───────────────────────────────
_DISCORD_MSG_MAX = 1990          # 2000 − 10 de buffer de segurança
_DISCORD_TIMEOUT_MAX = timedelta(days=28)   # limite máximo de timeout do Discord
_DISCORD_BAN_DELETE_MAX = 7     # delete_message_days máximo permitido
_DISCORD_CHANNEL_LIMIT = 490    # deixa 10 de folga antes do limite de 500
_GROQ_AUDIO_MAX_BYTES = 25 * 1024 * 1024   # 25 MB — limite do Groq Whisper

# Avisos de flood sem Groq (instantâneos, variados)
_AVISOS_FLOOD = [
    "{m} para.", "{m}, chega.", "spam não, {m}.",
    "{m} menos.", "para {m}.", "{m}, corta isso.",
    "{m} — flood.", "não {m}.", "{m} já chega.",
]


async def _manter_digitando(channel: discord.TextChannel, parar: asyncio.Event) -> None:
    """Background task: renova o indicador 'digitando...' até parar ser setado."""
    while not parar.is_set():
        try:
            await channel.trigger_typing()
        except Exception:
            pass
        try:
            await asyncio.wait_for(asyncio.shield(parar.wait()), timeout=7.0)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass


def _limpar_markdown(texto: str) -> str:
    """Remove formatação markdown do texto para envio como usuário comum."""
    # Remove blocos de código
    texto = re.sub(r"```[\s\S]*?```", "", texto)
    texto = re.sub(r"`[^`]+`", lambda m: m.group(0)[1:-1], texto)
    # Remove negrito/itálico
    texto = re.sub(r"\*{1,3}([^*]+)\*{1,3}", r"\1", texto)
    texto = re.sub(r"_{1,3}([^_]+)_{1,3}", r"\1", texto)
    # Remove cabeçalhos
    texto = re.sub(r"^#{1,6}\s+", "", texto, flags=re.MULTILINE)
    # Remove listas numeradas e com bullet
    texto = re.sub(r"^\s*\d+\.\s+", "", texto, flags=re.MULTILINE)
    texto = re.sub(r"^\s*[-*•]\s+", "", texto, flags=re.MULTILINE)
    # Remove links markdown [texto](url) → texto
    texto = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", texto)
    # Colapsa múltiplas linhas em branco
    texto = re.sub(r"\n{3,}", "\n\n", texto)
    return texto.strip()


async def _safe_send(
    channel: discord.TextChannel,
    texto: str,
    reply_msg: discord.Message | None = None,
) -> None:
    """
    Envia texto respeitando o limite de 2000 chars do Discord.
    Divide em múltiplas mensagens se necessário. Primeira parte usa reply se fornecido.
    Backoff automático em caso de rate limit (429).
    """
    if not texto:
        return
    # Dividir em blocos de no máximo _DISCORD_MSG_MAX chars em fronteiras de palavra
    chunks: list[str] = []
    restante = texto
    while restante:
        if len(restante) <= _DISCORD_MSG_MAX:
            chunks.append(restante)
            break
        corte = restante.rfind(" ", 0, _DISCORD_MSG_MAX)
        if corte <= 0:
            corte = _DISCORD_MSG_MAX
        chunks.append(restante[:corte])
        restante = restante[corte:].lstrip()

    for i, chunk in enumerate(chunks):
        tentativas = 0
        while tentativas < 3:
            try:
                if i == 0 and reply_msg is not None:
                    await reply_msg.reply(chunk)
                else:
                    await channel.send(chunk)
                break
            except discord.HTTPException as e:
                if e.status == 429:
                    retry_after = float(getattr(e, "retry_after", 2.0))
                    log.warning(f"[RATE_LIMIT] aguardando {retry_after:.1f}s")
                    await asyncio.sleep(retry_after + 0.5)
                    tentativas += 1
                else:
                    log.error(f"[SEND] HTTP {e.status}: {e.text[:80]}")
                    break
            except Exception as e:
                log.error(f"[SEND] erro: {e}")
                break
        if len(chunks) > 1 and i < len(chunks) - 1:
            await asyncio.sleep(0.6)


async def _digitar_e_enviar(
    channel: discord.TextChannel,
    texto: str,
    reply_msg: discord.Message | None = None,
) -> None:
    """
    Exibe o indicador 'digitando...' por um tempo proporcional ao texto
    antes de enviar — chamado APÓS a resposta já estar pronta.
    """
    palavras = max(len(texto.split()), 1)
    # Pequeno delay natural após ter o texto pronto (simula releitura/correção)
    delay = min(0.4 + palavras * 0.07 + random.uniform(0.0, 0.35), 2.5)
    delay = max(delay, 0.4)

    parar = asyncio.Event()
    task = asyncio.ensure_future(_manter_digitando(channel, parar))
    try:
        await asyncio.sleep(delay)
    finally:
        parar.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

    await _safe_send(channel, texto, reply_msg)


async def _iniciar_typing_antes(channel: discord.TextChannel) -> tuple[asyncio.Event, asyncio.Task]:
    """
    Inicia o indicador 'digitando...' ANTES da chamada à IA.
    Retorna (parar, task) — chame parar.set() + task.cancel() após enviar.
    """
    parar = asyncio.Event()
    task = asyncio.ensure_future(_manter_digitando(channel, parar))
    return parar, task


async def _parar_typing(parar: asyncio.Event, task: asyncio.Task) -> None:
    parar.set()
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


# ── Resposta humana: split, reação, silêncio e perfil ────────────────────────

_RESPOSTAS_GENERICAS = re.compile(
    r"^(?:entendido|certo|ok|compreendido|claro|sim|de acordo|combinado"
    r"|faz sentido|correto|exato|perfeito|ótimo|ótimo\.|beleza)\.*$",
    re.IGNORECASE,
)

# Padrões servis/assistente-genérico que o bot nunca deve usar
_PADROES_SERVIS = re.compile(
    r"(?:parece que a conversa terminou|não hesite em|estou aqui para ajudar"
    r"|posso ajudar com mais alguma|se precisar de mais|fico à disposição"
    r"|qualquer dúvida|estou disponível|conte comigo|estarei aqui"
    r"|foi um prazer|até a próxima|se quiser saber mais)",
    re.IGNORECASE,
)

def _e_resposta_generica(texto: str) -> bool:
    """Retorna True se a resposta é um placeholder genérico ou padrão servil."""
    t = texto.strip()
    return bool(_RESPOSTAS_GENERICAS.match(t)) or bool(_PADROES_SERVIS.search(t))


async def _enviar_em_sequencia(
    channel: discord.TextChannel,
    texto: str,
    reply_msg: discord.Message | None = None,
) -> None:
    """
    Divide respostas longas em 2-3 mensagens curtas com pausa entre elas,
    simulando o jeito humano de escrever no chat.
    Respostas curtas (≤1 frase) são enviadas direto.
    """
    # Não divide respostas curtas
    if len(texto) <= 120 or texto.count(".") <= 1:
        await _digitar_e_enviar(channel, texto, reply_msg)
        return

    # Tenta quebrar em frases
    import re as _re
    frases = [f.strip() for f in _re.split(r'(?<=[.!?])\s+', texto) if f.strip()]
    if len(frases) <= 1:
        await _digitar_e_enviar(channel, texto, reply_msg)
        return

    # Agrupa em no máximo 2 blocos, garantindo ≤ _DISCORD_MSG_MAX cada
    meio = len(frases) // 2
    blocos = [" ".join(frases[:meio]), " ".join(frases[meio:])]
    blocos = [b[:_DISCORD_MSG_MAX] for b in blocos if b]

    primeiro = True
    for bloco in blocos:
        await _digitar_e_enviar(channel, bloco, reply_msg if primeiro else None)
        primeiro = False
        if len(blocos) > 1:
            await asyncio.sleep(random.uniform(0.8, 1.8))


async def _reagir_ou_responder(
    message: discord.Message,
    texto: str,
) -> None:
    """
    Decide se reage com emoji ou responde em texto.
    Mensagens muito curtas e afirmativas viram reação.
    Remove markdown e deduplica antes de enviar.
    """
    if not texto:
        return

    # Strip de markdown — o bot fala como humano, não como documento
    texto = _limpar_markdown(texto)
    if not texto:
        return

    # Deduplicação: não envia resposta idêntica à última neste canal
    _chave_dup = texto.strip().lower()[:120]
    if _ultima_resposta_canal.get(message.channel.id) == _chave_dup:
        log.debug(f"[DEDUP] resposta idêntica ignorada em #{message.channel.name}")
        return
    _ultima_resposta_canal[message.channel.id] = _chave_dup

    _curta = len(texto.split()) <= 4
    _afirmativa = re.search(
        r'\b(sim|certo|exato|ok|claro|concordo|tambem|faz sentido|verdade)\b',
        texto.lower()
    )
    _negativa = re.search(r'\b(nao|não|errado|discordo|negativo)\b', texto.lower())

    if _curta and _afirmativa and random.random() < 0.55:
        try:
            await message.add_reaction("👍")
        except Exception:
            await _digitar_e_enviar(message.channel, texto, message)
        return

    if _curta and _negativa and random.random() < 0.45:
        try:
            await message.add_reaction("👎")
        except Exception:
            await _digitar_e_enviar(message.channel, texto, message)
        return

    await _enviar_em_sequencia(message.channel, texto, message)


async def _atualizar_perfil_usuario(user_id: int, autor: str, mensagem: str, resposta: str) -> None:
    """
    Atualiza o perfil do usuário após cada interação.
    A cada 5 interações gera um resumo via 8b para persistir.
    Também extrai episódios memoráveis (eventos específicos) para memória de longo prazo.
    """
    perfil = perfis_usuarios.setdefault(user_id, {"resumo": "", "n": 0, "atualizado": "", "episodios": []})
    perfil.setdefault("episodios", [])
    perfil["n"] = perfil.get("n", 0) + 1

    # Extrai episódio memorável a cada interação (só se a mensagem tiver substância)
    if GROQ_API_KEY and len(mensagem.split()) >= 6:
        try:
            ep_r = await _groq_client().chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=40,
                temperature=0.2,
                messages=[
                    {"role": "system", "content":
                     "Extraia UM fato ou evento específico e memorável desta conversa em 1 frase curta. "
                     "Exemplo: 'Travou no Dark Souls 3 na fase do dragão.' "
                     "Se não há nada memorável, responda exatamente: NENHUM"},
                    {"role": "user", "content": f"{autor}: {mensagem[:300]}\nBot: {resposta[:200]}"},
                ],
            )
            _registrar_tokens("8b", ep_r.usage.total_tokens if ep_r.usage else 40)
            ep_txt = ep_r.choices[0].message.content.strip()
            if ep_txt and ep_txt.upper() != "NENHUM" and len(ep_txt) > 5:
                episodios = perfil["episodios"]
                # Evita duplicatas próximas
                if not episodios or ep_txt.lower() not in episodios[-1].lower():
                    episodios.append(ep_txt)
                    if len(episodios) > 20:  # mantém os 20 mais recentes
                        episodios.pop(0)
                    salvar_dados()
                    log.debug(f"[EPISÓDIO] {autor}: {ep_txt}")
        except Exception as e:
            log.debug(f"[EPISÓDIO] falha: {e}")

    # Só gera resumo a cada 5 interações (evita chamadas desnecessárias)
    if perfil["n"] % 5 != 0 or not GROQ_API_KEY:
        return

    historico_recente = f"Usuário: {mensagem[:200]}\nBot: {resposta[:200]}"
    resumo_anterior = perfil.get("resumo", "")

    try:
        r = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=80,
            temperature=0.3,
            messages=[
                {"role": "system", "content":
                 "Você atualiza um resumo compacto de como um usuário se comunica e o que gosta. "
                 "Máximo 2 frases. Foco em: tom, assuntos frequentes, preferências observadas. "
                 "Se não há padrão claro ainda, diga 'sem padrão definido'."},
                {"role": "user", "content":
                 f"Resumo anterior: {resumo_anterior}\n\nNova interação:\n{historico_recente}"},
            ],
        )
        novo_resumo = r.choices[0].message.content.strip()
        perfil["resumo"] = novo_resumo
        perfil["atualizado"] = agora_utc().strftime("%Y-%m-%d")
        _registrar_tokens("8b", r.usage.total_tokens if r.usage else 80)
        salvar_dados()
        log.debug(f"[PERFIL] {autor}: {novo_resumo[:60]}")
    except Exception as e:
        log.debug(f"[PERFIL] falha ao atualizar: {e}")


# ── Participação ativa: debate e monitoramento de canal ───────────────────────

def _montar_ctx_canal(channel_id: int, n: int = 12) -> str:
    """Monta contexto recente do canal como string legível."""
    mem = list(canal_memoria.get(channel_id, []))
    if not mem:
        return ""
    return "\n".join(f"{m['autor']}: {m['conteudo'][:120]}" for m in mem[-n:])


async def _participar_debate(message: discord.Message, tema: str):
    """
    Participa ativamente de um debate com opinião própria e contexto completo.
    Usa o histórico recente para se posicionar de forma coerente, não apenas reagir
    à última mensagem — como alguém que acompanhou a conversa desde o início.
    """
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return
    ctx = _montar_ctx_canal(message.channel.id, n=10)
    humor_txt = f" Humor atual: {_humor_sessao}." if _humor_sessao else ""
    perfil_autor = _contexto_usuario(message.author.id)
    perfil_txt = f"\n{perfil_autor}" if perfil_autor else ""
    system = (
        f"Você é o shell_engenheiro, membro veterano de um servidor Discord brasileiro.{humor_txt}{perfil_txt}\n"
        f"Há um debate em andamento sobre: {tema!r}.\n"
        "Você TEM opiniões próprias e as defende. Não é neutro, não é mediador — você tem um ponto de vista.\n"
        "Pode concordar parcialmente, discordar diretamente, fazer uma pergunta que force reflexão, "
        "ou trazer um ângulo que ninguém considerou ainda.\n"
        "Formas naturais de entrar: discordar de alguém pelo nome, complementar um argumento, "
        "jogar uma pergunta retórica, ou simplesmente afirmar sua posição sem pedir permissão.\n"
        "Máximo 2 frases. Sem emojis, sem asteriscos, sem markdown. Sem introduções como \'bem,\' ou \'na verdade\'.\n"
        "Se já participou muito nessa conversa ou não tem nada genuíno a acrescentar: responda SILÊNCIO."
    )
    try:
        resp = await _groq_client().chat.completions.create(
            model=_escolher_modelo(),
            max_tokens=100,
            temperature=0.92,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": ctx or f"{message.author.display_name}: {message.content[:300]}"},
            ],
        )
        _registrar_tokens("8b", resp.usage.total_tokens if resp.usage else 150)
        texto = resp.choices[0].message.content.strip()
        if texto and "SILÊNCIO" not in texto.upper() and len(texto) > 8:
            await asyncio.sleep(random.uniform(2, 6))  # delay humano
            await _digitar_e_enviar(message.channel, texto)
            log.info(f"[DEBATE] participou em #{message.channel.name}: {texto[:60]}")
    except Exception as e:
        log.debug(f"[DEBATE] _participar_debate falhou: {e}")


async def _interjetar_conversa(message: discord.Message):
    """
    Entra numa conversa espontaneamente quando tem algo genuíno a contribuir.
    Pipeline em duas etapas:
      1. Triagem: decide se vale a pena e QUE TIPO de entrada faz sentido
      2. Geração: produz a interjeição com personalidade e contexto completo

    Tipos de entrada possíveis (retornados pela triagem):
      OPINIAO  — tem posição própria sobre o tema
      PERGUNTA — quer entender melhor ou provocar reflexão
      FATO     — tem informação relevante que agrega
      DISCORDA — discorda de algo dito
      PASS     — não tem nada genuíno a dizer
    """
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return

    ctx = _montar_ctx_canal(message.channel.id, n=8)
    if not ctx:
        return

    humor_txt = f" Humor atual: {_humor_sessao}." if _humor_sessao else ""
    perfil_autor = _contexto_usuario(message.author.id)
    perfil_txt = f"\n{perfil_autor}" if perfil_autor else ""

    # Etapa 1: triagem qualificada — não só GO/PASS, mas define o ângulo
    system_triagem = (
        "Você avalia se um bot de Discord deve entrar numa conversa e como.\n"
        "Responda com UMA das opções: OPINIAO, PERGUNTA, FATO, DISCORDA, PASS\n"
        "OPINIAO — o tema dá para tomar posição própria interessante\n"
        "PERGUNTA — algo na conversa merece uma pergunta que force reflexão\n"
        "FATO — há informação concreta e relevante que pode ser acrescentada\n"
        "DISCORDA — alguém disse algo questionável ou factualmente errado\n"
        "PASS — bate-papo trivial, emojis, cumprimentos, nada com substância\n"
        "Responda apenas a palavra, sem explicação."
    )
    try:
        triagem = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=5,
            temperature=0.0,
            messages=[
                {"role": "system", "content": system_triagem},
                {"role": "user", "content": ctx},
            ],
        )
        _registrar_tokens("8b", triagem.usage.total_tokens if triagem.usage else 10)
        tipo = triagem.choices[0].message.content.strip().upper()
        if "PASS" in tipo or tipo not in ("OPINIAO", "PERGUNTA", "FATO", "DISCORDA"):
            return

        # Etapa 2: geração com instrução específica por tipo
        instrucao_tipo = {
            "OPINIAO": (
                "Você tem uma opinião sobre esse tema e vai expressá-la diretamente. "
                "Tome uma posição clara — não fique em cima do muro. "
                "Pode ser controverso se for honesto."
            ),
            "PERGUNTA": (
                "Faça UMA pergunta que force as pessoas a pensarem diferente. "
                "Não pergunte o óbvio — pergunte o que ninguém pensou ainda. "
                "Pergunta real, não retórica vazia."
            ),
            "FATO": (
                "Você tem uma informação concreta e relevante sobre o tema. "
                "Coloque o fato de forma direta, sem rodeios. "
                "Pode contextualizar em 1 frase adicional se necessário."
            ),
            "DISCORDA": (
                "Você discorda de algo que foi dito. "
                "Seja direto: cite o ponto específico e diga por que está errado ou incompleto. "
                "Sem ser grosseiro, mas sem suavizar demais."
            ),
        }.get(tipo, "Comente de forma direta e genuína.")

        system_resp = (
            f"Você é o shell_engenheiro, membro veterano de um servidor Discord brasileiro.{humor_txt}{perfil_txt}\n"
            f"{instrucao_tipo}\n"
            "Máximo 2 frases. Sem emojis, sem asteriscos, sem markdown.\n"
            "Não comece com \'bem\', \'na verdade\', \'interessante\' ou qualquer introdução genérica.\n"
            "Se perceber que não tem nada genuíno a dizer desse ângulo: responda SILÊNCIO."
        )
        resp = await _groq_client().chat.completions.create(
            model=_escolher_modelo(),
            max_tokens=90,
            temperature=0.88,
            messages=[
                {"role": "system", "content": system_resp},
                {"role": "user", "content": ctx},
            ],
        )
        _registrar_tokens("8b", resp.usage.total_tokens if resp.usage else 80)
        texto = resp.choices[0].message.content.strip()
        if texto and "SILÊNCIO" not in texto.upper() and len(texto) > 8:
            await asyncio.sleep(random.uniform(3, 9))  # delay humano
            await _digitar_e_enviar(message.channel, texto)
            log.info(f"[MONITOR] {tipo} em #{message.channel.name}: {texto[:60]}")
    except Exception as e:
        log.debug(f"[MONITOR] _interjetar falhou: {e}")


async def _reagir_midia_autonoma(message: discord.Message, descricao: str):
    """
    Reage de forma autônoma a uma imagem/vídeo enviada num canal monitorado.
    Sem ser mencionado — age como membro que viu a mídia e tem algo genuíno a dizer.
    Cooldown de 90s por canal para não spammar.
    """
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return

    _canal_id = message.channel.id
    _agora = agora_utc()
    _ultima = ultima_interjeccao.get(_canal_id, datetime(1970, 1, 1, tzinfo=timezone.utc))
    if (_agora - _ultima).total_seconds() < 90:
        return  # cooldown

    ctx = _montar_ctx_canal(_canal_id, n=5)
    legenda = message.content.strip()
    humor_txt = f" Humor: {_humor_sessao}." if _humor_sessao else ""

    prompt_midia = (
        f"Mídia enviada por {message.author.display_name}"
        + (f" com legenda: '{legenda}'" if legenda else "")
        + f".\nDescrição do conteúdo: {descricao}"
        + (f"\nContexto recente do canal:\n{ctx}" if ctx else "")
    )

    system_midia = (
        f"Você é o shell_engenheiro, membro de um servidor Discord brasileiro.{humor_txt}\n"
        "Alguém enviou uma imagem/vídeo no canal. Você viu e quer comentar de forma genuína — "
        "como um membro normal reagiria: pode ser uma observação, uma piada, uma opinião, uma pergunta.\n"
        "1 frase no máximo. Sem emojis. Sem asteriscos. Sem markdown. Sem introdução.\n"
        "Se não tiver NADA genuíno a dizer sobre a mídia: responda SILÊNCIO."
    )
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=80,
            temperature=0.9,
            messages=[
                {"role": "system", "content": system_midia},
                {"role": "user", "content": prompt_midia},
            ],
        )
        _registrar_tokens("8b", resp.usage.total_tokens if resp.usage else 50)
        texto = _limpar_markdown(resp.choices[0].message.content.strip())
        if texto and "SILÊNCIO" not in texto.upper() and len(texto) > 5:
            ultima_interjeccao[_canal_id] = _agora
            await asyncio.sleep(random.uniform(2, 7))
            await _digitar_e_enviar(message.channel, texto)
            log.info(f"[MIDIA] reagiu em #{message.channel.name}: {texto[:60]}")
    except Exception as e:
        log.debug(f"[MIDIA] _reagir_midia_autonoma falhou: {e}")


async def traduzir_texto(texto: str, idioma: str = "ingles") -> str:
    """Traduz texto via Groq 8b-instant."""
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return "Tradução indisponível no momento."
    try:
        resp = await _groq_client().chat.completions.create(
            model="llama-3.1-8b-instant",
            max_tokens=250,
            temperature=0,
            messages=[
                {"role": "system", "content": f"Traduza para {idioma}. Responda APENAS com a traducao, sem explicacoes."},
                {"role": "user", "content": texto},
            ],
        )
        _registrar_tokens("8b", resp.usage.total_tokens if resp.usage else 180)
        return resp.choices[0].message.content.strip()
    except Exception as e:
        return f"Erro na tradução: {e}"


async def criar_ticket_canal(guild: discord.Guild, membro: discord.Member, motivo: str, autor: str) -> discord.TextChannel | None:
    """Cria canal privado de ticket para o membro + equipe de mod."""
    nome_canal = f"ticket-{membro.name.lower()[:16]}"
    # Verifica se já existe
    existente = discord.utils.get(guild.text_channels, name=nome_canal)
    if existente:
        return existente
    # Permissões: apenas o membro e quem tem permissão de gerenciar mensagens
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(read_messages=False),
        membro: discord.PermissionOverwrite(read_messages=True, send_messages=True),
        guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    for role in guild.roles:
        if role.permissions.manage_messages:
            overwrites[role] = discord.PermissionOverwrite(read_messages=True, send_messages=True)
    try:
        canal = await guild.create_text_channel(
            nome_canal, overwrites=overwrites,
            reason=f"Ticket aberto por {autor} para {membro.display_name}"
        )
        await canal.send(
            f"Ticket aberto para {membro.mention}.\n"
            f"Motivo: {motivo or 'não especificado'}\n"
            f"Aberto por: {autor}\n"
            f"Equipe de moderação será notificada em breve."
        )
        return canal
    except Exception as e:
        log.error(f"[TICKET] falhou: {e}")
        return None


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
        # Cap: Discord permite no máximo 28 dias de timeout
        ate = min(agora_utc() + timedelta(minutes=minutos), agora_utc() + _DISCORD_TIMEOUT_MAX)
        await membro.timeout(ate, reason=motivo)
        silenciamentos[membro.id] += 1
        infracoes[membro.id] = 0
        salvar_dados()
        txt_sil = await confirmar_acao(
            f"Silenciei {membro.display_name} por {descricao} (ocorrência {vez + 1}).",
            f"{membro.mention} silenciado por {descricao}."
        )
        await canal.send(txt_sil)
        log.info(f"Silenciado: {membro.display_name} por {descricao} (vez {vez + 1})")
    except Exception as e:
        log.error(f"Falha ao silenciar {membro.display_name}: {e}")
        txt_err = await _aviso_infrator(membro.mention, "atingiu limite de infrações — intervenção da equipe necessária")
        await canal.send(txt_err)


def _atualizar_contexto(guild: discord.Guild):
    global _contexto_servidor, _contexto_compacto
    _contexto_servidor = build_server_context(guild)
    _contexto_compacto = build_server_context_compact(guild)


# ── Anti-raid: lockdown e detecção de ataques ────────────────────────────────

async def _ativar_lockdown(guild: discord.Guild, motivo: str = "atividade suspeita detectada") -> None:
    """
    Ativa lockdown: bloqueia envio de mensagens para @everyone em todos os
    canais de texto públicos. Auto-desativa após 10 minutos.
    """
    global _lockdown_ativo, _permissoes_pre_lockdown
    if _lockdown_ativo:
        return
    _lockdown_ativo = True
    _permissoes_pre_lockdown.clear()
    everyone = guild.default_role

    for canal in guild.text_channels:
        try:
            ow = canal.overwrites_for(everyone)
            _permissoes_pre_lockdown[canal.id] = ow.copy()
            ow.send_messages = False
            await canal.set_permissions(everyone, overwrite=ow, reason=f"LOCKDOWN: {motivo}")
        except Exception:
            pass

    canal_audit = guild.get_channel(_canal_auditoria_id())
    mod = mencao_mod(guild)
    if canal_audit:
        try:
            await canal_audit.send(
                f"LOCKDOWN ATIVADO — {motivo}\n"
                f"{mod} intervenção necessária. Canais bloqueados para @everyone.\n"
                f"Lockdown auto-desfaz em 10 minutos, ou use: `desativar lockdown`"
            )
        except Exception:
            pass
    log.warning(f"[LOCKDOWN] Ativado: {motivo}")

    await asyncio.sleep(600)
    await _desativar_lockdown(guild, automatico=True)


async def _desativar_lockdown(guild: discord.Guild, automatico: bool = False) -> None:
    global _lockdown_ativo
    if not _lockdown_ativo:
        return
    _lockdown_ativo = False
    everyone = guild.default_role

    for canal in guild.text_channels:
        try:
            ow_original = _permissoes_pre_lockdown.get(canal.id)
            if ow_original is not None:
                await canal.set_permissions(everyone, overwrite=ow_original, reason="Lockdown encerrado")
            else:
                await canal.set_permissions(everyone, send_messages=None, reason="Lockdown encerrado")
        except Exception:
            pass

    canal_audit = guild.get_channel(_canal_auditoria_id())
    if canal_audit:
        sufixo = " (automático — 10min)" if automatico else " (manual)"
        try:
            await canal_audit.send(f"LOCKDOWN DESATIVADO{sufixo}. Canais reabertos para @everyone.")
        except Exception:
            pass
    log.info("[LOCKDOWN] Desativado")


# ── Sistema de denúncias com canal privado ────────────────────────────────────

_GATILHO_DENUNCIA = re.compile(
    r'\b(?:denuncia[r]?|denúncia|quero denunciar|quero reportar|reportar|reporte'
    r'|infring[iu]|desrespeitando|abusando|t[aá] abusando|abusar)\b',
    re.IGNORECASE,
)
_GATILHO_ANONIMO = re.compile(
    r'\ban[oô]nim[ao]|sem identificar|sem revelar|em segredo|sem meu nome\b',
    re.IGNORECASE,
)


async def _criar_canal_denuncia_privado(
    guild: discord.Guild,
    membro: discord.Member,
    seq: int,
) -> discord.TextChannel | None:
    """
    Cria canal de texto temporário e privado para coleta da denúncia.
    Visível apenas para: membro denunciante + bot + cargo de moderação (só leitura).
    @everyone não vê o canal.
    """
    if len(guild.channels) >= _DISCORD_CHANNEL_LIMIT:
        log.warning("[DENUNCIA] servidor no limite de canais (%d >= %d)", len(guild.channels), _DISCORD_CHANNEL_LIMIT)
        return None

    cargo_mod = guild.get_role(_CARGO_MODERADORES_ID)
    everyone = guild.default_role
    bot_member = guild.get_member(client.user.id)

    overwrites: dict = {
        everyone: discord.PermissionOverwrite(read_messages=False, send_messages=False),
        membro: discord.PermissionOverwrite(read_messages=True, send_messages=True),
    }
    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(
            read_messages=True, send_messages=True, manage_channels=True, manage_messages=True
        )
    if cargo_mod:
        # Mods podem ler mas não intervir durante a coleta
        overwrites[cargo_mod] = discord.PermissionOverwrite(read_messages=True, send_messages=False)

    # Tenta usar categoria de moderação existente
    categoria = (
        discord.utils.get(guild.categories, name="Denúncias")
        or discord.utils.get(guild.categories, name="denuncias")
        or discord.utils.get(guild.categories, name="Moderação")
        or discord.utils.get(guild.categories, name="Moderacao")
    )

    try:
        canal = await guild.create_text_channel(
            f"denuncia-{seq:04d}",
            overwrites=overwrites,
            category=categoria,
            topic="Canal privado temporário de denúncia — será excluído após o envio",
            reason="Denúncia privada",
        )
        return canal
    except Exception as e:
        log.error(f"[DENUNCIA] falha ao criar canal privado: {e}")
        return None


async def _iniciar_denuncia(message: discord.Message, anonimo: bool = False) -> None:
    """
    Cria canal privado e inicia o wizard de denúncia.
    Para denúncias anônimas o nome do denunciante NÃO aparece no relatório final.
    """
    global _denuncia_seq
    user_id = message.author.id
    autor = message.author.display_name
    guild = message.guild

    _denuncia_seq += 1
    seq = _denuncia_seq

    canal_priv = await _criar_canal_denuncia_privado(guild, message.author, seq)

    if canal_priv:
        _denuncia_wizard[user_id] = {
            "step": "denunciado",
            "dados": {
                "denunciante": "[anônimo]" if anonimo else autor,
                "anonimo": anonimo,
                "ts": agora_utc().isoformat(),
            },
            "canal_privado_id": canal_priv.id,
            "seq": seq,
        }
        aviso_anon = "Identidade protegida — não vai aparecer no relatório." if anonimo else ""
        await canal_priv.send(
            f"#{seq:04d} {aviso_anon}\nQuem vai ser denunciado?"
        )
        await message.channel.send(
            f"{canal_priv.mention} — canal privado criado. Continua lá."
        )
    else:
        # Fallback: wizard no canal atual
        _denuncia_wizard[user_id] = {
            "step": "denunciado",
            "dados": {"denunciante": "[anônimo]" if anonimo else autor, "anonimo": anonimo, "ts": agora_utc().isoformat()},
            "canal_privado_id": None,
            "seq": seq,
        }
        await message.channel.send(
            "Não consegui criar o canal privado (sem permissão). "
            "Vamos registrar aqui mesmo. Quem você quer denunciar?"
        )


async def _processar_denuncia_wizard(message: discord.Message) -> bool:
    """
    Processa as respostas do wizard de denúncia.
    Só consome mensagens se o usuário estiver no canal privado correto (ou fallback).
    Retorna True se a mensagem foi consumida.
    """
    global _denuncia_seq
    user_id = message.author.id
    estado = _denuncia_wizard.get(user_id)
    if not estado:
        return False

    # Verificar se a mensagem está no canal correto do wizard
    canal_privado_id = estado.get("canal_privado_id")
    if canal_privado_id and message.channel.id != canal_privado_id:
        return False  # ignora mensagens fora do canal da denúncia

    conteudo = message.content.strip()
    dados = estado["dados"]
    step = estado["step"]
    canal = message.channel

    if step == "denunciado":
        alvo_nome = message.mentions[0].display_name if message.mentions else conteudo
        dados["denunciado"] = alvo_nome
        estado["step"] = "descricao"
        await canal.send(f"{alvo_nome} anotado. O que aconteceu?")

    elif step == "descricao":
        dados["descricao"] = conteudo[:800]
        estado["step"] = "canal_ocorrencia"
        await canal.send("Onde aconteceu? Canal, voz, DM.")

    elif step == "canal_ocorrencia":
        dados["local"] = f"#{message.channel_mentions[0].name}" if message.channel_mentions else conteudo[:100]
        estado["step"] = "evidencia"
        await canal.send("Tem evidência? Manda agora ou diz que não tem.")

    elif step == "evidencia":
        if message.attachments:
            dados["evidencia"] = " | ".join(a.url for a in message.attachments[:5])
        elif conteudo.lower() in ("não", "nao", "nao tenho", "não tenho", "n", "-", "sem evidencia", "sem evidência"):
            dados["evidencia"] = "nenhuma"
        else:
            dados["evidencia"] = conteudo[:400]

        # ── Finalizar e publicar no canal de auditoria ────────────────────
        seq = estado.get("seq", _denuncia_seq)
        anonimo = dados.get("anonimo", False)
        denuncia_id = f"DEN-{'ANON-' if anonimo else ''}{seq:04d}"
        dados.update({"id": denuncia_id, "status": "pendente"})
        denuncias_pendentes.append(dict(dados))
        del _denuncia_wizard[user_id]

        guild = message.guild
        canal_audit = guild.get_channel(_canal_auditoria_id()) if guild else None
        mod = mencao_mod(guild) if guild else "@moderacao"

        relato = (
            f"DENÚNCIA {denuncia_id}\n"
            f"Denunciante: {dados['denunciante']}\n"
            f"Denunciado: {dados.get('denunciado', 'não informado')}\n"
            f"Local: {dados.get('local', 'não informado')}\n"
            f"Descrição: {dados.get('descricao', '')}\n"
            f"Evidência: {dados.get('evidencia', 'nenhuma')}\n"
            f"Status: PENDENTE — {mod}"
        )
        if canal_audit:
            try:
                await canal_audit.send(f"```\n{relato}\n```")
            except Exception:
                pass

        await canal.send(f"{denuncia_id} registrada. Equipe notificada. Canal some em 60s.")

        # Apagar canal privado após 60s
        if canal_privado_id and guild:
            async def _apagar_canal_denuncia():
                await asyncio.sleep(60)
                try:
                    c = guild.get_channel(canal_privado_id)
                    if c:
                        await c.delete(reason=f"Denúncia {denuncia_id} concluída")
                except Exception:
                    pass
            asyncio.ensure_future(_apagar_canal_denuncia())

    return True


# ── Extração de equipe em tempo real ─────────────────────────────────────────

def _extrair_equipe_real(guild: discord.Guild) -> dict:
    """
    Extrai membros da equipe diretamente dos cargos do Discord.
    Hierarquia de 4 níveis:
      proprietarios — usuários com controle total do agente
      colaboradores — cargo _CARGO_COLABORADORES_ID
      moderadores   — cargo _CARGO_MODERADORES_ID
      membros       — cargo _CARGO_MEMBROS_ID (participantes gerais)
    """
    def _status_str(m: discord.Member) -> str:
        return {
            discord.Status.online: "online",
            discord.Status.idle: "ausente",
            discord.Status.dnd: "ocupado",
        }.get(m.status, "offline")

    prop_lst, colab_lst, mods_lst, membros_lst = [], [], [], []
    for m in guild.members:
        if m.bot:
            continue
        dados = {
            "nome": m.display_name,
            "id": m.id,
            "status": _status_str(m),
            "msgs": atividade_mensagens.get(m.id, 0),
        }
        role_ids = {c.id for c in m.roles}
        if m.id in _PROPRIETARIOS_IDS:
            prop_lst.append(dados)
        elif _CARGO_COLABORADORES_ID in role_ids:
            colab_lst.append(dados)
        elif _CARGO_MODERADORES_ID in role_ids:
            mods_lst.append(dados)
        elif _CARGO_MEMBROS_ID in role_ids:
            membros_lst.append(dados)

    # Retrocompatibilidade: mantém chaves antigas apontando para os níveis corretos
    return {
        "proprietarios": prop_lst,
        "colaboradores": colab_lst,
        "moderadores": mods_lst,
        "membros": membros_lst,
        # aliases para código legado que usa as chaves antigas
        "donos": prop_lst,
        "superiores": colab_lst,
        "mods": mods_lst,
    }


# ── Detecção de pendências não tratadas em canais ─────────────────────────────

_SINAIS_AJUDA = re.compile(
    r'\b(?:ajuda|preciso de ajuda|socorro|algu[eé]m pode|como fa[cç]o|n[aã]o consigo'
    r'|n[aã]o funciona|deu erro|t[aá] dando erro|problema|bug|quebrou|travou'
    r'|n[aã]o entendo|tem como|algu[eé]m sabe|o que fazer|como resolvo'
    r'|me ajudem|me ajuda|preciso de suporte)\b',
    re.IGNORECASE,
)

async def _detectar_pendencias_canal(canal: discord.TextChannel, limite: int = 40) -> list[dict]:
    """
    Varre as últimas mensagens do canal em busca de sinais de ajuda/problema
    sem resposta da equipe ou do bot nos últimos 10-90 minutos.
    """
    cargo_mod = _cargo_mod_id()
    cargos_sup = _cargos_superiores_ids()
    agora = agora_utc()
    msgs: list[discord.Message] = []
    try:
        async for m in canal.history(limit=limite):
            msgs.append(m)
    except Exception:
        return []
    msgs.reverse()
    pendencias: list[dict] = []
    for i, m in enumerate(msgs):
        if m.author.bot or not m.content:
            continue
        if not _SINAIS_AJUDA.search(m.content):
            continue
        delta_min = (agora - m.created_at.replace(tzinfo=timezone.utc)).total_seconds() / 60
        if delta_min < 10 or delta_min > 90:
            continue
        respondido = any(
            j > i and (
                msgs[j].author == client.user
                or msgs[j].author.id in DONOS_IDS
                or any(c.id == cargo_mod or c.id in cargos_sup for c in getattr(msgs[j].author, "roles", []))
            )
            for j in range(i + 1, len(msgs))
        )
        if not respondido:
            pendencias.append({"msg": m, "conteudo": m.content[:200], "autor": m.author.display_name, "min_atras": int(delta_min)})
    return pendencias


# ── Task: accountability e participação autônoma da equipe ────────────────────

async def _task_accountability_equipe():
    """
    Task em background (ciclo de 3h):
    1. Varre canais monitorados + canais de suporte/geral por pendências sem resposta.
       Para cada uma: bot responde diretamente com informação útil ou aciona equipe.
    2. Verifica atividade dos mods. Se 2+ mods estiverem offline/inativos, posta
       nota discreta no canal de auditoria.
    3. Participa autonomamente em canal quieto dos monitorados (sem depender de pedido).
    """
    await client.wait_until_ready()
    await asyncio.sleep(900)  # 15min após iniciar

    while not client.is_closed():
        await asyncio.sleep(10800)  # 3 horas
        if not GROQ_API_KEY:
            continue
        try:
            guild = client.get_guild(SERVIDOR_ID)
            if not guild:
                continue
            agora = agora_utc()

            # ── 1. Pendências sem resposta ──────────────────────────────────
            ids_verificar: set[int] = set(canais_monitorados)
            for c in guild.text_channels:
                if any(kw in c.name.lower() for kw in ("geral", "ajuda", "suporte", "duvida", "dúvida", "chat")):
                    ids_verificar.add(c.id)

            respondidos = 0
            for cid in ids_verificar:
                if respondidos >= 3:
                    break
                canal_obj = guild.get_channel(cid)
                if not isinstance(canal_obj, discord.TextChannel):
                    continue
                pends = await _detectar_pendencias_canal(canal_obj)
                for p in pends:
                    if respondidos >= 3:
                        break
                    ctx_mem = list(canal_memoria.get(cid, []))
                    ctx_txt = "\n".join(f"{m['autor']}: {m['conteudo'][:100]}" for m in ctx_mem[-6:])
                    try:
                        r = await _groq_client().chat.completions.create(
                            model="llama-3.1-8b-instant",
                            max_tokens=120,
                            temperature=0.5,
                            messages=[
                                {"role": "system", "content":
                                 f"{system_com_contexto()}\n"
                                 "Um membro fez uma pergunta ou relatou um problema sem resposta. "
                                 "Responda de forma útil e precisa. Se não souber, diga e indique onde buscar. "
                                 "Se exigir intervenção humana da equipe, diga que vai acionar. Sem emojis."},
                                {"role": "user", "content":
                                 f"Contexto recente:\n{ctx_txt}\n\n"
                                 f"Pendência de {p['autor']} há {p['min_atras']}min:\n{p['conteudo']}"},
                            ],
                        )
                        texto_r = r.choices[0].message.content.strip()
                        _registrar_tokens("8b", r.usage.total_tokens if r.usage else 120)
                        if texto_r and "SILÊNCIO" not in texto_r.upper():
                            await p["msg"].reply(texto_r, mention_author=False)
                            respondidos += 1
                            log.info(f"[ACCOUNT] pendência de {p['autor']} em #{canal_obj.name} respondida")
                            await asyncio.sleep(4)
                    except Exception as e:
                        log.debug(f"[ACCOUNT] falha ao responder pendência: {e}")

            # ── 2. Verificar atividade da equipe ────────────────────────────
            equipe = _extrair_equipe_real(guild)
            todos_equipe = equipe["moderadores"] + equipe["colaboradores"]
            inativos = [m for m in todos_equipe if m["msgs"] == 0 and m["status"] == "offline"]
            if len(inativos) >= 2:
                canal_audit = guild.get_channel(_canal_auditoria_id())
                if canal_audit:
                    nomes_in = ", ".join(m["nome"] for m in inativos[:5])
                    try:
                        await canal_audit.send(
                            f"[Accountability] {len(inativos)} membro{'s' if len(inativos) != 1 else ''} "
                            f"da equipe sem atividade nesta sessão: {nomes_in}. "
                            f"Considere verificar a presença da moderação."
                        )
                        log.info(f"[ACCOUNT] notificou inatividade de {len(inativos)} mods")
                    except Exception:
                        pass

            # ── 3. Participação autônoma em canal quieto ────────────────────
            # Posta algo relevante em um canal monitorado que ficou quieto por 45-180min
            for cid in list(canais_monitorados):
                canal_q = guild.get_channel(cid)
                if not isinstance(canal_q, discord.TextChannel):
                    continue
                ultimo_post = _ultima_iniciativa.get(cid)
                if ultimo_post and (agora - ultimo_post) < timedelta(hours=2):
                    continue
                mem_canal = list(canal_memoria.get(cid, []))
                if not mem_canal:
                    continue
                ultima_msg_tempo = None
                try:
                    async for m in canal_q.history(limit=1):
                        ultima_msg_tempo = m.created_at.replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if not ultima_msg_tempo:
                    continue
                silencio_min = (agora - ultima_msg_tempo).total_seconds() / 60
                if not (45 <= silencio_min <= 180):
                    continue
                ctx_q = "\n".join(f"{m['autor']}: {m['conteudo'][:100]}" for m in mem_canal[-8:])
                try:
                    rq = await _groq_client().chat.completions.create(
                        model="llama-3.1-8b-instant",
                        max_tokens=70,
                        temperature=0.9,
                        messages=[
                            {"role": "system", "content":
                             f"{system_com_contexto()}\n"
                             "O canal está quieto. Você pode postar algo como membro normal: uma pergunta provocativa, "
                             "uma observação sobre o que foi discutido, uma notícia de tech, ou retomar um ponto interessante. "
                             "Só poste se tiver algo genuinamente interessante a dizer. "
                             "Se não há nada relevante, responda exatamente: SILÊNCIO"},
                            {"role": "user", "content":
                             f"Canal quieto há {int(silencio_min)}min. Histórico recente:\n{ctx_q}"},
                        ],
                    )
                    txt_q = rq.choices[0].message.content.strip()
                    _registrar_tokens("8b", rq.usage.total_tokens if rq.usage else 70)
                    if txt_q and "SILÊNCIO" not in txt_q.upper():
                        await canal_q.send(txt_q)
                        _ultima_iniciativa[cid] = agora
                        log.info(f"[ACCOUNT] participação autônoma em #{canal_q.name}: {txt_q[:50]}")
                    break  # só um canal por ciclo
                except Exception as e:
                    log.debug(f"[ACCOUNT] falha participação autônoma: {e}")

        except Exception as e:
            log.debug(f"[ACCOUNT] erro no ciclo principal: {e}")


async def _task_relatorio_semanal():
    """Task em background: posta relatório semanal no canal de auditoria toda segunda-feira às 08h (Brasília)."""
    await client.wait_until_ready()
    brasilia = timezone(timedelta(hours=-3))
    while not client.is_closed():
        agora = datetime.now(brasilia)
        # Calcula próxima segunda-feira às 08h
        dias_ate_segunda = (7 - agora.weekday()) % 7 or 7
        proxima = agora.replace(hour=8, minute=0, second=0, microsecond=0) + timedelta(days=dias_ate_segunda)
        espera = (proxima - agora).total_seconds()
        await asyncio.sleep(espera)
        guild = client.get_guild(SERVIDOR_ID)
        if not guild:
            continue
        canal = guild.get_channel(_canal_auditoria_id())
        if not canal:
            continue
        try:
            rel = await relatorio_membros(guild, 7)
            await canal.send(f"**Relatório semanal automático  -  semana encerrada**\n```\n{rel[:1800]}\n```")
            log.info("[SEMANAL] Relatório postado.")
        except Exception as e:
            log.error(f"[SEMANAL] Falha: {e}")


async def _task_iniciativa_proativa():
    """
    Task em background: retoma threads abertas de forma orgânica.
    A cada 30 minutos, verifica se há algum usuário com conversa recente incompleta
    e, com critério estrito, posta uma continuação natural no canal.
    """
    await client.wait_until_ready()
    await asyncio.sleep(300)  # espera 5min após iniciar antes de começar
    while not client.is_closed():
        await asyncio.sleep(1800)  # verifica a cada 30 minutos
        if not GROQ_API_KEY:
            continue
        try:
            guild = client.get_guild(SERVIDOR_ID)
            if not guild:
                continue

            agora = agora_utc()
            for user_id, estado in list(conversas_groq.items()):
                canal_id = estado.get("canal")
                ultima = estado.get("ultima")
                if not canal_id or not ultima:
                    continue

                # Só retoma conversas entre 20min e 4h atrás (janela de retomada natural)
                delta = agora - ultima
                if not (timedelta(minutes=20) <= delta <= timedelta(hours=4)):
                    continue

                # Verifica cooldown por canal (max 1 iniciativa por canal a cada 2h)
                ultimo_post = _ultima_iniciativa.get(canal_id)
                if ultimo_post and (agora - ultimo_post) < timedelta(hours=2):
                    continue

                # Pega histórico recente
                historico = list(historico_groq.get((user_id, canal_id), []))
                if not historico or len(historico) < 2:
                    continue

                # Monta resumo das últimas trocas
                ultimas = historico[-4:]
                resumo = "\n".join(
                    f"{'Usuário' if m['role'] == 'user' else 'Bot'}: {m['content'][:150]}"
                    for m in ultimas
                )

                # Pede à IA se há algo genuinamente novo a dizer
                r = await _groq_client().chat.completions.create(
                    model="llama-3.1-8b-instant",
                    max_tokens=60,
                    temperature=0.85,
                    messages=[
                        {"role": "system", "content":
                         "Você é um bot de Discord. Às vezes volta a uma conversa com algo genuinamente útil ou interessante. "
                         "CRITÉRIO RÍGIDO: só continue se tiver algo NOVO e CONCRETO a acrescentar — um link, uma informação relevante, "
                         "uma reflexão que muda algo. Não repita o que já foi dito. Não force. "
                         "Se não há nada novo, responda exatamente: SILÊNCIO. "
                         "Se há algo novo, responda diretamente (1-2 frases), sem saudação nem 'aliás' ou 'a propósito'."},
                        {"role": "user", "content":
                         f"Conversa de {delta.seconds // 60} minutos atrás:\n{resumo}\n\n"
                         "Há algo genuinamente novo a acrescentar?"},
                    ],
                )
                txt = r.choices[0].message.content.strip()
                _registrar_tokens("8b", r.usage.total_tokens if r.usage else 60)

                if txt and txt.upper() != "SILÊNCIO" and "SILÊNCIO" not in txt.upper():
                    canal = guild.get_channel(canal_id)
                    if canal:
                        await canal.send(txt)
                        _ultima_iniciativa[canal_id] = agora
                        log.info(f"[INICIATIVA] postou em #{canal.name}: {txt[:60]}")

        except Exception as e:
            log.debug(f"[INICIATIVA] erro: {e}")


# ── Suporte a canais de voz ───────────────────────────────────────────────────

async def _entrar_canal_voz(guild: discord.Guild, nome_canal: str | None = None) -> discord.VoiceChannel | None:
    """Encontra e entra em um canal de voz. Retorna o canal ou None."""
    canais = [c for c in guild.channels if isinstance(c, discord.VoiceChannel)]
    if not canais:
        return None
    if nome_canal:
        canal = next(
            (c for c in canais if nome_canal.lower() in c.name.lower()),
            None
        )
    else:
        # Canal com mais membros ou primeiro disponível
        canal = max(canais, key=lambda c: len(c.members)) if canais else None
    return canal


async def _conectar_voz(canal: discord.VoiceChannel) -> tuple:
    """
    Tenta conectar ao canal de voz com timeout de 5s.
    Retorna (VoiceClient, None) em sucesso ou (None, mensagem_erro) em falha.

    A lib discord.py-self possui loop de retry interno em channel.connect() que
    ignora o código 4017 e tenta reconectar indefinidamente. Para evitar isso,
    envolvemos a chamada em asyncio.wait_for com timeout curto: na 1ª falha por
    E2EE o timeout expira, cancelamos a task e retornamos imediatamente.
    """
    _MSG_E2EE = (
        f"Não consigo entrar em {canal.mention}: o canal usa criptografia E2EE (DAVE), "
        f"que não é suportada por bots. Desative o E2EE nas configurações do canal e tente novamente."
    )

    # Monitora o gateway para detectar 4017 antes do retry da lib
    _e2ee_detectado = False
    _orig_dispatch = client.dispatch

    def _patch_dispatch(event, *args, **kwargs):
        nonlocal _e2ee_detectado
        # discord.py-self dispara 'socket_raw_receive' ou loga internamente;
        # capturamos via on_error do voice se necessário — mas o timeout basta.
        _orig_dispatch(event, *args, **kwargs)

    try:
        vc = await asyncio.wait_for(canal.connect(), timeout=5.0)
        return vc, None
    except asyncio.TimeoutError:
        # Força desconexão se ficou preso tentando
        try:
            if canal.guild.voice_client:
                await canal.guild.voice_client.disconnect(force=True)
        except Exception:
            pass
        # Timeout após 5s = quase certamente E2EE (1ª tentativa leva ~2s e falha)
        log.warning(f"[VOZ] Timeout ao conectar em #{canal.name} — provável E2EE/DAVE (4017).")
        return None, _MSG_E2EE
    except discord.errors.ConnectionClosed as e:
        if getattr(e, 'code', None) == 4017 or "4017" in str(e) or "E2EE" in str(e):
            log.warning(f"[VOZ] E2EE/DAVE bloqueou conexão em #{canal.name} (4017).")
            return None, _MSG_E2EE
        return None, f"Erro de conexão: {e}"
    except Exception as e:
        return None, f"Erro ao conectar: {e}"


@client.event
async def on_ready():
    global _humor_sessao
    carregar_config()
    carregar_dados()
    print(f"Conectado como {client.user}")
    guild = client.get_guild(SERVIDOR_ID)
    if guild:
        pode = tem_permissao_moderacao(guild)
        log.info(f"Servidor: {guild.name} | moderação: {'sim' if pode else 'apenas avisos'}")
        _atualizar_contexto(guild)
        log.info(f"Contexto mapeado: {len(guild.channels)} canais, {len(guild.roles)} cargos, {guild.member_count} membros.")
    else:
        log.error(f"Servidor {SERVIDOR_ID} não encontrado.")

    # Gera humor da sessão via IA (persiste até próximo reinício)
    if GROQ_API_KEY:
        try:
            hora = datetime.now().hour
            periodo = "madrugada" if hora < 6 else "manhã" if hora < 12 else "tarde" if hora < 18 else "noite"
            r = await _groq_client().chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=20,
                temperature=1.1,
                messages=[
                    {"role": "system", "content":
                     "Você define o humor de um bot de Discord para a sessão. "
                     "Responda com UMA expressão curta e informal (3-6 palavras) que descreva o estado de espírito. "
                     "Exemplos: 'levemente impaciente com tudo', 'animado e curioso', 'calmo e direto', "
                     "'com vontade de debater', 'entediado mas prestativo'. Sem explicações."},
                    {"role": "user", "content": f"É {periodo}, gere o humor da sessão de hoje."},
                ],
            )
            _humor_sessao = r.choices[0].message.content.strip().strip('"').strip("'")
            log.info(f"[HUMOR] Sessão: {_humor_sessao}")
        except Exception as e:
            log.debug(f"[HUMOR] falha ao gerar: {e}")

    asyncio.ensure_future(_task_relatorio_semanal())
    asyncio.ensure_future(_task_iniciativa_proativa())
    asyncio.ensure_future(_task_accountability_equipe())


@client.event
async def on_guild_channel_create(channel):
    if channel.guild.id == SERVIDOR_ID:
        _atualizar_contexto(channel.guild)


@client.event
async def on_guild_channel_delete(channel):
    if channel.guild.id == SERVIDOR_ID:
        _atualizar_contexto(channel.guild)


@client.event
async def on_guild_role_create(role):
    if role.guild.id == SERVIDOR_ID:
        _atualizar_contexto(role.guild)


@client.event
async def on_guild_role_delete(role):
    if role.guild.id == SERVIDOR_ID:
        _atualizar_contexto(role.guild)


@client.event
async def on_member_join(member: discord.Member):
    """Registra entrada de membro e loga no canal de auditoria. Detecta raids."""
    if member.guild.id != SERVIDOR_ID:
        return

    agora = agora_utc()
    ts = agora.isoformat()

    # ── Detecção de raid: 5+ entradas em 30 segundos ─────────────────────────
    _joins_recentes.append(agora)
    _joins_recentes[:] = [t for t in _joins_recentes if agora - t < timedelta(seconds=30)]
    if len(_joins_recentes) >= 5 and not _lockdown_ativo:
        log.warning(f"[RAID] {len(_joins_recentes)} entradas em 30s — ativando lockdown")
        asyncio.ensure_future(_ativar_lockdown(member.guild, f"raid: {len(_joins_recentes)} entradas em 30s"))

    if member.id not in registro_entradas:
        registro_entradas[member.id] = []
    registro_entradas[member.id].append(ts)
    nomes_historico[member.id] = member.display_name
    salvar_dados()

    idade_conta = agora - member.created_at.replace(tzinfo=timezone.utc)
    conta_nova = idade_conta.days < 7
    vezes = len(registro_entradas[member.id])

    canal_audit = member.guild.get_channel(_canal_auditoria_id())
    if canal_audit:
        aviso = " | CONTA NOVA" if conta_nova else ""
        reentrada = f" | Reentrada n.{vezes}" if vezes > 1 else ""
        await canal_audit.send(
            f"[ENTRADA]{aviso}{reentrada} {member.display_name} ({member.id}) "
            f"entrou. Conta criada há {formatar_duracao(idade_conta)}."
        )

    # ── Boas-vindas automática no canal geral ─────────────────────────────────
    canal_geral = discord.utils.get(member.guild.text_channels, name="geral") \
               or discord.utils.get(member.guild.text_channels, name="chat") \
               or discord.utils.get(member.guild.text_channels, name="testes") \
               or member.guild.system_channel
    if canal_geral:
        if vezes > 1:
            bv = f"Eae {member.mention}, voltou por aqui. Seja bem-vindo de volta."
        elif conta_nova:
            bv = f"Eae {member.mention}, conta nova em folha. Leia as regras em {CANAL_REGRAS()} antes de qualquer coisa."
        else:
            bv = f"Eae {member.mention}, bem-vindo ao servidor. Leia as regras em {CANAL_REGRAS()} e aproveita."
        try:
            await canal_geral.send(bv)
        except Exception:
            pass

        # Comentário orgânico espontâneo após boas-vindas (simula membro notando a entrada)
        if GROQ_API_KEY and not conta_nova and random.random() < 0.35:
            try:
                contexto_entrada = (
                    f"reentrada número {vezes}" if vezes > 1
                    else f"primeira entrada, conta com {idade_conta.days} dias"
                )
                r = await _groq_client().chat.completions.create(
                    model="llama-3.1-8b-instant",
                    max_tokens=25,
                    temperature=0.95,
                    messages=[
                        {"role": "system", "content":
                         "Você é um membro veterano de um servidor Discord brasileiro. "
                         "Faça UM comentário curto e casual sobre a entrada de alguém (1 frase). "
                         "Pode ser irônico, receptivo ou neutro — como um membro real faria. "
                         "Sem emojis. Sem 'bem-vindo' repetido. Sem clichê."},
                        {"role": "user", "content":
                         f"{member.display_name} entrou no servidor ({contexto_entrada})."},
                    ],
                )
                txt = r.choices[0].message.content.strip()
                if txt:
                    await asyncio.sleep(random.uniform(8, 25))
                    await canal_geral.send(txt)
            except Exception:
                pass

    # Atualiza contexto do servidor
    _atualizar_contexto(member.guild)
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
        canal_audit = member.guild.get_channel(_canal_auditoria_id())
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

    canal_audit = member.guild.get_channel(_canal_auditoria_id())
    if canal_audit:
        await canal_audit.send(
            f"[SAÍDA] {member.display_name} ({member.id}) saiu. "
            f"Ficou por {ficou_txt}."
        )

    _atualizar_contexto(member.guild)
    log.info(f"Saída: {member.display_name} ({member.id}) | ficou: {ficou_txt}")

    # Reação orgânica à saída (comentário espontâneo no canal geral)
    if GROQ_API_KEY and ficou_segundos and ficou_segundos > 3600:  # só se ficou mais de 1h
        try:
            canal_geral = discord.utils.get(member.guild.text_channels, name="geral") \
                       or discord.utils.get(member.guild.text_channels, name="chat") \
                       or member.guild.system_channel
            if canal_geral and random.random() < 0.25:  # 25% de chance — não toda saída
                r = await _groq_client().chat.completions.create(
                    model="llama-3.1-8b-instant",
                    max_tokens=30,
                    temperature=0.9,
                    messages=[
                        {"role": "system", "content":
                         "Você é um bot de Discord. Um membro acabou de sair do servidor. "
                         "Faça UM comentário curto e casual (max 1 frase), como se fosse um membro do servidor notando. "
                         "Pode ser neutro, levemente irônico ou simplesmente observar. Sem emojis. Sem 'tchau' ou despedidas formais."},
                        {"role": "user", "content":
                         f"{member.display_name} saiu do servidor depois de {ficou_txt}."},
                    ],
                )
                txt = r.choices[0].message.content.strip()
                if txt:
                    await asyncio.sleep(random.uniform(5, 30))  # delay orgânico
                    await canal_geral.send(txt)
        except Exception:
            pass


@client.event
async def on_member_update(before: discord.Member, after: discord.Member):
    """Reage organicamente a mudanças de cargo e apelido."""
    if after.guild.id != SERVIDOR_ID:
        return
    if after == client.user:
        return

    guild = after.guild
    canal_geral = (
        discord.utils.get(guild.text_channels, name="geral")
        or discord.utils.get(guild.text_channels, name="chat")
        or guild.system_channel
    )
    if not canal_geral or not GROQ_API_KEY:
        return

    # ── Mudança de cargo ───────────────────────────────────────────────────────
    cargos_antes = set(before.roles)
    cargos_depois = set(after.roles)
    ganhou = cargos_depois - cargos_antes
    perdeu = cargos_antes - cargos_depois

    for cargo in ganhou:
        if cargo.is_default():
            continue
        # Não comenta em cargos internos de bot/sistema
        if cargo.managed:
            continue
        if random.random() > 0.45:
            continue
        try:
            r = await _groq_client().chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=30,
                temperature=0.9,
                messages=[
                    {"role": "system", "content":
                     "Você é um membro veterano de Discord. Comente brevemente (1 frase) "
                     "sobre um membro receber um novo cargo. Pode ser receptivo, irônico ou neutro. "
                     "Sem emojis. Sem parabéns forçado."},
                    {"role": "user", "content":
                     f"{after.display_name} recebeu o cargo '{cargo.name}'."},
                ],
            )
            _registrar_tokens("8b", r.usage.total_tokens if r.usage else 30)
            txt = r.choices[0].message.content.strip()
            if txt:
                await asyncio.sleep(random.uniform(4, 15))
                await canal_geral.send(txt)
        except Exception:
            pass
        break  # comenta só o primeiro cargo ganho

    # ── Mudança de apelido ─────────────────────────────────────────────────────
    if before.display_name != after.display_name and random.random() < 0.3:
        try:
            r = await _groq_client().chat.completions.create(
                model="llama-3.1-8b-instant",
                max_tokens=25,
                temperature=0.9,
                messages=[
                    {"role": "system", "content":
                     "Você é um membro de Discord. Comente em 1 frase curta sobre alguém ter mudado o apelido. "
                     "Seja casual, pode ser levemente irônico ou apenas observar. Sem emojis."},
                    {"role": "user", "content":
                     f"{before.display_name} virou {after.display_name}."},
                ],
            )
            _registrar_tokens("8b", r.usage.total_tokens if r.usage else 25)
            txt = r.choices[0].message.content.strip()
            if txt:
                await asyncio.sleep(random.uniform(3, 12))
                await canal_geral.send(txt)
        except Exception:
            pass


# Palavras-chave ofensivas em nomes de emoji customizado do servidor
# (emojis Unicode são ambíguos demais para filtrar  -  muitos usos legítimos)
NOMES_EMOJI_OFENSIVOS = [
    "nigger", "crioulo",
    "viado", "bicha",
    "retardado",
    "nazi", "hitler", "kkk",
]


@client.event
async def on_voice_state_update(member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
    """Monitora entradas e saidas de canais de voz."""
    if not member.guild or member.guild.id != SERVIDOR_ID:
        return
    if member == client.user:
        return

    # Notifica no canal de auditoria quando membros entram/saem
    canal_audit = member.guild.get_channel(_canal_auditoria_id())
    if not canal_audit:
        return

    entrou = after.channel and (not before.channel or before.channel.id != after.channel.id)
    saiu = before.channel and (not after.channel or before.channel.id != after.channel.id)

    if entrou:
        log.info(f"[VOZ] {member.display_name} entrou em #{after.channel.name}")
    elif saiu:
        log.info(f"[VOZ] {member.display_name} saiu de #{before.channel.name}")


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

    # Só filtra emojis customizados  -  Unicode tem muitos usos legítimos
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
                    f"Leia as regras em {CANAL_REGRAS()}."
                )
            except Exception:
                pass
            infracoes[membro.id] += 1
            salvar_dados()
            log.info(f"Reação removida: {membro.display_name}: {emoji.name}")
            break


@client.event
async def on_message(message: discord.Message):
    try:
        # ── DMs: tratar denúncias anônimas ───────────────────────────────────
        if not message.guild and message.author != client.user:
            await _on_dm_denuncia(message)
            return
        await _on_message_impl(message)
    except Exception as e:
        log.error(f"Erro não tratado em on_message: {e}", exc_info=True)


async def _on_dm_denuncia(message: discord.Message) -> None:
    """DMs: redireciona para o servidor onde o canal privado é criado."""
    await message.channel.send(
        "Para denúncias, me menciona no servidor: @Shell quero denunciar [nome].\n"
        "Para denúncia anônima: @Shell reporte anônimo.\n"
        "Vou criar um canal privado só para você."
    )


async def _on_message_impl(message: discord.Message):
    if message.author == client.user:
        return

    if not message.guild or message.guild.id != SERVIDOR_ID:
        return

    # ── Captura respostas de outros bots para memória de contexto ───────────────
    # Erros de permissão, rejeições de comandos, etc. precisam estar no contexto
    # para que o bot entenda que sua ação anterior falhou e não repita.
    if message.author.bot and message.author != client.user:
        _txt_bot = (message.content or "").strip()
        if _txt_bot and len(_txt_bot) < 400:
            # Detecta se é uma resposta de erro/rejeição (indicadores comuns)
            _e_erro_bot = bool(re.search(
                r'(não tem permiss|sem permiss|não podes|você não pode|invalid|erro|error'
                r'|❌|⛔|🚫|não autorizado|permissão necessária|permission)',
                _txt_bot, re.IGNORECASE
            ))
            # Só armazena erros ou respostas curtas relevantes (evita flood de bot)
            if _e_erro_bot:
                canal_memoria[message.channel.id].append({
                    "autor": f"[BOT:{message.author.display_name}]",
                    "conteudo": f"ERRO/REJEIÇÃO: {_txt_bot[:200]}",
                })
                log.debug(f"[BOT] erro de {message.author.display_name} capturado: {_txt_bot[:60]}")
        return  # bots não passam pelo fluxo principal

    # Ignorar mensagens de outros bots com prefixo (ex: 7!afk, !cmd, /cmd)
    # Só ignora se começar com prefixo e não mencionar este bot
    conteudo_raw = message.content
    if re.match(r'^\s*\S+[!/]\S', conteudo_raw) and client.user not in message.mentions:
        return

    # ── Wizard de denúncia: prioridade máxima para manter o fluxo ────────────
    if not message.author.bot and message.author.id in _denuncia_wizard:
        consumido = await _processar_denuncia_wizard(message)
        if consumido:
            return

    # ── Gatilho de denúncia: cria canal privado e inicia coleta ─────────────
    if (not message.author.bot
            and client.user in message.mentions
            and _GATILHO_DENUNCIA.search(message.content)):
        anonimo = bool(_GATILHO_ANONIMO.search(message.content))
        await _iniciar_denuncia(message, anonimo)
        return

    # ── Detecção de mention bombing (ataque por menções em massa) ────────────
    if (not message.author.bot
            and not eh_autorizado(message.author)
            and len(message.mentions) >= 6):
        log.warning(f"[ATAQUE] Mention bombing por {message.author.display_name}: {len(message.mentions)} menções")
        try:
            await message.delete()
        except Exception:
            pass
        await message.channel.send(
            f"{message.author.mention}, menções em massa não são permitidas. "
            f"Próxima ocorrência resulta em punição."
        )
        canal_audit = message.guild.get_channel(_canal_auditoria_id())
        if canal_audit:
            try:
                await canal_audit.send(
                    f"[ATAQUE] Mention bombing detectado: {message.author.display_name} "
                    f"({message.author.id}) — {len(message.mentions)} menções em #{message.channel.name}"
                )
            except Exception:
                pass
        return

    # ── Lockdown manual por proprietário/colaborador ──────────────────────────
    if (not message.author.bot
            and eh_autorizado(message.author)
            and re.search(r'\b(ativar|ligar|iniciar)\s+lockdown\b|\blockdown\s+(on|ativo|ativa)\b',
                          message.content, re.IGNORECASE)):
        asyncio.ensure_future(_ativar_lockdown(message.guild, f"ativado manualmente por {message.author.display_name}"))
        return

    if (not message.author.bot
            and eh_autorizado(message.author)
            and re.search(r'\b(desativar|desligar|encerrar)\s+lockdown\b|\blockdown\s+(off|inativo)\b',
                          message.content, re.IGNORECASE)):
        asyncio.ensure_future(_desativar_lockdown(message.guild))
        return

    # ── Áudio inline: transcreve mensagens de voz antes de tudo ─────────────────
    # Mensagens de voz chegam com content=''. Sem transcrever antes, o bot nunca
    # detecta gatilhos nem processa ordens ditas no áudio.
    _conteudo_base = message.content
    _transcricao_audio_inline: str = ""
    _tom_audio: dict = {}        # tom detectado para injetar no contexto da resposta
    _att_audio_inline = None
    if not message.author.bot and not _conteudo_base.strip() and message.attachments:
        _att_audio_inline = next(
            (a for a in message.attachments
             if (a.content_type or "").startswith("audio/")
             or (a.filename or "").endswith((".mp3", ".ogg", ".wav", ".webm", ".m4a", ".flac", ".opus"))),
            None,
        )
        if _att_audio_inline:
            if _att_audio_inline.size <= _GROQ_AUDIO_MAX_BYTES:
                _transcricao_audio_inline = await _transcrever_audio(
                    _att_audio_inline.url, _att_audio_inline.filename or ""
                )
                if _transcricao_audio_inline:
                    _conteudo_base = _transcricao_audio_inline
                    log.info(f"[AUDIO] inline de {message.author.display_name}: {_conteudo_base[:80]!r}")
                    # Analisa tom em paralelo — não bloqueia o fluxo principal
                    _tom_audio = await _analisar_tom_audio(_transcricao_audio_inline, message.author.id)
                    if _tom_audio:
                        _tom_audio_pendente[message.author.id] = _tom_audio
                        log.info(
                            f"[AUDIO] tom={_tom_audio.get('tom')} ritmo={_tom_audio.get('ritmo')} "
                            f"intensidade={_tom_audio.get('intensidade')} "
                            f"padrão={_tom_audio.get('padrao') or 'variado'}"
                        )
            else:
                log.warning(
                    f"[AUDIO] arquivo de {message.author.display_name} grande demais "
                    f"({_att_audio_inline.size // 1024 // 1024}MB), ignorado"
                )

    # ── Mídia passiva: processa imagens/vídeos/áudios em canais monitorados ────────
    if (not message.author.bot
            and message.attachments
            and message.channel.id in canais_monitorados):
        _audios_passivos = [
            a for a in message.attachments
            if (a.content_type or "").startswith("audio/")
            or (a.filename or "").endswith((".mp3", ".ogg", ".wav", ".webm", ".m4a", ".opus"))
        ]
        _imagens_passivas = [
            a for a in message.attachments
            if (a.content_type or "").startswith("image/")
            or (a.content_type or "").startswith("video/")
            or (a.filename or "").lower().endswith((".png", ".jpg", ".jpeg", ".gif", ".webp", ".mp4", ".mov"))
        ]

        if _audios_passivos:
            # Reusa transcrição inline se já feita para o mesmo arquivo
            if _transcricao_audio_inline and _audios_passivos[0].url == getattr(_att_audio_inline, "url", None):
                _transcricao_passiva = _transcricao_audio_inline
            else:
                _transcricao_passiva = await _transcrever_audio(_audios_passivos[0].url, _audios_passivos[0].filename)
            if _transcricao_passiva:
                canal_memoria[message.channel.id].append({
                    "autor": message.author.display_name,
                    "conteudo": f"[áudio]: {_transcricao_passiva[:300]}",
                })
                log.info(f"[AUDIO] transcrição passiva em #{message.channel.name}: {_transcricao_passiva[:60]}")

        if _imagens_passivas:
            # Descreve a imagem/vídeo e reage autonomamente
            _att_img = _imagens_passivas[0]
            _ct_img = (_att_img.content_type or "").lower()
            if _ct_img.startswith("image/"):
                _desc_img = await _descrever_imagem(_att_img.url, message.content or "")
            else:
                _desc_img = f"[Vídeo: {_att_img.filename}, {_att_img.size // 1024}KB]"
            if _desc_img:
                canal_memoria[message.channel.id].append({
                    "autor": message.author.display_name,
                    "conteudo": f"[imagem/vídeo enviado]: {_desc_img[:200]}",
                })
                # Dispara reação autônoma em background
                asyncio.ensure_future(_reagir_midia_autonoma(message, _desc_img))

    # ── Memória do canal: registra toda mensagem humana ──────────────────────
    # Permite ao bot entender o contexto da conversa sem intervenção manual
    if not message.author.bot and (message.content.strip() or message.attachments):
        _conteudo_mem = message.content[:300]
        if message.attachments:
            _nomes_mem = ", ".join(a.filename for a in message.attachments[:3])
            _conteudo_mem = (_conteudo_mem + f" [anexo: {_nomes_mem}]").strip() if _conteudo_mem else f"[anexo: {_nomes_mem}]"
        canal_memoria[message.channel.id].append({
            "autor": message.author.display_name,
            "conteudo": _conteudo_mem,
        })

    # ── Rastrear atividade + participação ativa (debate / monitoramento) ──────
    if not message.author.bot and message.content.strip() and not _TRIVIAIS.match(message.content.strip()):
        atividade_mensagens[message.author.id] += 1
        _canal_id = message.channel.id
        _agora = agora_utc()
        _debate = debates_ativos.get(_canal_id)
        if _debate and _agora < _debate["fim"]:
            _debate["msgs"] = _debate.get("msgs", 0) + 1
            _ultima = ultima_interjeccao.get(_canal_id, datetime(1970, 1, 1, tzinfo=timezone.utc))
            # Debate ativo: cooldown de 60s, entra após ≥2 msgs
            if _debate["msgs"] >= 2 and (_agora - _ultima).total_seconds() > 60:
                ultima_interjeccao[_canal_id] = _agora
                asyncio.ensure_future(_participar_debate(message, _debate["tema"]))
        elif _canal_id in canais_monitorados:
            _ultima = ultima_interjeccao.get(_canal_id, datetime(1970, 1, 1, tzinfo=timezone.utc))
            _secs = (_agora - _ultima).total_seconds()
            # Chance dinâmica: quanto mais tempo sem falar, maior a chance de entrar
            # 3min cooldown mínimo; chance sobe de 12% até 35% com o tempo
            _chance = min(0.35, 0.12 + (_secs - 180) / 1800 * 0.23) if _secs > 180 else 0
            if _chance > 0 and random.random() < _chance:
                ultima_interjeccao[_canal_id] = _agora
                asyncio.ensure_future(_interjetar_conversa(message))

    autor = message.author.display_name
    user_id = message.author.id
    # _conteudo_base já foi transcrito do áudio inline no bloco acima, se necessário
    conteudo = _conteudo_base

    _eh_dono = message.author.id in DONOS_IDS
    _eh_superior_ = eh_superior(message.author)   # donos + cargos superiores
    _eh_mod_ = eh_mod_exclusivo(message.author)    # só moderação (não superiores)
    eh_teste = message.author.id in _contas_teste_ids()

    # ── Filtro de mensagens triviais ──────────────────────────────────────────
    # "kkk", ".", emojis soltos, "ok" — não processam IA mas ainda coletam na memória
    _conteudo_limpo = conteudo.strip()
    _e_trivial = bool(_TRIVIAIS.match(_conteudo_limpo)) and client.user not in message.mentions

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

    _gatilho_nome = (
        bool(GATILHOS_NOME.search(conteudo))
        and not _GATILHO_EXCLUIDO.search(conteudo)
        and not _GATILHO_NEGATIVO.search(conteudo)
    )
    mencionado = (
        client.user in message.mentions
        or client.user.id in ids_mencionados
        or _gatilho_nome
        or eh_resposta_ao_bot
    )

    # ── Visão: processar anexos quando o bot é acionado ──────────────────────
    # Verifica anexos na mensagem atual E na mensagem referenciada (reply)
    _ref_resolvida = (
        getattr(message.reference, "resolved", None)
        if message.reference else None
    )
    _tem_anexo = bool(
        message.attachments
        or (isinstance(_ref_resolvida, discord.Message) and _ref_resolvida.attachments)
    )
    if mencionado and not _e_trivial and _tem_anexo:
        # Detecta se a mensagem é puramente de voz (sem texto original)
        _eh_mensagem_voz_pura = bool(_transcricao_audio_inline and not message.content.strip())
        _desc_visual = await _processar_anexos_visuais(message, conteudo_real=conteudo)
        if _desc_visual:
            if _eh_mensagem_voz_pura:
                # Áudio puro: conteudo JÁ É a transcrição.
                # Se _desc_visual contém apenas a re-transcrição do mesmo áudio, descarta.
                # Se contém descrição de imagem referenciada (reply), agrega.
                _so_audio = all(
                    "[Áudio" in linha
                    for linha in _desc_visual.strip().splitlines() if linha.strip()
                )
                if not _so_audio:
                    # Há descrição de imagem/outro: agrega ao conteudo
                    conteudo = (conteudo + "\n" + _desc_visual).strip()
                # else: só repetição de áudio, ignora
            else:
                # Mensagem com texto + anexo (imagem, arquivo, etc.): agrega descrição
                conteudo = (conteudo + "\n" + _desc_visual).strip() if conteudo.strip() else _desc_visual
            _n_total = len(message.attachments) + (len(_ref_resolvida.attachments) if isinstance(_ref_resolvida, discord.Message) else 0)
            log.info(f"[VISAO] {_n_total} anexo(s) processado(s) para {autor}")

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
        log.info(f"[DONO] {autor} | mencionado={mencionado} | conteudo={conteudo[:80]!r}")
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send(f"{message.author.mention}, modo ausente desativado.")
        if await _processar_wizard(message):
            return
        if await _verificar_confirmacao_pendente(message):
            return
        tratado = await processar_ordem(message)
        log.info(f"[DONO] processar_ordem retornou {tratado}")
        if not tratado and mencionado and not _e_trivial:
            _tp, _tt = await _iniciar_typing_antes(message.channel)
            try:
                # Só interpreta como instrução quando há intenção clara de ação
                if _tem_intencao_de_acao(conteudo):
                    intencao_ia = await _ia_parsear_instrucao(conteudo, message.guild)
                    if intencao_ia:
                        log.info(f"[DONO] IA interpretou: {intencao_ia.get('acao')}")
                        await _parar_typing(_tp, _tt)
                        tratado_ia = await _ia_executar(intencao_ia, message, message.guild)
                        if tratado_ia:
                            return
                        _tp, _tt = await _iniciar_typing_antes(message.channel)
                # Continua conversa ativa antes de cair em resposta_inicial_superior
                estado_conv = conversas.get(user_id)
                if estado_conv and (estado_conv.get("canal") is None or estado_conv["canal"] == message.channel.id):
                    resp_conv = await continuar_conversa(user_id, conteudo, autor, message.guild)
                    if resp_conv:
                        await _parar_typing(_tp, _tt)
                        await _reagir_ou_responder(message, resp_conv)
                        asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resp_conv))
                        return
                log.info(f"[DONO] chamando resposta_inicial_superior para {autor}")
                resposta = await resposta_inicial_superior(conteudo, autor, user_id, message.guild, message.author, message.channel.id, message)
                log.info(f"[DONO] resposta obtida ({len(resposta)} chars): {resposta[:60]!r}")
            finally:
                await _parar_typing(_tp, _tt)
            if resposta and not _e_resposta_generica(resposta):
                await _reagir_ou_responder(message, resposta)
                asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resposta))
            elif resposta:
                await _digitar_e_enviar(message.channel, resposta, message)
            else:
                log.warning(f"[DONO] resposta vazia  -  enviando fallback")
                await _digitar_e_enviar(message.channel, "Entendido.", message)
        elif not tratado:
            await processar_links(message)
        return

    # ── Superiores: isentos de punição, comandos + ordens gerais (sem precisar mencionar) ──
    if _eh_superior_:
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send(f"{message.author.mention}, modo ausente desativado.")
        if await _processar_wizard(message):
            return
        if await _verificar_confirmacao_pendente(message):
            return
        tratado = await processar_ordem(message)
        if not tratado and mencionado and not _e_trivial:
            _tp, _tt = await _iniciar_typing_antes(message.channel)
            try:
                if _tem_intencao_de_acao(conteudo):
                    intencao_ia = await _ia_parsear_instrucao(conteudo, message.guild)
                    if intencao_ia:
                        log.info(f"[SUP] IA interpretou: {intencao_ia.get('acao')}")
                        await _parar_typing(_tp, _tt)
                        tratado_ia = await _ia_executar(intencao_ia, message, message.guild)
                        if tratado_ia:
                            return
                        _tp, _tt = await _iniciar_typing_antes(message.channel)
                estado_conv = conversas.get(user_id)
                if estado_conv and (estado_conv.get("canal") is None or estado_conv["canal"] == message.channel.id):
                    resp_conv = await continuar_conversa(user_id, conteudo, autor, message.guild)
                    if resp_conv:
                        await _parar_typing(_tp, _tt)
                        await _reagir_ou_responder(message, resp_conv)
                        asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resp_conv))
                        return
                resposta = await resposta_inicial_superior(conteudo, autor, user_id, message.guild, message.author, message.channel.id, message)
            finally:
                await _parar_typing(_tp, _tt)
            if resposta and not _e_resposta_generica(resposta):
                await _reagir_ou_responder(message, resposta)
                asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resposta))
            elif resposta:
                await _digitar_e_enviar(message.channel, resposta, message)
        elif not tratado:
            await processar_links(message)
        return

    # ── Equipe de mod: isenta de punições, comandos de moderação (sem precisar mencionar) ──
    if _eh_mod_:
        tratado = await processar_ordem_mod(message)
        if not tratado and mencionado and not _e_trivial:
            _tp, _tt = await _iniciar_typing_antes(message.channel)
            try:
                resposta = await resposta_inicial(conteudo, autor, user_id, message.guild, message.author, message.channel.id)
            finally:
                await _parar_typing(_tp, _tt)
            if resposta and not _e_resposta_generica(resposta):
                await _reagir_ou_responder(message, resposta)
                asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resposta))
            elif resposta:
                await _digitar_e_enviar(message.channel, resposta, message)
        return  # mods nunca são punidos

    # ── Detectar flood (membros comuns) ───────────────────────────────────────
    if detectar_flood(message.author.id, conteudo):
        # Resposta instantânea sem chamar Groq — flood exige reação imediata
        txt_flood = random.choice(_AVISOS_FLOOD).format(m=message.author.mention)
        await message.channel.send(txt_flood)
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
                await silenciar(message.author, message.channel, "discriminação  -  tolerância zero")
            else:
                txt_disc = await _aviso_infrator(message.author.mention, "discriminação ou racismo — tolerância zero")
                await message.channel.send(txt_disc)
            return

        if count >= 3:
            if tem_permissao_moderacao(message.guild) and hasattr(message.author, 'timeout'):
                await silenciar(message.author, message.channel, "3 infrações")
            else:
                txt_lim = await _aviso_infrator(message.author.mention, "3 infrações acumuladas")
                await message.channel.send(txt_lim)
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
                corpo = f"por se referir de {' e '.join(itens)}, conforme os termos em {CANAL_REGRAS()}"

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
            await _digitar_e_enviar(message.channel, texto, message)
            return

    # ── Queries factuais do servidor (cargos, membros por cargo, etc.) ────────
    if mencionado and message.guild:
        resp_direta = await query_servidor_direto(message.guild, message.content)
        if resp_direta:
            await _digitar_e_enviar(message.channel, resp_direta, message)
            return

    # ── Continuar conversa em andamento (mesmo canal e sem @menção nova) ────────
    estado_conv = conversas.get(user_id)
    if estado_conv and client.user not in message.mentions and not _e_trivial:
        canal_conv = estado_conv.get("canal")
        if canal_conv is None or canal_conv == message.channel.id:
            _tp, _tt = await _iniciar_typing_antes(message.channel)
            try:
                resposta = await continuar_conversa(user_id, conteudo, autor, message.guild)
            finally:
                await _parar_typing(_tp, _tt)
            if resposta:
                log.info(f"Conversa: {autor}: {conteudo}")
                await _reagir_ou_responder(message, resposta)
                asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resposta))
                return
        else:
            del conversas[user_id]

    # ── Continuar conversa Claude ativa ──────────────────────────────────────
    estado_groq = conversas_groq.get(user_id)
    if estado_groq and client.user not in message.mentions and not GATILHOS_NOME.search(conteudo) and not _e_trivial:
        if estado_groq["canal"] == message.channel.id:
            tempo_ocioso = agora_utc() - estado_groq["ultima"]
            if tempo_ocioso <= TIMEOUT_CONVERSA_GROQ:
                # Queries factuais respondem direto sem IA
                if message.guild:
                    resp_direta = await query_servidor_direto(message.guild, message.content)
                    if resp_direta:
                        await message.reply(resp_direta)
                        return
                _tp, _tt = await _iniciar_typing_antes(message.channel)
                try:
                    resposta = await responder_com_groq(conteudo, autor, user_id, message.guild, message.channel.id)
                finally:
                    await _parar_typing(_tp, _tt)
                log.info(f"Claude cont: {autor}: {conteudo}")
                await _reagir_ou_responder(message, resposta)
                asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resposta))
                return
            else:
                # Conversa expirou  -  limpa histórico do canal para evitar drift
                historico_groq.pop((user_id, estado_groq["canal"]), None)
                del conversas_groq[user_id]
        else:
            # Mudou de canal  -  limpa histórico do canal anterior
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

        _tp, _tt = await _iniciar_typing_antes(message.channel)
        try:
            resposta = await resposta_inicial(conteudo, autor, user_id, message.guild, message.author, message.channel.id)
        finally:
            await _parar_typing(_tp, _tt)
        log.info(f"Menção de {autor}: {conteudo[:80]}")
        if resposta and not _e_resposta_generica(resposta):
            await _reagir_ou_responder(message, resposta)
            asyncio.ensure_future(_atualizar_perfil_usuario(user_id, autor, conteudo, resposta))
        elif resposta:
            await _digitar_e_enviar(message.channel, resposta, message)
        log.info(f"Respondido: {autor}")


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN não definido. Configure a variável de ambiente antes de iniciar.")

try:
    client.run(TOKEN)
except discord.errors.LoginFailure:
    raise SystemExit("Token inválido ou expirado. Atualize a variável DISCORD_TOKEN no Railway.")
except KeyboardInterrupt:
    pass
