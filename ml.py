"""Aprendizado de máquina: previsão de demanda por produto em cada evento e
agrupamento de produtos por padrão de consumo.

Tarefa (regressão supervisionada): prever quantas unidades de cada produto serão vendidas
em um evento, usando apenas o que se sabe ANTES do evento (tipo, público estimado,
duração, dia, mês, temperatura prevista, preço e o histórico recente do produto).
"""
import json
import math

import joblib
import numpy as np
import pandas as pd
from sklearn.cluster import KMeans
from sklearn.compose import ColumnTransformer
from sklearn.ensemble import HistGradientBoostingRegressor, RandomForestRegressor
from sklearn.impute import SimpleImputer
from sklearn.inspection import permutation_importance
from sklearn.linear_model import Ridge
from sklearn.metrics import mean_absolute_error, mean_squared_error, r2_score, silhouette_score
from sklearn.pipeline import Pipeline
from sklearn.preprocessing import OneHotEncoder, StandardScaler

from db import MODELOS_DIR, consultar

MARGEM_MANUAL = 0.20
MARGEM_SUGESTAO = 0.10
PROPORCAO_TESTE = 0.20

COLS_CATEGORICAS = ["produto", "categoria", "tipo"]
COLS_NUMERICAS = ["publico_estimado", "duracao_h", "temperatura_prevista", "dia_semana", "fim_de_semana",
                  "mes", "preco_venda", "hist_taxa_tipo", "hist_taxa_geral"]
FEATURES = COLS_CATEGORICAS + COLS_NUMERICAS

NOMES_FEATURES = {
    "produto": "Produto", "categoria": "Categoria do produto", "tipo": "Tipo de evento",
    "publico_estimado": "Público estimado", "duracao_h": "Duração do evento",
    "temperatura_prevista": "Temperatura prevista", "dia_semana": "Dia da semana",
    "fim_de_semana": "Fim de semana", "mes": "Mês", "preco_venda": "Preço de venda",
    "hist_taxa_tipo": "Histórico do produto no mesmo tipo de evento",
    "hist_taxa_geral": "Histórico recente do produto",
}

ARQ_MODELO = MODELOS_DIR / "modelo_demanda.joblib"
ARQ_METRICAS = MODELOS_DIR / "metricas.json"
ARQ_PREV_TESTE = MODELOS_DIR / "previsoes_teste.csv"
ARQ_CLUSTERS = MODELOS_DIR / "clusters_produtos.csv"


def carregar_base(con) -> pd.DataFrame:
    """Uma linha por evento x produto, com o que foi enviado e vendido (eventos realizados)
    ou só os dados do evento (eventos planejados)."""
    df = consultar(con, """
        SELECT e.id AS evento_id, e.nome AS evento, e.tipo, e.data, e.duracao_h, e.publico_estimado,
               e.publico_real, e.temperatura_prevista, e.status,
               p.id AS produto_id, p.nome AS produto, p.categoria, p.preco_venda, p.custo_unitario,
               p.validade_dias, p.embalagem,
               COALESCE(env.qtd, 0) AS enviado, COALESCE(v.qtd, 0) AS vendido
        FROM eventos e
        CROSS JOIN produtos p
        LEFT JOIN (SELECT evento_id, produto_id, SUM(quantidade) AS qtd FROM movimentacoes
                   WHERE tipo = 'SAIDA_EVENTO' GROUP BY 1, 2) env
               ON env.evento_id = e.id AND env.produto_id = p.id
        LEFT JOIN (SELECT evento_id, produto_id, SUM(quantidade) AS qtd FROM vendas GROUP BY 1, 2) v
               ON v.evento_id = e.id AND v.produto_id = p.id
        WHERE p.ativo = 1
        ORDER BY e.data, e.id, p.id
    """)
    df["data"] = pd.to_datetime(df["data"])
    return df


def montar_features(df: pd.DataFrame) -> pd.DataFrame:
    """Cria as variáveis de entrada. O histórico usa só eventos anteriores (shift),
    evitando vazamento de informação do próprio evento."""
    df = df.sort_values(["data", "evento_id", "produto_id"]).copy()
    df["dia_semana"] = df["data"].dt.dayofweek
    df["fim_de_semana"] = (df["dia_semana"] >= 4).astype(int)
    df["mes"] = df["data"].dt.month
    realizado = df["status"] == "Realizado"
    df["taxa"] = np.where(realizado, df["vendido"] / df["publico_estimado"] * 1000, np.nan)
    df["hist_taxa_tipo"] = (df.groupby(["produto_id", "tipo"])["taxa"]
                            .transform(lambda s: s.shift(1).rolling(3, min_periods=1).mean()))
    df["hist_taxa_geral"] = (df.groupby("produto_id")["taxa"]
                             .transform(lambda s: s.shift(1).rolling(5, min_periods=1).mean()))
    return df


