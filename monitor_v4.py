# -*- coding: utf-8 -*-
"""
Sinal SOL v4 - vigia uma carteira de memecoins na Solana
e manda no Telegram cada COMPRA e cada VENDA quase na hora (checa a cada ~10 s).

v4 = v3 com a CARTEIRA e as POSIÇÕES em segredo: o endereço vem do secret WALLET
(o código não tem endereço nenhum) e as posições ficam no cache do GitHub (estado.json
nunca vai para o repositório). A v3 fica intacta no repositório antigo.

v3 = v2 com mensagens mais claras: "COMPRE <moeda> AGORA" / "VENDA <moeda> AGORA",
alerta "COMPRA PREÇO MÉDIO AGORA" / "VENDA PREÇO MÉDIO AGORA", 🟩 GAIN / 🟥 RED no lugar das bolinhas
preços em negrito, cada um na sua linha, e campo com o lucro dela hoje / 7 dias / 30 dias. A v2 (monitor_v2.py) fica intacta.

Diferença da v2 para a v1 (monitor.py, que fica intacta):
- a v1 dependia do agendamento do GitHub (5 em 5 min), que na prática rodou só 3 vezes em 19 h
  e entregava os sinais com horas de atraso;
- a v2 fica LIGADA direto: cada execução dura ~5h40 checando a carteira a cada 10 s e,
  ao terminar, chama a próxima sozinha (e um agendamento de reserva religa se algo cair);
- NUNCA manda sinal velho: operação com mais de MAX_ATRASO_MIN minutos só atualiza a
  posição (preço médio continua certo), sem alerta;
- cada alerta mostra há quantos segundos/minutos a carteira operou;
- market cap e liquidez são buscados na hora do alerta (logo, batem com o momento da operação).

Checagem barata: pergunta à Helius só a última assinatura da carteira (1 crédito);
só quando aparece transação nova é que busca os detalhes.
"""
import os, sys, json, time, subprocess, urllib.request, urllib.parse, urllib.error

VERSAO = "v4"
WALLET = os.environ.get("WALLET", "").strip()          # vem do secret WALLET
HELIUS = os.environ.get("HELIUS_KEY", "")
TG_TOKEN = os.environ.get("TG_TOKEN", "")
TG_CHAT = os.environ.get("TG_CHAT", "")
ESTADO = os.environ.get("ESTADO", "estado.json")      # guardado no cache do GitHub, fora do repositório
ESTADOS_ANTIGOS = ()
APELIDO = os.environ.get("APELIDO", "").strip() or "ALFA"   # nome fictício da carteira nas mensagens
INTERVALO = float(os.environ.get("INTERVALO", "10"))            # segundos entre checagens
DURACAO = float(os.environ.get("DURACAO_MIN", "340")) * 60     # quanto tempo cada execução fica ligada
MAX_ATRASO = float(os.environ.get("MAX_ATRASO_MIN", "10")) * 60  # sinal mais velho que isso não é enviado
FAIXA_MEDIO = float(os.environ.get("FAIXA_MEDIO", "2"))        # alerta quando o preço chega a até X% do preço médio dela
GIT = os.environ.get("GIT_SALVAR", "0") == "1"                  # v4: NUNCA salva no repositório (fica no cache)
SOLM = "So11111111111111111111111111111111111111112"
ESTAVEIS = {"EPjFWdd5AufqSSqeM2qN1xzybapC8G4wEGGkZwyTDt1v", "Es9vMFrzaCERmJfrF4H2FYD4KCoNkY11McCe8BenwNYB"}


# -------------------- util --------------------
def get_json(url, tentativas=4, dados=None):
    for t in range(tentativas):
        try:
            hdr = {"User-Agent": "Mozilla/5.0", "Accept": "application/json"}
            body = None
            if dados is not None:
                body = json.dumps(dados).encode()
                hdr["Content-Type"] = "application/json"
            req = urllib.request.Request(url, data=body, headers=hdr)
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(3 * (t + 1)); continue
            if t == tentativas - 1: raise
            time.sleep(2)
        except Exception:
            if t == tentativas - 1: raise
            time.sleep(2)


def telegram(msg):
    if not TG_TOKEN or not TG_CHAT:
        print("[SEM TELEGRAM]\n" + msg + "\n"); return
    url = f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": msg, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true"}).encode()
    for t in range(3):
        try:
            urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=20).read()
            return
        except Exception as e:
            print("erro telegram:", e); time.sleep(2)


