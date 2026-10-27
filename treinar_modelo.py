"""Treina e avalia os modelos de previsão de demanda e salva o melhor em modelos/.

Uso:  python treinar_modelo.py
"""
from db import conectar
from ml import ARQ_METRICAS, treinar


def main():
    con = conectar()
    r = treinar(con)
    con.close()

    print(f"Base: {r['linhas']} linhas ({r['eventos']} eventos x {r['produtos']} produtos), {r['periodo'][0]} a {r['periodo'][1]}")
    print(f"Treino: {r['treino']['eventos']} eventos | Teste: {r['teste']['eventos']} eventos "
          f"({r['teste']['periodo'][0]} a {r['teste']['periodo'][1]})\n")
    print(f"{'Modelo':48} {'MAE':>8} {'RMSE':>8} {'R²':>7} {'WAPE':>7}")
    for nome, m in r["resultados_teste"].items():
        print(f"{nome:48} {m['MAE']:8.1f} {m['RMSE']:8.1f} {m['R2']:7.3f} {m['WAPE']:7.1%}")
    print(f"\nMelhor modelo: {r['melhor_modelo']}")
    print("\nValidação cruzada temporal (MAE):")
    for nome, m in r["validacao_cruzada_temporal"].items():
        print(f"  {nome:30} {m['MAE_medio']:.1f} ± {m['MAE_desvio']:.1f}")
    print("\nVariáveis mais importantes:")
    for d in r["importancia_variaveis"][:5]:
        print(f"  {d['variavel']:48} {d['importancia']:.2f}")
    i = r["impacto_operacional_teste"]
    print("\nImpacto no período de teste (sugestão do modelo x método manual):")
    print(f"  Unidades enviadas: {i['unidades_enviadas_manual']:,} manual x {i['unidades_sugeridas_ml']:,} modelo")
    print(f"  Sobras: {i['sobra_manual_un']:,} manual x {i['sobra_ml_un']:,} modelo")
    print(f"  Custo de sobras perecíveis: R$ {i['custo_sobra_pereciveis_manual']:,.2f} x R$ {i['custo_sobra_pereciveis_ml']:,.2f}")
    print(f"  Itens com ruptura (manual): {i['rupturas_manual_pct']:.1%} | itens com falta (modelo): {i['itens_com_falta_ml_pct']:.1%}")
    c = r["agrupamento_produtos"]
    print(f"\nAgrupamento de produtos: k={c['k']}, silhouette={c['silhouette']:.3f}")
    print(f"\nMétricas salvas em {ARQ_METRICAS}")


if __name__ == "__main__":
    main()
