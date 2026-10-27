"""Gera a massa de dados FICTÍCIOS (simulados) da operação de estoque e vendas em eventos.

Os dados imitam a rotina de um espaço de eventos: compras viram lotes com validade, os
produtos saem do depósito para cada evento, são vendidos nos pontos de venda e as sobras
voltam ao depósito. Lotes que vencem antes de serem usados geram perda por validade.

A quantidade enviada a cada evento segue o "método manual" descrito pela comunidade:
público estimado x consumo médio por pessoa x um ajuste aproximado por tipo de evento,
com 20% de margem. A demanda real depende também da temperatura e da combinação
categoria x tipo de evento, o que gera sobras e faltas que o modelo de ML tenta reduzir.

Uso:  python gerar_dados.py
"""
import math
from datetime import date, datetime, timedelta

import numpy as np

from auth import garantir_usuarios_demo
from db import DB_PATH, conectar, criar_banco

SEMENTE = 42
INICIO = date(2025, 3, 3)
HOJE = date(2026, 10, 27)
MARGEM_MANUAL = 0.20
USUARIOS = ["ana.estoque", "carlos.bar", "julia.supervisao", "rafael.deposito"]

# nome, categoria, unidade, preço, custo, validade (dias), embalagem,
# consumo base (unidades por 1.000 pessoas num evento de 5 h), sensibilidade à temperatura
PRODUTOS = [
    ("Cerveja lata 350ml", "Bebida alcoólica", "lata", 14.0, 4.5, 180, 24, 950, 0.035),
    ("Drink pronto lata 269ml", "Bebida alcoólica", "lata", 22.0, 7.0, 180, 12, 220, 0.030),
    ("Água mineral 500ml", "Bebida não alcoólica", "garrafa", 7.0, 1.2, 365, 12, 620, 0.045),
    ("Refrigerante lata 350ml", "Bebida não alcoólica", "lata", 8.0, 2.2, 240, 12, 360, 0.025),
    ("Energético 250ml", "Bebida não alcoólica", "lata", 15.0, 5.0, 365, 24, 110, 0.010),
    ("Suco natural 300ml", "Bebida não alcoólica", "garrafa", 12.0, 3.5, 5, 10, 110, 0.025),
    ("Café espresso", "Café", "dose", 7.0, 1.4, 120, 50, 55, -0.040),
    ("Hambúrguer artesanal", "Lanche", "un", 32.0, 11.0, 4, 20, 150, -0.005),
    ("Cachorro-quente", "Lanche", "un", 18.0, 5.0, 4, 30, 210, -0.005),
    ("Pizza (fatia)", "Lanche", "fatia", 20.0, 6.0, 3, 16, 120, -0.005),
    ("Sanduíche natural", "Lanche", "un", 18.0, 6.0, 3, 20, 65, 0.010),
    ("Batata frita (porção)", "Lanche", "porção", 22.0, 6.0, 90, 25, 170, -0.005),
    ("Pão de queijo (porção)", "Snack", "porção", 12.0, 3.0, 30, 50, 130, -0.025),
    ("Pipoca", "Snack", "saco", 12.0, 2.5, 60, 50, 140, 0.000),
    ("Salgadinho (pacote)", "Snack", "pacote", 9.0, 3.0, 120, 40, 85, 0.000),
    ("Churros", "Doce", "un", 14.0, 3.5, 3, 30, 85, -0.015),
    ("Sorvete (picolé)", "Doce", "un", 10.0, 3.0, 180, 40, 95, 0.070),
    ("Chocolate (barra)", "Doce", "un", 9.0, 3.2, 240, 30, 55, -0.010),
]

