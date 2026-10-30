"""Acesso ao banco SQLite da solução (estrutura, conexão e consultas comuns)."""
import sqlite3
from pathlib import Path

import pandas as pd

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "dados"
DB_PATH = DATA_DIR / "estoque_eventos.db"
MODELOS_DIR = BASE_DIR / "modelos"
POWERBI_DIR = BASE_DIR / "powerbi"

TIPOS_MOVIMENTACAO = [
    "ENTRADA",          # compra / recebimento no depósito
    "SAIDA_EVENTO",     # envio do depósito para o evento
    "RETORNO_EVENTO",   # sobra que volta do evento para o depósito
    "PERDA_VALIDADE",   # descarte por vencimento
    "PERDA_AVARIA",     # quebra, dano ou extravio no depósito
    "AJUSTE",           # correção de inventário (pode ser positiva ou negativa)
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS produtos (
    id              INTEGER PRIMARY KEY,
    nome            TEXT    NOT NULL UNIQUE,
    categoria       TEXT    NOT NULL,
    unidade         TEXT    NOT NULL,
    preco_venda     REAL    NOT NULL,
    custo_unitario  REAL    NOT NULL,
    validade_dias   INTEGER NOT NULL,
    embalagem       INTEGER NOT NULL,
    estoque_minimo  INTEGER NOT NULL DEFAULT 0,
    ativo           INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS eventos (
    id                   INTEGER PRIMARY KEY,
    nome                 TEXT    NOT NULL,
    tipo                 TEXT    NOT NULL,
    data                 TEXT    NOT NULL,
    hora_inicio          INTEGER NOT NULL,
    duracao_h            REAL    NOT NULL,
    publico_estimado     INTEGER NOT NULL,
    publico_real         INTEGER,
    temperatura_prevista REAL,
    status               TEXT    NOT NULL CHECK (status IN ('Planejado', 'Realizado'))
);

CREATE TABLE IF NOT EXISTS lotes (
    id                 INTEGER PRIMARY KEY,
    produto_id         INTEGER NOT NULL REFERENCES produtos(id),
    codigo             TEXT    NOT NULL UNIQUE,
    data_entrada       TEXT    NOT NULL,
    data_validade      TEXT    NOT NULL,
    quantidade_inicial INTEGER NOT NULL,
    custo_unitario     REAL    NOT NULL
);

CREATE TABLE IF NOT EXISTS movimentacoes (
    id          INTEGER PRIMARY KEY,
    data_hora   TEXT    NOT NULL,
    tipo        TEXT    NOT NULL CHECK (tipo IN ('ENTRADA', 'SAIDA_EVENTO', 'RETORNO_EVENTO',
                                                 'PERDA_VALIDADE', 'PERDA_AVARIA', 'AJUSTE')),
    produto_id  INTEGER NOT NULL REFERENCES produtos(id),
    lote_id     INTEGER REFERENCES lotes(id),
    evento_id   INTEGER REFERENCES eventos(id),
    quantidade  INTEGER NOT NULL,
    usuario     TEXT,
    observacao  TEXT
);

CREATE TABLE IF NOT EXISTS vendas (
    id             INTEGER PRIMARY KEY,
    data_hora      TEXT    NOT NULL,
    evento_id      INTEGER NOT NULL REFERENCES eventos(id),
    produto_id     INTEGER NOT NULL REFERENCES produtos(id),
    lote_id        INTEGER REFERENCES lotes(id),
    ponto_venda    TEXT    NOT NULL,
    quantidade     INTEGER NOT NULL CHECK (quantidade > 0),
    preco_unitario REAL    NOT NULL,
    forma_pagamento TEXT
);

CREATE TABLE IF NOT EXISTS usuarios (
    id          INTEGER PRIMARY KEY,
    login       TEXT    NOT NULL UNIQUE,
    nome        TEXT    NOT NULL,
    perfil      TEXT    NOT NULL CHECK (perfil IN ('Administrador', 'Estoque', 'Vendas')),
    senha_hash  TEXT    NOT NULL,
    salt        TEXT    NOT NULL,
    ativo       INTEGER NOT NULL DEFAULT 1,
    criado_em   TEXT    NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_mov_lote   ON movimentacoes(lote_id);
CREATE INDEX IF NOT EXISTS ix_mov_evento ON movimentacoes(evento_id);
CREATE INDEX IF NOT EXISTS ix_vendas_ev  ON vendas(evento_id, produto_id);
CREATE INDEX IF NOT EXISTS ix_vendas_lote ON vendas(evento_id, lote_id);

-- Saldo de cada lote no depósito (vendas acontecem no evento, sobre o que foi enviado).
CREATE VIEW IF NOT EXISTS vw_saldo_lotes AS
SELECT l.id AS lote_id, l.codigo, l.produto_id, p.nome AS produto, p.categoria,
       l.data_entrada, l.data_validade, l.quantidade_inicial, l.custo_unitario,
       COALESCE(SUM(CASE m.tipo
                        WHEN 'ENTRADA'        THEN m.quantidade
                        WHEN 'RETORNO_EVENTO' THEN m.quantidade
                        WHEN 'AJUSTE'         THEN m.quantidade
                        ELSE -m.quantidade END), 0) AS saldo
FROM lotes l
JOIN produtos p ON p.id = l.produto_id
LEFT JOIN movimentacoes m ON m.lote_id = l.id
GROUP BY l.id;

-- Estoque que está no evento: enviado - vendido - devolvido, por lote.
CREATE VIEW IF NOT EXISTS vw_estoque_evento AS
SELECT e.evento_id, e.produto_id, e.lote_id,
       e.enviado - COALESCE(v.vendido, 0) - COALESCE(r.devolvido, 0) AS saldo_evento
FROM (SELECT evento_id, produto_id, lote_id, SUM(quantidade) AS enviado
      FROM movimentacoes WHERE tipo = 'SAIDA_EVENTO' GROUP BY 1, 2, 3) e
LEFT JOIN (SELECT evento_id, lote_id, SUM(quantidade) AS vendido
           FROM vendas GROUP BY 1, 2) v
       ON v.evento_id = e.evento_id AND v.lote_id = e.lote_id
LEFT JOIN (SELECT evento_id, lote_id, SUM(quantidade) AS devolvido
           FROM movimentacoes WHERE tipo = 'RETORNO_EVENTO' GROUP BY 1, 2) r
       ON r.evento_id = e.evento_id AND r.lote_id = e.lote_id;
"""


def conectar() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    con = sqlite3.connect(DB_PATH, check_same_thread=False)
    con.execute("PRAGMA foreign_keys = ON")
    return con


def criar_banco(con: sqlite3.Connection) -> None:
    con.executescript(SCHEMA)
    # Ponto de venda (lanchonete) que recebeu o envio ao evento; coluna adicionada depois da primeira versão.
    if "ponto_venda" not in {c[1] for c in con.execute("PRAGMA table_info(movimentacoes)")}:
        con.execute("ALTER TABLE movimentacoes ADD COLUMN ponto_venda TEXT")
    con.commit()


def consultar(con: sqlite3.Connection, sql: str, params=()) -> pd.DataFrame:
    return pd.read_sql_query(sql, con, params=params)
