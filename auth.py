"""Autenticação: senhas guardadas como hash PBKDF2-SHA256 com salt, nunca em texto puro."""
import hashlib
import hmac
import os
import secrets
from datetime import datetime

PERFIS = ["Administrador", "Estoque", "Vendas"]
ITERACOES = 200_000

# Usuários de demonstração criados junto com a massa de dados (ver README). Na nuvem, as senhas vêm
# das variáveis SENHA_ADMIN, SENHA_ESTOQUE e SENHA_VENDAS (Secrets do Streamlit Cloud).
USUARIOS_DEMO = [
    ("admin", "Administrador do sistema", "Administrador", os.environ.get("SENHA_ADMIN", "admin123")),
    ("estoque", "Equipe de estoque", "Estoque", os.environ.get("SENHA_ESTOQUE", "estoque123")),
    ("vendas", "Equipe de vendas", "Vendas", os.environ.get("SENHA_VENDAS", "vendas123")),
]


def gerar_hash(senha: str, salt: str | None = None) -> tuple[str, str]:
    salt = salt or secrets.token_hex(16)
    h = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), bytes.fromhex(salt), ITERACOES)
    return h.hex(), salt


def criar_usuario(con, login: str, nome: str, perfil: str, senha: str) -> None:
    if perfil not in PERFIS:
        raise ValueError(f"Perfil inválido: {perfil}")
    if len(senha) < 6:
        raise ValueError("A senha precisa ter pelo menos 6 caracteres.")
    if con.execute("SELECT 1 FROM usuarios WHERE lower(login) = lower(?)", (login.strip(),)).fetchone():
        raise ValueError("Já existe um usuário com esse login.")
    senha_hash, salt = gerar_hash(senha)
    con.execute("INSERT INTO usuarios (login, nome, perfil, senha_hash, salt, criado_em) VALUES (?, ?, ?, ?, ?, ?)",
                (login.strip(), nome.strip(), perfil, senha_hash, salt,
                 datetime.now().isoformat(sep=" ", timespec="seconds")))
    con.commit()


def autenticar(con, login: str, senha: str) -> dict | None:
    # O login diferencia maiúsculas de minúsculas: "Admin" não entra como "admin".
    row = con.execute("SELECT login, nome, perfil, senha_hash, salt FROM usuarios WHERE login = ? AND ativo = 1",
                      (login.strip(),)).fetchone()
    if row is None:
        gerar_hash(senha)  # mesmo custo de tempo para usuário inexistente
        return None
    calculado, _ = gerar_hash(senha, row[4])
    if not hmac.compare_digest(calculado, row[3]):
        return None
    return {"login": row[0], "nome": row[1], "perfil": row[2]}


def garantir_usuarios_demo(con) -> None:
    """Cria os usuários de demonstração quando a tabela está vazia."""
    if con.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0] == 0:
        for login, nome, perfil, senha in USUARIOS_DEMO:
            criar_usuario(con, login, nome, perfil, senha)
