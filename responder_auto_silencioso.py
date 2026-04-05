import discord
import re
import aiohttp
import json
import os
import io
import xml.etree.ElementTree as ET
import random
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from difflib import SequenceMatcher

try:
    import anthropic
    ANTHROPIC_DISPONIVEL = True
except ImportError:
    ANTHROPIC_DISPONIVEL = False
    print("[AVISO] Pacote anthropic não encontrado. Respostas via Claude desativadas.")

def agora_utc():
    return datetime.now(timezone.utc)

TOKEN = os.environ.get("DISCORD_TOKEN", "")
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
SERVIDOR_ID = 1487599082825584761
DONOS_IDS = {1487591389653897306, 1321848653878661172}
CONTAS_TESTE = {1375560046930563306}  # têm comandos de dono mas NÃO são isentos de punição
CARGO_EQUIPE_MOD_ID = 1487859369008697556  # equipe de moderação com acesso a comandos

# ── Canal de auditoria ───────────────────────────────────────────────────────
CANAL_AUDITORIA_ID = 1490180079899115591

# ── Chave da API VirusTotal ──────────────────────────────────────────────────
# Coloque sua chave aqui: https://www.virustotal.com/gui/my-apikey
VIRUSTOTAL_API_KEY = "SUA_CHAVE_AQUI"

client = discord.Client()


def tem_permissao_moderacao(guild: discord.Guild) -> bool:
    """Verifica se a conta tem permissão de administrador ou moderação no servidor."""
    membro_self = guild.get_member(client.user.id)
    if membro_self is None:
        return False
    perms = membro_self.guild_permissions
    return perms.administrator or perms.moderate_members or perms.manage_messages


def eh_autorizado(member: discord.Member) -> bool:
    """Retorna True se o membro é dono ou pertence à equipe de moderação."""
    if member.id in DONOS_IDS or member.id in CONTAS_TESTE:
        return True
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
        total = sum(len(v) for v in palavras_custom.values())
        print(f"[DADOS] {len(infracoes)} usuários e {total} palavras customizadas carregadas.")
    except Exception as e:
        print(f"[DADOS] Erro ao carregar: {e}")

