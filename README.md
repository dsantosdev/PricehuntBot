# 🤖 PricehuntBot — Guia de instalação e uso

Bot do Telegram que monitora grupos em busca de produtos e cria alertas de preço.

---

## 📋 Pré-requisitos

- Python 3.11+
- Conta no Telegram
- Bot criado via [@BotFather](https://t.me/BotFather)

---

## ⚙️ Instalação

```bash
# 1. Clone ou copie os arquivos do bot
mkdir pricehuntbot && cd pricehuntbot
# coloque bot.py aqui

# 2. Crie um ambiente virtual (recomendado)
python -m venv .venv
source .venv/bin/activate        # Linux/Mac
.venv\Scripts\activate           # Windows

# 3. Instale as dependências
pip install "python-telegram-bot==20.7" aiosqlite apscheduler
```

---

## 🔑 Configuração

Abra `bot.py` e edite a linha:

```python
BOT_TOKEN = "SEU_TOKEN_AQUI"
```

Cole o token que o @BotFather te deu.

---

## ▶️ Execução

```bash
python bot.py
```

O banco de dados `pricehunt.db` será criado automaticamente na mesma pasta.

---

## 📱 Como usar o bot

### 1. Início
Envie `/start` ao bot no Telegram para ver o menu principal.

### 2. Busca avulsa
- Clique em **🔍 Buscar produto nos grupos**
- Digite o nome (ou parte) do produto
- Informe o preço máximo (ou `/pular` para sem limite)
- O bot mostrará atalhos para cada grupo onde pode ter o produto

### 3. Criar alerta
- Clique em **🔔 Criar alerta**
- Informe o nome do produto
- Informe o preço máximo (opcional, `/pular` para ignorar)
- Informe por quanto tempo o alerta deve durar:
  - `7d` = 7 dias
  - `12h` = 12 horas
  - `30m` = 30 minutos
  - `/pular` = sem expiração

### 4. Gerenciar alertas
- **📋 Meus alertas** → lista todos os alertas
- Clique em um alerta para ver detalhes
- **✏️ Editar** → altere nome, preço ou expiração
- **🔕 Desativar / ✅ Ativar** → pausa/retoma o monitoramento
- **🗑️ Excluir** → remove o alerta permanentemente

---

## 🏗️ Adicionar o bot aos grupos

Para o bot monitorar mensagens em tempo real, ele **deve ser adicionado** como membro dos grupos que você quer monitorar.

> O bot só consegue "ver" grupos onde ele foi adicionado.

---

## 🔔 Como funciona o alerta automático

Quando alguém envia uma mensagem em um grupo onde o bot está:

1. O bot compara o texto da mensagem com todos os alertas ativos dos membros daquele grupo
2. Se o produto for mencionado (busca parcial, sem maiúsculas/minúsculas):
   - Se houver limite de preço: procura valores numéricos na mensagem e verifica se algum está dentro do limite
   - Se estiver dentro do limite (ou sem limite): envia notificação no privado do usuário com link direto para a mensagem

---

## 🗂️ Estrutura dos arquivos

```
pricehuntbot/
├── bot.py           # código principal
├── pricehunt.db     # banco de dados SQLite (criado automaticamente)
└── README.md        # este arquivo
```

---

## 🚀 Rodar em produção (Linux)

Crie um serviço systemd para o bot rodar em background e reiniciar automaticamente:

```ini
# /etc/systemd/system/pricehuntbot.service
[Unit]
Description=PricehuntBot
After=network.target

[Service]
User=seu_usuario
WorkingDirectory=/caminho/para/pricehuntbot
ExecStart=/caminho/para/pricehuntbot/.venv/bin/python bot.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable pricehuntbot
sudo systemctl start pricehuntbot
sudo systemctl status pricehuntbot
```

---

## ⚠️ Limitações da API do Telegram

- O bot **não consegue ler o histórico** de mensagens anteriores — só monitora mensagens novas após ser adicionado ao grupo
- Para busca histórica seria necessário usar a **API do Telegram para usuários** (MTProto), que requer aprovação especial
- O bot só rastreia grupos onde **ele próprio é membro**

---

## 📬 Suporte

Dúvidas? Abra uma issue ou edite o bot conforme sua necessidade!
