"""Interface da solução: captura de dados de estoque/vendas, painéis e resultados do ML.

Uso:  streamlit run app.py
"""
import io
import json
import math
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import pandas as pd
import plotly.express as px
import streamlit as st

import auth
import gerar_dados
import ml
from db import DB_PATH, conectar, consultar, criar_banco
from produtos_casa import PRODUTOS_CASA

st.set_page_config(page_title="Estoque e Vendas em Eventos", page_icon="📦", layout="wide",
                   initial_sidebar_state="collapsed")

CORES = px.colors.qualitative.Safe
px.defaults.color_discrete_sequence = CORES
# Lanchonetes da casa, informadas pela comunidade.
PONTOS_VENDA = ["BAR 3º ANDAR", "BAR 4º ANDAR", "CONCESSÃO 103", "CONCESSÃO 112", "CONCESSÃO 116", "CONCESSÃO 123",
                "CONCESSÃO 128 - L1", "CONCESSÃO 133", "CONCESSÃO 501", "CONCESSÃO 504", "CONCESSÃO 516",
                "CONCESSÃO 522", "CONCESSÃO 524", "CONCESSÃO 530"]
FORMAS_PAGAMENTO = ["Pix", "Crédito", "Débito", "Pulseira cashless"]
CATEGORIAS = ["Bebida alcoólica", "Bebida não alcoólica", "Café", "Doce", "Lanche", "Snack"]
TIPOS_EVENTO = ["Show", "Festival", "Evento corporativo", "Evento infantil", "E-sports"]
# Valores usados quando o cadastro informa só nome, categoria e preço: unidade, validade (dias) e embalagem.
PADRAO_CATEGORIA = {"Bebida alcoólica": ("un", 180, 12), "Bebida não alcoólica": ("un", 180, 12),
                    "Café": ("dose", 120, 50), "Doce": ("un", 90, 30), "Lanche": ("un", 3, 20),
                    "Snack": ("pacote", 90, 40)}
CUSTO_PADRAO = 0.35  # custo estimado como fração do preço, até a gestão informar o valor real
# O servidor do Streamlit Cloud roda em UTC; datas e horários do sistema seguem o horário de Brasília.
FUSO = timezone(timedelta(hours=-3))
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
    carregar_produtos_casa(con)
    return con


def carregar_produtos_casa(con):
    """Inclui os produtos da casa que ainda não existem: sem preço e inativos até a gestão informar o preço."""
    # Comparação em Python: o lower() do SQLite não converte letras acentuadas (Á, É...).
    existentes = {r[0].strip().lower() for r in con.execute("SELECT nome FROM produtos")}
    for cat, nomes in PRODUTOS_CASA.items():
        unidade, validade, emb = PADRAO_CATEGORIA[cat]
        for nome in nomes:
            if nome.lower() not in existentes:
                con.execute("INSERT INTO produtos (nome, categoria, unidade, preco_venda, custo_unitario, validade_dias,"
                            " embalagem, estoque_minimo, ativo) VALUES (?, ?, ?, 0, 0, ?, ?, 0, 0)",
                            (nome, cat, unidade, validade, emb))
    con.commit()


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
    return datetime.now(FUSO).replace(tzinfo=None).isoformat(sep=" ", timespec="seconds" if segundos else "minutes")


def hoje():
    return datetime.now(FUSO).date()


def baixar_excel(df, arquivo, rotulo="📥 Baixar em Excel", key=None):
    """Botão de download em .xlsx, que abre direto no Excel (o CSV confunde quem não conhece a extensão)."""
    buf = io.BytesIO()
    df.to_excel(buf, index=False, sheet_name="Dados")
    st.download_button(rotulo, buf.getvalue(), arquivo, key=key,
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")


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


def produtos_ativos():
    prods = produtos()
    return prods[prods["ativo"] == 1].reset_index(drop=True)


def nome_em_uso(nome, exceto_id=None):
    prods = produtos()
    iguais = prods["nome"].str.strip().str.lower() == nome.strip().lower()
    return bool((iguais & (prods["id"] != exceto_id)).any())


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


def estoque_no_evento(evento_id, lanchonete=None):
    """Saldo no evento por produto, lote e lanchonete: enviado - devolvido - vendido naquela lanchonete."""
    df = q("""
        SELECT m.produto_id, p.nome AS produto, m.lote_id, l.codigo, l.data_validade, m.ponto_venda AS lanchonete,
               SUM(CASE WHEN m.tipo = 'SAIDA_EVENTO' THEN m.quantidade ELSE -m.quantidade END)
               - COALESCE((SELECT SUM(v.quantidade) FROM vendas v
                           WHERE v.evento_id = m.evento_id AND v.lote_id = m.lote_id
                             AND v.ponto_venda IS m.ponto_venda), 0) AS saldo_evento
        FROM movimentacoes m
        JOIN produtos p ON p.id = m.produto_id
        JOIN lotes l ON l.id = m.lote_id
        WHERE m.evento_id = ? AND m.tipo IN ('SAIDA_EVENTO', 'RETORNO_EVENTO')
        GROUP BY m.produto_id, m.lote_id, m.ponto_venda
        HAVING saldo_evento > 0
        ORDER BY p.nome, l.data_validade""", (evento_id,))
    return df if lanchonete is None else df[df["lanchonete"] == lanchonete].reset_index(drop=True)


def lanchonetes_do_evento(evento_id):
    """Lanchonetes que ainda têm produto no evento, na ordem da lista da casa."""
    com_saldo = set(estoque_no_evento(evento_id)["lanchonete"].dropna())
    return [p for p in PONTOS_VENDA if p in com_saldo]


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
    dia = hoje()
    prox = q("SELECT COALESCE(MAX(id), 0) + 1 AS n FROM lotes")["n"].iloc[0]
    codigo = f"L{dia:%y%m%d}-{produto_id:02d}-{prox:05d}"
    cur = con.execute(
        "INSERT INTO lotes (produto_id, codigo, data_entrada, data_validade, quantidade_inicial, custo_unitario)"
        " VALUES (?, ?, ?, ?, ?, ?)", (produto_id, codigo, dia.isoformat(), validade.isoformat(), quantidade, custo))
    con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, quantidade, usuario, observacao)"
                " VALUES (?, 'ENTRADA', ?, ?, ?, ?, ?)", (agora(), produto_id, cur.lastrowid, quantidade, usuario, obs))
    con.commit()
    invalidar()
    return codigo