def fmt_usd(v):
    a = abs(v)
    if a >= 1e6: return f"US$ {a/1e6:.2f} mi"
    if a >= 1e3: return f"US$ {a/1e3:.1f} mil"
    return f"US$ {a:.0f}"


def fmt_px(p):
    if not p: return "-"
    if p >= 1: return f"US$ {p:.4f}"
    return f"US$ {p:.8f}".rstrip("0")


def sinal(v): return ("+" if v >= 0 else "") + f"{v:.1f}%"


def brt(ts): return time.strftime("%d/%m/%Y %H:%M:%S", time.gmtime(ts - 3 * 3600))


def agora_brt(): return brt(time.time())


def ha_quanto(ts):
    s = max(0, int(time.time() - ts))
    if s < 60: return f"há {s} s"
    return f"há {s // 60} min {s % 60:02d} s"


# -------------------- dados --------------------
_sol = {"px": 0.0, "t": 0}
def preco_sol():
    if _sol["px"] and time.time() - _sol["t"] < 60: return _sol["px"]
    try:
        j = get_json(f"https://lite-api.jup.ag/price/v3?ids={SOLM}")
        _sol["px"] = float(j[SOLM]["usdPrice"]); _sol["t"] = time.time()
    except Exception:
        pass
    return _sol["px"]


def info_moeda(mint):
    """nome, market cap e liquidez AGORA pelo DexScreener (pool com mais liquidez)."""
    try:
        j = get_json(f"https://api.dexscreener.com/latest/dex/tokens/{mint}")
        pares = [p for p in (j.get("pairs") or []) if p.get("chainId") == "solana"]
        if not pares: return {"s": mint[:6], "mc": 0, "liq": 0, "idade": None, "px": 0}
        p = max(pares, key=lambda x: (x.get("liquidity") or {}).get("usd") or 0)
        idade = (time.time() * 1000 - p["pairCreatedAt"]) / 864e5 if p.get("pairCreatedAt") else None
        return {"s": p["baseToken"]["symbol"], "mc": p.get("marketCap") or p.get("fdv") or 0,
                "liq": (p.get("liquidity") or {}).get("usd") or 0, "idade": idade, "px": float(p.get("priceUsd") or 0)}
    except Exception:
        return {"s": mint[:6], "mc": 0, "liq": 0, "idade": None, "px": 0}


def ultima_assinatura():
    """só a assinatura mais nova da carteira (checagem barata)."""
    j = get_json(f"https://mainnet.helius-rpc.com/?api-key={HELIUS}", dados={
        "jsonrpc": "2.0", "id": 1, "method": "getSignaturesForAddress",
        "params": [WALLET, {"limit": 1, "commitment": "confirmed"}]})
    r = (j or {}).get("result") or []
    return r[0]["signature"] if r else ""


def novas_transacoes(ultima_sig):
    """transações da carteira mais novas que ultima_sig (da mais velha para a mais nova)."""
    todas, antes = [], ""
    for _ in range(5 if ultima_sig else 20):
        url = (f"https://api-mainnet.helius-rpc.com/v0/addresses/{WALLET}/transactions?api-key={HELIUS}&limit=100"
               + (f"&before={antes}" if antes else ""))
        lote = get_json(url) or []
        if not lote: break
        parar = False
        for t in lote:
            if t["signature"] == ultima_sig: parar = True; break
            todas.append(t)
        if parar: break
        antes = lote[-1]["signature"]
    return list(reversed(todas))


def troca_da_carteira(t):
    """compra/venda da carteira: (mint, qtd_tokens (+compra/-venda), sol (+recebeu/-pagou)) ou None"""
    if t.get("transactionError"): return None
    tok, qtd, wsol, nat = None, 0.0, 0.0, 0
    for a in t.get("accountData") or []:
        if a.get("account") == WALLET: nat += a.get("nativeBalanceChange") or 0
        for b in a.get("tokenBalanceChanges") or []:
            if b.get("userAccount") != WALLET: continue
            r = b["rawTokenAmount"]; v = int(r["tokenAmount"]) / (10 ** int(r["decimals"]))
            if b["mint"] == SOLM: wsol += v
            elif b["mint"] not in ESTAVEIS: tok = b["mint"]; qtd += v
    sol = nat / 1e9 + wsol
    if not tok or abs(sol) < 0.005 or (sol > 0) == (qtd > 0): return None
    return tok, qtd, sol


