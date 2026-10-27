"""Exporta o banco e os resultados do modelo em CSV (modelo estrela) para o Power BI.

Arquivos no padrão brasileiro: separador ";" e decimal ",", codificação UTF-8.
Uso:  python exportar_powerbi.py
"""
import json

import pandas as pd

import ml
from db import POWERBI_DIR, conectar, consultar

CONSULTAS = {
    "dim_produto": "SELECT * FROM produtos",
    "dim_evento": "SELECT * FROM eventos",
    "dim_lote": "SELECT * FROM lotes",
    "fato_vendas": """SELECT v.id, v.data_hora, date(v.data_hora) AS data, v.evento_id, v.produto_id, v.lote_id,
                             v.ponto_venda, v.forma_pagamento, v.quantidade, v.preco_unitario,
                             v.quantidade * v.preco_unitario AS valor
                      FROM vendas v""",
    "fato_movimentacoes": """SELECT m.*, date(m.data_hora) AS data, m.quantidade * l.custo_unitario AS valor_custo
                             FROM movimentacoes m LEFT JOIN lotes l ON l.id = m.lote_id""",
    "saldo_lotes": "SELECT * FROM vw_saldo_lotes",
}


def salvar(df: pd.DataFrame, nome: str):
    df.to_csv(POWERBI_DIR / f"{nome}.csv", index=False, sep=";", decimal=",", encoding="utf-8-sig")
    print(f"  {nome}.csv: {len(df):,} linhas")


def main():
    POWERBI_DIR.mkdir(exist_ok=True)
    con = conectar()
    for nome, sql in CONSULTAS.items():
        salvar(consultar(con, sql), nome)

    if ml.carregar_modelo() is not None:
        salvar(pd.read_csv(ml.ARQ_PREV_TESTE), "ml_previsoes_teste")
        salvar(pd.read_csv(ml.ARQ_CLUSTERS), "ml_clusters_produtos")
        m = json.loads(ml.ARQ_METRICAS.read_text(encoding="utf-8"))
        salvar(pd.DataFrame(m["resultados_teste"]).T.reset_index(names="modelo"), "ml_metricas_modelos")
        planejados = consultar(con, "SELECT id FROM eventos WHERE status = 'Planejado'")["id"].tolist()
        if planejados:
            prev = ml.prever_eventos(con, planejados)
            salvar(prev[["evento_id", "evento", "tipo", "data", "produto_id", "produto", "categoria",
                         "publico_estimado", "previsao", "sugestao"]].round({"previsao": 1}),
                   "ml_previsoes_eventos_planejados")
    con.close()
    print(f"Arquivos em {POWERBI_DIR}")


if __name__ == "__main__":
    main()