# peso no sorteio, faixa de público, faixa de duração (h), dias da semana possíveis
# (0 = segunda), hora de início e multiplicador de consumo por categoria
TIPOS_EVENTO = {
    "Show": dict(
        peso=0.36, publico=(3500, 9500), duracao=(4, 5), dias=[4, 5], hora=20,
        mult={"Bebida alcoólica": 1.25, "Bebida não alcoólica": 0.95, "Lanche": 1.0,
              "Snack": 0.9, "Doce": 0.85, "Café": 0.5}),
    "Festival": dict(
        peso=0.14, publico=(7000, 12000), duracao=(9, 12), dias=[5, 6], hora=13,
        mult={"Bebida alcoólica": 1.45, "Bebida não alcoólica": 1.35, "Lanche": 1.45,
              "Snack": 1.1, "Doce": 1.1, "Café": 0.7}),
    "Evento corporativo": dict(
        peso=0.20, publico=(300, 2000), duracao=(5, 8), dias=[1, 2, 3], hora=9,
        mult={"Bebida alcoólica": 0.45, "Bebida não alcoólica": 1.15, "Lanche": 0.85,
              "Snack": 0.7, "Doce": 0.8, "Café": 2.8}),
    "Evento infantil": dict(
        peso=0.14, publico=(1200, 4000), duracao=(4, 6), dias=[5, 6], hora=10,
        mult={"Bebida alcoólica": 0.25, "Bebida não alcoólica": 1.45, "Lanche": 1.1,
              "Snack": 1.6, "Doce": 1.9, "Café": 0.7}),
    "E-sports": dict(
        peso=0.16, publico=(1500, 5000), duracao=(7, 10), dias=[5, 6], hora=12,
        mult={"Bebida alcoólica": 0.8, "Bebida não alcoólica": 1.25, "Lanche": 1.2,
              "Snack": 1.35, "Doce": 1.0, "Café": 0.6}),
}
EXTRA_PRODUTO_TIPO = {("Energético 250ml", "E-sports"): 2.2, ("Sorvete (picolé)", "Evento infantil"): 1.3}

NOMES_EVENTO = {
    "Show": ["Show Banda Horizonte", "Show Luna Vidal", "Show Os Quatro Ventos", "Show DJ Kairo",
             "Show Trio Asfalto", "Show Marina Sol", "Show Coletivo Batuque", "Show Rafa Moreno",
             "Show Noite do Samba", "Show Tributo ao Rock Nacional"],
    "Festival": ["Festival Sabores do Brasil", "Festival Indie Paulista", "Festival Eletrônico Aurora",
                 "Festival Sertanejo de Verão", "Festival Cultura de Rua"],
    "Evento corporativo": ["Convenção de Vendas TechSul", "Encontro Anual Agro Vale", "Summit Fintech",
                           "Feira de Inovação no Varejo", "Congresso de Logística", "Workshop RH em Foco"],
    "Evento infantil": ["Domingo em Família: Circo", "Festa das Crianças", "Musical Infantil Estrelinha",
                        "Domingo em Família: Ciência Divertida"],
    "E-sports": ["Copa Arena de Games", "Final da Liga Universitária de E-sports", "Torneio Battle Cup",
                 "Campeonato Paulista de Jogos de Luta"],
}

# Temperatura média de São Paulo por mês (°C), usada como previsão do dia
TEMP_MES = {1: 26, 2: 26, 3: 25, 4: 23, 5: 20, 6: 19, 7: 18, 8: 20, 9: 21, 10: 23, 11: 24, 12: 25}

PONTOS_VENDA = {
    "Bebida alcoólica": ["Bar Central", "Bar Pista", "Quiosque Norte"],
    "Bebida não alcoólica": ["Bar Central", "Bar Pista", "Quiosque Norte"],
    "Café": ["Praça de Alimentação", "Quiosque Norte"],
    "Lanche": ["Praça de Alimentação", "Quiosque Norte"],
    "Snack": ["Praça de Alimentação", "Quiosque Norte"],
    "Doce": ["Praça de Alimentação", "Quiosque Norte"],
}

FORMAS_PAGAMENTO = ["Pix", "Crédito", "Débito", "Pulseira cashless"]
PESOS_PAGAMENTO = [0.38, 0.27, 0.20, 0.15]

rng = np.random.default_rng(SEMENTE)


def arredonda_embalagem(qtd: float, embalagem: int) -> int:
    return int(math.ceil(max(qtd, 0) / embalagem) * embalagem)


