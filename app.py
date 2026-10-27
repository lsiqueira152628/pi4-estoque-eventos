"""Interface da solução: captura de dados de estoque/vendas, painéis e resultados do ML.

Uso:  streamlit run app.py
"""
import json
import math
import os
from datetime import date, datetime, timedelta

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import gerar_dados
import ml
from db import DB_PATH, conectar, consultar, criar_banco

st.set_page_config(page_title="Estoque e Vendas em Eventos", page_icon="📦", layout="wide")

CORES = px.colors.qualitative.Safe
px.defaults.color_discrete_sequence = CORES
PONTOS_VENDA = ["Bar Central", "Bar Pista", "Quiosque Norte", "Praça de Alimentação"]
FORMAS_PAGAMENTO = ["Pix", "Crédito", "Débito", "Pulseira cashless"]
# Na versão publicada o retreino fica desligado, para os testes do grupo não alterarem as métricas do relatório.
# Para liberar: PERMITIR_RETREINO = "1" nos Secrets do Streamlit Cloud (ou variável de ambiente local).
PERMITIR_RETREINO = os.environ.get("PERMITIR_RETREINO", "0") == "1"


@st.cache_resource
def get_con():
    if not DB_PATH.exists():  # primeira execução (ex.: na nuvem): gera a massa simulada e treina o modelo
        with st.spinner("Primeira execução: gerando os dados simulados e treinando o modelo (cerca de 2 minutos)..."):
            gerar_dados.main()
            con = conectar()
            ml.treinar(con)
            con.close()
    con = conectar()
    criar_banco(con)
    auth.garantir_usuarios_demo(con)
    return con


con = get_con()


def q(sql, params=()):
    return consultar(con, sql, params)


def brl(v):
    return f"R$ {v:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def num(v):
    return f"{v:,.0f}".replace(",", ".")


def brl_curto(v):
    """Valor monetário abreviado para os cartões de indicadores (ex.: R$ 33,5 mi)."""
    for limite, sufixo in ((1e6, "mi"), (1e3, "mil")):
        if abs(v) >= limite:
            return f"R$ {v / limite:,.1f} {sufixo}".replace(".", ",")
    return brl(v)


def num_curto(v):
    return f"{v / 1e6:,.2f} mi".replace(".", ",") if abs(v) >= 1e6 else num(v)


def agora(segundos=False):
    return datetime.now().isoformat(sep=" ", timespec="seconds" if segundos else "minutes")


# ---------------------------------------------------------------- consultas (com cache)
@st.cache_data
def vendas_agregadas():
    return q("""
        SELECT v.evento_id, v.produto_id, v.ponto_venda, v.forma_pagamento,
               (CAST(strftime('%H', v.data_hora) AS INTEGER) - e.hora_inicio + 24) % 24 AS hora,
               SUM(v.quantidade) AS unidades, SUM(v.quantidade * v.preco_unitario) AS receita, COUNT(*) AS transacoes
        FROM vendas v JOIN eventos e ON e.id = v.evento_id
        GROUP BY 1, 2, 3, 4, 5""")


@st.cache_data
def eventos():
    df = q("SELECT * FROM eventos ORDER BY data, id")
    df["data"] = pd.to_datetime(df["data"])
    return df


@st.cache_data
def produtos():
    return q("SELECT * FROM produtos ORDER BY categoria, nome")


@st.cache_data
def perdas():
    df = q("""SELECT m.data_hora, m.tipo, m.produto_id, m.quantidade, m.quantidade * l.custo_unitario AS custo
              FROM movimentacoes m LEFT JOIN lotes l ON l.id = m.lote_id
              WHERE m.tipo IN ('PERDA_VALIDADE', 'PERDA_AVARIA')""")
    df["data_hora"] = pd.to_datetime(df["data_hora"])
    return df


@st.cache_data
def metricas_modelo():
    return json.loads(ml.ARQ_METRICAS.read_text(encoding="utf-8")) if ml.ARQ_METRICAS.exists() else None


def saldo_lotes():
    return q("SELECT * FROM vw_saldo_lotes WHERE saldo <> 0 ORDER BY produto, data_validade")


def estoque_no_evento(evento_id):
    return q("""
        SELECT m.produto_id, p.nome AS produto, m.lote_id, l.codigo, l.data_validade,
               SUM(CASE WHEN m.tipo = 'SAIDA_EVENTO' THEN m.quantidade ELSE -m.quantidade END)
               - COALESCE((SELECT SUM(v.quantidade) FROM vendas v
                           WHERE v.evento_id = m.evento_id AND v.lote_id = m.lote_id), 0) AS saldo_evento
        FROM movimentacoes m
        JOIN produtos p ON p.id = m.produto_id
        JOIN lotes l ON l.id = m.lote_id
        WHERE m.evento_id = ? AND m.tipo IN ('SAIDA_EVENTO', 'RETORNO_EVENTO')
        GROUP BY m.produto_id, m.lote_id
        HAVING saldo_evento > 0
        ORDER BY p.nome, l.data_validade""", (evento_id,))


def eventos_abertos():
    """Eventos planejados que já receberam produtos (podem registrar vendas)."""
    return q("""SELECT DISTINCT e.id, e.nome, e.data FROM eventos e
                JOIN movimentacoes m ON m.evento_id = e.id AND m.tipo = 'SAIDA_EVENTO'
                WHERE e.status = 'Planejado' ORDER BY e.data""")


def invalidar():
    st.cache_data.clear()


def rotulo_evento(row):
    data = pd.to_datetime(row["data"]).strftime("%d/%m/%Y")
    return f"{data} · {row['nome']}"