def salvar_dados():
    try:
        with open(DADOS_PATH, "w") as f:
            json.dump({
                "infracoes": {str(k): v for k, v in infracoes.items()},
                "ultimo_motivo": {str(k): v for k, v in ultimo_motivo.items()},
                "silenciamentos": {str(k): v for k, v in silenciamentos.items()},
                "palavras_custom": palavras_custom,
            }, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[DADOS] Erro ao salvar: {e}")

# Histórico de flood, infrações e conversas por usuário
historico_mensagens = defaultdict(list)
historico_conteudo: dict[int, list] = defaultdict(list)
infracoes: dict[int, int] = defaultdict(int)
silenciamentos: dict[int, int] = defaultdict(int)
ultimo_motivo: dict[int, str] = {}
conversas: dict[int, dict] = {}
ausencia: dict[int, dict] = {}
historico_claude: dict[int, list] = {}  # histórico de conversa por usuário para API Claude
conversas_claude: dict[int, dict] = {}  # {user_id: {"canal": int, "ultima": datetime}}
TIMEOUT_CONVERSA_CLAUDE = timedelta(minutes=5)  # tempo sem mensagem para encerrar conversa

GATILHOS_NOME = re.compile(r"\bshell\b|\bengenheir\w*", re.IGNORECASE)

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


def formatar_duracao(delta: timedelta) -> str:
    dias = delta.days
    anos = dias // 365
    meses = (dias % 365) // 30
    dias_r = dias % 30
    partes = []
    if anos: partes.append(f"{anos} ano{'s' if anos != 1 else ''}")
    if meses: partes.append(f"{meses} {'meses' if meses != 1 else 'mês'}")
    if dias_r and not anos: partes.append(f"{dias_r} dia{'s' if dias_r != 1 else ''}")
    return ", ".join(partes) if partes else "menos de um dia"


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

    return (
        f"{membro.display_name} tem conta criada há {idade_conta} "
        f"e está no servidor há {tempo_servidor}. "
        f"Cargos: {cargos_txt}. "
        f"Singularidades: {sing_txt}."
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
    "a coisa ficou preta", "humor negro", "lista negra",
    "mercado negro", "inveja branca", "nao sou tuas negas",
    "farinha do mesmo saco", "japoronga", "japinha",
    "carcamano", "bugre", "monhe", "chinoca",
    "vachina", "xing ling", "gringo sujo",
]

# ── LGBTfobia ────────────────────────────────────────────────────────────────
LGBTFOBIA = [
    "viado", "viadao", "viadagem", "viada",
    "veado", "veadao", "veada",
    "bicha", "bichinha", "bixa",
    "boiola", "bolta", "bolagato",
    "sapatao", "gilete", "traveco",
]

# ── Capacitismo ───────────────────────────────────────────────────────────────
CAPACITISMO = [
    "retardado", "retardada", "mongoloide", "mongol",
    "debil mental", "debil", "aleijado", "aleijada",
    "coxo", "maneta", "surdo mudo", "anao",
]

# ── Misoginia ─────────────────────────────────────────────────────────────────
MISOGINIA = [
    "puta", "piranha", "vaca", "cachorra", "galinha",
    "mulher da vida", "mulher de vida facil",
    "maria vai com as outras", "prostituta", "meretriz",
    "corna", "corno",
]

# ── Ofensas indiretas / incitação ────────────────────────────────────────────
FRASES_OFENSIVAS = [
    "vai se enforcar", "se mata", "morre logo", "se suicida",
    "sua mae", "sua mãe",
    "nao presta", "nao vale nada", "lixo da sociedade",
    "feito nas coxas", "meia tigela", "lixo humano",
]

# Lista unificada de ofensas sérias (discriminação, etc.)
DISCRIMINACAO = RACISMO + LGBTFOBIA + CAPACITISMO + MISOGINIA + FRASES_OFENSIVAS

DISCORD_INVITE = re.compile(r"discord\.(gg|com\/invite)\/\w+", re.IGNORECASE)
URL_PATTERN = re.compile(r"https?://[^\s]+", re.IGNORECASE)

# Palavras ambíguas que só disparam com reforço de contexto
AMBIGUAS = {"pau", "comer", "rola", "comer", "gala", "fenda"}

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


def score_contexto(msg_norm: str, listas: list[list[str]]) -> int:
    return sum(
        1 for lista in listas
        if any(contem_fuzzy(msg_norm, p) for p in lista)
    )


def contem_ambigua_com_contexto(msg_norm: str, palavra: str) -> bool:
    if not contem_fuzzy(msg_norm, palavra):
        return False
    reforco = PALAVRAS_VULGARES + CONTEUDO_SEXUAL + DISCRIMINACAO
    reforco_sem_ambigua = [p for p in reforco if normalizar(p) != normalizar(palavra)]
    return any(contem_fuzzy(msg_norm, p) for p in reforco_sem_ambigua)


def eh_xingamento_direcionado(mensagem: str) -> bool:
    """
    Verifica se o xingamento é direcionado a alguém (ofensa) ou solto (desabafo/diversão).
    Considera ofensa quando há menção (@) ou expressões como "seu/sua/você/vc".
    """
    msg = mensagem.lower()
    padroes_direcao = [
        r"<@!?\d+>",             # menção direta
        r"\bseu\b", r"\bsua\b",  # "seu idiota", "sua merda"
        r"\bvocê\b", r"\bvc\b",  # "você é um..."
        r"\bele\b", r"\bela\b",  # falando de alguém
        r"\besse cara\b", r"\bessa cara\b",
        r"\besse membro\b",
    ]
    return any(re.search(p, msg) for p in padroes_direcao)


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
    agora = datetime.now()

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
        print(f"[VIRUSTOTAL ERRO] {e}")
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
                f"⚠️ Ei, {message.author.mention}! O link que você enviou foi bloqueado. "
                f"O VirusTotal detectou **{maliciosos} ameaça(s) maliciosa(s)** e "
                f"**{suspeitos} suspeita(s)**. Por segurança do servidor, ele foi removido."
            )
            print(f"[VIRUSTOTAL] Link bloqueado de {message.author.display_name}: {url} | malic={maliciosos} susp={suspeitos}")
            return  # Uma notificação por vez é suficiente