def _pipeline(modelo, escalar=False) -> Pipeline:
    numerico = [("imputar", SimpleImputer(strategy="median"))]
    if escalar:
        numerico.append(("escalar", StandardScaler()))
    prep = ColumnTransformer([
        ("cat", OneHotEncoder(handle_unknown="ignore"), COLS_CATEGORICAS),
        ("num", Pipeline(numerico), COLS_NUMERICAS),
    ], sparse_threshold=0)
    return Pipeline([("prep", prep), ("modelo", modelo)])


def candidatos() -> dict:
    return {
        "Regressão Ridge (linear)": _pipeline(Ridge(alpha=1.0), escalar=True),
        "Random Forest": _pipeline(RandomForestRegressor(n_estimators=300, min_samples_leaf=2,
                                                         random_state=42, n_jobs=-1)),
        "Gradient Boosting": _pipeline(HistGradientBoostingRegressor(max_iter=400, learning_rate=0.05,
                                                                     random_state=42)),
    }


def metricas(y_real, y_prev) -> dict:
    y_real, y_prev = np.asarray(y_real, float), np.asarray(y_prev, float)
    return {
        "MAE": float(mean_absolute_error(y_real, y_prev)),
        "RMSE": float(math.sqrt(mean_squared_error(y_real, y_prev))),
        "R2": float(r2_score(y_real, y_prev)),
        "WAPE": float(np.abs(y_real - y_prev).sum() / y_real.sum()),
    }


def sugestao_abastecimento(previsao, embalagem, margem=MARGEM_SUGESTAO):
    return (np.ceil(np.maximum(previsao, 0) * (1 + margem) / embalagem) * embalagem).astype(int)


def _avaliar_cv_temporal(treino: pd.DataFrame, n_folds=4) -> dict:
    """Validação cruzada respeitando o tempo: treina com eventos antigos, valida nos seguintes."""
    eventos = treino["evento_id"].drop_duplicates().to_numpy()
    tamanho = len(eventos) // (n_folds + 1)
    resultado = {}
    for nome, modelo in candidatos().items():
        maes = []
        for k in range(1, n_folds + 1):
            ev_tr, ev_va = eventos[: k * tamanho], eventos[k * tamanho:(k + 1) * tamanho]
            tr, va = treino[treino.evento_id.isin(ev_tr)], treino[treino.evento_id.isin(ev_va)]
            modelo.fit(tr[FEATURES], tr["taxa"])
            prev = modelo.predict(va[FEATURES]) * va["publico_estimado"] / 1000
            maes.append(mean_absolute_error(va["vendido"], prev))
        resultado[nome] = {"MAE_medio": float(np.mean(maes)), "MAE_desvio": float(np.std(maes))}
    return resultado