# ---------------------------------------------------------------- operações de escrita
def registrar_entrada(produto_id, quantidade, validade, custo, usuario, obs):
    hoje = date.today()
    prox = q("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM lotes")["n"].iloc[0]
    codigo = f"L{hoje:%y%m%d}-{produto_id:02d}-{prox:05d}"
    cur = con.execute(
        "INSERT INTO lotes (produto_id, codigo, data_entrada, data_validade, quantidade_inicial, custo_unitario)"
        " VALUES (?, ?, ?, ?, ?, ?)", (produto_id, codigo, hoje.isoformat(), validade.isoformat(), quantidade, custo))
    con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, quantidade, usuario, observacao)"
                " VALUES (?, 'ENTRADA', ?, ?, ?, ?, ?)", (agora(), produto_id, cur.lastrowid, quantidade, usuario, obs))
    con.commit()
    invalidar()
    return codigo


def enviar_para_evento(evento_id, produto_id, quantidade, usuario):
    """Baixa FIFO (primeiro a vencer, primeiro a sair) só de lotes válidos na data do evento."""
    data_ev = q("SELECT data FROM eventos WHERE id = ?", (evento_id,))["data"].iloc[0]
    lotes = q("SELECT lote_id, saldo FROM vw_saldo_lotes WHERE produto_id = ? AND saldo > 0 AND data_validade >= ?"
              " ORDER BY data_validade", (produto_id, data_ev))
    if lotes["saldo"].sum() < quantidade:
        raise ValueError(f"Estoque válido insuficiente: há {num(lotes['saldo'].sum())} unidades no depósito.")
    restante = quantidade
    for lote in lotes.itertuples():
        usar = min(restante, int(lote.saldo))
        con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, evento_id, quantidade, usuario,"
                    " observacao) VALUES (?, 'SAIDA_EVENTO', ?, ?, ?, ?, ?, 'Abastecimento do evento')",
                    (agora(), produto_id, lote.lote_id, evento_id, usar, usuario))
        restante -= usar
        if restante == 0:
            break
    con.commit()
    invalidar()


def registrar_venda(evento_id, produto_id, quantidade, ponto_venda, pagamento):
    estoque = estoque_no_evento(evento_id)
    estoque = estoque[estoque["produto_id"] == produto_id]
    if estoque["saldo_evento"].sum() < quantidade:
        raise ValueError(f"Só há {num(estoque['saldo_evento'].sum())} unidades deste produto no evento.")
    preco = q("SELECT preco_venda FROM produtos WHERE id = ?", (produto_id,))["preco_venda"].iloc[0]
    restante = quantidade
    for lote in estoque.itertuples():
        usar = min(restante, int(lote.saldo_evento))
        con.execute("INSERT INTO vendas (data_hora, evento_id, produto_id, lote_id, ponto_venda, quantidade,"
                    " preco_unitario, forma_pagamento) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (agora(True), evento_id, produto_id, lote.lote_id, ponto_venda, usar, preco, pagamento))
        restante -= usar
        if restante == 0:
            break
    con.commit()
    invalidar()
    return preco * quantidade


def encerrar_evento(evento_id, publico_real, usuario):
    sobras = estoque_no_evento(evento_id)
    for s in sobras.itertuples():
        con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, evento_id, quantidade, usuario,"
                    " observacao) VALUES (?, 'RETORNO_EVENTO', ?, ?, ?, ?, ?, 'Sobra devolvida ao depósito')",
                    (agora(), s.produto_id, s.lote_id, evento_id, int(s.saldo_evento), usuario))
    con.execute("UPDATE eventos SET status = 'Realizado', publico_real = ? WHERE id = ?", (publico_real, evento_id))
    con.commit()
    invalidar()
    return int(sobras["saldo_evento"].sum()) if len(sobras) else 0


def registrar_perda_ou_ajuste(tipo, produto_id, lote_id, quantidade, usuario, obs):
    con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, quantidade, usuario, observacao)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)", (agora(), tipo, produto_id, lote_id, quantidade, usuario, obs))
    con.commit()
    invalidar()