# ── Auditoria de ofensas ──────────────────────────────────────────────────────

async def enviar_auditoria(guild: discord.Guild, membro: discord.Member, violacoes: list[str], msg_id: int):
    """Envia log da ofensa apagada para o canal de auditoria como arquivo .txt."""
    canal_audit = guild.get_channel(CANAL_AUDITORIA_ID)
    if not canal_audit:
        print(f"[AUDITORIA] Canal {CANAL_AUDITORIA_ID} não encontrado.")
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


SYSTEM_CLAUDE = (
    "Você é o assistente automático de um servidor Discord brasileiro. "
    "Personalidade: direto, seco, rude quando necessário, mas nunca abusivo. "
    "Fala como brasileiro jovem, com gírias naturais do cotidiano. "
    "Sem emojis, sem listas, sem markdown, sem asteriscos. "
    "Respostas curtas, máximo 2 frases. "
    "Você conhece as regras do servidor e orienta usuários quando perguntado. "
    "Quando não souber algo, admite na lata sem inventar. "
    "Você não é um bot amigável, é um sistema de moderação que tolera conversas."
)

SYSTEM_ACAO = (
    "Você é o sistema de moderação de um servidor Discord brasileiro. "
    "Acabei de executar uma ação de moderação. Gere UMA frase curta confirmando o que foi feito, "
    "de forma direta e seca, como um brasileiro jovem falaria. "
    "Sem emojis, sem asteriscos, sem markdown. Inclua os dados exatos que receber no contexto."
)

async def confirmar_acao(descricao: str, fallback: str) -> str:
    """Gera confirmação de ação de moderação via Claude. Usa fallback se API indisponível."""
    if not ANTHROPIC_DISPONIVEL or not ANTHROPIC_API_KEY:
        return fallback
    try:
        ac = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resp = await ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=80,
            system=SYSTEM_ACAO,
            messages=[{"role": "user", "content": descricao}],
        )
        return resp.content[0].text.strip()
    except Exception as e:
        print(f"[Claude Ação] Erro: {e}")
        return fallback


