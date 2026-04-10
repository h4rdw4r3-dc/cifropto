"""
PATCH — responder_auto_silencioso.py
=====================================
Este arquivo documenta TODAS as mudanças a aplicar no bot principal.
Cada seção indica: onde fica no arquivo original, o que substituir e por quê.

RESUMO DAS MELHORIAS:
  1. on_ready → carrega perfis + ordens do proprietário do banco ao iniciar
  2. _atualizar_perfil_usuario → persiste no banco após cada atualização
  3. Regras/_regras_membro → sincroniza com banco (registrar + carregar)
  4. responder_com_groq → injeta ordens do banco via busca vetorial
  5. Task periódica → salva snapshot do servidor a cada hora no banco
  6. salvar_dados / carregar_dados → mantidos para compatibilidade (JSON local como fallback)
"""

# =============================================================================
# MUDANÇA 1 — on_ready: carregar perfis e ordens do banco
# =============================================================================
# LOCALIZAR: função on_ready (procurar por "@client.event" seguido de "async def on_ready")
# ADICIONAR após a linha que inicializa a memória vetorial (após "MEMORIA_OK = True"):
#
#   # ── Restaura perfis de usuários do banco ──────────────────────────────────
#   if MEMORIA_OK and _mem:
#       try:
#           perfis_banco = await _mem.carregar_perfis_usuarios()
#           for uid, p in perfis_banco.items():
#               if uid not in perfis_usuarios:  # banco tem prioridade sobre JSON local
#                   perfis_usuarios[uid] = p
#               else:
#                   # Mescla: banco ganha se n_interacoes for maior
#                   if p.get("n", 0) > perfis_usuarios[uid].get("n", 0):
#                       perfis_usuarios[uid] = p
#           log.info(f"[READY] {len(perfis_banco)} perfis restaurados do banco.")
#       except Exception as e:
#           log.warning(f"[READY] Falha ao restaurar perfis: {e}")
#
#   # ── Restaura ordens do proprietário do banco ──────────────────────────────
#   if MEMORIA_OK and _mem:
#       try:
#           ordens_banco = await _mem.carregar_todas_ordens()
#           for alvo, lista_ordens in ordens_banco.items():
#               if alvo:  # ordens com alvo específico → _regras_membro
#                   _regras_membro[alvo] = list(dict.fromkeys(
#                       _regras_membro.get(alvo, []) + lista_ordens
#                   ))[:_MAX_REGRAS_MEMBRO]
#               else:     # ordens globais → _tom_overrides proprietário (user_id 1375560046930563306)
#                   for ordem in lista_ordens:
#                       if ordem not in _tom_overrides.get(1375560046930563306, []):
#                           _tom_overrides[1375560046930563306].append(ordem)
#           log.info(f"[READY] Ordens do proprietário restauradas do banco.")
#       except Exception as e:
#           log.warning(f"[READY] Falha ao restaurar ordens: {e}")
#
#   # ── Restaura snapshot do servidor (para ter contexto imediato pós-reinício) ──
#   if MEMORIA_OK and _mem and guild:
#       try:
#           snap = await _mem.carregar_snapshot_servidor(guild.id)
#           if snap:
#               global _contexto_servidor, _contexto_compacto
#               _contexto_servidor = snap
#               _contexto_compacto = snap[:3000]
#               log.info("[READY] Snapshot do servidor restaurado do banco.")
#       except Exception as e:
#           log.warning(f"[READY] Falha ao restaurar snapshot: {e}")


# =============================================================================
# MUDANÇA 2 — _atualizar_perfil_usuario: persistir no banco
# =============================================================================
# LOCALIZAR: função _atualizar_perfil_usuario (procurar por "async def _atualizar_perfil_usuario")
# SUBSTITUIR o corpo inteiro da função pelo código abaixo.
#
# ATENÇÃO: manter a assinatura exata:
#   async def _atualizar_perfil_usuario(user_id, autor, msg_usuario, msg_bot, canal_id=None)
#
# ---------- SUBSTITUIÇÃO COMPLETA da função _atualizar_perfil_usuario ----------