# ---------------------------------------------------------------- páginas
def pagina_painel():
    st.title("📊 Painel da operação")
    ev = eventos()
    realizados = ev[ev["status"] == "Realizado"]
    c1, c2 = st.columns([2, 3])
    ini, fim = realizados["data"].min().date(), realizados["data"].max().date()
    periodo = c1.date_input("Período", (ini, fim), min_value=ini, max_value=fim, format="DD/MM/YYYY")
    tipos = c2.multiselect("Tipos de evento", sorted(ev["tipo"].unique()), default=sorted(ev["tipo"].unique()))
    if len(periodo) != 2 or not tipos:
        st.info("Selecione o período completo e ao menos um tipo de evento.")
        return
    sel = realizados[realizados["data"].dt.date.between(*periodo) & realizados["tipo"].isin(tipos)]
    vendas = (vendas_agregadas().merge(sel[["id", "nome", "tipo", "data", "publico_real"]], left_on="evento_id",
                                       right_on="id")
              .merge(produtos()[["id", "nome", "categoria"]].rename(columns={"id": "pid", "nome": "produto"}),
                     left_on="produto_id", right_on="pid"))
    pd_perdas = perdas()
    pd_perdas = pd_perdas[pd_perdas["data_hora"].dt.date.between(*periodo)]
    estoque = saldo_lotes()

    k = st.columns(3) + st.columns(3)
    k[0].metric("Faturamento", brl_curto(vendas["receita"].sum()))
    k[1].metric("Unidades vendidas", num_curto(vendas["unidades"].sum()))
    k[2].metric("Transações", num_curto(vendas["transacoes"].sum()))
    k[3].metric("Ticket médio", brl(vendas["receita"].sum() / max(vendas["transacoes"].sum(), 1)))
    k[4].metric("Perdas", brl_curto(pd_perdas["custo"].sum()), help="Vencimento + avaria, a preço de custo")
    k[5].metric("Estoque no depósito", brl_curto((estoque["saldo"] * estoque["custo_unitario"]).sum()))

    g1, g2 = st.columns(2)
    mensal = vendas.assign(mes=vendas["data"].dt.to_period("M").dt.to_timestamp()).groupby(
        ["mes", "tipo"], as_index=False)["receita"].sum()
    g1.plotly_chart(px.bar(mensal, x="mes", y="receita", color="tipo", title="Faturamento por mês e tipo de evento",
                           labels={"mes": "", "receita": "R$", "tipo": "Tipo"}), width="stretch")

    publico = sel.set_index("tipo")["publico_real"].groupby(level=0).sum()
    consumo = vendas.groupby(["categoria", "tipo"])["unidades"].sum().unstack()
    consumo = (consumo / publico * 1000).round(0)
    g2.plotly_chart(px.imshow(consumo, text_auto=True, color_continuous_scale="Blues", aspect="auto",
                              title="Padrão de consumo: unidades por 1.000 pessoas",
                              labels={"x": "Tipo de evento", "y": "", "color": "un./1.000"}),
                    width="stretch")

    g3, g4 = st.columns(2)
    top = vendas.groupby("produto", as_index=False)["receita"].sum().nlargest(10, "receita").sort_values("receita")
    g3.plotly_chart(px.bar(top, x="receita", y="produto", orientation="h", title="Top 10 produtos por faturamento",
                           labels={"receita": "R$", "produto": ""}), width="stretch")
    hora = vendas.groupby(["hora", "tipo"], as_index=False)["unidades"].sum()
    hora["unidades"] = hora["unidades"] / hora["tipo"].map(sel.groupby("tipo").size())
    g4.plotly_chart(px.line(hora.sort_values("hora"), x="hora", y="unidades", color="tipo", markers=True,
                            title="Vendas ao longo do evento (média por evento)",
                            labels={"hora": "Horas desde a abertura", "unidades": "Unidades", "tipo": "Tipo"}),
                    width="stretch")

    g5, g6 = st.columns(2)
    pm = pd_perdas.assign(mes=pd_perdas["data_hora"].dt.to_period("M").dt.to_timestamp()).groupby(
        ["mes", "tipo"], as_index=False)["custo"].sum()
    pm["tipo"] = pm["tipo"].map({"PERDA_VALIDADE": "Vencimento", "PERDA_AVARIA": "Avaria"})
    g5.plotly_chart(px.bar(pm, x="mes", y="custo", color="tipo", title="Perdas por mês (custo)",
                           labels={"mes": "", "custo": "R$", "tipo": "Motivo"},
                           color_discrete_map={"Vencimento": "#CC6677", "Avaria": "#DDCC77"}),
                    width="stretch")
    pag = vendas.groupby(["ponto_venda", "forma_pagamento"], as_index=False)["receita"].sum()
    g6.plotly_chart(px.bar(pag, x="ponto_venda", y="receita", color="forma_pagamento",
                           title="Faturamento por ponto de venda e forma de pagamento",
                           labels={"ponto_venda": "", "receita": "R$", "forma_pagamento": "Pagamento"}),
                    width="stretch")

    st.subheader("Eventos do período")
    por_evento = vendas.groupby(["data", "nome", "tipo", "publico_real"], as_index=False).agg(
        receita=("receita", "sum"), unidades=("unidades", "sum"))
    por_evento["receita_por_pessoa"] = por_evento["receita"] / por_evento["publico_real"]
    st.dataframe(por_evento.sort_values("data", ascending=False), hide_index=True, width="stretch",
                 column_config={"data": st.column_config.DateColumn("Data", format="DD/MM/YYYY"),
                                "nome": "Evento", "tipo": "Tipo", "publico_real": "Público",
                                "receita": st.column_config.NumberColumn("Faturamento", format="R$ %.2f"),
                                "unidades": "Unidades",
                                "receita_por_pessoa": st.column_config.NumberColumn("R$/pessoa", format="R$ %.2f")})


def pagina_venda(usuario):
    st.title("🛒 Registrar venda")
    abertos = eventos_abertos()
    if abertos.empty:
        st.info("Nenhum evento em andamento. Envie produtos a um evento planejado em **Movimentações > Envio "
                "para evento** para liberar o registro de vendas.")
        return
    idx = st.selectbox("Evento", abertos.index, format_func=lambda i: rotulo_evento(abertos.loc[i]))
    evento_id = int(abertos.loc[idx, "id"])
    estoque = estoque_no_evento(evento_id)
    if estoque.empty:
        st.warning("Este evento não tem mais produtos disponíveis.")
        return
    disp = estoque.groupby(["produto_id", "produto"], as_index=False)["saldo_evento"].sum()
    with st.form("venda", clear_on_submit=True):
        c1, c2 = st.columns(2)
        pi = c1.selectbox("Produto", disp.index,
                          format_func=lambda i: f"{disp.loc[i, 'produto']} (disponível: {num(disp.loc[i, 'saldo_evento'])})")
        qtd = c2.number_input("Quantidade", min_value=1, value=1, step=1)
        c3, c4 = st.columns(2)
        pdv = c3.selectbox("Ponto de venda", PONTOS_VENDA)
        pag = c4.radio("Forma de pagamento", FORMAS_PAGAMENTO, horizontal=True)
        if st.form_submit_button("Registrar venda", type="primary", width="stretch"):
            try:
                total = registrar_venda(evento_id, int(disp.loc[pi, "produto_id"]), int(qtd), pdv, pag)
                st.success(f"Venda registrada: {qtd} × {disp.loc[pi, 'produto']} = {brl(total)}")
            except ValueError as e:
                st.error(str(e))
    st.subheader("Estoque no evento")
    st.dataframe(estoque_no_evento(evento_id)[["produto", "codigo", "data_validade", "saldo_evento"]],
                 hide_index=True, width="stretch",
                 column_config={"produto": "Produto", "codigo": "Lote", "data_validade": "Validade",
                                "saldo_evento": "Disponível"})
    ultimas = q("""SELECT v.data_hora, p.nome AS produto, v.quantidade, v.ponto_venda, v.forma_pagamento,
                   v.quantidade * v.preco_unitario AS total
                   FROM vendas v JOIN produtos p ON p.id = v.produto_id
                   WHERE v.evento_id = ? ORDER BY v.id DESC LIMIT 20""", (evento_id,))
    if not ultimas.empty:
        st.subheader("Últimas vendas deste evento")
        st.dataframe(ultimas, hide_index=True, width="stretch")