async def responder_com_claude(pergunta: str, autor: str, user_id: int, guild=None, canal_id: int = None) -> str:
    # Mantém conversa ativa no canal atual (independe de ter API key)
    if canal_id:
        conversas_claude[user_id] = {"canal": canal_id, "ultima": agora_utc()}

    if not ANTHROPIC_DISPONIVEL or not ANTHROPIC_API_KEY:
        return random.choice([
            "Fala.", f"Tô aqui, {autor}. O que é?", "Pode falar.",
            "Diz.", "Sim?", "O que quer?", "Tô ouvindo.",
        ])

    hist = historico_claude.setdefault(user_id, [])
    hist.append({"role": "user", "content": f"{autor}: {pergunta}"})
    if len(hist) > 12:
        hist[:] = hist[-12:]

    try:
        ac = anthropic.AsyncAnthropic(api_key=ANTHROPIC_API_KEY)
        resposta = await ac.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=150,
            system=SYSTEM_CLAUDE,
            messages=hist,
        )
        texto = resposta.content[0].text.strip()
        hist.append({"role": "assistant", "content": texto})
        return texto
    except Exception as e:
        print(f"[Claude API] Erro: {e}")
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
            estado["etapa"] = 2
            if any(p in msg_l for p in ["bem", "bom", "otimo", "ótimo", "tranquilo", "tudo"]):
                return random.choice(["Que bom. O que quer?", "Ótimo. O que precisa?", "Beleza. O que é?"])
            if any(p in msg_l for p in ["mal", "ruim", "chateado", "cansado", "triste"]):
                estado["contexto"] = "desabafo"
                return random.choice(["O que aconteceu?", "Me conta.", "O que rolou?", "Fala o que é."])
            return random.choice([f"O que quer, {autor}?", f"O que é, {autor}?", f"Fala.", "Sim?"])
        if etapa == 2:
            del conversas[user_id]
            if any(p in msg_l for p in ["regra", "norma", "proibido"]):
                return REGRAS
            if "?" in msg:
                return f"Isso não é comigo. Chama um mod."
            return random.choice(["Fala logo o que precisa.", "Pode falar.", "Tô ouvindo.", "Qual é?"])

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
            return f"Registrado. Moderação vai ver que {autor} precisa de atenção: {msg}."

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
        return await responder_com_claude(msg, autor, user_id, guild)

    del conversas[user_id]
    return await responder_com_claude(msg, autor, user_id, guild)


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

    if any(p in msg for p in ["consegue", "pode banir", "você bane", "voce bane", "o que você faz", "o que voce faz", "pra que serve", "para que serve", "você pode", "voce pode", "poderia banir"]):
        iniciar_conversa(user_id, "capacidades", canal_id=canal_id)
        return f"Posso sim. Quer saber o que exatamente tô fazendo aqui?"

    if any(p in msg for p in ["obrigado", "obrigada", "valeu", "vlw", "thanks", "grato", "grata"]):
        return random.choice([
            ".", "Tá.", "Certo.", "Ok.", "Tmj.", "Nada não.",
            f"Isso aí, {autor}.", "De nada.", "Tranquilo.",
        ])

    if any(p in msg for p in ["oi", "olá", "ola", "hey", "salve", "eai", "fala", "tudo bem", "tudo bom", "boa tarde", "bom dia", "boa noite"]):
        iniciar_conversa(user_id, "saudacao", canal_id=canal_id)
        return random.choice([
            f"Fala, {autor}. O que quer?",
            f"Oi. O que é?",
            f"Tô aqui.",
            f"Sim?",
            f"O que há, {autor}?",
            f"Pode falar.",
            f"Oi.",
        ])

    if any(p in msg for p in ["quem é você", "quem e voce", "quem és", "o que é você", "o que voce é"]):
        return random.choice([
            f"Sou o sistema deste servidor. Monitoro o chat e aplico as regras.",
            f"Sou o assistente daqui. Cuido do chat e respondo quando chamado.",
            f"Sistema de moderação automática. Por que pergunta?",
        ])

    if any(p in msg for p in ["como vai", "como você tá", "como voce ta", "tá bem", "ta bem", "tudo certo"]):
        return random.choice([
            "Operando normalmente.", "Tô por aqui.", "Funcionando.", "Aqui firme.",
            "Por aqui. E você?",
        ])

    return await responder_com_claude(conteudo, autor, user_id, guild, canal_id)


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
        "vou me ausentar", "ausentar",
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
    texto = conteudo.strip().lstrip("!/.")
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
            await message.channel.send(f"{alvo.mention}, aviso da administração: {texto}")

    # ── chamar mod ─────────────────────────────────────────────────────────────
    elif cmd in ("chamar-mod", "chamarmod", "mod", "moderação", "moderacao", "chamar"):
        motivo = resto or "sem motivo especificado."
        await message.channel.send(f"{mod}, atenção necessária: {motivo}")

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

    # ── ausente [duração] [motivo] ─────────────────────────────────────────────
    elif cmd == "ausente":
        minutos, motivo = parsear_ausencia(message.content)
        ate = agora_utc() + timedelta(minutes=minutos) if minutos else None
        ausencia[message.author.id] = {"ate": ate, "motivo": motivo}

        partes = ["Modo ausente ativado"]
        if motivo:
            partes.append(f"motivo: {motivo}")
        if minutos:
            partes.append(f"duração: {minutos} minuto{'s' if minutos != 1 else ''}")
        else:
            partes.append("duração: indefinida, use 'voltar' para desativar")
        await message.channel.send(". ".join(partes) + ".")

    # ── voltar ─────────────────────────────────────────────────────────────────
    elif cmd in ("voltar", "voltei", "retornei", "presente"):
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send("Modo ausente desativado. Bem-vindo de volta, engenheiro!")
        else:
            await message.channel.send("Você não estava marcado como ausente.")


    # ── ajuda ──────────────────────────────────────────────────────────────────
    elif cmd in ("ajuda", "help", "comandos"):
        await message.channel.send(
            "Tudo sem prefixo. Para silenciar alguém diga silenciar e mencione o usuário, opcionalmente com o tempo em minutos. "
            "Para desfazer diga dessilenciar. Para banir diga banir seguido do usuário, duração e motivo. "
            "Para revogar um banimento diga desbanir. Para expulsar temporariamente diga expulsar. "
            "Para enviar um aviso público diga avisar e mencione quem. Para chamar a moderação diga chamar mod. "
            "Para ativar ausência diga ausente com duração e motivo, "
            "e para voltar diga voltei. Para adicionar uma palavra ao filtro diga adicionar seguido da palavra e a categoria. "
            "Para remover diga remover seguido da palavra. Para ver as regras diga regras."
        )

    else:
        return False

    return True


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
        print(f"[SILENCIADO] {membro.display_name} por {descricao} (vez {vez + 1})")
    except Exception as e:
        print(f"[ERRO] Não foi possível silenciar {membro.display_name}: {e}")
        await canal.send(f"{membro.mention} atingiu o limite de infrações. {mod}, tomem providências.")