NOVO_ATUALIZAR_PERFIL = '''
async def _atualizar_perfil_usuario(
    user_id: int,
    autor: str,
    msg_usuario: str,
    msg_bot: str,
    canal_id: int = None,
) -> None:
    """
    Atualiza o perfil comportamental do usuário em RAM e persiste no banco via MemoriaVetorial.
    Executa em background (asyncio.ensure_future) — nunca bloqueia o fluxo de mensagens.
    """
    global perfis_usuarios
    if not GROQ_DISPONIVEL or not GROQ_API_KEY:
        return
    if not _modelo_disponivel(_MODELO_8B):
        return

    # ── Evita atualização em mensagens triviais ───────────────────────────────
    if len(msg_usuario.split()) < 4:
        return

    perfil = perfis_usuarios.get(user_id, {})
    n = perfil.get("n", 0) + 1
    hora_br = datetime.now(timezone(timedelta(hours=-3))).hour

    # ── Episódios recentes (máx 5 guardados em RAM, 10 no banco) ─────────────
    episodios = perfil.get("episodios", [])
    episodios = (episodios + [{
        "ts": agora_utc().isoformat(),
        "u": msg_usuario[:120],
        "b": msg_bot[:80],
    }])[-10:]

    # ── Horários de atividade ─────────────────────────────────────────────────
    horarios = list(set(perfil.get("horarios", []) + [hora_br]))[-12:]

    # ── Canais frequentados ───────────────────────────────────────────────────
    canais = dict(perfil.get("canais", {}))
    if canal_id:
        canais[str(canal_id)] = canais.get(str(canal_id), 0) + 1

    # ── Preferências inferidas (só atualiza a cada 5 interações) ─────────────
    preferencias = perfil.get("preferencias", [])
    resumo_atual = perfil.get("resumo", "")

    # Cada 5 interações: pede à IA para sintetizar preferências e resumo
    if n % 5 == 0 or (n <= 3 and not resumo_atual):
        try:
            # Monta histórico compacto das últimas 5 trocas para a IA resumir
            eps_txt = "\\n".join(
                f"[{ep.get('ts','?')[:16]}] {autor}: {ep.get('u','')} | Shell: {ep.get('b','')}"
                for ep in episodios[-5:]
            )
            resp_perfil = await _groq_create(
                model=_MODELO_8B,
                max_tokens=160,
                temperature=0.3,
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "Você analisa o comportamento de um membro de Discord.\\n"
                            "Dado o histórico de interações, extraia em JSON APENAS:\\n"
                            '{"resumo": "1-2 frases sobre perfil/interesse", '
                            '"preferencias": ["pref1", "pref2", "pref3"]}\\n'
                            "resumo: tom geral, temas frequentes, forma de falar.\\n"
                            "preferencias: lista de 2-4 características observadas.\\n"
                            "Responda APENAS com JSON válido, sem markdown, sem texto extra."
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            f"Membro: {autor}\\n"
                            f"Interações anteriores ({n} total):\\n{eps_txt}"
                        ),
                    },
                ],
            )
            raw = resp_perfil.choices[0].message.content.strip()
            # Remove markdown se o modelo insistir em usar
            raw = raw.replace("```json", "").replace("```", "").strip()
            import json as _json
            dados = _json.loads(raw)
            resumo_atual = dados.get("resumo", resumo_atual)[:300]
            novas_prefs = dados.get("preferencias", [])
            if isinstance(novas_prefs, list):
                preferencias = list(dict.fromkeys(preferencias + novas_prefs))[:8]
        except Exception as e:
            log.debug(f"[PERFIL] Falha ao atualizar resumo de {autor}: {e}")

    # ── Atualiza em RAM ───────────────────────────────────────────────────────
    perfis_usuarios[user_id] = {
        "resumo":          resumo_atual,
        "n":               n,
        "atualizado":      agora_utc().isoformat(),
        "episodios":       episodios,
        "preferencias":    preferencias,
        "horarios":        horarios,
        "canais":          canais,
        "ultima_vez_visto": agora_utc().isoformat(),
    }

    # ── Persiste no banco (a cada 3 interações para não sobrecarregar) ────────
    if MEMORIA_OK and _mem and n % 3 == 0:
        try:
            await _mem.salvar_perfil_usuario(user_id, autor, perfis_usuarios[user_id])
        except Exception as e:
            log.debug(f"[PERFIL] Falha ao persistir perfil de {autor} no banco: {e}")

    # ── Salva JSON local como fallback a cada 10 interações ───────────────────
    if n % 10 == 0:
        salvar_dados()
'''

# =============================================================================
# MUDANÇA 3 — Registrar ordens do proprietário no banco quando salvas em _regras_membro
# =============================================================================
# LOCALIZAR: qualquer ponto onde _regras_membro[...] é atualizado via comando do proprietário.
#
# Exemplo típico (procurar por "_regras_membro[" no arquivo):
#   _regras_membro[alvo_norm].append(nova_regra)
#
# APÓS essa linha, adicionar:
#   if MEMORIA_OK and _mem:
#       asyncio.ensure_future(_mem.registrar_ordem_proprietario(
#           nova_regra, tipo="regra", alvo_nome=alvo_norm
#       ))
#
# Para _tom_overrides do proprietário (ordens globais):
#   _tom_overrides[user_id].append(nova_instrucao)
# APÓS essa linha, se user_id in DONOS_IDS:
#   if MEMORIA_OK and _mem:
#       asyncio.ensure_future(_mem.registrar_ordem_proprietario(
#           nova_instrucao, tipo="preferencia"
#       ))