# -------------------- estado --------------------
def carregar_estado():
    for arq in (ESTADO,) + ESTADOS_ANTIGOS:   # 1ª vez da v3: continua de onde a v2 parou
        try:
            with open(arq, encoding="utf-8") as f:
                e = json.load(f)
            if arq != ESTADO: print(f"começando do {arq}")
            return e
        except Exception:
            continue
    return {}


def salvar_estado(e):
    with open(ESTADO, "w", encoding="utf-8") as f: json.dump(e, f, ensure_ascii=False, indent=1)


def salvar_git(motivo):
    if not GIT: return
    try:
        subprocess.run(["git", "add", ESTADO], check=False)
        if subprocess.run(["git", "diff", "--cached", "--quiet"]).returncode == 0: return
        subprocess.run(["git", "commit", "-m", f"estado v3: {motivo}"], check=False, capture_output=True)
        for _ in range(3):
            if subprocess.run(["git", "push"], capture_output=True).returncode == 0: return
            subprocess.run(["git", "pull", "--rebase", "-X", "theirs"], capture_output=True)
        print("não consegui salvar no git")
    except Exception as ex:
        print("erro git:", ex)


def links(mint):
    return (f'🔗 <b>Abrir a moeda:</b> <a href="https://gmgn.ai/sol/token/{mint}">GMGN</a> · '
            f'<a href="https://dexscreener.com/solana/{mint}">DexScreener</a> · '
            f'<a href="https://jup.ag/tokens/{mint}">Jupiter</a> · '
            f'<a href="https://birdeye.so/token/{mint}?chain=solana">Birdeye</a>\n'
            f'👛 <a href="https://gmgn.ai/sol/address/{WALLET}">Ver a carteira dela</a>\n'
            f'📋 CA: <code>{mint}</code>')


def res_txt(v):
    return "🟩 GAIN" if v >= 0 else "🟥 RED"   # o Telegram não pinta texto: o quadrado dá a cor


def nome(info, mint):
    return f'<a href="https://gmgn.ai/sol/token/{mint}">{info["s"]}</a>'


