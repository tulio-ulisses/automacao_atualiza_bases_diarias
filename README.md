# Automação de atualização das bases diárias

Este repositório atualiza diariamente as bases operacionais no Supabase.

## Execução

A automação roda todos os dias às 02:00, no horário de Brasília (`America/Sao_Paulo`), e também pode ser iniciada manualmente pelo GitHub Actions.

Os scripts são executados obrigatoriamente nesta ordem:

1. `I_contencioso.py`
2. `II_loy.py`
3. `III_historico.py`
4. `IIII_agenda.py`

Se um script falhar, o workflow é interrompido e os scripts seguintes não são executados.

## Destino no Supabase

As cargas substituem integralmente os dados das tabelas correspondentes:

- `contencioso`
- `peticoes_loy`
- `historico`
- `historico_120`
- `agenda`

`historico_120` é derivada pelo próprio script de Histórico.

## GitHub Secrets necessários

O repositório precisa destes Repository Secrets:

- `IILEX_USERNAME`
- `IILEX_PASSWORD`
- `LOY_TOKEN_SERVICOS`
- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`

Nenhuma credencial deve ser gravada diretamente nos arquivos `.py`.

## Preparação do Supabase

Antes da primeira execução, rode no SQL Editor do Supabase o arquivo:

`supabase/preparar_carga_iilex.sql`

Ele cria/atualiza a função utilizada pelos scripts para preparar as tabelas antes do full refresh.