def fator_duracao(horas: float) -> float:
    return (horas / 5) ** 0.5


def sortear_eventos():
    tipos = list(TIPOS_EVENTO)
    pesos = np.array([TIPOS_EVENTO[t]["peso"] for t in tipos])
    eventos = []
    semana = INICIO
    fim_planejados = date(2026, 12, 20)
    while semana <= fim_planejados:
        qtd_semana = 1 + int(rng.random() < 0.25)
        for _ in range(qtd_semana if rng.random() < 0.95 else 0):
            tipo = str(rng.choice(tipos, p=pesos / pesos.sum()))
            cfg = TIPOS_EVENTO[tipo]
            dia = semana + timedelta(days=int(rng.choice(cfg["dias"])))
            publico_est = int(round(rng.uniform(*cfg["publico"]), -2))
            duracao = float(rng.integers(cfg["duracao"][0], cfg["duracao"][1] + 1))
            temp_prev = round(TEMP_MES[dia.month] + rng.normal(0, 2.5) - (2 if cfg["hora"] >= 19 else 0), 1)
            eventos.append(dict(
                nome=f"{rng.choice(NOMES_EVENTO[tipo])}", tipo=tipo, data=dia, hora_inicio=cfg["hora"],
                duracao_h=duracao, publico_estimado=publico_est, temperatura_prevista=temp_prev))
        semana += timedelta(days=7)
    eventos.sort(key=lambda e: e["data"])
    # nomes repetidos ganham a edição (ex.: "Festival Indie Paulista - 2ª edição")
    vistos = {}
    for ev in eventos:
        vistos[ev["nome"]] = vistos.get(ev["nome"], 0) + 1
        if vistos[ev["nome"]] > 1:
            ev["nome"] = f"{ev['nome']} - {vistos[ev['nome']]}ª edição"
        ev["status"] = "Realizado" if ev["data"] < HOJE else "Planejado"
    return eventos