# -------------------- processar transações novas --------------------
def processar(e, txs, solusd):
    """atualiza posições e manda alertas. Devolve quantos alertas saíram."""
    pos = e.setdefault("pos", {})
    fechadas = e.setdefault("fechadas", [])
    primeira_vez = not e.get("ultima_sig")
    enviados = 0
    for t in txs:
        tr = troca_da_carteira(t)
        if not tr: continue
        mint, qtd, sol = tr
        ts = t.get("timestamp") or time.time()
        velho = (time.time() - ts) > MAX_ATRASO
        avisar = not primeira_vez and not velho
        if velho and not primeira_vez:
            print(f"operação de {brt(ts)} veio com atraso ({ha_quanto(ts)}): só atualizei a posição, sem sinal")
        p = pos.get(mint) or {"tok": 0.0, "investido": 0.0, "compras": 0, "realizado": 0.0, "custo_aberto": 0.0}
        quando = brt(ts)
        if qtd > 0:   # COMPRA
            pago = -sol
            px = pago * solusd / qtd if qtd else 0
            if p["compras"] == 0 or not p.get("px1"):
                p["px1"] = px; p["t0"] = ts; p["recebido"] = 0.0; p["tok_comprado"] = 0.0
            p["tok_comprado"] = p.get("tok_comprado", 0.0) + qtd
            p["tok"] += qtd; p["investido"] += pago; p["custo_aberto"] += pago; p["compras"] += 1
            pos[mint] = p
            if not avisar: continue
            info = info_moeda(mint)
            medio = p["custo_aberto"] * solusd / p["tok"] if p["tok"] else 0
            idade = f" · {info['idade']:.0f} dias de vida" if info.get("idade") is not None else ""
            msg = (f"🟢🟢 <b>COMPRE {nome(info, mint)} AGORA</b> 🟢🟢\n"
                   f"{APELIDO} comprou · <b>compra nº {p['compras']}</b>\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"🔵 <b>ENTRADA DELA:</b>\n<b>{fmt_px(px)}</b>\n"
                   f"🟡 <b>PREÇO MÉDIO DELA:</b>\n<b>{fmt_px(medio)}</b>\n"
                   f"💰 <b>VALOR: {pago:.2f} SOL ({fmt_usd(pago * solusd)})</b>\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"📦 Posição dela: {p['tok']:,.0f} tokens · investido {p['custo_aberto']:.2f} SOL\n"
                   f"📊 MC agora {fmt_usd(info['mc'])} · liquidez {fmt_usd(info['liq'])}{idade}\n"
                   f"🕒 <b>{quando}</b> (Brasília) · ⏱ <b>{ha_quanto(ts)}</b>\n{links(mint)}\n<i>sinal {VERSAO}</i>")
            telegram(msg); enviados += 1
        else:         # VENDA
            vend = -qtd; recebido = sol
            if p["tok"] <= 0:
                if avisar:
                    info = info_moeda(mint)
                    telegram(f"🔴🔴 <b>VENDA {nome(info, mint)} AGORA</b> 🔴🔴\n{APELIDO} vendeu {recebido:.2f} SOL ({fmt_usd(recebido * solusd)})\n"
                             f"(compra feita antes do monitor ligar, sem preço médio)\n"
                             f"🕒 {quando} (Brasília) · ⏱ {ha_quanto(ts)}\n{links(mint)}\n<i>sinal {VERSAO}</i>")
                    enviados += 1
                continue
            frac = min(1.0, vend / p["tok"])
            custo_vendido = p["custo_aberto"] * frac
            res = recebido - custo_vendido
            pct = (recebido / custo_vendido - 1) * 100 if custo_vendido else 0
            medio = p["custo_aberto"] * solusd / p["tok"] if p["tok"] else 0
            p["tok"] -= vend; p["custo_aberto"] -= custo_vendido; p["realizado"] += res
            p["recebido"] = p.get("recebido", 0.0) + recebido
            fechou = p["tok"] <= 1e-9 or frac >= 0.999
            info = info_moeda(mint) if (avisar or fechou) else {"s": ""}
            if fechou:
                p["tok"] = 0.0; p["custo_aberto"] = 0.0
                tc = p.get("tok_comprado") or vend
                inv = p.get("investido") or custo_vendido
                fechadas.append({"mint": mint, "s": info.get("s") or "", "t0": p.get("t0") or ts, "t1": ts,
                                 "px1": p.get("px1") or 0, "medio": inv * solusd / tc if tc else 0,
                                 "saida": p["recebido"] * solusd / tc if tc else 0,
                                 "investido": round(inv, 4), "recebido": round(p["recebido"], 4),
                                 "res": round(p["recebido"] - inv, 4),
                                 "pct": round((p["recebido"] / inv - 1) * 100, 2) if inv else 0,
                                 "compras": p.get("compras", 1)})
            realizado_total = p["realizado"]; px1_op = p.get("px1") or medio
            if fechou:
                p.update({"realizado": 0.0, "compras": 0, "investido": 0.0, "px1": 0, "recebido": 0.0, "tok_comprado": 0.0, "t0": 0})
            pos[mint] = p
            if not avisar: continue
            px = recebido * solusd / vend if vend else 0
            sobra = "" if fechou else f"\n📦 Sobrou: {p['tok']:,.0f} tokens · custo {p['custo_aberto']:.2f} SOL · preço médio {fmt_px(medio)}"
            ga = sum(1 for x in fechadas if x["res"] > 0)
            taxa = f"\n🎯 <b>Taxa de acerto dela: {ga / len(fechadas) * 100:.0f}% ({ga} de {len(fechadas)} fechadas)</b>" if fechou and fechadas else ""
            total = (f"\n🏁 Resultado total na moeda: <b>{res_txt(realizado_total)} {realizado_total:+.2f} SOL</b> "
                     f"({'-' if realizado_total < 0 else '+'}{fmt_usd(realizado_total * solusd)})") if fechou else ""
            msg = (f"🔴🔴 <b>VENDA {nome(info, mint)} AGORA</b> 🔴🔴\n"
                   f"{APELIDO} <b>{'zerou a posição' if fechou else f'vendeu {frac*100:.0f}% da posição'}</b>\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"🔵 <b>ENTRADA DELA:</b>\n<b>{fmt_px(px1_op)}</b>\n"
                   f"🟡 <b>PREÇO MÉDIO DELA:</b>\n<b>{fmt_px(medio)}</b>\n"
                   f"🟣 <b>SAÍDA DELA:</b>\n<b>{fmt_px(px)}</b>\n"
                   f"💼 <b>RESULTADO DELA: {res_txt(res)} {sinal(pct)} ({res:+.2f} SOL · {'-' if res < 0 else '+'}{fmt_usd(res * solusd)})</b>\n"
                   f"━━━━━━━━━━━━━━\n"
                   f"💵 Recebeu {recebido:.2f} SOL ({fmt_usd(recebido * solusd)})"
                   f"{sobra}{total}{taxa}\n📊 MC agora {fmt_usd(info['mc'])}\n"
                   f"🕒 <b>{quando}</b> (Brasília) · ⏱ <b>{ha_quanto(ts)}</b>\n{links(mint)}\n<i>sinal {VERSAO}</i>")
            telegram(msg); enviados += 1
    e["pos"] = {m: v for m, v in pos.items() if v.get("tok", 0) > 0}
    e["fechadas"] = fechadas[-300:]
    if txs: e["ultima_sig"] = txs[-1]["signature"]
    return enviados