def enviar_para_evento(evento_id, produto_id, quantidade, usuario, ponto_venda=None):
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
                    " observacao, ponto_venda) VALUES (?, 'SAIDA_EVENTO', ?, ?, ?, ?, ?, 'Abastecimento do evento', ?)",
                    (agora(), produto_id, lote.lote_id, evento_id, usar, usuario, ponto_venda))
        restante -= usar
        if restante == 0:
            break
    con.commit()
    invalidar()


def registrar_venda(evento_id, produto_id, quantidade, ponto_venda, pagamento):
    estoque = estoque_no_evento(evento_id, ponto_venda)
    estoque = estoque[estoque["produto_id"] == produto_id]
    if estoque["saldo_evento"].sum() < quantidade:
        raise ValueError(f"Só há {num(estoque['saldo_evento'].sum())} unidades deste produto em {ponto_venda}.")
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


def devolver(evento_id, produto_id, lote_id, lanchonete, quantidade, usuario, obs="Sobra devolvida ao depósito"):
    con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, evento_id, quantidade, usuario,"
                " observacao, ponto_venda) VALUES (?, 'RETORNO_EVENTO', ?, ?, ?, ?, ?, ?, ?)",
                (agora(), produto_id, lote_id, evento_id, quantidade, usuario, obs, lanchonete))


def registrar_contagem(evento_id, lanchonete, contado, usuario, final=True):
    """Contagem de uma lanchonete, na abertura (final=False) ou no fechamento (final=True).

    contado: {produto_id: unidades contadas}. Abertura: o contado fica na lanchonete e o que não chegou volta ao
    depósito. Fechamento: o contado volta ao depósito e a diferença vira perda; ela também passa pelo depósito
    (retorno + perda por avaria), o que zera o saldo da lanchonete sem alterar o saldo do depósito e leva a falta
    para as perdas do Painel.
    """
    estoque = estoque_no_evento(evento_id, lanchonete)
    falta = 0
    for pid, bloco in estoque.groupby("produto_id"):
        resta = min(int(contado.get(pid, 0)), int(bloco["saldo_evento"].sum()))
        # Considera contados primeiro os lotes de validade mais longa: os que vencem antes saem antes.
        for lote in bloco.sort_values("data_validade", ascending=False).itertuples():
            volta = min(resta, int(lote.saldo_evento))
            dif = int(lote.saldo_evento) - volta
            if not final:
                if dif:
                    devolver(evento_id, pid, lote.lote_id, lanchonete, dif, usuario,
                             f"Não conferido na abertura da lanchonete {lanchonete}")
                    falta += dif
                resta -= volta
                continue
            if volta:
                devolver(evento_id, pid, lote.lote_id, lanchonete, volta, usuario, f"Contagem da lanchonete {lanchonete}")
            if dif:
                devolver(evento_id, pid, lote.lote_id, lanchonete, dif, usuario, "Diferença na contagem")
                con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, evento_id, quantidade,"
                            " usuario, observacao, ponto_venda) VALUES (?, 'PERDA_AVARIA', ?, ?, ?, ?, ?, ?, ?)",
                            (agora(), int(pid), lote.lote_id, evento_id, dif, usuario,
                             f"Diferença na contagem da lanchonete {lanchonete}", lanchonete))
                falta += dif
            resta -= volta
    con.commit()
    invalidar()
    return falta


def transferir(evento_id, produto_id, origem, destino, quantidade, usuario):
    """Passa produto de uma lanchonete para outra no mesmo evento (coluna Transferência da folha de controle)."""
    estoque = estoque_no_evento(evento_id, origem)
    estoque = estoque[estoque["produto_id"] == produto_id]
    if estoque["saldo_evento"].sum() < quantidade:
        raise ValueError(f"Só há {num(estoque['saldo_evento'].sum())} unidades deste produto em {origem}.")
    restante = quantidade
    for lote in estoque.itertuples():
        usar = min(restante, int(lote.saldo_evento))
        devolver(evento_id, produto_id, lote.lote_id, origem, usar, usuario, f"Transferência para {destino}")
        con.execute("INSERT INTO movimentacoes (data_hora, tipo, produto_id, lote_id, evento_id, quantidade, usuario,"
                    " observacao, ponto_venda) VALUES (?, 'SAIDA_EVENTO', ?, ?, ?, ?, ?, ?, ?)",
                    (agora(), produto_id, lote.lote_id, evento_id, usar, usuario, f"Transferência de {origem}", destino))
        restante -= usar
        if restante == 0:
            break
    con.commit()
    invalidar()