def pagina_movimentacoes(usuario):
    st.title("📦 Movimentações de estoque")
    prods = produtos()
    nome_prod = dict(zip(prods["id"], prods["nome"]))
    abas = st.tabs(["Entrada (compra)", "Envio para evento", "Encerrar evento", "Perda / ajuste", "Histórico"])

    with abas[0]:
        with st.form("entrada", clear_on_submit=True):
            c1, c2 = st.columns(2)
            pid = c1.selectbox("Produto", prods["id"], format_func=nome_prod.get)
            p = prods.set_index("id").loc[pid]
            qtd = c2.number_input(f"Quantidade (embalagem de {p['embalagem']})", min_value=1,
                                  value=int(p["embalagem"]), step=int(p["embalagem"]))
            c3, c4 = st.columns(2)
            validade = c3.date_input("Data de validade", date.today() + timedelta(days=int(p["validade_dias"])),
                                     format="DD/MM/YYYY")
            custo = c4.number_input("Custo unitário (R$)", min_value=0.0, value=float(p["custo_unitario"]), step=0.1)
            obs = st.text_input("Observação", "Recebimento de compra")
            if st.form_submit_button("Registrar entrada", type="primary"):
                if validade <= date.today():
                    st.error("A validade precisa ser posterior a hoje.")
                else:
                    st.success(f"Lote {registrar_entrada(int(pid), int(qtd), validade, custo, usuario, obs)} registrado.")

    with abas[1]:
        ev = eventos()
        plan = ev[ev["status"] == "Planejado"].reset_index(drop=True)
        if plan.empty:
            st.info("Cadastre um evento planejado em **Cadastros**.")
        else:
            with st.form("envio"):
                ei = st.selectbox("Evento", plan.index, format_func=lambda i: rotulo_evento(plan.loc[i]))
                c1, c2 = st.columns(2)
                pid = c1.selectbox("Produto", prods["id"], format_func=nome_prod.get, key="envio_prod")
                qtd = c2.number_input("Quantidade", min_value=1, value=100, step=1)
                if st.form_submit_button("Enviar para o evento", type="primary"):
                    try:
                        enviar_para_evento(int(plan.loc[ei, "id"]), int(pid), int(qtd), usuario)
                        st.success(f"{num(qtd)} × {nome_prod[pid]} enviados para {plan.loc[ei, 'nome']}.")
                    except ValueError as e:
                        st.error(str(e))
            st.caption("A quantidade sugerida pelo modelo de ML para cada produto está na página **Previsão de demanda**, "
                       "que também permite enviar tudo de uma vez.")

    with abas[2]:
        abertos = eventos_abertos()
        if abertos.empty:
            st.info("Nenhum evento em andamento.")
        else:
            with st.form("encerrar"):
                ei = st.selectbox("Evento", abertos.index, format_func=lambda i: rotulo_evento(abertos.loc[i]))
                evento_id = int(abertos.loc[ei, "id"])
                est = int(eventos().set_index("id").loc[evento_id, "publico_estimado"])
                publico = st.number_input("Público real", min_value=0, value=est, step=100)
                st.caption("Ao encerrar, todas as sobras voltam ao depósito (retorno) e o evento passa a 'Realizado', "
                           "entrando no histórico usado pelo modelo.")
                if st.form_submit_button("Encerrar evento e devolver sobras", type="primary"):
                    st.success(f"Evento encerrado. {num(encerrar_evento(evento_id, int(publico), usuario))} "
                               "unidades devolvidas ao depósito.")

    with abas[3]:
        lotes = saldo_lotes()
        lotes = lotes[lotes["saldo"] > 0]
        with st.form("perda"):
            tipo = st.radio("Tipo", ["PERDA_VALIDADE", "PERDA_AVARIA", "AJUSTE"], horizontal=True,
                            format_func={"PERDA_VALIDADE": "Perda por validade", "PERDA_AVARIA": "Avaria",
                                         "AJUSTE": "Ajuste de inventário"}.get)
            li = st.selectbox("Lote", lotes.index, format_func=lambda i: (
                f"{lotes.loc[i, 'produto']} · {lotes.loc[i, 'codigo']} · vence {lotes.loc[i, 'data_validade']} "
                f"· saldo {num(lotes.loc[i, 'saldo'])}"))
            qtd = st.number_input("Quantidade (no ajuste, use valor negativo para reduzir)", value=1, step=1)
            obs = st.text_input("Motivo / observação")
            if st.form_submit_button("Registrar", type="primary"):
                saldo = int(lotes.loc[li, "saldo"])
                if tipo != "AJUSTE" and not 0 < qtd <= saldo:
                    st.error(f"Informe uma quantidade entre 1 e {saldo}.")
                elif tipo == "AJUSTE" and (qtd == 0 or saldo + qtd < 0):
                    st.error("Ajuste inválido: o saldo do lote não pode ficar negativo.")
                else:
                    registrar_perda_ou_ajuste(tipo, int(lotes.loc[li, "produto_id"]), int(lotes.loc[li, "lote_id"]),
                                              int(qtd), usuario, obs)
                    st.success("Movimentação registrada.")

    with abas[4]:
        tipo = st.multiselect("Filtrar tipos", ["ENTRADA", "SAIDA_EVENTO", "RETORNO_EVENTO", "PERDA_VALIDADE",
                                                "PERDA_AVARIA", "AJUSTE"])
        filtro = f"WHERE m.tipo IN ({','.join('?' * len(tipo))})" if tipo else ""
        hist = q(f"""SELECT m.data_hora, m.tipo, p.nome AS produto, l.codigo AS lote, e.nome AS evento,
                            m.quantidade, m.usuario, m.observacao
                     FROM movimentacoes m JOIN produtos p ON p.id = m.produto_id
                     LEFT JOIN lotes l ON l.id = m.lote_id LEFT JOIN eventos e ON e.id = m.evento_id
                     {filtro} ORDER BY m.id DESC LIMIT 300""", tuple(tipo))
        st.dataframe(hist, hide_index=True, width="stretch")