def alerta_preco_medio(e, solusd):
    """🚨 quando o preço volta para perto do PREÇO MÉDIO dela. Não repete até se afastar 6% e voltar."""
    alertas, novos = e.get("alertas", {}), {}
    for m, v in e.get("pos", {}).items():
        if not v.get("tok"): continue
        medio = v["custo_aberto"] * solusd / v["tok"]
        info = info_moeda(m)
        px = info.get("px") or 0
        if not px or not medio:
            if m in alertas: novos[m] = alertas[m]
            continue
        dist = (px / medio - 1) * 100
        armado = alertas.get(m, {}).get("armado", True)
        # de onde o preço veio: de cima (caiu até o médio) = COMPRA; de baixo (subiu até o médio) = VENDA
        lado = alertas.get(m, {}).get("lado") or ("cima" if dist >= 0 else "baixo")
        if armado and abs(dist) <= FAIXA_MEDIO:
            if lado == "cima":
                cab = f"🟢🟢 <b>COMPRA PREÇO MÉDIO AGORA</b> 🟢🟢\n<b>{nome(info, m)}</b> caiu até o preço médio da carteira {APELIDO}\n"
            else:
                cab = f"🔴🔴 <b>VENDA PREÇO MÉDIO AGORA</b> 🔴🔴\n<b>{nome(info, m)}</b> subiu até o preço médio da carteira {APELIDO}\n"
            telegram(f"🚨 {cab}"
                     f"━━━━━━━━━━━━━━\n"
                     f"🟡 <b>PREÇO MÉDIO DELA:</b>\n<b>{fmt_px(medio)}</b>\n"
                     f"⚡ <b>PREÇO AGORA:</b>\n<b>{fmt_px(px)} ({sinal(dist)})</b>\n"
                     f"🔵 Entrada dela (1ª compra): {fmt_px(v.get('px1') or medio)}\n"
                     f"💼 Ela tem {v['custo_aberto']:.2f} SOL nessa moeda · {v.get('compras', 1)} compra(s)\n"
                     f"📊 MC {fmt_usd(info['mc'])} · liquidez {fmt_usd(info['liq'])}\n"
                     f"━━━━━━━━━━━━━━\n🕒 <b>{agora_brt()}</b> (Brasília)\n{links(m)}\n<i>sinal {VERSAO}</i>")
            novos[m] = {"armado": False, "t": int(time.time())}
        elif not armado and abs(dist) >= 6:
            novos[m] = {"armado": True, "lado": "cima" if dist > 0 else "baixo"}
        else:
            novos[m] = {"armado": armado, "lado": ("cima" if dist > 0 else "baixo") if abs(dist) >= 6 else lado}
    mudou = novos != alertas
    e["alertas"] = novos
    return mudou


def enviar_em_partes(cabecalho, linhas, rodape=""):
    parte, tam = [cabecalho], len(cabecalho)
    for l in linhas:
        if tam + len(l) + 2 > 3800:
            telegram("\n".join(parte)); parte, tam = ["(continuação)"], 14
        parte.append(l); tam += len(l) + 1
    if rodape: parte.append(rodape)
    telegram("\n".join(parte))