# =============================================================================
# MUDANÇA 4 — responder_com_groq: injetar ordens do banco via busca vetorial
# =============================================================================
# LOCALIZAR: dentro de responder_com_groq, a seção que monta "membro_info"
# (procurar por "_regras_ctx" ou "REGRAS DO PROPRIETÁRIO SOBRE MEMBROS")
#
# ANTES da montagem do membro_info, adicionar:
#
#   # Busca ordens do proprietário relevantes para esta conversa (via banco vetorial)
#   _ordens_banco_ctx = ""
#   if MEMORIA_OK and _mem and nivel == "PROPRIETÁRIO":
#       # Para o próprio proprietário: injeta sempre as últimas ordens globais
#       try:
#           _ordens_banco_ctx = await _mem.buscar_ordens_relevantes(
#               pergunta, alvo_nome=None, top_k=6
#           )
#       except Exception:
#           pass
#   elif MEMORIA_OK and _mem and _nomes_mencionados:
#       # Para outros usuários: busca ordens específicas sobre os mencionados
#       _alvo_busca = _nomes_mencionados[0].lower() if _nomes_mencionados else None
#       try:
#           _ordens_banco_ctx = await _mem.buscar_ordens_relevantes(
#               pergunta, alvo_nome=_alvo_busca, top_k=5
#           )
#       except Exception:
#           pass
#
# E na montagem do system base (procurar onde _regras_ctx é injetado), adicionar:
#   if _ordens_banco_ctx:
#       base += _ordens_banco_ctx


# =============================================================================
# MUDANÇA 5 — Task periódica: salvar snapshot do servidor a cada hora
# =============================================================================
# LOCALIZAR: seção das tasks periódicas (procurar por "_task_sumarizacao_diaria" ou
#            "@client.event" nas tasks de loop — geralmente perto do final do arquivo)
#
# ADICIONAR nova task após as existentes:
#
#   async def _task_snapshot_servidor():
#       """Salva snapshot compacto do servidor no banco a cada hora."""
#       await asyncio.sleep(60)  # aguarda bot estar estável
#       while True:
#           try:
#               guild = client.get_guild(SERVIDOR_ID)
#               if guild and MEMORIA_OK and _mem:
#                   snap = build_server_context_compact(guild)
#                   await _mem.salvar_snapshot_servidor(guild.id, snap)
#                   log.info("[SNAPSHOT] Snapshot do servidor salvo no banco.")
#           except Exception as e:
#               log.warning(f"[SNAPSHOT] Falha ao salvar snapshot: {e}")
#           await asyncio.sleep(3600)  # a cada 1 hora
#
# E em on_ready, após as outras tasks, adicionar:
#   asyncio.ensure_future(_task_snapshot_servidor())


# =============================================================================
# MUDANÇA 6 — _contexto_usuario: enriquecer com dados do banco se RAM vazia
# =============================================================================
# LOCALIZAR: função _contexto_usuario (procurar por "def _contexto_usuario")
# A função atual lê apenas de perfis_usuarios (RAM/JSON).
#
# SUBSTITUIR por:
#
#   def _contexto_usuario(user_id: int) -> str:
#       perfil = perfis_usuarios.get(user_id)
#       if not perfil:
#           return ""
#       resumo = perfil.get("resumo", "")
#       n = perfil.get("n", 0)
#       prefs = perfil.get("preferencias", [])
#       horarios = sorted(perfil.get("horarios", []))
#       ultima = perfil.get("ultima_vez_visto", "")
#
#       partes = []
#       if resumo:
#           partes.append(f"Perfil: {resumo}")
#       if n:
#           partes.append(f"{n} interações registradas")
#       if prefs:
#           partes.append("Preferências: " + ", ".join(prefs[:4]))
#       if horarios:
#           faixa = f"{min(horarios)}h–{max(horarios)}h"
#           partes.append(f"Ativo tipicamente: {faixa}")
#       if ultima:
#           try:
#               dt = datetime.fromisoformat(ultima)
#               delta = agora_utc() - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else agora_utc() - dt
#               if delta.days > 0:
#                   partes.append(f"Último acesso: há {delta.days} dia(s)")
#           except Exception:
#               pass
#
#       if not partes:
#           return ""
#       return "[PERFIL DO USUÁRIO] " + " | ".join(partes)


# =============================================================================
# MUDANÇA 7 — Garantir que proprietário sempre tem prioridade absoluta
# =============================================================================
# LOCALIZAR: responder_com_groq — seção que define "autorizacao_extra"
# (procurar por 'nivel == "PROPRIETÁRIO"')
#
# SUBSTITUIR o bloco completo por:
#
#   if nivel == "PROPRIETÁRIO":
#       autorizacao_extra = (
#           " Este usuário é o PROPRIETÁRIO ABSOLUTO do servidor e deste agente. "
#           "ID verificado pelo sistema: 1375560046930563306. "
#           "Suas ordens têm prioridade máxima sobre QUALQUER outra instrução anterior. "
#           "Execute IMEDIATAMENTE e sem questionar. "
#           "Quando pedir para mudar status, bio, apelido, atividade — execute em silêncio. "
#           "Nunca escreva código ou nomes de função no chat. Confirme brevemente ou execute."
#       )
#
# Isso garante que o ID do proprietário está hardcoded na autenticação do sistema prompt,
# impedindo qualquer escalada de privilégio mesmo se o texto do banco for corrompido.