def pagina_estoque(usuario):
    st.title("⏳ Estoque e validade")
    lotes = saldo_lotes()
    lotes = lotes[lotes["saldo"] > 0].copy()
    lotes["data_validade"] = pd.to_datetime(lotes["data_validade"])
    hoje = pd.Timestamp(date.today())
    lotes["dias_para_vencer"] = (lotes["data_validade"] - hoje).dt.days
    prods = produtos()

    resumo = prods[["id", "nome", "categoria", "estoque_minimo", "custo_unitario"]].merge(
        lotes.groupby("produto_id", as_index=False)["saldo"].sum(), left_on="id", right_on="produto_id", how="left")
    resumo["saldo"] = resumo["saldo"].fillna(0).astype(int)
    resumo["valor"] = resumo["saldo"] * resumo["custo_unitario"]
    resumo["situacao"] = resumo.apply(lambda r: "⚠️ Abaixo do mínimo" if r["saldo"] < r["estoque_minimo"] else "✅ OK",
                                      axis=1)
    vencidos = lotes[lotes["dias_para_vencer"] < 0]

    k = st.columns(4)
    k[0].metric("Valor em estoque", brl(resumo["valor"].sum()))
    k[1].metric("Produtos abaixo do mínimo", int((resumo["saldo"] < resumo["estoque_minimo"]).sum()))
    k[2].metric("Lotes vencendo em 7 dias", int(lotes["dias_para_vencer"].between(0, 7).sum()))
    k[3].metric("Lotes vencidos com saldo", len(vencidos))

    if not vencidos.empty:
        st.error(f"{len(vencidos)} lote(s) vencido(s) ainda com saldo no depósito.")
        if st.button("Registrar perda por validade dos lotes vencidos"):
            for r in vencidos.itertuples():
                registrar_perda_ou_ajuste("PERDA_VALIDADE", r.produto_id, r.lote_id, int(r.saldo), usuario,
                                          "Descarte por vencimento")
            st.rerun()

    st.subheader("Saldo por produto")
    st.dataframe(resumo[["nome", "categoria", "saldo", "estoque_minimo", "valor", "situacao"]], hide_index=True,
                 width="stretch",
                 column_config={"nome": "Produto", "categoria": "Categoria", "saldo": "Saldo",
                                "estoque_minimo": "Mínimo", "situacao": "Situação",
                                "valor": st.column_config.NumberColumn("Valor (custo)", format="R$ %.2f")})

    st.subheader("Lotes por validade")
    dias = st.slider("Mostrar lotes que vencem nos próximos (dias)", 1, 365, 30)
    prox = lotes[lotes["dias_para_vencer"] <= dias].sort_values("data_validade")
    st.dataframe(prox[["produto", "codigo", "data_entrada", "data_validade", "dias_para_vencer", "saldo"]],
                 hide_index=True, width="stretch",
                 column_config={"produto": "Produto", "codigo": "Lote", "data_entrada": "Entrada",
                                "data_validade": st.column_config.DateColumn("Validade", format="DD/MM/YYYY"),
                                "dias_para_vencer": st.column_config.ProgressColumn(
                                    "Dias para vencer", min_value=0, max_value=dias, format="%d"),
                                "saldo": "Saldo"})