@client.event
async def on_ready():
    carregar_dados()
    print(f"Conectado como {client.user}")
    guild = client.get_guild(SERVIDOR_ID)
    if guild:
        pode = tem_permissao_moderacao(guild)
        print(f"Servidor: {guild.name} | Permissao de moderacao: {'sim' if pode else 'não, apenas avisos'}")
    else:
        print(f"Servidor {SERVIDOR_ID} nao encontrado. Verifique se esta no servidor.")


@client.event
async def on_message(message: discord.Message):
    if message.author == client.user:
        return

    if not message.guild or message.guild.id != SERVIDOR_ID:
        return

    autor = message.author.display_name
    conteudo = message.content
    eh_dono = message.author.id in DONOS_IDS
    eh_teste = message.author.id in CONTAS_TESTE
    eh_mod = eh_autorizado(message.author)  # donos, contas teste e equipe de mod

    # ── Verificar menção/gatilho para autorizar comandos ──────────────────────
    ids_mencionados = {m.id for m in message.mentions} | {
        int(m) for m in ID_PATTERN.findall(conteudo)
    }
    mencionado = (
        client.user in message.mentions
        or client.user.id in ids_mencionados
        or bool(GATILHOS_NOME.search(conteudo))
    )

    # ── Conta de teste: comandos liberados, mas sofre punições normalmente ────
    if eh_teste and not eh_dono:
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send(f"{message.author.mention}, modo ausente desativado.")
        if mencionado:
            tratado = await processar_ordem(message)
            if tratado:
                return
            # Comando não reconhecido: cai na conversa normal abaixo
        # Continua para verificação de violações abaixo

    # ── Donos: isentos de punição, comandos sempre ativos ─────────────────────
    if eh_dono:
        if message.author.id in ausencia:
            del ausencia[message.author.id]
            await message.channel.send(f"{message.author.mention}, modo ausente desativado.")

        violacoes = detectar_violacoes(conteudo)
        if violacoes:
            lista = ", ".join(desc for desc, _ in violacoes)
            try:
                await message.author.send(
                    f"Ciente, {autor}. Você usou linguagem que normalmente seria punida ({lista}). "
                    f"Como proprietário está isento, mas tenha ciência do exemplo que passa ao servidor."
                )
            except Exception:
                pass

        tratado = await processar_ordem(message)
        await processar_links(message)
        if not tratado and mencionado:
            resposta = await resposta_inicial(conteudo, autor, user_id, message.guild, message.author, message.channel.id)
            await message.reply(resposta)
        return

    # ── Equipe de mod: isenta de punições, comandos via menção ───────────────────
    if eh_mod:
        if mencionado:
            tratado = await processar_ordem(message)
            if not tratado:
                resposta = await resposta_inicial(conteudo, autor, user_id, message.guild, message.author, message.channel.id)
                await message.reply(resposta)
        return  # mods nunca são punidos, independente do que disserem

    # ── Detectar flood (membros comuns) ───────────────────────────────────────
    if detectar_flood(message.author.id, conteudo):
        await message.channel.send(
            f"Ei {message.author.mention}, para com o spam! Regra número 1 dos canais em {CANAL_REGRAS}."
        )
        print(f"[FLOOD] {autor}")
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

        print(f"[INFRAÇÃO {count}/3] {autor}: {[(d, p) for d, p in violacoes]}")

        msg_id = message.id
        try:
            await message.delete()
        except Exception:
            pass
        await enviar_auditoria(message.guild, message.author, violacoes, msg_id)

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
    user_id = message.author.id
    if mencionado and message.mentions:
        alvos_info = [m for m in message.mentions if m != client.user]
        if alvos_info and any(p in conteudo.lower() for p in ["info", "informação", "quem é", "tempo no", "quando entrou", "idade"]):
            texto = await info_membro(alvos_info[0])
            await message.reply(texto)
            return

    # ── Stats do servidor ─────────────────────────────────────────────────────
    if mencionado and any(p in conteudo.lower() for p in ["quantos membros", "membros do servidor", "estatística", "estatistica", "quem está no servidor"]):
        await message.reply(await stats_servidor(message.guild))
        return

    # ── Continuar conversa em andamento (mesmo canal e sem @menção nova) ────────
    estado_conv = conversas.get(user_id)
    if estado_conv and client.user not in message.mentions:
        canal_conv = estado_conv.get("canal")
        if canal_conv is None or canal_conv == message.channel.id:
            resposta = await continuar_conversa(user_id, conteudo, autor, message.guild)
            if resposta:
                print(f"[CONVERSA] {autor}: {conteudo}")
                await message.reply(resposta)
                return
        else:
            del conversas[user_id]

    # ── Continuar conversa Claude ativa (sem novo gatilho, mesmo canal, dentro do timeout) ──
    estado_claude = conversas_claude.get(user_id)
    if estado_claude and client.user not in message.mentions and not GATILHOS_NOME.search(conteudo):
        if estado_claude["canal"] == message.channel.id:
            tempo_ocioso = agora_utc() - estado_claude["ultima"]
            if tempo_ocioso <= TIMEOUT_CONVERSA_CLAUDE:
                resposta = await responder_com_claude(conteudo, autor, user_id, message.guild, message.channel.id)
                print(f"[CLAUDE CONT] {autor}: {conteudo}")
                await message.reply(resposta)
                return
            else:
                del conversas_claude[user_id]
        else:
            del conversas_claude[user_id]

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
        print(f"[MENÇÃO] {autor}: {conteudo}")
        await message.reply(resposta)
        print(f"[RESPONDIDO] {autor}")


if not TOKEN:
    raise SystemExit("DISCORD_TOKEN não definido. Configure a variável de ambiente antes de iniciar.")

try:
    client.run(TOKEN)
except discord.errors.LoginFailure:
    raise SystemExit("Token inválido ou expirado. Atualize a variável DISCORD_TOKEN no Railway.")
except KeyboardInterrupt:
    pass
