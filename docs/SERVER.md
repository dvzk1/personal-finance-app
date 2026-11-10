# Servidor: uso local, servidor próprio e sincronização agendada

O app é um processo só, `finance/server.py`: serve a tela (o build em `frontend/dist`), a
API que a tela usa e os relatórios. `./start.sh` faz o build quando o frontend mudou e sobe
o servidor em <http://localhost:5273>.

## No próprio computador (padrão)

Escuta só em `127.0.0.1`. Três travas impedem que outro site aberto no navegador leia ou
grave os seus dados:

- **Host**: só `localhost`, `127.0.0.1` e `[::1]`. Barra o DNS rebinding.
- **Origin** em toda gravação: tem de ser a do próprio app.
- **Token da sessão** em toda gravação, no cabeçalho `X-Finance-Token`. A tela o busca em
  `GET /api/session`, que outro site não consegue ler. Muda a cada vez que o servidor sobe.

Um script local que queira gravar faz o mesmo: lê o token e o envia.

```bash
TOKEN=$(curl -s -H 'Host: localhost:5273' http://127.0.0.1:5273/api/session | jq -r .token)
curl -s -X POST -H "X-Finance-Token: $TOKEN" -H 'Content-Type: application/json' \
  http://localhost:5273/api/budget -d @orcamento.json
```

Corpos acima de 1 MiB são recusados, e um erro inesperado devolve só um aviso; o
detalhe fica no terminal do servidor.

### Agente e servidor ao mesmo tempo

A CLI e o agente gravam no mesmo banco que o servidor. O relatório que a tela recebe é
recalculado quando o banco muda por fora (`PRAGMA data_version`) ou quando sai um
`invest.json` novo, então o que o agente aplicou aparece no próximo recarregar. Na
planilha do Google não há como perceber isso, e o relatório vale por 30 segundos.

O dashboard não escreve mais `data/.decisions.json` nem `data/.invest_decisions.json`:
esses arquivos são só do agente.

## Servidor próprio (acesso pelo celular ou outro computador)

Para abrir o app de outro aparelho, rode o servidor numa máquina sempre ligada e acesse
**pela rede privada**: [Tailscale](https://tailscale.com) ou túnel SSH. Não abra porta no
roteador.

```bash
# /etc/finance-control.env (chmod 600), fora do repositório
FINANCE_HOST=100.x.y.z            # IP do Tailscale da máquina (ou 0.0.0.0)
FINANCE_TOKEN=uma-senha-longa-com-16-ou-mais-caracteres
FINANCE_SYNC_SCHEDULE=1
CLIENT_ID=...                     # credenciais da Pluggy, como no .env
CLIENT_SECRET=...
ITEM_IDS=...
```

Com `FINANCE_HOST` definido, o servidor não sobe sem `FINANCE_TOKEN`. Todo acesso, inclusive
à tela, pede a senha (HTTP Basic; o usuário pode ser qualquer um). O navegador pergunta uma
vez e lembra. O Tailscale cifra o tráfego; num túnel SSH, o SSH cifra.

Instalação com systemd (`deploy/finance.service`):

```bash
git clone https://github.com/dvzk1/personal-finance-app /opt/personal-finance-app
cd /opt/personal-finance-app
uv sync --frozen
npm --prefix frontend ci && npm --prefix frontend run build
uv run python -m finance.seed          # ou importe: finance.backup import arquivo.db
sudo cp deploy/finance.service /etc/systemd/system/
sudo systemctl enable --now finance
```

Depois de atualizar o código, refaça o `uv sync`, o build e `sudo systemctl restart finance`.

### Cópia cifrada fora da máquina

`deploy/offsite-backup.sh` exporta o banco, cifra com a chave **pública** do
[age](https://age-encryption.org) e grava numa pasta de destino (disco externo, pasta
sincronizada ou remoto montado com rclone), mantendo as 30 mais novas. O servidor grava as
cópias, mas não consegue abrir nenhuma: a chave privada fica só com você.

```bash
age-keygen -o chave.txt                 # no SEU computador; guarde chave.txt fora do servidor
# no /etc/finance-control.env:
AGE_RECIPIENT=age1...                   # a linha "public key" do age-keygen
FINANCE_OFFSITE_DIR=/mnt/backup/finance

sudo cp deploy/finance-offsite.* /etc/systemd/system/
sudo systemctl enable --now finance-offsite.timer    # todo dia às 4h
```

Restaurar: `age -d -i chave.txt finance-AAAAMMDD-HHMMSS.db.age > finance.db` e
`uv run python -m finance.backup import finance.db`.

## Sincronização agendada

Com `FINANCE_SYNC_SCHEDULE=1`, o servidor faz sozinho o que `./start.sh --sync` faz: puxa as
transações da Pluggy, aplica as regras, atualiza os saldos da carteira e regera os
relatórios. A primeira rodada é ao subir.

No Meu Pluggy cada conexão se atualiza a cada 24 horas. O servidor pergunta à Pluggy quando
cada uma vai se atualizar (`nextAutoSyncAt`) e roda 15 minutos depois da primeira. Se a
Pluggy não informar, roda a cada `FINANCE_SYNC_HOURS` horas (6). Nunca roda duas vezes em
menos de 15 minutos. Ler da Pluggy não gasta a cota do Open Finance: ela entrega o que já
buscou.

- `GET /api/sync/status`: rodando ou não, início e fim da última, se deu certo, o que
  entrou e quando é a próxima.
- `POST /api/sync/run`: roda agora (com o token da sessão, como toda gravação).

Durante uma rodada, que leva de segundos a um minuto, uma gravação feita pela tela espera
ela terminar: a sincronização reescreve o Ledger, e as duas juntas perderiam uma delas.

O que precisa de decisão (lançamento sem categoria, aporte sem destino) continua na fila
do `finance.show queue` e na aba Revisar.