def pagina_previsao(usuario):
    st.title("🔮 Previsão de demanda e sugestão de abastecimento")
    if ml.carregar_modelo() is None:
        st.warning("Modelo ainda não treinado. Vá em **Modelo de ML** e clique em Treinar.")
        return
    ev = eventos()
    plan = ev[ev["status"] == "Planejado"].reset_index(drop=True)
    if plan.empty:
        st.info("Não há eventos planejados. Cadastre um em **Cadastros**.")
        return
    c1, c2 = st.columns([3, 1])
    ei = c1.selectbox("Evento planejado", plan.index, format_func=lambda i: (
        f"{rotulo_evento(plan.loc[i])} · {plan.loc[i, 'tipo']} · {num(plan.loc[i, 'publico_estimado'])} pessoas"))
    margem = c2.slider("Margem de segurança", 0, 30, int(ml.MARGEM_SUGESTAO * 100), format="%d%%") / 100
    evento = plan.loc[ei]
    evento_id = int(evento["id"])

    prev = ml.prever_eventos(con, [evento_id], margem)
    lotes = saldo_lotes()
    validos = lotes[(lotes["saldo"] > 0) & (lotes["data_validade"] >= evento["data"].date().isoformat())]
    saldo = validos.groupby("produto_id")["saldo"].sum()
    enviado = estoque_no_evento(evento_id).groupby("produto_id")["saldo_evento"].sum()
    prev["ja_enviado"] = prev["produto_id"].map(enviado).fillna(0).astype(int)
    prev["saldo_deposito"] = prev["produto_id"].map(saldo).fillna(0).astype(int)
    faltante = (prev["sugestao"] - prev["ja_enviado"] - prev["saldo_deposito"]).clip(lower=0)
    prev["comprar"] = (faltante / prev["embalagem"]).apply(math.ceil) * prev["embalagem"]
    prev["custo_compra"] = prev["comprar"] * prev["custo_unitario"]

    k = st.columns(4)
    k[0].metric("Unidades previstas", num(prev["previsao"].sum()))
    k[1].metric("Faturamento previsto", brl((prev["previsao"] * prev["preco_venda"]).sum()))
    k[2].metric("Itens a comprar", int((prev["comprar"] > 0).sum()))
    k[3].metric("Custo da compra", brl(prev["custo_compra"].sum()))
    st.caption(f"Tipo: **{evento['tipo']}** · duração {evento['duracao_h']:.0f} h · temperatura prevista "
               f"{evento['temperatura_prevista']:.1f} °C · modelo: {ml.carregar_modelo()['nome']}")

    graf = prev.sort_values("previsao")
    st.plotly_chart(px.bar(graf, y="produto", x=["previsao", "sugestao"], barmode="group", orientation="h",
                           title="Previsão de vendas x quantidade sugerida para envio", height=560,
                           labels={"value": "Unidades", "produto": "", "variable": ""})
                    .for_each_trace(lambda t: t.update(name={"previsao": "Previsão", "sugestao": "Sugestão"}[t.name])),
                    width="stretch")
    st.dataframe(prev[["produto", "categoria", "previsao", "sugestao", "ja_enviado", "saldo_deposito", "comprar",
                       "custo_compra"]].round({"previsao": 0}), hide_index=True, width="stretch",
                 column_config={"produto": "Produto", "categoria": "Categoria", "previsao": "Previsão (un.)",
                                "sugestao": "Sugestão de envio", "ja_enviado": "Já no evento",
                                "saldo_deposito": "Saldo válido no depósito", "comprar": "Comprar",
                                "custo_compra": st.column_config.NumberColumn("Custo da compra", format="R$ %.2f")})
    c1, c2 = st.columns(2)
    c1.download_button("Baixar lista de compras (CSV)", prev[prev["comprar"] > 0][["produto", "comprar", "custo_compra"]]
                       .to_csv(index=False, sep=";", decimal=",").encode("utf-8-sig"),
                       f"compras_evento_{evento_id}.csv", "text/csv")
    if c2.button("Enviar ao evento a quantidade sugerida (com o estoque disponível)", type="primary"):
        enviados = 0
        for r in prev.itertuples():
            qtd = min(r.sugestao - r.ja_enviado, r.saldo_deposito)
            if qtd > 0:
                enviar_para_evento(evento_id, r.produto_id, int(qtd), usuario)
                enviados += 1
        st.toast(f"Envio registrado para {enviados} produto(s).", icon="✅")
        st.rerun()