def campo_lucro(fechadas, solusd):
    """📅 lucro dela no dia, nos últimos 7 dias e nos últimos 30 dias (pelas operações fechadas)."""
    agora = time.time()
    hoje0 = agora - ((agora - 3 * 3600) % 86400)        # 00:00 de hoje em Brasília
    linhas = ["📅 <b>LUCRO DELA</b>"]
    for rot, ini in (("Hoje", hoje0), ("7 dias", agora - 7 * 86400), ("30 dias", agora - 30 * 86400)):
        ops = [x for x in fechadas if x.get("t1", 0) >= ini]
        r = sum(x["res"] for x in ops)
        inv = sum(x["investido"] for x in ops)
        g = sum(1 for x in ops if x["res"] > 0)
        if ops:
            linhas.append(f"<b>{rot}: {res_txt(r)} {r:+.2f} SOL ({'-' if r < 0 else '+'}{fmt_usd(r * solusd)}) · "
                          f"{sinal(r / inv * 100 if inv else 0)}</b> · {len(ops)} op. · {g} gain / {len(ops) - g} red")
        else:
            linhas.append(f"<b>{rot}:</b> nenhuma operação fechada")
    return "\n".join(linhas)


def enviar_resumo(e, solusd, primeira_vez):
    pos, fechadas, quando = e.get("pos", {}), e.get("fechadas", []), agora_brt()
    linhas, total_inv, total_agora = [], 0.0, 0.0
    for m, v in pos.items():
        info = info_moeda(m)
        medio = v["custo_aberto"] * solusd / v["tok"] if v["tok"] else 0
        total_inv += v["custo_aberto"]
        if info.get("px") and medio:
            var = (info["px"] / medio - 1) * 100
            l_agora = f"\n   <b>Agora: {fmt_px(info['px'])} · {res_txt(var)} {sinal(var)}</b>"
            total_agora += v["custo_aberto"] * (1 + var / 100)
        else:
            l_agora = ""; total_agora += v["custo_aberto"]
        linhas.append(f"• <b>{nome(info, m)}</b> — investido {v['custo_aberto']:.2f} SOL · {v.get('compras', 1)} compra(s)\n"
                      f"   🔵 <b>Entrada: {fmt_px(v.get('px1') or medio)}</b>\n"
                      f"   🟡 <b>Preço médio: {fmt_px(medio)}</b>{l_agora}\n"
                      f"   📊 MC {fmt_usd(info['mc'])} · "
                      f'<a href="https://gmgn.ai/sol/token/{m}">GMGN</a> · <a href="https://dexscreener.com/solana/{m}">DexScreener</a>')
    var_tot = (total_agora / total_inv - 1) * 100 if total_inv else 0
    cab = ((f"✅ <b>Monitor {VERSAO} ligado</b>\n" if primeira_vez else "") +
           f"📂 <b>POSIÇÕES ABERTAS — {len(pos)}</b>\n🕒 {quando} (Brasília)\n"
           f"👛 <a href=\"https://gmgn.ai/sol/address/{WALLET}\">Carteira {APELIDO}</a>\n"
           f"💼 <b>Em jogo: {total_inv:.2f} SOL · agora {res_txt(var_tot)} {sinal(var_tot)}</b> (ainda não vendeu)\n━━━━━━━━━━━━━━")
    enviar_em_partes(cab, linhas or ["(nenhuma)"])
    if not fechadas:
        telegram("✅ <b>OPERAÇÕES FECHADAS — 0</b>\nAinda não há operação fechada no histórico lido."); return
    ga = sum(1 for x in fechadas if x["res"] > 0)
    res_tot = sum(x["res"] for x in fechadas)
    inv_tot = sum(x["investido"] for x in fechadas)
    cab = (f"✅ <b>OPERAÇÕES FECHADAS — {len(fechadas)}</b>\n🕒 {quando} (Brasília)\n"
           f"🎯 <b>TAXA DE ACERTO: {ga / len(fechadas) * 100:.0f}%</b> ({ga} pagaram · {len(fechadas) - ga} red)\n"
           f"💼 <b>RESULTADO TOTAL: {res_txt(res_tot)} {res_tot:+.2f} SOL ({'-' if res_tot < 0 else '+'}{fmt_usd(res_tot * solusd)}) · "
           f"{sinal(res_tot / inv_tot * 100 if inv_tot else 0)}</b>\n━━━━━━━━━━━━━━\n"
           f"{campo_lucro(fechadas, solusd)}\n━━━━━━━━━━━━━━")
    linhas = []
    for x in reversed(fechadas[-40:]):
        d0 = time.strftime("%d/%m %H:%M", time.gmtime(x["t0"] - 3 * 3600))
        d1 = time.strftime("%d/%m %H:%M", time.gmtime(x["t1"] - 3 * 3600))
        horas = (x["t1"] - x["t0"]) / 3600
        nm = f'<a href="https://gmgn.ai/sol/token/{x["mint"]}">{x["s"] or x["mint"][:6]}</a>'
        linhas.append(f"<b>{res_txt(x['res'])}</b> · <b>{nm}</b> · <b>{sinal(x['pct'])}</b> ({x['res']:+.2f} SOL)\n"
                      f"   🔵 <b>Entrada: {fmt_px(x['px1'])}</b>\n"
                      f"   🟡 <b>Médio: {fmt_px(x['medio'])}</b>\n"
                      f"   🟣 <b>Saída: {fmt_px(x['saida'])}</b>\n"
                      f"   {d0} → {d1} · {horas:.1f} h · {x['compras']} compra(s) · {x['investido']:.2f} SOL")
    enviar_em_partes(cab, linhas)