def encerrar_evento(evento_id, publico_real, usuario):
    sobras = estoque_no_evento(evento_id)
    for s in sobras.itertuples():
        devolver(evento_id, s.produto_id, s.lote_id, s.lanchonete, int(s.saldo_evento), usuario)
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
    if sel.empty:
        st.info("Nenhum evento realizado no período e nos tipos selecionados. Amplie o período.")
        return
    vendas =(vendas_agregadas().merge(sel[["id", "nome", "tipo", "data", "publico_real"]], left_on="evento_id",
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
    publico = publico.where(publico > 0)  # evento encerrado sem público não gera divisão por zero
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
    por_evento["receita_por_pessoa"] = por_evento["receita"] / por_evento["publico_real"].where(
        por_evento["publico_real"] > 0)
    baixar_excel(por_evento.sort_values("data", ascending=False), "eventos_do_periodo.xlsx", key="xl_painel")
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
    lanchonetes = lanchonetes_do_evento(evento_id)
    if not lanchonetes:
        st.warning("Este evento não tem mais produtos disponíveis.")
        return
    # A lanchonete fica fora do formulário: a lista de produtos mostra só o que foi enviado para ela.
    pdv = st.selectbox("Lanchonete", lanchonetes, key="venda_pdv")
    estoque = estoque_no_evento(evento_id, pdv)
    disp = estoque.groupby(["produto_id", "produto"], as_index=False)["saldo_evento"].sum()
    with st.form("venda", clear_on_submit=True):
        c1, c2 = st.columns(2)
        pi = c1.selectbox("Produto", disp.index,
                          format_func=lambda i: f"{disp.loc[i, 'produto']} (disponível: {num(disp.loc[i, 'saldo_evento'])})")
        qtd = c2.number_input("Quantidade", min_value=1, value=1, step=1)
        pag = st.radio("Forma de pagamento", FORMAS_PAGAMENTO, horizontal=True)
        if st.form_submit_button("Registrar venda", type="primary", width="stretch"):
            try:
                total = registrar_venda(evento_id, int(disp.loc[pi, "produto_id"]), int(qtd), pdv, pag)
                st.success(f"Venda registrada: {qtd} × {disp.loc[pi, 'produto']} = {brl(total)}")
            except ValueError as e:
                st.error(str(e))
    st.subheader(f"Estoque em {pdv}")
    st.dataframe(estoque_no_evento(evento_id, pdv)[["produto", "codigo", "data_validade", "saldo_evento"]],
                 hide_index=True, width="stretch",
                 column_config={"produto": "Produto", "codigo": "Lote", "data_validade": "Validade",
                                "saldo_evento": "Disponível"})
    ultimas = q("""SELECT v.data_hora, p.nome AS produto, v.quantidade, v.ponto_venda, v.forma_pagamento,
                   v.quantidade * v.preco_unitario AS total
                   FROM vendas v JOIN produtos p ON p.id = v.produto_id
                   WHERE v.evento_id = ? AND v.ponto_venda = ? ORDER BY v.id DESC LIMIT 20""", (evento_id, pdv))
    if not ultimas.empty:
        st.subheader("Últimas vendas desta lanchonete")
        st.dataframe(ultimas, hide_index=True, width="stretch")


def pagina_movimentacoes(usuario):
    st.title("📦 Movimentações de estoque")
    prods = produtos_ativos()
    nome_prod = dict(zip(prods["id"], prods["nome"]))
    # Seletor em vez de abas: no celular as abas ficavam cortadas.
    aba = st.radio("Operação", ["Entrada (compra)", "Envio para evento", "Transferência entre lanchonetes",
                                "Contagem e encerramento", "Perda / ajuste", "Histórico"], horizontal=True)

    if aba == "Entrada (compra)":
        # O produto fica fora do formulário para que embalagem, validade e custo acompanhem a troca de produto.
        pid = st.selectbox("Produto", prods["id"], format_func=nome_prod.get, key="entrada_prod")
        p = prods.set_index("id").loc[pid]
        with st.form(f"entrada_{pid}", clear_on_submit=True):
            c1, c2 = st.columns(2)
            qtd = c1.number_input(f"Quantidade (embalagem de {p['embalagem']})", min_value=1,
                                  value=int(p["embalagem"]), step=1)
            validade = c2.date_input("Data de validade", hoje() + timedelta(days=int(p["validade_dias"])),
                                     format="DD/MM/YYYY")
            c3, c4 = st.columns(2)
            custo = c3.number_input("Custo unitário (R$)", min_value=0.0, value=float(p["custo_unitario"]), step=0.1)
            obs = c4.text_input("Observação", "Recebimento de compra")
            if st.form_submit_button("Registrar entrada", type="primary"):
                if validade <= hoje():
                    st.error("A validade precisa ser posterior a hoje.")
                else:
                    codigo = registrar_entrada(int(pid), int(qtd), validade, custo, usuario, obs)
                    st.success(f"Lote {codigo} registrado: {num(qtd)} × {p['nome']}.")

    elif aba == "Envio para evento":
        ev = eventos()
        plan = ev[ev["status"] == "Planejado"].reset_index(drop=True)
        if plan.empty:
            st.info("Cadastre um evento planejado em **Cadastros**.")
        else:
            with st.form("envio"):
                ei = st.selectbox("Evento", plan.index, format_func=lambda i: rotulo_evento(plan.loc[i]))
                c1, c2 = st.columns(2)
                pid = c1.selectbox("Produto", prods["id"], format_func=nome_prod.get, key="envio_prod")
                pdv = c2.selectbox("Lanchonete (destino)", PONTOS_VENDA)
                qtd = st.number_input("Quantidade", min_value=1, value=100, step=1)
                if st.form_submit_button("Enviar para o evento", type="primary"):
                    try:
                        enviar_para_evento(int(plan.loc[ei, "id"]), int(pid), int(qtd), usuario, pdv)
                        st.success(f"{num(qtd)} × {nome_prod[pid]} enviados para {pdv} — {plan.loc[ei, 'nome']}.")
                    except ValueError as e:
                        st.error(str(e))
            st.caption("A quantidade sugerida pelo modelo de ML para cada produto está na página **Previsão de demanda**, "
                       "que também permite enviar tudo de uma vez.")

    elif aba == "Transferência entre lanchonetes":
        abertos = eventos_abertos()
        if abertos.empty:
            st.info("Nenhum evento em andamento.")
            return
        ei = st.selectbox("Evento", abertos.index, format_func=lambda i: rotulo_evento(abertos.loc[i]), key="tr_ev")
        evento_id = int(abertos.loc[ei, "id"])
        lanch = lanchonetes_do_evento(evento_id)
        if not lanch:
            st.info("Nenhuma lanchonete com produto neste evento.")
            return
        origem = st.selectbox("De (lanchonete que cede)", lanch, key="tr_origem")
        disp = estoque_no_evento(evento_id, origem).groupby(["produto_id", "produto"], as_index=False)[
            "saldo_evento"].sum()
        with st.form("transferencia", clear_on_submit=True):
            pi = st.selectbox("Produto", disp.index, format_func=lambda i: (
                f"{disp.loc[i, 'produto']} (disponível: {num(disp.loc[i, 'saldo_evento'])})"))
            c1, c2 = st.columns(2)
            destino = c1.selectbox("Para (lanchonete que recebe)", [p for p in PONTOS_VENDA if p != origem])
            qtd = c2.number_input("Quantidade", min_value=1, value=1, step=1)
            if st.form_submit_button("Transferir", type="primary"):
                try:
                    transferir(evento_id, int(disp.loc[pi, "produto_id"]), origem, destino, int(qtd), usuario)
                    st.success(f"{num(qtd)} × {disp.loc[pi, 'produto']} transferidos de {origem} para {destino}.")
                except ValueError as e:
                    st.error(str(e))
        st.caption("Para mandar mais produto do depósito (reposição), use **Envio para evento** com a mesma lanchonete.")

    elif aba == "Contagem e encerramento":
        abertos = eventos_abertos()
        if abertos.empty:
            st.info("Nenhum evento em andamento.")
            return
        ei = st.selectbox("Evento", abertos.index, format_func=lambda i: rotulo_evento(abertos.loc[i]), key="enc_ev")
        evento_id = int(abertos.loc[ei, "id"])
        estoque = estoque_no_evento(evento_id)
        if not estoque.empty:
            st.markdown("**1. Contagem por lanchonete (abertura e fechamento)**")
            resumo = estoque.groupby("lanchonete", dropna=False, as_index=False).agg(
                produtos=("produto_id", "nunique"), unidades=("saldo_evento", "sum"))
            st.dataframe(resumo.fillna({"lanchonete": "(sem lanchonete)"}), hide_index=True, width="stretch",
                         column_config={"lanchonete": "Lanchonete a contar", "produtos": "Produtos",
                                        "unidades": "Unidades no sistema"})
            lanchonetes = lanchonetes_do_evento(evento_id)
            if lanchonetes:
                pdv = st.selectbox("Lanchonete que está sendo contada", lanchonetes, key="enc_pdv")
                momento = st.radio("Momento da contagem", ["Abertura (início do evento)", "Fechamento (término)"],
                                   horizontal=True, key="enc_momento")
                final = momento.startswith("Fechamento")
                sist = estoque_no_evento(evento_id, pdv).groupby(["produto_id", "produto"], as_index=False)[
                    "saldo_evento"].sum().rename(columns={"saldo_evento": "sistema"})
                sist["contado"] = sist["sistema"]
                st.caption("Digite na coluna **Contado** a quantidade que sobrou. O contado volta ao depósito; "
                           "a diferença é registrada como perda." if final else
                           "Digite na coluna **Contado** o que está na lanchonete. O que não chegou volta ao "
                           "depósito; para mandar mais, use **Envio para evento**.")
                editado = st.data_editor(
                    sist, hide_index=True, width="stretch", key=f"contagem_{evento_id}_{pdv}_{final}",
                    disabled=["produto_id", "produto", "sistema"], column_order=["produto", "sistema", "contado"],
                    column_config={"produto": "Produto", "sistema": "No sistema",
                                   "contado": st.column_config.NumberColumn("Contado", min_value=0, step=1)})
                if st.button(f"Confirmar contagem de {pdv}", type="primary"):
                    contado = editado.set_index("produto_id")["contado"].fillna(0)
                    if (contado > editado.set_index("produto_id")["sistema"]).any():
                        st.error("A quantidade contada não pode ser maior que a do sistema. Confira a contagem ou "
                                 "registre a venda que faltou.")
                    else:
                        falta = registrar_contagem(evento_id, pdv, contado.astype(int).to_dict(), usuario, final)
                        st.toast(f"Contagem de {'fechamento' if final else 'abertura'} de {pdv} registrada. "
                                 f"Diferença: {num(falta)} unidade(s).", icon="✅")
                        st.rerun()
        else:
            st.success("Todas as lanchonetes deste evento já foram contadas.")
        st.markdown("**2. Encerrar o evento**")
        with st.form("encerrar"):
            est = int(eventos().set_index("id").loc[evento_id, "publico_estimado"])
            publico = st.number_input("Público real", min_value=1, value=max(est, 1), step=100)
            st.caption("Lanchonetes não contadas devolvem ao depósito o saldo do sistema. O evento passa a "
                       "'Realizado' e entra no histórico usado pelo modelo.")
            if st.form_submit_button("Encerrar evento", type="primary"):
                st.success(f"Evento encerrado. {num(encerrar_evento(evento_id, int(publico), usuario))} "
                           "unidades devolvidas ao depósito sem contagem.")

    elif aba == "Perda / ajuste":
        lotes = saldo_lotes()
        lotes = lotes[lotes["saldo"] > 0]
        if lotes.empty:
            st.info("Não há lotes com saldo no depósito.")
            return
        with st.form("perda"):
            tipo = st.radio("Tipo", ["PERDA_VALIDADE", "PERDA_AVARIA", "AJUSTE"], horizontal=True,
                            format_func={"PERDA_VALIDADE": "Perda por validade", "PERDA_AVARIA": "Avaria",
                                         "AJUSTE": "Ajuste de inventário"}.get)
            li = st.selectbox("Lote", lotes.index, format_func=lambda i: (
                f"{lotes.loc[i, 'produto']} · {lotes.loc[i, 'codigo']} · vence "
                f"{pd.to_datetime(lotes.loc[i, 'data_validade']):%d/%m/%Y} · saldo {num(lotes.loc[i, 'saldo'])}"))
            qtd = st.number_input("Quantidade (no ajuste, use valor negativo para reduzir)", value=1, step=1)
            obs = st.text_input("Motivo / observação")
            if st.form_submit_button("Registrar", type="primary"):
                saldo = int(lotes.loc[li, "saldo"])
                validade = pd.to_datetime(lotes.loc[li, "data_validade"]).date()
                if tipo == "PERDA_VALIDADE" and validade > hoje():
                    st.error(f"Este lote vence em {validade:%d/%m/%Y}. Perda por validade só vale para lote vencido; "
                             "para outros motivos, use Avaria ou Ajuste de inventário.")
                elif tipo != "AJUSTE" and not 0 < qtd <= saldo:
                    st.error(f"Informe uma quantidade entre 1 e {saldo}.")
                elif tipo == "AJUSTE" and (qtd == 0 or saldo + qtd < 0):
                    st.error("Ajuste inválido: o saldo do lote não pode ficar negativo.")
                elif not obs.strip():
                    st.error("Informe o motivo.")
                else:
                    registrar_perda_ou_ajuste(tipo, int(lotes.loc[li, "produto_id"]), int(lotes.loc[li, "lote_id"]),
                                              int(qtd), usuario, obs)
                    st.success("Movimentação registrada.")

    else:
        tipo = st.multiselect("Filtrar tipos", ["ENTRADA", "SAIDA_EVENTO", "RETORNO_EVENTO", "PERDA_VALIDADE",
                                                "PERDA_AVARIA", "AJUSTE"])
        filtro = f"WHERE m.tipo IN ({','.join('?' * len(tipo))})" if tipo else ""
        hist = q(f"""SELECT m.data_hora, m.tipo, p.nome AS produto, l.codigo AS lote, e.nome AS evento,
                            m.ponto_venda AS lanchonete, m.quantidade, m.usuario, m.observacao
                     FROM movimentacoes m JOIN produtos p ON p.id = m.produto_id
                     LEFT JOIN lotes l ON l.id = m.lote_id LEFT JOIN eventos e ON e.id = m.evento_id
                     {filtro} ORDER BY m.id DESC LIMIT 300""", tuple(tipo))
        st.dataframe(hist, hide_index=True, width="stretch")
        baixar_excel(hist, "historico_movimentacoes.xlsx", key="xl_hist")


def pagina_estoque(usuario):
    st.title("⏳ Estoque e validade")
    lotes = saldo_lotes()
    lotes = lotes[lotes["saldo"] > 0].copy()
    lotes["data_validade"] = pd.to_datetime(lotes["data_validade"])
    lotes["dias_para_vencer"] = (lotes["data_validade"] - pd.Timestamp(hoje())).dt.days
    prods = produtos()
    prods = prods[(prods["ativo"] == 1) | prods["id"].isin(lotes["produto_id"])]

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
    baixar_excel(resumo[["nome", "categoria", "saldo", "estoque_minimo", "valor", "situacao"]], "saldo_por_produto.xlsx",
                 key="xl_saldo")

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
    baixar_excel(prox[["produto", "codigo", "data_entrada", "data_validade", "dias_para_vencer", "saldo"]],
                 "lotes_por_validade.xlsx", key="xl_lotes")


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
    # Produto recém-cadastrado não tem vendas para o modelo aprender: não recebe sugestão automática.
    com_historico = set(q("SELECT DISTINCT produto_id FROM vendas")["produto_id"])
    prev["historico"] = prev["produto_id"].isin(com_historico).map({True: "Sim", False: "Sem histórico"})
    prev.loc[prev["historico"] != "Sim", ["previsao", "sugestao"]] = 0
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
    st.dataframe(prev[["produto", "categoria", "historico", "previsao", "sugestao", "ja_enviado", "saldo_deposito",
                       "comprar", "custo_compra"]].round({"previsao": 0}), hide_index=True, width="stretch",
                 column_config={"produto": "Produto", "categoria": "Categoria", "historico": "Histórico",
                                "previsao": "Previsão (un.)",
                                "sugestao": "Sugestão de envio", "ja_enviado": "Já no evento",
                                "saldo_deposito": "Saldo válido no depósito", "comprar": "Comprar",
                                "custo_compra": st.column_config.NumberColumn("Custo da compra", format="R$ %.2f")})
    if (prev["historico"] != "Sim").any():
        st.caption("Produtos **sem histórico** ainda não têm vendas registradas: defina a quantidade em "
                   "**Movimentações > Envio para evento**. Depois de alguns eventos, o modelo passa a prevê-los.")
    compras = prev[prev["comprar"] > 0][["produto", "comprar", "custo_compra"]].rename(
        columns={"produto": "Produto", "comprar": "Quantidade a comprar", "custo_compra": "Custo (R$)"})
    baixar_excel(compras, f"lista_compras_evento_{evento_id}.xlsx", "📥 Baixar lista de compras (Excel)",
                 key="xl_compras")
    c1, c2 = st.columns([2, 3], vertical_alignment="bottom")
    pdv = c1.selectbox("Lanchonete (destino)", PONTOS_VENDA, key="prev_pdv")
    if c2.button("Enviar ao evento a quantidade sugerida (com o estoque disponível)", type="primary"):
        enviados = 0
        for r in prev.itertuples():
            qtd = min(r.sugestao - r.ja_enviado, r.saldo_deposito)
            if qtd > 0:
                enviar_para_evento(evento_id, r.produto_id, int(qtd), usuario, pdv)
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


def campos_evento(chave, ev=None):
    """Campos do formulário de evento; ev traz os valores atuais na edição."""
    c1, c2, c3 = st.columns(3)
    nome = c1.text_input("Nome do evento", ev["nome"] if ev is not None else "", key=f"{chave}_nome")
    tipo = c2.selectbox("Tipo", TIPOS_EVENTO, TIPOS_EVENTO.index(ev["tipo"]) if ev is not None else 0,
                        key=f"{chave}_tipo")
    data_ev = c3.date_input("Data", ev["data"].date() if ev is not None else hoje() + timedelta(days=7),
                            format="DD/MM/YYYY", key=f"{chave}_data")
    c4, c5, c6, c7 = st.columns(4)
    hora = c4.number_input("Hora de início", 0, 23, int(ev["hora_inicio"]) if ev is not None else 20,
                           key=f"{chave}_hora")
    duracao = c5.number_input("Duração (h)", 1, 16, int(ev["duracao_h"]) if ev is not None else 5,
                              key=f"{chave}_dur")
    publico = c6.number_input("Público estimado", 50, 50000, int(ev["publico_estimado"]) if ev is not None else 5000,
                              step=100, key=f"{chave}_pub")
    temp = c7.number_input("Temperatura prevista (°C)", -5.0, 45.0,
                           float(ev["temperatura_prevista"]) if ev is not None else 24.0, step=0.5, key=f"{chave}_temp")
    return nome.strip(), tipo, data_ev, int(hora), float(duracao), int(publico), float(temp)


def erro_evento(nome, data_ev):
    if not nome:
        return "Informe o nome do evento."
    if data_ev < hoje():
        return "A data do evento não pode estar no passado."
    return None


def campos_produto(chave, p=None):
    """Nome, categoria e preço bastam; em branco (0), os demais campos usam o padrão da categoria."""
    nome = st.text_input("Nome", p["nome"] if p is not None else "", key=f"{chave}_nome")
    c1, c2 = st.columns(2)
    cat = c1.selectbox("Categoria", CATEGORIAS, CATEGORIAS.index(p["categoria"]) if p is not None else 0,
                       key=f"{chave}_cat")
    preco = c2.number_input("Preço de venda (R$)", 0.0, 1000.0, float(p["preco_venda"]) if p is not None else 0.0,
                            step=0.5, key=f"{chave}_preco")
    with st.expander("Mais detalhes (opcional)"):
        st.caption(f"Deixe 0 para usar o padrão da categoria; o custo padrão é {CUSTO_PADRAO:.0%} do preço.")
        c3, c4 = st.columns(2)
        custo = c3.number_input("Custo (R$)", 0.0, 1000.0, float(p["custo_unitario"]) if p is not None else 0.0,
                                step=0.5, key=f"{chave}_custo")
        unidade = c4.text_input("Unidade", p["unidade"] if p is not None else "", key=f"{chave}_un")
        c5, c6, c7 = st.columns(3)
        validade = c5.number_input("Validade (dias)", 0, 3650, int(p["validade_dias"]) if p is not None else 0,
                                   key=f"{chave}_val")
        emb = c6.number_input("Embalagem (un.)", 0, 1000, int(p["embalagem"]) if p is not None else 0,
                              key=f"{chave}_emb")
        minimo = c7.number_input("Estoque mínimo", 0, 1_000_000, int(p["estoque_minimo"]) if p is not None else 0,
                                 key=f"{chave}_min")
    un_pad, val_pad, emb_pad = PADRAO_CATEGORIA[cat]
    return (nome.strip(), cat, unidade.strip() or un_pad, preco, custo or round(preco * CUSTO_PADRAO, 2),
            int(validade) or val_pad, int(emb) or emb_pad, int(minimo))


def pagina_cadastros():
    st.title("🗂️ Cadastros")
    aba = st.radio("Cadastro", ["Eventos", "Produtos", "Preços em lista", "Importar produtos (Excel)"],
                   horizontal=True)

    if aba == "Eventos":
        modo = st.radio("Ação", ["Novo evento", "Editar ou excluir evento planejado"], horizontal=True,
                        key="modo_evento")
        if modo == "Novo evento":
            with st.form("novo_evento", clear_on_submit=True):
                nome, tipo, data_ev, hora, duracao, publico, temp = campos_evento("novo_ev")
                if st.form_submit_button("Cadastrar evento", type="primary"):
                    if erro := erro_evento(nome, data_ev):
                        st.error(erro)
                    else:
                        con.execute("INSERT INTO eventos (nome, tipo, data, hora_inicio, duracao_h, publico_estimado,"
                                    " temperatura_prevista, status) VALUES (?, ?, ?, ?, ?, ?, ?, 'Planejado')",
                                    (nome, tipo, data_ev.isoformat(), hora, duracao, publico, temp))
                        con.commit()
                        invalidar()
                        st.success("Evento cadastrado. Veja a previsão de demanda dele na página **Previsão de demanda**.")
        else:
            plan = eventos()
            plan = plan[plan["status"] == "Planejado"].reset_index(drop=True)
            if plan.empty:
                st.info("Não há eventos planejados.")
            else:
                st.caption("Só eventos planejados podem ser alterados; os realizados formam o histórico do modelo.")
                ei = st.selectbox("Evento", plan.index, format_func=lambda i: rotulo_evento(plan.loc[i]),
                                  key="edit_ev_sel")
                ev = plan.loc[ei]
                evento_id = int(ev["id"])
                with st.form(f"editar_evento_{evento_id}"):
                    nome, tipo, data_ev, hora, duracao, publico, temp = campos_evento(f"ed_ev_{evento_id}", ev)
                    if st.form_submit_button("Salvar alterações", type="primary"):
                        if erro := erro_evento(nome, data_ev):
                            st.error(erro)
                        else:
                            con.execute("UPDATE eventos SET nome = ?, tipo = ?, data = ?, hora_inicio = ?, duracao_h = ?,"
                                        " publico_estimado = ?, temperatura_prevista = ? WHERE id = ?",
                                        (nome, tipo, data_ev.isoformat(), hora, duracao, publico, temp, evento_id))
                            con.commit()
                            invalidar()
                            st.success("Evento atualizado.")
                usado = q("SELECT (SELECT COUNT(*) FROM movimentacoes WHERE evento_id = ?)"
                          " + (SELECT COUNT(*) FROM vendas WHERE evento_id = ?) AS n", (evento_id, evento_id))["n"].iloc[0]
                if usado:
                    st.caption("Este evento já recebeu produtos ou vendas e não pode ser excluído. "
                               "Para fechá-lo, use **Movimentações > Encerrar evento**.")
                elif st.button("🗑️ Excluir este evento", key=f"excluir_ev_{evento_id}"):
                    con.execute("DELETE FROM eventos WHERE id = ?", (evento_id,))
                    con.commit()
                    invalidar()
                    st.rerun()
        ev = eventos().sort_values("data", ascending=False)
        st.dataframe(ev.drop(columns=["id"]), hide_index=True, width="stretch",
                     column_config={"data": st.column_config.DateColumn("Data", format="DD/MM/YYYY")})

    elif aba == "Produtos":
        modo = st.radio("Ação", ["Novo produto", "Editar produto"], horizontal=True, key="modo_produto")
        if modo == "Novo produto":
            with st.form("novo_produto", clear_on_submit=True):
                nome, cat, unidade, preco, custo, validade, emb, minimo = campos_produto("novo_prod")
                if st.form_submit_button("Cadastrar produto", type="primary"):
                    if not nome:
                        st.error("Informe o nome do produto.")
                    elif preco <= 0:
                        st.error("Informe o preço de venda.")
                    elif nome_em_uso(nome):
                        st.error(f"Já existe um produto chamado \"{nome}\". Use **Editar produto** para alterá-lo.")
                    else:
                        con.execute("INSERT INTO produtos (nome, categoria, unidade, preco_venda, custo_unitario,"
                                    " validade_dias, embalagem, estoque_minimo) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                                    (nome, cat, unidade, preco, custo, validade, emb, minimo))
                        con.commit()
                        invalidar()
                        st.success(f"Produto \"{nome}\" cadastrado.")
                        if preco < custo:
                            st.warning("Atenção: o preço de venda está abaixo do custo.")
        else:
            prods = produtos().reset_index(drop=True)
            pi = st.selectbox("Produto", prods.index, key="edit_prod_sel", format_func=lambda i: (
                prods.loc[i, "nome"] + ("" if prods.loc[i, "ativo"] else " (inativo)")))
            p = prods.loc[pi]
            produto_id = int(p["id"])
            with st.form(f"editar_produto_{produto_id}"):
                nome, cat, unidade, preco, custo, validade, emb, minimo = campos_produto(f"ed_prod_{produto_id}", p)
                ativo = st.checkbox("Produto ativo (desmarque para tirá-lo das listas sem perder o histórico)",
                                    bool(p["ativo"]), key=f"ed_prod_{produto_id}_ativo")
                if st.form_submit_button("Salvar alterações", type="primary"):
                    if not nome:
                        st.error("Informe o nome do produto.")
                    elif ativo and preco <= 0:
                        st.error("Informe o preço de venda para deixar o produto ativo.")
                    elif nome_em_uso(nome, produto_id):
                        st.error(f"Já existe outro produto chamado \"{nome}\".")
                    else:
                        con.execute("UPDATE produtos SET nome = ?, categoria = ?, unidade = ?, preco_venda = ?,"
                                    " custo_unitario = ?, validade_dias = ?, embalagem = ?, estoque_minimo = ?,"
                                    " ativo = ? WHERE id = ?",
                                    (nome, cat, unidade, preco, custo, validade, emb, minimo, int(ativo), produto_id))
                        con.commit()
                        invalidar()
                        st.success("Produto atualizado.")
                        if preco < custo:
                            st.warning("Atenção: o preço de venda está abaixo do custo.")
        st.dataframe(produtos().drop(columns=["id"]), hide_index=True, width="stretch",
                     column_config={"ativo": st.column_config.CheckboxColumn("Ativo")})

    elif aba == "Preços em lista":
        pagina_precos()
    else:
        pagina_importar_produtos()


def pagina_precos():
    """Preço de um produto por vez, pensado para o celular: salva e já mostra o próximo sem preço."""
    prods = produtos()
    pendentes = prods[prods["preco_venda"] <= 0]
    total = len(prods)
    st.progress(1 - len(pendentes) / total, f"{total - len(pendentes)} de {total} produtos com preço")
    so_pendentes = st.toggle("Só produtos sem preço", value=not pendentes.empty,
                             help="Desligue para corrigir o preço de um produto que já tem preço.")
    lista = (pendentes if so_pendentes else prods).reset_index(drop=True)
    if lista.empty:
        st.success("Todos os produtos já têm preço. 🎉")
        return
    # Ao salvar, o produto sai da lista de pendentes e a seleção volta para o primeiro da lista (o próximo).
    pi = st.selectbox("Produto", lista.index, key="preco_prod", format_func=lambda i: (
        f"{lista.loc[i, 'nome']} · {lista.loc[i, 'categoria']}"))
    p = lista.loc[pi]
    produto_id = int(p["id"])
    with st.form(f"preco_{produto_id}"):
        c1, c2 = st.columns(2)
        preco = c1.number_input("Preço de venda (R$)", 0.0, 1000.0, float(p["preco_venda"]), step=0.5)
        custo = c2.number_input("Custo (opcional)", 0.0, 1000.0, float(p["custo_unitario"]), step=0.1,
                                help=f"Em branco (0), o custo é estimado em {CUSTO_PADRAO:.0%} do preço.")
        if st.form_submit_button("Salvar e ir para o próximo", type="primary", width="stretch"):
            if preco <= 0:
                st.error("Informe o preço de venda.")
            else:
                con.execute("UPDATE produtos SET preco_venda = ?, custo_unitario = ?, ativo = 1 WHERE id = ?",
                            (preco, custo or round(preco * CUSTO_PADRAO, 2), produto_id))
                con.commit()
                invalidar()
                st.toast(f"{p['nome']}: {brl(preco)} salvo.", icon="✅")
                st.rerun()
    st.caption("Validade e embalagem usam o padrão da categoria e podem ser ajustadas em **Produtos > Editar "
               "produto**. No computador, também dá para preencher tudo pela planilha em **Importar produtos (Excel)**.")
    if so_pendentes:
        st.dataframe(lista[["nome", "categoria"]], hide_index=True, width="stretch",
                     column_config={"nome": "Ainda sem preço", "categoria": "Categoria"})


COLUNAS_IMPORTACAO = ["nome", "categoria", "unidade", "preco_venda", "custo_unitario", "validade_dias", "embalagem",
                      "estoque_minimo"]


def pagina_importar_produtos():
    st.markdown("Cadastre ou atualize vários produtos de uma vez a partir de uma planilha do Excel. "
                "Produtos com o mesmo nome de um já cadastrado são **atualizados**; os demais são **incluídos**.")
    modelo = pd.DataFrame([["Água mineral 500ml", "Bebida não alcoólica", "un", 8.0, 2.5, 365, 12, 0]],
                          columns=COLUNAS_IMPORTACAO)
    baixar_excel(modelo, "modelo_importacao_produtos.xlsx", "📥 Baixar planilha modelo", key="xl_modelo")
    st.caption("Categorias aceitas: " + ", ".join(CATEGORIAS) + ". Unidade e estoque mínimo são opcionais.")
    arquivo = st.file_uploader("Planilha preenchida (.xlsx ou .csv)", type=["xlsx", "csv"])
    if arquivo is None:
        return
    try:
        df = (pd.read_csv(arquivo, sep=None, engine="python") if arquivo.name.lower().endswith(".csv")
              else pd.read_excel(arquivo))
    except Exception:
        st.error("Não foi possível ler o arquivo. Use a planilha modelo.")
        return
    df.columns = [str(c).strip().lower() for c in df.columns]
    faltando = [c for c in COLUNAS_IMPORTACAO[:7] if c not in df.columns and c != "unidade"]
    if faltando:
        st.error("Colunas obrigatórias ausentes: " + ", ".join(faltando) + ". Use a planilha modelo.")
        return
    df["unidade"] = df.get("unidade", pd.Series(dtype=str)).fillna("un")
    df["estoque_minimo"] = df.get("estoque_minimo", pd.Series(dtype=float)).fillna(0)
    df = df[df["nome"].notna() & (df["nome"].astype(str).str.strip() != "")].copy()
    df["nome"] = df["nome"].astype(str).str.strip()
    for c in ["preco_venda", "custo_unitario", "validade_dias", "embalagem", "estoque_minimo"]:
        df[c] = pd.to_numeric(df[c], errors="coerce")
    df["problema"] = ""
    df.loc[~df["categoria"].isin(CATEGORIAS), "problema"] = "categoria inválida"
    df.loc[df[["preco_venda", "custo_unitario"]].isna().any(axis=1) | (df["preco_venda"] < 0)
           | (df["custo_unitario"] < 0), "problema"] = "preço ou custo vazio/inválido"
    df.loc[df["validade_dias"].isna() | (df["validade_dias"] < 1) | df["embalagem"].isna() | (df["embalagem"] < 1),
           "problema"] = "validade ou embalagem vazia/inválida"
    df.loc[df["nome"].str.lower().duplicated(keep="first"), "problema"] = "nome repetido na planilha"
    validos, rejeitados = df[df["problema"] == ""], df[df["problema"] != ""]
    existentes = set(produtos()["nome"].str.strip().str.lower())
    novos = (~validos["nome"].str.lower().isin(existentes)).sum()
    st.write(f"**{len(validos)}** linhas válidas ({novos} novos produtos e {len(validos) - novos} atualizações); "
             f"**{len(rejeitados)}** com problema.")
    if not rejeitados.empty:
        st.dataframe(rejeitados[["nome", "categoria", "problema"]], hide_index=True, width="stretch")
    if not validos.empty and st.button("Importar produtos válidos", type="primary"):
        ids = {n.strip().lower(): i for i, n in zip(produtos()["id"], produtos()["nome"])}
        for r in validos.itertuples():
            valores = (r.categoria, str(r.unidade), float(r.preco_venda), float(r.custo_unitario), int(r.validade_dias),
                       int(r.embalagem), int(r.estoque_minimo))
            if r.nome.lower() in ids:
                con.execute("UPDATE produtos SET categoria = ?, unidade = ?, preco_venda = ?, custo_unitario = ?,"
                            " validade_dias = ?, embalagem = ?, estoque_minimo = ?, ativo = 1 WHERE id = ?",
                            (*valores, int(ids[r.nome.lower()])))
            else:
                con.execute("INSERT INTO produtos (categoria, unidade, preco_venda, custo_unitario, validade_dias,"
                            " embalagem, estoque_minimo, nome) VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (*valores, r.nome))
        con.commit()
        invalidar()
        st.success(f"{len(validos)} produtos importados.")


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
                    st.success(f"Usuário {login.strip()} cadastrado. O login diferencia maiúsculas de minúsculas.")
                except ValueError as e:  # senha curta ou login já existente
                    st.error(str(e))
                except sqlite3.Error:
                    st.error("Não foi possível cadastrar o usuário. Tente novamente.")
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
    "📊 Painel": (pagina_painel, False, {"Administrador"}),
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
# Menu no topo da página, sempre visível: no celular o menu lateral ficava escondido e cobria a tela.
topo1, topo2 = st.columns([4, 1], vertical_alignment="bottom")
escolha = topo1.selectbox("Tela", permitidas, key="tela")
with topo2:
    st.caption(f"{usuario['nome']} · {usuario['perfil']}")
    if st.button("Sair", width="stretch"):
        del st.session_state["usuario"]
        st.rerun()
st.divider()

pagina, recebe_usuario, _ = PAGINAS[escolha]
if recebe_usuario:
    pagina(usuario["login"])
else:
    pagina()