def pagina_modelo():
    st.title("🤖 Modelo de aprendizado de máquina")
    m = metricas_modelo()
    if PERMITIR_RETREINO:
        if st.button("Treinar / retreinar modelo com o histórico atual"):
            with st.spinner("Treinando e avaliando modelos..."):
                ml.treinar(con)
            invalidar()
            st.rerun()
    else:
        st.caption("Resultados do modelo treinado com a base simulada original. O retreino está desativado nesta "
                   "versão de demonstração para preservar as métricas apresentadas no relatório.")
    if m is None:
        st.info("Modelo ainda não treinado.")
        return

    st.markdown(f"""
| Elemento | Descrição |
|---|---|
| **Tarefa** | {m['tarefa']} (aprendizado supervisionado) e agrupamento de produtos por padrão de consumo (não supervisionado) |
| **Dados** | {num(m['linhas'])} combinações evento × produto ({m['eventos']} eventos, {m['produtos']} produtos), de {pd.to_datetime(m['periodo'][0]):%d/%m/%Y} a {pd.to_datetime(m['periodo'][1]):%d/%m/%Y}, agregadas a partir das transações de venda |
| **Saída** | Unidades previstas por produto para um evento planejado e quantidade sugerida de envio (previsão + margem, arredondada à embalagem) |
| **Avaliação** | Divisão temporal: treino com {m['treino']['eventos']} eventos e teste com os {m['teste']['eventos']} mais recentes; métricas MAE, RMSE, R² e WAPE, comparadas ao método manual |
""")

    res = pd.DataFrame(m["resultados_teste"]).T.reset_index(names="Modelo")
    res["Melhor"] = res["Modelo"].eq(m["melhor_modelo"]).map({True: "⭐", False: ""})
    manual = m["resultados_teste"]["Método manual atual (sem a margem de 20%)"]["MAE"]
    melhor = m["resultados_teste"][m["melhor_modelo"]]["MAE"]
    i = m["impacto_operacional_teste"]
    k = st.columns(4)
    k[0].metric("Melhor modelo", m["melhor_modelo"])
    k[1].metric("Erro médio (MAE)", f"{melhor:.0f} un.", f"{(melhor / manual - 1):.0%} vs. manual", delta_color="inverse")
    k[2].metric("R² no teste", f"{m['resultados_teste'][m['melhor_modelo']]['R2']:.3f}")
    k[3].metric("Sobras no teste", num(i["sobra_ml_un"]), f"{(i['sobra_ml_un'] / i['sobra_manual_un'] - 1):.0%} vs. manual",
                delta_color="inverse")

    c1, c2 = st.columns(2)
    c1.plotly_chart(px.bar(res.sort_values("MAE"), x="MAE", y="Modelo", orientation="h", text_auto=".1f",
                           title="Erro absoluto médio no período de teste (menor é melhor)", labels={"Modelo": ""}),
                    width="stretch")
    imp = pd.DataFrame(m["importancia_variaveis"]).sort_values("importancia")
    c2.plotly_chart(px.bar(imp, x="importancia", y="variavel", orientation="h",
                           title="Importância das variáveis (permutação)", labels={"variavel": "", "importancia": ""}),
                    width="stretch")
    st.dataframe(res[["Melhor", "Modelo", "MAE", "RMSE", "R2", "WAPE"]], hide_index=True, width="stretch",
                 column_config={"MAE": st.column_config.NumberColumn(format="%.1f"),
                                "RMSE": st.column_config.NumberColumn(format="%.1f"),
                                "R2": st.column_config.NumberColumn("R²", format="%.3f"),
                                "WAPE": st.column_config.NumberColumn(format="percent")})

    if ml.ARQ_PREV_TESTE.exists():
        teste = pd.read_csv(ml.ARQ_PREV_TESTE)
        c1, c2 = st.columns(2)
        fig = px.scatter(teste, x="vendido", y="previsao", color="categoria", hover_data=["evento", "produto"],
                         title="Real x previsto (eventos de teste)", log_x=True, log_y=True,
                         labels={"vendido": "Vendido (real)", "previsao": "Previsto"})
        lim = [max(teste["vendido"].min(), 1), teste["vendido"].max()]
        fig.add_shape(type="line", x0=lim[0], y0=lim[0], x1=lim[1], y1=lim[1], line=dict(dash="dash", color="gray"))
        c1.plotly_chart(fig, width="stretch")
        por_ev = teste.groupby(["data", "evento"], as_index=False)[["vendido", "enviado", "sugestao"]].sum()
        por_ev = por_ev.melt(id_vars=["data", "evento"], var_name="serie", value_name="unidades")
        por_ev["serie"] = por_ev["serie"].map({"vendido": "Vendido", "enviado": "Enviado (manual)",
                                               "sugestao": "Sugestão do modelo"})
        c2.plotly_chart(px.line(por_ev, x="data", y="unidades", color="serie", markers=True,
                                title="Por evento de teste: vendido x enviado x sugerido",
                                labels={"data": "", "unidades": "Unidades", "serie": ""}), width="stretch")

    st.subheader("Impacto operacional no período de teste")
    imp_df = pd.DataFrame({
        "Indicador": ["Unidades enviadas", "Sobras (unidades)", "Custo das sobras de perecíveis",
                      "Itens com falta de produto"],
        "Método manual": [num(i["unidades_enviadas_manual"]), num(i["sobra_manual_un"]),
                          brl(i["custo_sobra_pereciveis_manual"]), f"{i['rupturas_manual_pct']:.1%}"],
        f"Sugestão do modelo (margem {i['margem_sugestao']:.0%})": [
            num(i["unidades_sugeridas_ml"]), num(i["sobra_ml_un"]), brl(i["custo_sobra_pereciveis_ml"]),
            f"{i['itens_com_falta_ml_pct']:.1%}"],
    })
    st.dataframe(imp_df, hide_index=True, width="stretch")
    st.caption("Limitação: as vendas registradas são limitadas ao que foi enviado; quando faltou produto, a demanda real "
               "foi maior que a venda. Por isso a falta estimada para o modelo é um limite inferior.")

    cv = pd.DataFrame(m["validacao_cruzada_temporal"]).T.reset_index(names="Modelo")
    st.subheader("Validação cruzada temporal (4 janelas, conjunto de treino)")
    st.dataframe(cv, hide_index=True, width="stretch",
                 column_config={"MAE_medio": st.column_config.NumberColumn("MAE médio", format="%.1f"),
                                "MAE_desvio": st.column_config.NumberColumn("Desvio", format="%.1f")})

    if ml.ARQ_CLUSTERS.exists():
        cl = pd.read_csv(ml.ARQ_CLUSTERS)
        info = m["agrupamento_produtos"]
        st.subheader(f"Agrupamento de produtos por padrão de consumo (K-Means, k={info['k']}, "
                     f"silhouette={info['silhouette']:.2f})")
        mapa = cl.set_index("produto").drop(columns=["grupo", "grupo_nome"])
        ordem = cl.sort_values("grupo")["produto"]
        c1, c2 = st.columns([3, 2])
        c1.plotly_chart(px.imshow(mapa.loc[ordem], color_continuous_scale="RdBu_r", color_continuous_midpoint=1,
                                  aspect="auto", text_auto=".1f", height=600,
                                  title="Consumo relativo à média do produto (1,0 = média)",
                                  labels={"x": "", "y": "", "color": "Relativo"}), width="stretch")
        c2.dataframe(cl[["grupo_nome", "produto"]].sort_values(["grupo_nome", "produto"]), hide_index=True,
                     width="stretch", height=600, column_config={"grupo_nome": "Grupo", "produto": "Produto"})


