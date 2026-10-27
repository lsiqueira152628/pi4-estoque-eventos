# Análise de Dados de Estoque e Vendas em Eventos — PI IV (UNIVESP)

Solução para captura, organização e análise de dados de estoque, vendas e validade de produtos
em eventos, com aprendizado de máquina para previsão de demanda e interface web de visualização.

> **Dados simulados.** A base é gerada por `gerar_dados.py` a partir de regras que imitam a operação
> de um espaço de eventos (público, tipo de evento, temperatura, validade dos lotes). Nenhum dado real
> de clientes ou da empresa é utilizado.

## Componentes

| Arquivo | Função |
|---|---|
| `db.py` | Estrutura do banco SQLite (produtos, eventos, lotes, movimentações, vendas, usuários) e views de saldo |
| `gerar_dados.py` | Gera a massa simulada: 115 eventos, 18 produtos, ~1.100 lotes, ~1,4 milhão de transações de venda |
| `ml.py` / `treinar_modelo.py` | Prepara as variáveis, treina e avalia os modelos e salva o melhor em `modelos/` |
| `app.py` | Interface Streamlit: login, painel, registro de vendas, movimentações, estoque e validade, previsão, modelo de ML, cadastros e usuários |
| `auth.py` | Login com senha em hash PBKDF2-SHA256 + salt e perfis de acesso |
| `exportar_powerbi.py` | Exporta o banco e os resultados do ML em CSV (modelo estrela) para o Power BI |

## Como executar (Windows)

```bash
python -m venv C:\venvs\pi4
C:\venvs\pi4\Scripts\pip install -r requirements.txt
C:\venvs\pi4\Scripts\python gerar_dados.py
C:\venvs\pi4\Scripts\python treinar_modelo.py
C:\venvs\pi4\Scripts\streamlit run app.py
```

Use um caminho curto para o ambiente virtual: o Windows limita o tamanho dos caminhos de arquivo.
Se o banco não existir, o `app.py` gera os dados e treina o modelo sozinho na primeira execução.

### Usuários de demonstração

| Login | Senha | Perfil | Acesso |
|---|---|---|---|
| admin | admin123 | Administrador | Todas as páginas e gestão de usuários |
| estoque | estoque123 | Estoque | Painel, movimentações, estoque e validade, previsão, modelo de ML, cadastros |
| vendas | vendas123 | Vendas | Painel e registro de vendas |

Essas são as senhas padrão para rodar localmente. No link público, defina outras em
*Settings > Secrets* do Streamlit Cloud (antes da primeira abertura do app):

```toml
SENHA_ADMIN = "..."
SENHA_ESTOQUE = "..."
SENHA_VENDAS = "..."
```

O botão de retreinar o modelo (página **Modelo de ML**) fica desligado por padrão, para que testes na versão
publicada não alterem as métricas. Para ligá-lo, defina `PERMITIR_RETREINO = "1"` nos Secrets ou como variável
de ambiente. O script `treinar_modelo.py` continua treinando o modelo pela linha de comando.

## Acesso pelo celular

- **Mesma rede Wi-Fi:** com o app rodando no computador, abra no celular o endereço "Network URL" que o
  Streamlit mostra no terminal (ex.: `http://192.168.0.10:8501`).
- **Link público (Streamlit Community Cloud, gratuito):** suba esta pasta para um repositório no GitHub,
  entre em share.streamlit.io, clique em *Create app*, escolha o repositório e o arquivo `app.py`.
  A primeira abertura leva cerca de 2 minutos, porque gera os dados e treina o modelo.

## Aprendizado de máquina

| Elemento | Descrição |
|---|---|
| Tarefa | Regressão supervisionada: prever quantas unidades de cada produto serão vendidas em um evento |
| Dados de entrada | Produto, categoria, tipo de evento, público estimado, duração, temperatura prevista, dia da semana, mês, preço e histórico de consumo do produto (últimos eventos do mesmo tipo e últimos eventos em geral), calculados apenas com eventos anteriores |
| Saída | Unidades previstas por produto e quantidade sugerida de envio (previsão + margem de segurança, arredondada à embalagem) |
| Avaliação | Divisão temporal (83 eventos de treino e 21 mais recentes de teste) e validação cruzada temporal; métricas MAE, RMSE, R² e WAPE, comparadas ao método manual |

Modelos comparados: Regressão Ridge, Random Forest e Gradient Boosting, além de duas referências
(método manual e média histórica). Complemento não supervisionado: agrupamento K-Means dos produtos
pelo perfil de consumo por tipo de evento, avaliado pelo coeficiente silhouette.
Os resultados completos ficam em `modelos/metricas.json` e na página **Modelo de ML** do app.

## Power BI

Rode `python exportar_powerbi.py` e importe os CSVs da pasta `powerbi/` (separador `;`, decimal `,`).
Relacionamentos: `fato_vendas[produto_id]` → `dim_produto[id]`, `fato_vendas[evento_id]` → `dim_evento[id]`,
`fato_vendas[lote_id]` → `dim_lote[id]`, e o mesmo para `fato_movimentacoes`.

Medidas sugeridas (DAX):

```
Faturamento = SUM(fato_vendas[valor])
Unidades vendidas = SUM(fato_vendas[quantidade])
Ticket médio = DIVIDE([Faturamento], COUNTROWS(fato_vendas))
Perdas por validade (R$) = CALCULATE(SUM(fato_movimentacoes[valor_custo]), fato_movimentacoes[tipo] = "PERDA_VALIDADE")
Consumo por 1.000 pessoas = DIVIDE([Unidades vendidas], SUM(dim_evento[publico_real])) * 1000
```