def main():
    if DB_PATH.exists():
        DB_PATH.unlink()
    con = conectar()
    criar_banco(con)
    garantir_usuarios_demo(con)
    cur = con.cursor()

    produtos = []
    for i, (nome, cat, un, preco, custo, val, emb, base, sens) in enumerate(PRODUTOS, start=1):
        cur.execute(
            "INSERT INTO produtos (id, nome, categoria, unidade, preco_venda, custo_unitario, validade_dias,"
            " embalagem, estoque_minimo) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (i, nome, cat, un, round(preco * 1.06, 2), round(custo * 1.06, 2), val, emb,
             emb * 2 if val > 30 else 0))  # preços de cadastro já com o reajuste de 2026
        produtos.append(dict(id=i, nome=nome, categoria=cat, preco=preco, custo=custo, validade=val,
                             embalagem=emb, base=base, sens=sens))

    eventos = sortear_eventos()
    lotes = {p["id"]: [] for p in produtos}  # lotes em aberto: dict(id, validade, saldo, custo)
    seq_lote = 0
    movs, vendas = [], []

    def registrar_perdas_vencidas(ate: date):
        for p in produtos:
            for lote in lotes[p["id"]]:
                if lote["saldo"] > 0 and lote["validade"] < ate:
                    quando = datetime.combine(lote["validade"] + timedelta(days=1), datetime.min.time()) + timedelta(hours=8)
                    movs.append((quando, "PERDA_VALIDADE", p["id"], lote["id"], None, lote["saldo"],
                                 str(rng.choice(USUARIOS)), "Descarte por vencimento"))
                    lote["saldo"] = 0
            lotes[p["id"]] = [l for l in lotes[p["id"]] if l["saldo"] > 0]

    meses_desde_inicio = lambda d: (d.year - INICIO.year) * 12 + d.month - INICIO.month

    for ev_id, ev in enumerate(eventos, start=1):
        ev["id"] = ev_id
        cfg = TIPOS_EVENTO[ev["tipo"]]
        realizado = ev["status"] == "Realizado"
        publico_real = int(ev["publico_estimado"] * rng.normal(1.0, 0.07)) if realizado else None
        cur.execute(
            "INSERT INTO eventos (id, nome, tipo, data, hora_inicio, duracao_h, publico_estimado, publico_real,"
            " temperatura_prevista, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (ev_id, ev["nome"], ev["tipo"], ev["data"].isoformat(), ev["hora_inicio"], ev["duracao_h"],
             ev["publico_estimado"], publico_real, ev["temperatura_prevista"], ev["status"]))
        if not realizado:
            continue

        registrar_perdas_vencidas(ev["data"])
        inicio_ev = datetime.combine(ev["data"], datetime.min.time()) + timedelta(hours=ev["hora_inicio"])
        temp_real = ev["temperatura_prevista"] + rng.normal(0, 1.5)
        reajuste = 1.06 if ev["data"] >= date(2026, 1, 1) else 1.0
        tendencia = 1 + 0.004 * meses_desde_inicio(ev["data"])

        for p in produtos:
            mult_tipo = cfg["mult"][p["categoria"]] * EXTRA_PRODUTO_TIPO.get((p["nome"], ev["tipo"]), 1.0)

            # Planejamento manual: conhece só aproximadamente o efeito do tipo de evento
            estimativa_manual = (p["base"] / 1000 * ev["publico_estimado"] * fator_duracao(ev["duracao_h"])
                                 * (1 + 0.5 * (mult_tipo - 1)))
            plano = arredonda_embalagem(estimativa_manual * (1 + MARGEM_MANUAL), p["embalagem"])

            # Compra: perecíveis só o necessário; não perecíveis compram também para o próximo evento
            disponivel = sum(l["saldo"] for l in lotes[p["id"]] if l["validade"] >= ev["data"])
            falta = plano - disponivel
            if falta > 0:
                qtd_compra = falta if p["validade"] <= 7 else falta + plano
                qtd_compra = arredonda_embalagem(qtd_compra, p["embalagem"])
                antecedencia = int(rng.integers(1, 3)) if p["validade"] <= 7 else int(rng.integers(2, 7))
                entrada = ev["data"] - timedelta(days=antecedencia)
                validade = entrada + timedelta(days=p["validade"])
                seq_lote += 1
                custo = round(p["custo"] * reajuste, 2)
                codigo = f"L{entrada:%y%m%d}-{p['id']:02d}-{seq_lote:05d}"
                cur.execute(
                    "INSERT INTO lotes (id, produto_id, codigo, data_entrada, data_validade, quantidade_inicial,"
                    " custo_unitario) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (seq_lote, p["id"], codigo, entrada.isoformat(), validade.isoformat(), qtd_compra, custo))
                movs.append((datetime.combine(entrada, datetime.min.time()) + timedelta(hours=10), "ENTRADA",
                             p["id"], seq_lote, None, qtd_compra, str(rng.choice(USUARIOS)), "Recebimento de compra"))
                lotes[p["id"]].append(dict(id=seq_lote, validade=validade, saldo=qtd_compra))

            # Envio ao evento (FIFO pela validade)
            enviados = []  # [lote_id, quantidade enviada, quantidade ainda no evento]
            restante = plano
            for lote in sorted(lotes[p["id"]], key=lambda l: l["validade"]):
                if restante == 0:
                    break
                if lote["validade"] < ev["data"] or lote["saldo"] == 0:
                    continue
                q = min(restante, lote["saldo"])
                lote["saldo"] -= q
                restante -= q
                enviados.append([lote["id"], q, q])
                movs.append((inicio_ev - timedelta(hours=3), "SAIDA_EVENTO", p["id"], lote["id"], ev_id, q,
                             str(rng.choice(USUARIOS)), "Abastecimento do evento"))

            # Demanda real e vendas (limitadas ao que foi enviado)
            demanda_media = (p["base"] / 1000 * publico_real * mult_tipo * fator_duracao(ev["duracao_h"])
                             * max(0.3, 1 + p["sens"] * (temp_real - 22)) * tendencia)
            demanda = int(rng.poisson(demanda_media * rng.lognormal(0, 0.10)))
            vendido_total = min(demanda, plano)
            if vendido_total > 0:
                pdvs = PONTOS_VENDA[p["categoria"]]
                horas = int(ev["duracao_h"])
                curva = np.exp(-0.5 * ((np.arange(horas) - 0.6 * horas) / (0.35 * horas)) ** 2)
                peso_pdv = rng.dirichlet(np.ones(len(pdvs)) * 4)
                pesos = (peso_pdv[:, None] * curva[None, :]).ravel()
                partes = rng.multinomial(vendido_total, pesos / pesos.sum()).reshape(len(pdvs), horas)
                preco = round(p["preco"] * reajuste, 2)
                for h in range(horas):
                    for k, pdv in enumerate(pdvs):
                        q = int(partes[k, h])
                        # cada transação no caixa leva de 1 a poucas unidades
                        tamanhos = rng.geometric(0.6, size=q)
                        tamanhos = tamanhos[np.cumsum(tamanhos) <= q].tolist()
                        if sum(tamanhos) < q:
                            tamanhos.append(q - sum(tamanhos))
                        segundos = np.sort(rng.integers(0, 3600, size=len(tamanhos)))
                        pagamentos = rng.choice(FORMAS_PAGAMENTO, size=len(tamanhos), p=PESOS_PAGAMENTO)
                        for t, seg, pag in zip(tamanhos, segundos, pagamentos):
                            momento = inicio_ev + timedelta(hours=h, seconds=int(seg))
                            for env in enviados:  # baixa FIFO nos lotes enviados
                                if t == 0:
                                    break
                                usar = min(t, env[2])
                                if usar:
                                    env[2] -= usar
                                    t -= usar
                                    vendas.append((momento, ev_id, p["id"], env[0], pdv, usar, preco, str(pag)))

            # Sobras voltam ao depósito
            fim_ev = inicio_ev + timedelta(hours=ev["duracao_h"] + 1)
            for lote_id, _, sobra in enviados:
                if sobra:
                    movs.append((fim_ev, "RETORNO_EVENTO", p["id"], lote_id, ev_id, sobra,
                                 str(rng.choice(USUARIOS)), "Sobra devolvida ao depósito"))
                    next(l for l in lotes[p["id"]] if l["id"] == lote_id)["saldo"] += sobra

            # Avarias ocasionais no depósito (latas amassadas, garrafas quebradas...)
            if p["validade"] > 30 and rng.random() < 0.08:
                lote = max(lotes[p["id"]], key=lambda l: l["saldo"], default=None)
                if lote and lote["saldo"] > 0:
                    q = min(lote["saldo"], int(rng.integers(1, 6)))
                    lote["saldo"] -= q
                    movs.append((fim_ev + timedelta(days=1), "PERDA_AVARIA", p["id"], lote["id"], None, q,
                                 str(rng.choice(USUARIOS)), "Avaria identificada na conferência"))

    registrar_perdas_vencidas(HOJE)

    movs.sort(key=lambda m: m[0])
    cur.executemany(
        "INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, evento_id, quantidade, usuario, observacao)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(m[0].isoformat(sep=" ", timespec="minutes"),) + m[1:] for m in movs])
    vendas.sort(key=lambda v: v[0])
    cur.executemany(
        "INSERT INTO vendas (data_hora, evento_id, produto_id, lote_id, ponto_venda, quantidade, preco_unitario,"
        " forma_pagamento) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(v[0].isoformat(sep=" ", timespec="seconds"),) + v[1:] for v in vendas])
    con.commit()

    n_real = sum(e["status"] == "Realizado" for e in eventos)
    print(f"Banco gerado em {DB_PATH}")
    print(f"  produtos: {len(produtos)} | eventos realizados: {n_real} | planejados: {len(eventos) - n_real}")
    print(f"  lotes: {seq_lote} | movimentações: {len(movs)} | registros de venda: {len(vendas)}")
    con.close()


if __name__ == "__main__":
    main()
