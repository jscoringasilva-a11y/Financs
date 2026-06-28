# 💰 Finanças Pessoais

Sistema de gestão financeira pessoal — web, responsivo, funciona no PC e no celular.

## Funcionalidades

- 📊 **Dashboard** com saldo, gráficos mensais e últimos lançamentos
- 💳 **Lançamentos** de receitas e despesas com categorias
- 🎯 **Metas** financeiras com barra de progresso
- 📈 **Relatórios** anuais e mensais com gráficos
- 🏷️ **Categorias** personalizáveis com ícones

---

## Como subir no Railway (deploy)

### 1. Crie uma conta no GitHub (se não tiver)
Acesse https://github.com e crie uma conta gratuita.

### 2. Crie um repositório no GitHub
- Clique em **New repository**
- Nome: `financas-pessoais`
- Deixe **Public**
- Clique em **Create repository**

### 3. Faça upload dos arquivos
- Na página do repositório, clique em **uploading an existing file**
- Arraste todos os arquivos desta pasta (incluindo a pasta `templates/`)
- Clique em **Commit changes**

### 4. Conecte ao Railway
- Acesse https://railway.app e faça login
- Clique em **New Project → GitHub Repository**
- Selecione o repositório `financas-pessoais`
- Railway detecta automaticamente o Python e sobe o sistema

### 5. Acesse pelo link
Após o deploy (≈ 2 minutos), Railway gera um link como:
`https://financas-pessoais-production.up.railway.app`

Esse link funciona no PC e no celular!

---

## Rodar localmente (opcional)

```bash
pip install -r requirements.txt
python app.py
```

Acesse: http://localhost:5000