def pagina_cadastros():
    st.title("🗂️ Cadastros")
    aba_ev, aba_prod = st.tabs(["Eventos", "Produtos"])
    with aba_ev:
        with st.form("novo_evento", clear_on_submit=True):
            st.markdown("**Novo evento planejado**")
            c1, c2, c3 = st.columns(3)
            nome = c1.text_input("Nome do evento")
            tipo = c2.selectbox("Tipo", ["Show", "Festival", "Evento corporativo", "Evento infantil", "E-sports"])
            data_ev = c3.date_input("Data", date.today() + timedelta(days=7), format="DD/MM/YYYY")
            c4, c5, c6, c7 = st.columns(4)
            hora = c4.number_input("Hora de início", 0, 23, 20)
            duracao = c5.number_input("Duração (h)", 1, 16, 5)
            publico = c6.number_input("Público estimado", 50, 50000, 5000, step=100)
            temp = c7.number_input("Temperatura prevista (°C)", -5.0, 45.0, 24.0, step=0.5)
            if st.form_submit_button("Cadastrar evento", type="primary"):
                if not nome.strip():
                    st.error("Informe o nome do evento.")
                else:
                    con.execute("INSERT INTO eventos (nome, tipo, data, hora_inicio, duracao_h, publico_estimado,"
                                " temperatura_prevista, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Planejado')",
                                (nome.strip(), tipo, data_ev.isoformat(), int(hora), float(duracao), int(publico),
                                 float(temp)))
                    con.commit()
                    invalidar()
                    st.success("Evento cadastrado. Veja a previsão de demanda dele na página **Previsão de demanda**.")
        ev = eventos().sort_values("data", ascending=False)
        st.dataframe(ev.drop(columns=["id"]), hide_index=True, width="stretch",
                     column_config={"data": st.column_config.DateColumn("Data", format="DD/MM/YYYY")})
    with aba_prod:
        with st.form("novo_produto", clear_on_submit=True):
            st.markdown("**Novo produto**")
            c1, c2, c3 = st.columns(3)
            nome = c1.text_input("Nome")
            cat = c2.selectbox("Categoria", sorted(produtos()["categoria"].unique()))
            unidade = c3.text_input("Unidade", "un")
            c4, c5, c6, c7 = st.columns(4)
            preco = c4.number_input("Preço de venda (R$)", 0.0, 1000.0, 10.0, step=0.5)
            custo = c5.number_input("Custo (R$)", 0.0, 1000.0, 3.0, step=0.5)
            validade = c6.number_input("Validade (dias)", 1, 3650, 30)
            emb = c7.number_input("Embalagem (un.)", 1, 1000, 12)
            if st.form_submit_button("Cadastrar produto", type="primary"):
                try:
                    con.execute("INSERT INTO produtos (nome, categoria, unidade, preco_venda, custo_unitario,"
                                " validade_dias, embalagem) VALUES (?, ?, ?, ?, ?, ?, ?)",
                                (nome.strip(), cat, unidade, preco, custo, int(validade), int(emb)))
                    con.commit()
                    invalidar()
                    st.success("Produto cadastrado.")
                except Exception as e:  # nome duplicado
                    st.error(f"Não foi possível cadastrar: {e}")
        st.dataframe(produtos().drop(columns=["id", "ativo"]), hide_index=True, width="stretch")


def pagina_usuarios():
    st.title("👤 Usuários")
    with st.form("novo_usuario", clear_on_submit=True):
        st.markdown("**Novo usuário**")
        c1, c2, c3, c4 = st.columns(4)
        login = c1.text_input("Login")
        nome = c2.text_input("Nome")
        perfil = c3.selectbox("Perfil", auth.PERFIS)
        senha = c4.text_input("Senha (mín. 6 caracteres)", type="password")
        if st.form_submit_button("Cadastrar usuário", type="primary"):
            if not login.strip() or not nome.strip():
                st.error("Informe login e nome.")
            else:
                try:
                    auth.criar_usuario(con, login, nome, perfil, senha)
                    st.success(f"Usuário {login.strip().lower()} cadastrado.")
                except Exception as e:  # login duplicado ou senha curta
                    st.error(f"Não foi possível cadastrar: {e}")
    usuarios = q("SELECT login, nome, perfil, ativo, criado_em FROM usuarios ORDER BY perfil, login")
    st.dataframe(usuarios, hide_index=True, width="stretch",
                 column_config={"login": "Login", "nome": "Nome", "perfil": "Perfil",
                                "ativo": st.column_config.CheckboxColumn("Ativo"), "criado_em": "Criado em"})
    st.caption("As senhas são armazenadas apenas como hash PBKDF2-SHA256 com salt individual.")


def tela_login():
    _, centro, _ = st.columns([1, 1.2, 1])
    with centro:
        st.markdown("## 📦 Estoque e Vendas em Eventos")
        st.caption("PI IV · UNIVESP · dados simulados")
        with st.form("login"):
            login = st.text_input("Usuário")
            senha = st.text_input("Senha", type="password")
            if st.form_submit_button("Entrar", type="primary", width="stretch"):
                usuario = auth.autenticar(con, login, senha)
                if usuario:
                    st.session_state["usuario"] = usuario
                    st.rerun()
                st.error("Usuário ou senha inválidos.")


# ---------------------------------------------------------------- navegação
# página: (função, recebe o login do usuário?, perfis com acesso)
PAGINAS = {
    "📊 Painel": (pagina_painel, False, {"Administrador", "Estoque", "Vendas"}),
    "🛒 Registrar venda": (pagina_venda, True, {"Administrador", "Vendas"}),
    "📦 Movimentações": (pagina_movimentacoes, True, {"Administrador", "Estoque"}),
    "⏳ Estoque e validade": (pagina_estoque, True, {"Administrador", "Estoque"}),
    "🔮 Previsão de demanda": (pagina_previsao, True, {"Administrador", "Estoque"}),
    "🤖 Modelo de ML": (pagina_modelo, False, {"Administrador", "Estoque"}),
    "🗂️ Cadastros": (pagina_cadastros, False, {"Administrador", "Estoque"}),
    "👤 Usuários": (pagina_usuarios, False, {"Administrador"}),
}

usuario = st.session_state.get("usuario")
if usuario is None:
    tela_login()
    st.stop()

permitidas = [nome for nome, (_, _, perfis) in PAGINAS.items() if usuario["perfil"] in perfis]
with st.sidebar:
    st.markdown("### 📦 Estoque e Vendas em Eventos")
    st.caption("PI IV · UNIVESP · dados simulados")
    escolha = st.radio("Navegação", permitidas, label_visibility="collapsed")
    st.divider()
    st.markdown(f"**{usuario['nome']}**  \n{usuario['login']} · {usuario['perfil']}")
    if st.button("Sair", width="stretch"):
        del st.session_state["usuario"]
        st.rerun()

pagina, recebe_usuario, _ = PAGINAS[escolha]
if recebe_usuario:
    pagina(usuario["login"])
else:
    pagina()