def _clusterizar_produtos(base: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Agrupa produtos pelo perfil de consumo nos tipos de evento (aprendizado não supervisionado)."""
    perfil = base.pivot_table(index="produto", columns="tipo", values="taxa", aggfunc="mean")
    relativo = perfil.div(perfil.mean(axis=1), axis=0)  # consumo relativo à média do produto
    X = StandardScaler().fit_transform(relativo)
    melhor = None
    for k in range(2, 6):
        km = KMeans(n_clusters=k, n_init=20, random_state=42).fit(X)
        s = silhouette_score(X, km.labels_)
        if melhor is None or s > melhor[1]:
            melhor = (k, s, km.labels_)
    k, sil, rotulos = melhor
    relativo = relativo.assign(grupo=rotulos)
    nomes = {}
    media_geral = relativo.drop(columns="grupo").mean()
    for g, bloco in relativo.groupby("grupo"):
        # tipo de evento em que o grupo mais se diferencia dos demais produtos
        destaque = (bloco.drop(columns="grupo").mean() - media_geral).sort_values(ascending=False)
        nomes[g] = f"Grupo {g + 1}: destaque em {destaque.index[0]}"
    relativo["grupo_nome"] = relativo["grupo"].map(nomes)
    saida = relativo.reset_index().round(3)
    return saida, {"k": int(k), "silhouette": float(sil)}


def treinar(con) -> dict:
    MODELOS_DIR.mkdir(exist_ok=True)
    base = montar_features(carregar_base(con))
    hist = base[base["status"] == "Realizado"].copy()

    eventos = hist[["evento_id", "data"]].drop_duplicates().sort_values("data")
    n_teste = int(round(len(eventos) * PROPORCAO_TESTE))
    ev_teste = set(eventos["evento_id"].iloc[-n_teste:])
    treino, teste = hist[~hist.evento_id.isin(ev_teste)], hist[hist.evento_id.isin(ev_teste)].copy()

    # Referências (baselines)
    teste["prev_manual"] = teste["enviado"] / (1 + MARGEM_MANUAL)
    teste["prev_media_hist"] = (teste["hist_taxa_tipo"].fillna(teste["hist_taxa_geral"])
                                * teste["publico_estimado"] / 1000)
    resultados = {
        "Método manual atual (sem a margem de 20%)": metricas(teste["vendido"], teste["prev_manual"]),
        "Média histórica do produto no tipo de evento": metricas(teste["vendido"], teste["prev_media_hist"]),
    }

    modelos = candidatos()
    for nome, modelo in modelos.items():
        modelo.fit(treino[FEATURES], treino["taxa"])
        teste[f"prev_{nome}"] = modelo.predict(teste[FEATURES]) * teste["publico_estimado"] / 1000
        resultados[nome] = metricas(teste["vendido"], teste[f"prev_{nome}"])

    melhor_nome = min(modelos, key=lambda n: resultados[n]["MAE"])
    melhor = modelos[melhor_nome]
    teste["previsao"] = teste[f"prev_{melhor_nome}"].clip(lower=0)

    imp = permutation_importance(melhor, teste[FEATURES], teste["taxa"], n_repeats=10, random_state=42,
                                 scoring="neg_mean_absolute_error")
    importancias = sorted(({"variavel": NOMES_FEATURES[f], "importancia": float(v)}
                           for f, v in zip(FEATURES, imp.importances_mean)),
                          key=lambda d: -d["importancia"])

    # Impacto operacional no período de teste: abastecer pela sugestão do modelo x método manual
    teste["sugestao"] = sugestao_abastecimento(teste["previsao"], teste["embalagem"])
    perecivel = teste["validade_dias"] <= 7
    sobra_manual = (teste["enviado"] - teste["vendido"]).clip(lower=0)
    sobra_ml = (teste["sugestao"] - teste["vendido"]).clip(lower=0)
    falta_ml = (teste["vendido"] - teste["sugestao"]).clip(lower=0)
    impacto = {
        "unidades_enviadas_manual": int(teste["enviado"].sum()),
        "unidades_sugeridas_ml": int(teste["sugestao"].sum()),
        "sobra_manual_un": int(sobra_manual.sum()),
        "sobra_ml_un": int(sobra_ml.sum()),
        "custo_sobra_pereciveis_manual": float((sobra_manual * teste["custo_unitario"])[perecivel].sum()),
        "custo_sobra_pereciveis_ml": float((sobra_ml * teste["custo_unitario"])[perecivel].sum()),
        "rupturas_manual_pct": float((teste["vendido"] >= teste["enviado"]).mean()),
        "itens_com_falta_ml_pct": float((falta_ml > 0).mean()),
        "unidades_faltantes_ml": int(falta_ml.sum()),
        "margem_sugestao": MARGEM_SUGESTAO,
    }

    cv = _avaliar_cv_temporal(treino)
    clusters, info_clusters = _clusterizar_produtos(hist)

    # Modelo final: retreinado com todo o histórico para prever os próximos eventos
    final = candidatos()[melhor_nome].fit(hist[FEATURES], hist["taxa"])
    joblib.dump({"modelo": final, "nome": melhor_nome, "features": FEATURES}, ARQ_MODELO)

    colunas_teste = ["evento_id", "evento", "tipo", "data", "produto", "categoria", "publico_estimado",
                     "enviado", "vendido", "prev_manual", "previsao", "sugestao"]
    teste[colunas_teste].assign(data=teste["data"].dt.date).round(1).to_csv(ARQ_PREV_TESTE, index=False)
    clusters.to_csv(ARQ_CLUSTERS, index=False)

    resumo = {
        "tarefa": "Regressão: unidades vendidas por produto em cada evento",
        "alvo_modelo": "unidades vendidas por 1.000 pessoas do público estimado (convertido em unidades)",
        "linhas": int(len(hist)), "eventos": int(hist.evento_id.nunique()),
        "produtos": int(hist.produto_id.nunique()),
        "periodo": [str(hist.data.min().date()), str(hist.data.max().date())],
        "treino": {"eventos": int(treino.evento_id.nunique()), "linhas": int(len(treino)),
                   "periodo": [str(treino.data.min().date()), str(treino.data.max().date())]},
        "teste": {"eventos": int(teste.evento_id.nunique()), "linhas": int(len(teste)),
                  "periodo": [str(teste.data.min().date()), str(teste.data.max().date())]},
        "resultados_teste": resultados, "melhor_modelo": melhor_nome, "validacao_cruzada_temporal": cv,
        "importancia_variaveis": importancias, "impacto_operacional_teste": impacto,
        "agrupamento_produtos": info_clusters,
    }
    ARQ_METRICAS.write_text(json.dumps(resumo, ensure_ascii=False, indent=2), encoding="utf-8")
    return resumo


def carregar_modelo():
    return joblib.load(ARQ_MODELO) if ARQ_MODELO.exists() else None


def prever_eventos(con, evento_ids, margem=MARGEM_SUGESTAO) -> pd.DataFrame:
    """Previsão de vendas e sugestão de abastecimento para eventos planejados."""
    pacote = carregar_modelo()
    if pacote is None:
        raise FileNotFoundError("Modelo não treinado. Rode: python treinar_modelo.py")
    base = montar_features(carregar_base(con))
    alvo = base[base["evento_id"].isin(evento_ids)].copy()
    alvo["previsao"] = (pacote["modelo"].predict(alvo[pacote["features"]]) * alvo["publico_estimado"] / 1000).clip(0)
    alvo["sugestao"] = sugestao_abastecimento(alvo["previsao"], alvo["embalagem"], margem)
    return alvo