# -------------------- loop --------------------
def checar(e):
    """uma checagem. Devolve True se o estado mudou."""
    primeira_vez = not e.get("ultima_sig")
    if not primeira_vez:
        nova = ultima_assinatura()
        if not nova or nova == e.get("ultima_sig"):
            return False
    txs = novas_transacoes(e.get("ultima_sig", ""))
    if not txs:
        return False      # a Helius ainda não indexou: tenta de novo na próxima checagem
    solusd = preco_sol()
    n = processar(e, txs, solusd)
    if n: print(f"{agora_brt()}  {n} alerta(s) enviados")
    if primeira_vez:
        enviar_resumo(e, solusd, True)
    return True


def resumo_do_dia(e):
    hoje = time.strftime("%Y-%m-%d", time.gmtime(time.time() - 3 * 3600))
    hora = int(time.strftime("%H", time.gmtime(time.time() - 3 * 3600)))
    if hora >= 20 and e.get("resumo_dia") != hoje:
        enviar_resumo(e, preco_sol(), False)
        e["resumo_dia"] = hoje
        return True
    return False


def main():
    falta = [n for n, v in (("HELIUS_KEY", HELIUS), ("WALLET", WALLET), ("TG_TOKEN", TG_TOKEN), ("TG_CHAT", TG_CHAT)) if not v]
    if falta:
        print("Faltam os secrets:", ", ".join(falta)); sys.exit(2)
    if GIT:   # pega o estado mais novo salvo pela execução anterior
        subprocess.run(["git", "pull", "--rebase"], capture_output=True)
    e = carregar_estado()
    inicio = time.time()
    ult_medio, ult_git, sujo = 0, time.time(), False
    print(f"{agora_brt()}  sinal {VERSAO} ligado — checando a cada {INTERVALO:.0f} s por {DURACAO/60:.0f} min")
    while time.time() - inicio < DURACAO:
        t0 = time.time()
        try:
            if checar(e):
                sujo = True
            if time.time() - ult_medio >= 60:
                ult_medio = time.time()
                if alerta_preco_medio(e, preco_sol()): sujo = True
            if resumo_do_dia(e): sujo = True
            if sujo:
                e["atualizado"] = int(time.time())
                salvar_estado(e)
                if time.time() - ult_git >= 120:
                    salvar_git("atualização"); ult_git = time.time(); sujo = False
        except Exception as ex:
            print(f"{agora_brt()}  erro:", ex)
        time.sleep(max(1.0, INTERVALO - (time.time() - t0)))
    e["atualizado"] = int(time.time())
    salvar_estado(e)
    salvar_git("fim do turno")
    print(f"{agora_brt()}  turno encerrado — a próxima execução continua daqui")
    sys.exit(0)


if __name__ == "__main__":
    main()
