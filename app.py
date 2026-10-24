from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date, timedelta
import json
import hmac
import unicodedata
from types import SimpleNamespace
from collections import defaultdict
import calendar

app = Flask(__name__)
import os

# Usa o PostgreSQL do Railway (DATABASE_URL); sem ele, cai no SQLite local.
_db_url = os.environ.get('DATABASE_URL', 'sqlite:///financa.db')
# Força o driver psycopg2 (o SQLAlchemy novo usaria outro driver por padrão)
for _prefixo in ('postgres://', 'postgresql://'):
    if _db_url.startswith(_prefixo):
        _db_url = 'postgresql+psycopg2://' + _db_url[len(_prefixo):]
        break
app.config['SQLALCHEMY_DATABASE_URI'] = _db_url
app.config['SQLALCHEMY_ENGINE_OPTIONS'] = {'pool_pre_ping': True}
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'financa-pessoal-secret-2024'

db = SQLAlchemy(app)

@app.template_filter('brl_num')
def brl_num(v):
    """Formata número no padrão brasileiro: 1.234,56"""
    return f"{(v or 0):,.2f}".replace(',', '_').replace('.', ',').replace('_', '.')

# ─── MODELS ───────────────────────────────────────────────────────────────────

class Categoria(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'receita' ou 'despesa'
    icone = db.Column(db.String(10), default='💰')
    cor = db.Column(db.String(20), default='#6366f1')
    lancamentos = db.relationship('Lancamento', backref='categoria', lazy=True)

class Conta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(20), nullable=False)  # 'corrente' ou 'poupanca'
    icone = db.Column(db.String(10), default='🏦')
    cor = db.Column(db.String(20), default='#4f8ef7')
    lancamentos = db.relationship('Lancamento', backref='conta', lazy=True)

class Lancamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'receita' ou 'despesa'
    data = db.Column(db.Date, nullable=False, default=date.today)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'), nullable=True)
    conta_id = db.Column(db.Integer, db.ForeignKey('conta.id'), nullable=True)
    observacao = db.Column(db.String(500))
    criado_em = db.Column(db.DateTime, default=datetime.utcnow)

class Meta(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    valor_alvo = db.Column(db.Float, nullable=False)
    valor_atual = db.Column(db.Float, default=0)
    prazo = db.Column(db.Date)
    icone = db.Column(db.String(10), default='🎯')
    ativa = db.Column(db.Boolean, default=True)
    criada_em = db.Column(db.DateTime, default=datetime.utcnow)

GRUPOS_PATRIMONIO = {
    'ativo': [
        ('Dinheiro e contas', '🏦'),
        ('Investimentos', '📈'),
        ('Imóveis', '🏠'),
        ('Veículos', '🚗'),
        ('Outros bens', '📦'),
    ],
    'passivo': [
        ('Financiamentos', '🏗️'),
        ('Empréstimos', '💸'),
        ('Cartão de crédito', '💳'),
        ('Outras dívidas', '🧾'),
    ],
}

class Patrimonio(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(200), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'ativo' ou 'passivo'
    grupo = db.Column(db.String(50), nullable=False)
    valor = db.Column(db.Float, nullable=False, default=0)
    atualizado_em = db.Column(db.DateTime, default=datetime.utcnow)

MESES_PT = ['Jan', 'Fev', 'Mar', 'Abr', 'Mai', 'Jun', 'Jul', 'Ago', 'Set', 'Out', 'Nov', 'Dez']

class Fechamento(db.Model):
    """Fotografia mensal: resultado do mês + ativos/passivos no momento do fechamento."""
    id = db.Column(db.Integer, primary_key=True)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)
    receitas = db.Column(db.Float)          # vazio quando o mês foi informado manualmente
    despesas = db.Column(db.Float)
    saldo_contas = db.Column(db.Float)
    total_ativos = db.Column(db.Float, nullable=False, default=0)
    total_passivos = db.Column(db.Float, nullable=False, default=0)
    manual = db.Column(db.Boolean, default=False)
    detalhes = db.Column(db.Text)           # JSON com cada item do patrimônio na data
    fechado_em = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('ano', 'mes', name='uq_fechamento_ano_mes'),)

    @property
    def patrimonio_liquido(self):
        return (self.total_ativos or 0) - (self.total_passivos or 0)

    @property
    def resultado(self):
        if self.receitas is None or self.despesas is None:
            return None
        return self.receitas - self.despesas

    @property
    def periodo(self):
        return f"{MESES_PT[self.mes - 1]}/{self.ano}"

    @property
    def chave(self):
        return f"{self.ano}-{self.mes:02d}"

# ─── SEED DATA ────────────────────────────────────────────────────────────────

def seed_categorias():
    if Categoria.query.count() == 0:
        categorias = [
            # Receitas
            Categoria(nome='Salário', tipo='receita', icone='💼', cor='#10b981'),
            Categoria(nome='Freelance', tipo='receita', icone='💻', cor='#06b6d4'),
            Categoria(nome='Investimentos', tipo='receita', icone='📈', cor='#8b5cf6'),
            Categoria(nome='Outros (Receita)', tipo='receita', icone='➕', cor='#3b82f6'),
            # Despesas
            Categoria(nome='Moradia', tipo='despesa', icone='🏠', cor='#f59e0b'),
            Categoria(nome='Alimentação', tipo='despesa', icone='🍽️', cor='#ef4444'),
            Categoria(nome='Transporte', tipo='despesa', icone='🚗', cor='#f97316'),
            Categoria(nome='Saúde', tipo='despesa', icone='🏥', cor='#ec4899'),
            Categoria(nome='Educação', tipo='despesa', icone='📚', cor='#6366f1'),
            Categoria(nome='Lazer', tipo='despesa', icone='🎮', cor='#14b8a6'),
            Categoria(nome='Roupas', tipo='despesa', icone='👕', cor='#a855f7'),
            Categoria(nome='Outros (Despesa)', tipo='despesa', icone='➖', cor='#64748b'),
        ]
        db.session.add_all(categorias)
        db.session.commit()

# ─── HELPERS ──────────────────────────────────────────────────────────────────

def get_resumo_mes(ano, mes):
    inicio = date(ano, mes, 1)
    ultimo_dia = calendar.monthrange(ano, mes)[1]
    fim = date(ano, mes, ultimo_dia)

    lancamentos = Lancamento.query.filter(
        Lancamento.data >= inicio,
        Lancamento.data <= fim
    ).all()

    receitas = sum(l.valor for l in lancamentos if l.tipo == 'receita')
    despesas = sum(l.valor for l in lancamentos if l.tipo == 'despesa')
    saldo = receitas - despesas

    return {'receitas': receitas, 'despesas': despesas, 'saldo': saldo, 'lancamentos': lancamentos}

# ─── ROUTES ───────────────────────────────────────────────────────────────────

@app.route('/')
def index():
    hoje = date.today()
    resumo = get_resumo_mes(hoje.year, hoje.month)

    # Saldo total histórico
    total_receitas = db.session.query(db.func.sum(Lancamento.valor)).filter_by(tipo='receita').scalar() or 0
    total_despesas = db.session.query(db.func.sum(Lancamento.valor)).filter_by(tipo='despesa').scalar() or 0
    saldo_total = total_receitas - total_despesas

    # Últimos lançamentos
    ultimos = Lancamento.query.order_by(Lancamento.data.desc(), Lancamento.criado_em.desc()).limit(8).all()

    # Metas ativas
    metas = Meta.query.filter_by(ativa=True).all()

    # Gráfico mensal (últimos 6 meses)
    meses_labels = []
    meses_receitas = []
    meses_despesas = []
    for i in range(5, -1, -1):
        m = hoje.month - i
        a = hoje.year
        while m <= 0:
            m += 12
            a -= 1
        r = get_resumo_mes(a, m)
        meses_labels.append(f"{calendar.month_abbr[m]}/{str(a)[2:]}")
        meses_receitas.append(round(r['receitas'], 2))
        meses_despesas.append(round(r['despesas'], 2))

    # Gastos por categoria no mês
    cat_data = defaultdict(float)
    for l in resumo['lancamentos']:
        if l.tipo == 'despesa':
            nome = l.categoria.nome if l.categoria else 'Sem categoria'
            cat_data[nome] += l.valor
    cat_labels = list(cat_data.keys())
    cat_valores = [round(v, 2) for v in cat_data.values()]

    return render_template('index.html',
        resumo=resumo,
        saldo_total=saldo_total,
        ultimos=ultimos,
        metas=metas,
        meses_labels=json.dumps(meses_labels),
        meses_receitas=json.dumps(meses_receitas),
        meses_despesas=json.dumps(meses_despesas),
        cat_labels=json.dumps(cat_labels),
        cat_valores=json.dumps(cat_valores),
        hoje=hoje
    )

@app.route('/lancamentos')
def lancamentos():
    page = request.args.get('page', 1, type=int)
    tipo = request.args.get('tipo', '')
    categoria_id = request.args.get('categoria_id', '')
    mes = request.args.get('mes', '')

    query = Lancamento.query
    if tipo:
        query = query.filter_by(tipo=tipo)
    if categoria_id:
        query = query.filter_by(categoria_id=categoria_id)
    if mes:
        ano, m = mes.split('-')
        inicio = date(int(ano), int(m), 1)
        ultimo_dia = calendar.monthrange(int(ano), int(m))[1]
        fim = date(int(ano), int(m), ultimo_dia)
        query = query.filter(Lancamento.data >= inicio, Lancamento.data <= fim)

    lancamentos = query.order_by(Lancamento.data.desc(), Lancamento.criado_em.desc()).paginate(page=page, per_page=20)
    categorias = Categoria.query.order_by(Categoria.tipo, Categoria.nome).all()
    contas = Conta.query.order_by(Conta.id).all()

    return render_template('lancamentos.html',
        lancamentos=lancamentos,
        categorias=categorias,
        contas=contas,
        filtro_tipo=tipo,
        filtro_categoria=categoria_id,
        filtro_mes=mes
    )

@app.route('/lancamento/novo', methods=['POST'])
def novo_lancamento():
    data = request.form
    l = Lancamento(
        descricao=data['descricao'],
        valor=float(data['valor']),
        tipo=data['tipo'],
        data=datetime.strptime(data['data'], '%Y-%m-%d').date(),
        categoria_id=int(data['categoria_id']) if data.get('categoria_id') else None,
        conta_id=int(data['conta_id']) if data.get('conta_id') else None,
        observacao=data.get('observacao', '')
    )
    db.session.add(l)
    db.session.commit()
    return redirect(request.referrer or url_for('index'))

@app.route('/lancamento/editar/<int:id>', methods=['POST'])
def editar_lancamento(id):
    l = Lancamento.query.get_or_404(id)
    data = request.form
    l.descricao = data['descricao']
    l.valor = float(data['valor'])
    l.tipo = data['tipo']
    l.data = datetime.strptime(data['data'], '%Y-%m-%d').date()
    l.categoria_id = int(data['categoria_id']) if data.get('categoria_id') else None
    l.conta_id = int(data['conta_id']) if data.get('conta_id') else None
    l.observacao = data.get('observacao', '')
    db.session.commit()
    return redirect(request.referrer or url_for('lancamentos'))

@app.route('/lancamento/excluir/<int:id>', methods=['POST'])
def excluir_lancamento(id):
    l = Lancamento.query.get_or_404(id)
    db.session.delete(l)
    db.session.commit()
    return redirect(request.referrer or url_for('lancamentos'))

@app.route('/metas')
def metas():
    metas_ativas = Meta.query.filter_by(ativa=True).order_by(Meta.criada_em.desc()).all()
    metas_concluidas = Meta.query.filter_by(ativa=False).order_by(Meta.criada_em.desc()).all()
    return render_template('metas.html', metas_ativas=metas_ativas, metas_concluidas=metas_concluidas)

@app.route('/meta/nova', methods=['POST'])
def nova_meta():
    data = request.form
    m = Meta(
        nome=data['nome'],
        valor_alvo=float(data['valor_alvo']),
        valor_atual=float(data.get('valor_atual', 0)),
        prazo=datetime.strptime(data['prazo'], '%Y-%m-%d').date() if data.get('prazo') else None,
        icone=data.get('icone', '🎯')
    )
    db.session.add(m)
    db.session.commit()
    return redirect(url_for('metas'))

@app.route('/meta/atualizar/<int:id>', methods=['POST'])
def atualizar_meta(id):
    m = Meta.query.get_or_404(id)
    m.valor_atual = float(request.form['valor_atual'])
    if m.valor_atual >= m.valor_alvo:
        m.ativa = False
    db.session.commit()
    return redirect(url_for('metas'))

@app.route('/meta/excluir/<int:id>', methods=['POST'])
def excluir_meta(id):
    m = Meta.query.get_or_404(id)
    db.session.delete(m)
    db.session.commit()
    return redirect(url_for('metas'))

@app.route('/categorias')
def categorias():
    cats = Categoria.query.order_by(Categoria.tipo, Categoria.nome).all()
    return render_template('categorias.html', categorias=cats)

@app.route('/categoria/nova', methods=['POST'])
def nova_categoria():
    data = request.form
    c = Categoria(
        nome=data['nome'],
        tipo=data['tipo'],
        icone=data.get('icone', '💰'),
        cor=data.get('cor', '#6366f1')
    )
    db.session.add(c)
    db.session.commit()
    return redirect(url_for('categorias'))

@app.route('/categoria/excluir/<int:id>', methods=['POST'])
def excluir_categoria(id):
    c = Categoria.query.get_or_404(id)
    db.session.delete(c)
    db.session.commit()
    return redirect(url_for('categorias'))

@app.route('/relatorios')
def relatorios():
    hoje = date.today()
    ano = request.args.get('ano', hoje.year, type=int)

    # Resumo por mês do ano
    meses_data = []
    for m in range(1, 13):
        r = get_resumo_mes(ano, m)
        meses_data.append({
            'mes': calendar.month_name[m],
            'mes_abr': calendar.month_abbr[m],
            'receitas': r['receitas'],
            'despesas': r['despesas'],
            'saldo': r['saldo']
        })

    # Totais do ano
    total_rec = sum(m['receitas'] for m in meses_data)
    total_desp = sum(m['despesas'] for m in meses_data)

    # Por categoria (ano todo)
    inicio_ano = date(ano, 1, 1)
    fim_ano = date(ano, 12, 31)
    lancamentos_ano = Lancamento.query.filter(
        Lancamento.data >= inicio_ano,
        Lancamento.data <= fim_ano
    ).all()

    cat_desp = defaultdict(float)
    cat_rec = defaultdict(float)
    for l in lancamentos_ano:
        nome = l.categoria.nome if l.categoria else 'Sem categoria'
        if l.tipo == 'despesa':
            cat_desp[nome] += l.valor
        else:
            cat_rec[nome] += l.valor

    # extract('year', ...) funciona tanto no SQLite quanto no PostgreSQL
    anos_disponiveis = db.session.query(
        db.extract('year', Lancamento.data)
    ).distinct().all()
    anos_disponiveis = sorted([int(a[0]) for a in anos_disponiveis if a[0]], reverse=True)
    if not anos_disponiveis:
        anos_disponiveis = [hoje.year]

    return render_template('relatorios.html',
        meses_data=meses_data,
        total_rec=total_rec,
        total_desp=total_desp,
        ano=ano,
        anos_disponiveis=anos_disponiveis,
        cat_desp=dict(sorted(cat_desp.items(), key=lambda x: x[1], reverse=True)),
        cat_rec=dict(sorted(cat_rec.items(), key=lambda x: x[1], reverse=True)),
        meses_labels=json.dumps([m['mes_abr'] for m in meses_data]),
        meses_rec_json=json.dumps([round(m['receitas'], 2) for m in meses_data]),
        meses_desp_json=json.dumps([round(m['despesas'], 2) for m in meses_data]),
    )

# ─── API ──────────────────────────────────────────────────────────────────────

@app.route('/api/categorias/<tipo>')
def api_categorias(tipo):
    cats = Categoria.query.filter_by(tipo=tipo).order_by(Categoria.nome).all()
    return jsonify([{'id': c.id, 'nome': c.nome, 'icone': c.icone} for c in cats])

# ─── INIT ─────────────────────────────────────────────────────────────────────

def seed_contas():
    if Conta.query.count() == 0:
        contas = [
            Conta(nome='Conta Corrente', tipo='corrente', icone='💳', cor='#4f8ef7'),
            Conta(nome='Poupança', tipo='poupanca', icone='🐷', cor='#10d98a'),
        ]
        db.session.add_all(contas)
        db.session.commit()

@app.route('/contas')
def contas():
    todas = Conta.query.all()
    resumos = []
    for c in todas:
        rec = sum(l.valor for l in c.lancamentos if l.tipo == 'receita')
        desp = sum(l.valor for l in c.lancamentos if l.tipo == 'despesa')
        resumos.append({'conta': c, 'saldo': rec - desp, 'receitas': rec, 'despesas': desp})
    hoje = date.today()
    return render_template('contas.html', resumos=resumos, hoje_str=hoje.strftime('%Y-%m-%d'))

@app.route('/conta/transferir', methods=['POST'])
def transferir():
    origem_id = int(request.form['origem_id'])
    destino_id = int(request.form['destino_id'])
    valor = float(request.form['valor'])
    descricao = request.form.get('descricao', 'Transferência')
    data_str = request.form.get('data', date.today().strftime('%Y-%m-%d'))
    data_transf = datetime.strptime(data_str, '%Y-%m-%d').date()

    # Saída da conta origem
    saida = Lancamento(
        descricao=f'Transferência → {Conta.query.get(destino_id).nome}',
        valor=valor, tipo='despesa', data=data_transf, conta_id=origem_id
    )
    # Entrada na conta destino
    entrada = Lancamento(
        descricao=f'Transferência ← {Conta.query.get(origem_id).nome}',
        valor=valor, tipo='receita', data=data_transf, conta_id=destino_id
    )
    db.session.add_all([saida, entrada])
    db.session.commit()
    return redirect(url_for('contas'))

def _nome_conta_repetido(nome, ignorar_id=None):
    alvo = _norm(nome)
    return any(_norm(c.nome) == alvo and c.id != ignorar_id for c in Conta.query.all())

@app.route('/conta/nova', methods=['POST'])
def nova_conta():
    nome = (request.form.get('nome') or '').strip()[:100]
    tipo = request.form.get('tipo')
    if not nome or tipo not in ('corrente', 'poupanca'):
        return redirect(url_for('contas', erro='dados'))
    if _nome_conta_repetido(nome):
        return redirect(url_for('contas', erro='repetido'))
    padrao = '🐷' if tipo == 'poupanca' else '💳'
    icone = (request.form.get('icone') or padrao)[:10]
    db.session.add(Conta(
        nome=nome, tipo=tipo, icone=icone,
        cor='#10d98a' if tipo == 'poupanca' else '#4f8ef7'))
    db.session.commit()
    return redirect(url_for('contas'))

@app.route('/conta/editar/<int:id>', methods=['POST'])
def editar_conta(id):
    c = Conta.query.get_or_404(id)
    nome = (request.form.get('nome') or '').strip()[:100]
    if not nome:
        return redirect(url_for('contas', erro='dados'))
    if _nome_conta_repetido(nome, ignorar_id=c.id):
        return redirect(url_for('contas', erro='repetido'))
    c.nome = nome
    if request.form.get('icone'):
        c.icone = request.form['icone'][:10]
    db.session.commit()
    return redirect(url_for('contas'))

@app.route('/api/contas')
def api_contas():
    contas = Conta.query.all()
    return jsonify([{'id': c.id, 'nome': c.nome, 'icone': c.icone} for c in contas])

def _saldo_conta(conta, ate=None):
    """Saldo da conta somando os lançamentos (até uma data, se informada) — mesma lógica da tela Contas."""
    total = 0
    for l in conta.lancamentos:
        if ate is not None and l.data > ate:
            continue
        total += l.valor if l.tipo == 'receita' else -l.valor
    return total

def _itens_patrimonio(ate=None):
    """Lista os itens do patrimônio já com o valor a usar (valor_exib). Nada é gravado no banco.
    - Item de ativo do grupo 'Dinheiro e contas' com o MESMO NOME de uma conta usa o saldo dela.
    - Toda conta (Conta Corrente, Poupança...) que não tenha item correspondente aparece sozinha nesse grupo."""
    reais = Patrimonio.query.order_by(Patrimonio.nome).all()
    todas = Conta.query.order_by(Conta.id).all()
    por_nome = {_norm(c.nome): c for c in todas}
    lista = []
    ligadas = set()
    for i in reais:
        conta = por_nome.get(_norm(i.nome)) if (i.tipo == 'ativo' and i.grupo == 'Dinheiro e contas') else None
        if conta is not None:
            ligadas.add(conta.id)
            valor, auto = _saldo_conta(conta, ate), True
        else:
            valor, auto = i.valor, False
        lista.append(SimpleNamespace(
            id=i.id, nome=i.nome, tipo=i.tipo, grupo=i.grupo, valor=i.valor,
            valor_exib=valor, auto=auto, fixo=False, atualizado_em=i.atualizado_em))
    for c in todas:
        if c.id not in ligadas:
            lista.append(SimpleNamespace(
                id=f'conta-{c.id}', nome=c.nome, tipo='ativo', grupo='Dinheiro e contas', valor=0,
                valor_exib=_saldo_conta(c, ate), auto=True, fixo=True, atualizado_em=None))
    lista.sort(key=lambda x: _norm(x.nome))
    return lista

@app.route('/patrimonio')
def patrimonio():
    itens = _itens_patrimonio()
    blocos = {}
    for tipo, grupos in GRUPOS_PATRIMONIO.items():
        blocos[tipo] = []
        for nome_grupo, icone in grupos:
            lista = [i for i in itens if i.tipo == tipo and i.grupo == nome_grupo]
            if lista:
                blocos[tipo].append({
                    'nome': nome_grupo,
                    'icone': icone,
                    'itens': lista,
                    'total': sum(i.valor_exib for i in lista),
                })
    total_ativos = sum(i.valor_exib for i in itens if i.tipo == 'ativo')
    total_passivos = sum(i.valor_exib for i in itens if i.tipo == 'passivo')
    return render_template('patrimonio.html',
        blocos=blocos,
        total_ativos=total_ativos,
        total_passivos=total_passivos,
        patrimonio_liquido=total_ativos - total_passivos,
        grupos=GRUPOS_PATRIMONIO)

def _grupo_valido(tipo, grupo):
    return tipo in GRUPOS_PATRIMONIO and grupo in [g for g, _ in GRUPOS_PATRIMONIO[tipo]]

@app.route('/patrimonio/novo', methods=['POST'])
def novo_patrimonio():
    d = request.form
    if _grupo_valido(d.get('tipo'), d.get('grupo')):
        db.session.add(Patrimonio(
            nome=d['nome'].strip(),
            tipo=d['tipo'],
            grupo=d['grupo'],
            valor=abs(float(d.get('valor') or 0)),
        ))
        db.session.commit()
    return redirect(url_for('patrimonio'))

@app.route('/patrimonio/editar/<int:id>', methods=['POST'])
def editar_patrimonio(id):
    p = Patrimonio.query.get_or_404(id)
    d = request.form
    p.nome = d['nome'].strip()
    if _grupo_valido(p.tipo, d.get('grupo')):
        p.grupo = d['grupo']
    p.valor = abs(float(d.get('valor') or 0))
    p.atualizado_em = datetime.utcnow()
    db.session.commit()
    return redirect(url_for('patrimonio'))

@app.route('/patrimonio/excluir/<int:id>', methods=['POST'])
def excluir_patrimonio(id):
    p = Patrimonio.query.get_or_404(id)
    db.session.delete(p)
    db.session.commit()
    return redirect(url_for('patrimonio'))

# ─── FECHAMENTO MENSAL ────────────────────────────────────────────────────────

def _parse_mes(texto):
    """Converte 'AAAA-MM' em (ano, mes). Retorna None se for inválido."""
    try:
        ano, mes = texto.split('-')
        ano, mes = int(ano), int(mes)
        if 2000 <= ano <= 2100 and 1 <= mes <= 12:
            return ano, mes
    except (AttributeError, ValueError):
        pass
    return None

def _eh_transferencia(l):
    # Transferências entre contas não são receita nem despesa de verdade
    return (l.descricao or '').startswith(('Transferência →', 'Transferência ←'))

def _calcular_fechamento(ano, mes):
    inicio = date(ano, mes, 1)
    fim = date(ano, mes, calendar.monthrange(ano, mes)[1])

    do_mes = Lancamento.query.filter(Lancamento.data >= inicio, Lancamento.data <= fim).all()
    receitas = sum(l.valor for l in do_mes if l.tipo == 'receita' and not _eh_transferencia(l))
    despesas = sum(l.valor for l in do_mes if l.tipo == 'despesa' and not _eh_transferencia(l))

    # Saldo das contas até o último dia do mês (mesma lógica da tela Contas)
    ate_fim = Lancamento.query.filter(Lancamento.conta_id.isnot(None), Lancamento.data <= fim).all()
    saldo_contas = sum(l.valor if l.tipo == 'receita' else -l.valor for l in ate_fim)

    # Poupança (e itens ligados a uma conta) entram com o saldo da conta até o fim do mês fechado
    itens = _itens_patrimonio(ate=fim)
    total_ativos = sum(i.valor_exib for i in itens if i.tipo == 'ativo')
    total_passivos = sum(i.valor_exib for i in itens if i.tipo == 'passivo')
    detalhes = [
        {'id': i.id, 'nome': i.nome, 'tipo': i.tipo, 'grupo': i.grupo, 'valor': i.valor_exib}
        for i in itens
    ]
    return {
        'receitas': receitas,
        'despesas': despesas,
        'saldo_contas': saldo_contas,
        'total_ativos': total_ativos,
        'total_passivos': total_passivos,
        'detalhes': json.dumps(detalhes),
    }

@app.route('/fechamento')
def fechamento():
    hoje = date.today()
    regs = Fechamento.query.order_by(Fechamento.ano, Fechamento.mes).all()

    linhas = []
    anterior = None
    for f in regs:
        var = pct = outros = None
        if anterior is not None:
            var = f.patrimonio_liquido - anterior.patrimonio_liquido
            if anterior.patrimonio_liquido:
                pct = var / abs(anterior.patrimonio_liquido) * 100
            if f.resultado is not None:
                outros = var - f.resultado
        linhas.append({'f': f, 'var': var, 'pct': pct, 'outros': outros})
        anterior = f

    # Detalhe de um mês (item a item, comparado com o fechamento anterior)
    detalhe = None
    ver = request.args.get('ver', type=int)
    if ver:
        for i, ln in enumerate(linhas):
            if ln['f'].id != ver:
                continue
            atual = json.loads(ln['f'].detalhes or '[]')
            ant_f = linhas[i - 1]['f'] if i > 0 else None
            ant_lista = json.loads(ant_f.detalhes or '[]') if ant_f else []
            tem_ant = bool(ant_lista)
            mapa_ant = {x['id']: x for x in ant_lista}
            mapa_atual = {x['id']: x for x in atual}
            itens = []
            for x in atual:
                p = mapa_ant.get(x['id'])
                itens.append({
                    'nome': x['nome'], 'tipo': x['tipo'], 'grupo': x['grupo'],
                    'atual': x['valor'],
                    'anterior': p['valor'] if p else None,
                    'novo': tem_ant and p is None,
                })
            for x in ant_lista:
                if x['id'] not in mapa_atual:
                    itens.append({
                        'nome': x['nome'], 'tipo': x['tipo'], 'grupo': x['grupo'],
                        'atual': None, 'anterior': x['valor'], 'novo': False,
                    })
            for it in itens:
                it['var'] = (it['atual'] or 0) - (it['anterior'] or 0) if tem_ant else None
                if it['var'] is None:
                    it['impacto'] = None
                else:
                    it['impacto'] = it['var'] if it['tipo'] == 'ativo' else -it['var']
            itens.sort(key=lambda it: (0 if it['tipo'] == 'ativo' else 1, it['grupo'], it['nome']))
            detalhe = {'linha': ln, 'itens': itens, 'tem_ant': tem_ant, 'ant': ant_f}
            break

    # Mês sugerido para fechar: o mês anterior ao atual
    m_ant, a_ant = hoje.month - 1, hoje.year
    if m_ant == 0:
        m_ant, a_ant = 12, a_ant - 1
    mes_sugerido = f"{a_ant}-{m_ant:02d}"

    return render_template('fechamento.html',
        linhas=linhas,
        linhas_desc=list(reversed(linhas)),
        detalhe=detalhe,
        mes_sugerido=mes_sugerido,
        mes_max=hoje.strftime('%Y-%m'),
        graf_labels=json.dumps([ln['f'].periodo for ln in linhas]),
        graf_pl=json.dumps([round(ln['f'].patrimonio_liquido, 2) for ln in linhas]),
        graf_ativos=json.dumps([round(ln['f'].total_ativos or 0, 2) for ln in linhas]),
        graf_passivos=json.dumps([round(ln['f'].total_passivos or 0, 2) for ln in linhas]),
    )

@app.route('/fechamento/fechar', methods=['POST'])
def fechar_mes():
    alvo = _parse_mes(request.form.get('mes', ''))
    hoje = date.today()
    if alvo and (alvo[0], alvo[1]) <= (hoje.year, hoje.month):
        ano, mes = alvo
        dados = _calcular_fechamento(ano, mes)
        f = Fechamento.query.filter_by(ano=ano, mes=mes).first()
        if f is None:
            f = Fechamento(ano=ano, mes=mes)
            db.session.add(f)
        f.receitas = dados['receitas']
        f.despesas = dados['despesas']
        f.saldo_contas = dados['saldo_contas']
        f.total_ativos = dados['total_ativos']
        f.total_passivos = dados['total_passivos']
        f.detalhes = dados['detalhes']
        f.manual = False
        f.fechado_em = datetime.utcnow()
        db.session.commit()
        return redirect(url_for('fechamento', ver=f.id))
    return redirect(url_for('fechamento'))

@app.route('/fechamento/manual', methods=['POST'])
def fechamento_manual():
    """Para informar à mão o patrimônio de um mês que já passou (ex.: o mês anterior ao primeiro fechamento)."""
    alvo = _parse_mes(request.form.get('mes', ''))
    if alvo:
        ano, mes = alvo
        ativos = abs(float(request.form.get('total_ativos') or 0))
        passivos = abs(float(request.form.get('total_passivos') or 0))
        f = Fechamento.query.filter_by(ano=ano, mes=mes).first()
        if f is None:
            db.session.add(Fechamento(
                ano=ano, mes=mes, total_ativos=ativos, total_passivos=passivos, manual=True
            ))
            db.session.commit()
        elif f.manual:
            f.total_ativos = ativos
            f.total_passivos = passivos
            f.fechado_em = datetime.utcnow()
            db.session.commit()
        # Mês já fechado pelo app: não sobrescreve
    return redirect(url_for('fechamento'))

@app.route('/fechamento/excluir/<int:id>', methods=['POST'])
def excluir_fechamento(id):
    # Remove só a fotografia do fechamento; lançamentos e patrimônio não são afetados
    f = Fechamento.query.get_or_404(id)
    db.session.delete(f)
    db.session.commit()
    return redirect(url_for('fechamento'))

# ─── CONECTOR COM O CLAUDE (MCP) ──────────────────────────────────────────────
# Só funciona se existir a variável MCP_TOKEN no Railway (mínimo 24 caracteres).
# O endereço do conector é  /mcp/<MCP_TOKEN>  — o token funciona como chave de acesso.
# Não existe ferramenta de apagar nem de editar: o Claude só consulta e lança.

MCP_VERSOES = ['2025-06-18', '2025-03-26', '2024-11-05']

def _hoje_brasil():
    # O servidor roda em UTC; horário de Brasília = UTC-3
    return (datetime.utcnow() - timedelta(hours=3)).date()

def _norm(txt):
    t = unicodedata.normalize('NFKD', str(txt or '')).encode('ascii', 'ignore').decode()
    return t.strip().casefold()

def _parse_valor(v):
    if isinstance(v, (int, float)):
        return float(v)
    s = str(v or '').replace('R$', '').replace(' ', '').strip()
    if ',' in s:
        s = s.replace('.', '').replace(',', '.')
    return float(s)

def _achar_por_nome(itens, nome):
    alvo = _norm(nome)
    exatos = [i for i in itens if _norm(i.nome) == alvo]
    if exatos:
        return exatos[0]
    parciais = [i for i in itens if alvo and (alvo in _norm(i.nome) or _norm(i.nome) in alvo)]
    return parciais[0] if len(parciais) == 1 else None

MCP_TOOLS = [
    {
        'name': 'listar_categorias_e_contas',
        'description': 'Lista as contas e as categorias (de receita e de despesa) cadastradas no app de finanças. Use antes de lançar, para escolher categoria e conta com os nomes certos.',
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'lancar',
        'description': 'Registra uma receita ou uma despesa no app de finanças. Confirme com o usuário descrição, valor, tipo, data e conta antes de chamar. O valor é sempre positivo; o tipo diz se é receita ou despesa.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'descricao': {'type': 'string', 'description': 'Ex.: Supermercado'},
                'valor': {'type': 'number', 'description': 'Valor em reais, positivo. Ex.: 80.50'},
                'tipo': {'type': 'string', 'enum': ['receita', 'despesa']},
                'data': {'type': 'string', 'description': 'AAAA-MM-DD. Se omitida, usa a data de hoje (Brasília).'},
                'categoria': {'type': 'string', 'description': 'Nome de uma categoria do mesmo tipo (opcional).'},
                'conta': {'type': 'string', 'description': 'Nome da conta (opcional; se omitida, usa a primeira conta).'},
                'observacao': {'type': 'string', 'description': 'Anotação opcional.'},
            },
            'required': ['descricao', 'valor', 'tipo'],
        },
        'annotations': {'readOnlyHint': False, 'destructiveHint': False},
    },
    {
        'name': 'ultimos_lancamentos',
        'description': 'Mostra os lançamentos mais recentes do app de finanças.',
        'inputSchema': {
            'type': 'object',
            'properties': {'limite': {'type': 'integer', 'description': 'Quantidade (1 a 30). Padrão 10.'}},
        },
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'resumo_mes',
        'description': 'Resumo de um mês: receitas, despesas (sem contar transferências entre contas), resultado e despesas por categoria. Sem parâmetros, usa o mês atual.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'ano': {'type': 'integer'},
                'mes': {'type': 'integer', 'description': '1 a 12'},
            },
        },
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'consultar_patrimonio',
        'description': 'Mostra o patrimônio: ativos e passivos por grupo, item a item, com totais e patrimônio líquido.',
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'consultar_contas',
        'description': 'Mostra o saldo de cada conta (entradas, saídas e saldo) e o saldo total.',
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'consultar_metas',
        'description': 'Mostra as metas financeiras (ativas e concluídas) com valor alvo, valor atual, percentual e prazo.',
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'consultar_fechamentos',
        'description': 'Mostra todos os meses já fechados: receitas, despesas, resultado, ativos, passivos, patrimônio líquido e a variação em relação ao mês anterior.',
        'inputSchema': {'type': 'object', 'properties': {}},
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'resumo_ano',
        'description': 'Resumo de um ano: receitas, despesas e resultado mês a mês (sem transferências entre contas), mais totais por categoria. Sem parâmetros, usa o ano atual.',
        'inputSchema': {'type': 'object', 'properties': {'ano': {'type': 'integer'}}},
        'annotations': {'readOnlyHint': True},
    },
    {
        'name': 'buscar_lancamentos',
        'description': 'Busca lançamentos com filtros (todos opcionais): texto na descrição, tipo, categoria, conta e período. Mostra os mais recentes e os totais do filtro.',
        'inputSchema': {
            'type': 'object',
            'properties': {
                'texto': {'type': 'string', 'description': 'Trecho da descrição.'},
                'tipo': {'type': 'string', 'enum': ['receita', 'despesa']},
                'categoria': {'type': 'string', 'description': 'Nome da categoria.'},
                'conta': {'type': 'string', 'description': 'Nome da conta.'},
                'data_inicio': {'type': 'string', 'description': 'AAAA-MM-DD'},
                'data_fim': {'type': 'string', 'description': 'AAAA-MM-DD'},
                'limite': {'type': 'integer', 'description': 'Quantos mostrar (1 a 100). Padrão 30.'},
            },
        },
        'annotations': {'readOnlyHint': True},
    },
]

def _mcp_listar_base(args):
    cats = Categoria.query.order_by(Categoria.tipo, Categoria.nome).all()
    todas = Conta.query.order_by(Conta.id).all()
    linhas = ['Contas (a primeira é a padrão):']
    linhas += [f'- {c.nome}' for c in todas] or ['- (nenhuma)']
    linhas.append('Categorias de receita:')
    linhas += [f'- {c.nome}' for c in cats if c.tipo == 'receita']
    linhas.append('Categorias de despesa:')
    linhas += [f'- {c.nome}' for c in cats if c.tipo == 'despesa']
    return '\n'.join(linhas), False

def _mcp_lancar(args):
    descricao = str(args.get('descricao') or '').strip()[:200]
    tipo = _norm(args.get('tipo'))
    if not descricao:
        return 'Informe a descrição do lançamento.', True
    if tipo not in ('receita', 'despesa'):
        return "O tipo precisa ser 'receita' ou 'despesa'.", True
    try:
        valor = round(_parse_valor(args.get('valor')), 2)
    except (ValueError, TypeError):
        return 'Valor inválido.', True
    if valor <= 0 or valor > 100000000:
        return 'O valor precisa ser maior que zero (sempre positivo).', True

    if args.get('data'):
        try:
            data_l = datetime.strptime(str(args['data']).strip(), '%Y-%m-%d').date()
        except ValueError:
            return 'Data inválida. Use o formato AAAA-MM-DD.', True
    else:
        data_l = _hoje_brasil()

    categoria = None
    if args.get('categoria'):
        cats = Categoria.query.filter_by(tipo=tipo).all()
        categoria = _achar_por_nome(cats, args['categoria'])
        if categoria is None:
            nomes = ', '.join(c.nome for c in sorted(cats, key=lambda c: c.nome))
            return f'Categoria não encontrada para {tipo}. Opções: {nomes}.', True

    todas = Conta.query.order_by(Conta.id).all()
    conta = None
    if args.get('conta'):
        conta = _achar_por_nome(todas, args['conta'])
        if conta is None:
            nomes = ', '.join(c.nome for c in todas)
            return f'Conta não encontrada. Opções: {nomes}.', True
    elif todas:
        conta = todas[0]

    try:
        l = Lancamento(
            descricao=descricao, valor=valor, tipo=tipo, data=data_l,
            categoria_id=categoria.id if categoria else None,
            conta_id=conta.id if conta else None,
            observacao=str(args.get('observacao') or '')[:500],
        )
        db.session.add(l)
        db.session.commit()
    except Exception:
        db.session.rollback()
        return 'Não consegui salvar o lançamento.', True

    texto = (f"Lançamento registrado: {tipo} de R$ {brl_num(valor)} — {descricao} "
             f"({data_l.strftime('%d/%m/%Y')}). "
             f"Categoria: {categoria.nome if categoria else 'sem categoria'}. "
             f"Conta: {conta.nome if conta else 'sem conta'}.")
    return texto, False

def _mcp_ultimos(args):
    try:
        limite = max(1, min(30, int(args.get('limite') or 10)))
    except (ValueError, TypeError):
        limite = 10
    itens = Lancamento.query.order_by(Lancamento.data.desc(), Lancamento.criado_em.desc()).limit(limite).all()
    if not itens:
        return 'Nenhum lançamento cadastrado.', False
    linhas = []
    for l in itens:
        sinal = '+' if l.tipo == 'receita' else '-'
        linhas.append(
            f"{l.data.strftime('%d/%m/%Y')} | {sinal}R$ {brl_num(l.valor)} | {l.descricao} | "
            f"{l.categoria.nome if l.categoria else 'sem categoria'} | {l.conta.nome if l.conta else 'sem conta'}"
        )
    return '\n'.join(linhas), False

def _mcp_resumo_mes(args):
    hoje = _hoje_brasil()
    try:
        ano = int(args.get('ano') or hoje.year)
        mes = int(args.get('mes') or hoje.month)
        inicio = date(ano, mes, 1)
    except (ValueError, TypeError):
        return 'Ano ou mês inválido.', True
    fim = date(ano, mes, calendar.monthrange(ano, mes)[1])
    lancs = Lancamento.query.filter(Lancamento.data >= inicio, Lancamento.data <= fim).all()
    reais = [l for l in lancs if not _eh_transferencia(l)]
    receitas = sum(l.valor for l in reais if l.tipo == 'receita')
    despesas = sum(l.valor for l in reais if l.tipo == 'despesa')
    por_cat = defaultdict(float)
    for l in reais:
        if l.tipo == 'despesa':
            por_cat[l.categoria.nome if l.categoria else 'Sem categoria'] += l.valor
    linhas = [
        f'Resumo de {mes:02d}/{ano} (sem transferências entre contas):',
        f'Receitas: R$ {brl_num(receitas)}',
        f'Despesas: R$ {brl_num(despesas)}',
        f'Resultado: R$ {brl_num(receitas - despesas)}',
    ]
    if por_cat:
        linhas.append('Despesas por categoria:')
        for nome, v in sorted(por_cat.items(), key=lambda x: x[1], reverse=True):
            linhas.append(f'- {nome}: R$ {brl_num(v)}')
    return '\n'.join(linhas), False

def _rs(v):
    return 'R$ ' + brl_num(v)

def _rs_sinal(v):
    return ('+' if v > 0 else '') + 'R$ ' + brl_num(v)

def _data_br(d):
    return d.strftime('%d/%m/%Y') if d else '—'

def _mcp_patrimonio(args):
    itens = _itens_patrimonio()
    if not itens:
        return 'Nenhum item de patrimônio cadastrado.', False
    linhas = []
    for tipo, titulo in (('ativo', 'ATIVOS'), ('passivo', 'PASSIVOS')):
        do_tipo = [i for i in itens if i.tipo == tipo]
        linhas.append(f'{titulo}: {_rs(sum(i.valor_exib for i in do_tipo))}')
        for grupo, _icone in GRUPOS_PATRIMONIO[tipo]:
            lista = [i for i in do_tipo if i.grupo == grupo]
            if not lista:
                continue
            linhas.append(f'  {grupo}: {_rs(sum(i.valor_exib for i in lista))}')
            for i in lista:
                origem = 'saldo automático da conta' if i.auto else f'atualizado em {_data_br(i.atualizado_em)}'
                linhas.append(f'    - {i.nome}: {_rs(i.valor_exib)} ({origem})')
    ativos = sum(i.valor_exib for i in itens if i.tipo == 'ativo')
    passivos = sum(i.valor_exib for i in itens if i.tipo == 'passivo')
    linhas.append(f'PATRIMÔNIO LÍQUIDO: {_rs(ativos - passivos)}')
    return '\n'.join(linhas), False

def _mcp_contas(args):
    todas = Conta.query.order_by(Conta.id).all()
    if not todas:
        return 'Nenhuma conta cadastrada.', False
    linhas = []
    total = 0
    for c in todas:
        rec = sum(l.valor for l in c.lancamentos if l.tipo == 'receita')
        desp = sum(l.valor for l in c.lancamentos if l.tipo == 'despesa')
        total += rec - desp
        linhas.append(f'{c.nome}: saldo {_rs(rec - desp)} (entradas {_rs(rec)} | saídas {_rs(desp)})')
    linhas.append(f'Saldo total das contas: {_rs(total)}')
    linhas.append('Obs.: as entradas e saídas incluem transferências entre contas, como na tela Contas.')
    return '\n'.join(linhas), False

def _mcp_metas(args):
    metas_ = Meta.query.order_by(Meta.ativa.desc(), Meta.criada_em.desc()).all()
    if not metas_:
        return 'Nenhuma meta cadastrada.', False
    linhas = []
    for m in metas_:
        pct = (m.valor_atual / m.valor_alvo * 100) if m.valor_alvo else 0
        prazo = f", prazo {_data_br(m.prazo)}" if m.prazo else ''
        status = 'ativa' if m.ativa else 'concluída'
        linhas.append(f'{m.nome} ({status}): {_rs(m.valor_atual)} de {_rs(m.valor_alvo)} ({pct:.0f}%){prazo}')
    return '\n'.join(linhas), False

def _mcp_fechamentos(args):
    regs = Fechamento.query.order_by(Fechamento.ano, Fechamento.mes).all()
    if not regs:
        return 'Nenhum mês fechado ainda.', False
    linhas = []
    anterior = None
    for f in regs:
        partes = [f'{f.periodo}' + (' (informado manualmente)' if f.manual else '')]
        if f.resultado is not None:
            partes.append(f'receitas {_rs(f.receitas)} | despesas {_rs(f.despesas)} | resultado {_rs(f.resultado)}')
        partes.append(f'ativos {_rs(f.total_ativos)} | passivos {_rs(f.total_passivos)} | patrimônio líquido {_rs(f.patrimonio_liquido)}')
        if anterior is not None:
            var = f.patrimonio_liquido - anterior.patrimonio_liquido
            texto = f'variação do patrimônio {_rs_sinal(var)}'
            if anterior.patrimonio_liquido:
                texto += f' ({var / abs(anterior.patrimonio_liquido) * 100:+.2f}%)'
            if f.resultado is not None:
                texto += f' | outros efeitos {_rs_sinal(var - f.resultado)}'
            partes.append(texto)
        linhas.append(' | '.join(partes))
        anterior = f
    return '\n'.join(linhas), False

def _mcp_resumo_ano(args):
    hoje = _hoje_brasil()
    try:
        ano = int(args.get('ano') or hoje.year)
        inicio, fim = date(ano, 1, 1), date(ano, 12, 31)
    except (ValueError, TypeError):
        return 'Ano inválido.', True
    lancs = Lancamento.query.filter(Lancamento.data >= inicio, Lancamento.data <= fim).all()
    reais = [l for l in lancs if not _eh_transferencia(l)]
    linhas = [f'Resumo de {ano} (sem transferências entre contas):']
    tot_r = tot_d = 0
    for m in range(1, 13):
        r = sum(l.valor for l in reais if l.data.month == m and l.tipo == 'receita')
        d = sum(l.valor for l in reais if l.data.month == m and l.tipo == 'despesa')
        tot_r += r
        tot_d += d
        if r or d:
            linhas.append(f'{MESES_PT[m - 1]}: receitas {_rs(r)} | despesas {_rs(d)} | resultado {_rs(r - d)}')
    linhas.append(f'TOTAL: receitas {_rs(tot_r)} | despesas {_rs(tot_d)} | resultado {_rs(tot_r - tot_d)}')
    for tipo, titulo in (('despesa', 'Despesas por categoria'), ('receita', 'Receitas por categoria')):
        por_cat = defaultdict(float)
        for l in reais:
            if l.tipo == tipo:
                por_cat[l.categoria.nome if l.categoria else 'Sem categoria'] += l.valor
        if por_cat:
            linhas.append(f'{titulo}:')
            for nome, v in sorted(por_cat.items(), key=lambda x: x[1], reverse=True):
                linhas.append(f'- {nome}: {_rs(v)}')
    return '\n'.join(linhas), False

def _mcp_buscar(args):
    filtros = []
    if args.get('texto'):
        filtros.append(Lancamento.descricao.ilike('%' + str(args['texto']).strip() + '%'))
    if args.get('tipo'):
        tipo = _norm(args['tipo'])
        if tipo not in ('receita', 'despesa'):
            return "O tipo precisa ser 'receita' ou 'despesa'.", True
        filtros.append(Lancamento.tipo == tipo)
    if args.get('categoria'):
        cat = _achar_por_nome(Categoria.query.all(), args['categoria'])
        if cat is None:
            return 'Categoria não encontrada. Use listar_categorias_e_contas para ver os nomes.', True
        filtros.append(Lancamento.categoria_id == cat.id)
    if args.get('conta'):
        conta = _achar_por_nome(Conta.query.all(), args['conta'])
        if conta is None:
            return 'Conta não encontrada. Use listar_categorias_e_contas para ver os nomes.', True
        filtros.append(Lancamento.conta_id == conta.id)
    for campo, op in (('data_inicio', '>='), ('data_fim', '<=')):
        if args.get(campo):
            try:
                d = datetime.strptime(str(args[campo]).strip(), '%Y-%m-%d').date()
            except ValueError:
                return f'{campo} inválida. Use o formato AAAA-MM-DD.', True
            filtros.append(Lancamento.data >= d if op == '>=' else Lancamento.data <= d)
    try:
        limite = max(1, min(100, int(args.get('limite') or 30)))
    except (ValueError, TypeError):
        limite = 30

    q = Lancamento.query.filter(*filtros)
    total = q.count()
    if total == 0:
        return 'Nenhum lançamento encontrado com esses filtros.', False
    soma = lambda t: db.session.query(db.func.sum(Lancamento.valor)).filter(*filtros, Lancamento.tipo == t).scalar() or 0
    rec, desp = soma('receita'), soma('despesa')
    itens = q.order_by(Lancamento.data.desc(), Lancamento.criado_em.desc()).limit(limite).all()
    linhas = [
        f'{total} lançamento(s) encontrado(s); mostrando {len(itens)} mais recentes.',
        f'Totais do filtro: receitas {_rs(rec)} | despesas {_rs(desp)} (transferências entre contas incluídas).',
    ]
    for l in itens:
        sinal = '+' if l.tipo == 'receita' else '-'
        linhas.append(
            f"{_data_br(l.data)} | {sinal}R$ {brl_num(l.valor)} | {l.descricao} | "
            f"{l.categoria.nome if l.categoria else 'sem categoria'} | {l.conta.nome if l.conta else 'sem conta'}"
        )
    return '\n'.join(linhas), False

MCP_FUNCOES = {
    'listar_categorias_e_contas': _mcp_listar_base,
    'lancar': _mcp_lancar,
    'ultimos_lancamentos': _mcp_ultimos,
    'resumo_mes': _mcp_resumo_mes,
    'consultar_patrimonio': _mcp_patrimonio,
    'consultar_contas': _mcp_contas,
    'consultar_metas': _mcp_metas,
    'consultar_fechamentos': _mcp_fechamentos,
    'resumo_ano': _mcp_resumo_ano,
    'buscar_lancamentos': _mcp_buscar,
}

def _mcp_executar(msg):
    """Trata uma mensagem JSON-RPC. Retorna None quando não há resposta (notificações)."""
    if not isinstance(msg, dict) or 'method' not in msg or msg.get('id') is None:
        return None
    id_ = msg['id']
    metodo = msg['method']
    params = msg.get('params') or {}

    def ok(resultado):
        return {'jsonrpc': '2.0', 'id': id_, 'result': resultado}

    def erro(codigo, texto):
        return {'jsonrpc': '2.0', 'id': id_, 'error': {'code': codigo, 'message': texto}}

    if metodo == 'initialize':
        pedida = params.get('protocolVersion')
        versao = pedida if pedida in MCP_VERSOES else MCP_VERSOES[0]
        return ok({
            'protocolVersion': versao,
            'capabilities': {'tools': {}},
            'serverInfo': {'name': 'financas', 'version': '1.0'},
        })
    if metodo == 'ping':
        return ok({})
    if metodo == 'tools/list':
        return ok({'tools': MCP_TOOLS})
    if metodo == 'tools/call':
        funcao = MCP_FUNCOES.get(params.get('name'))
        if funcao is None:
            return erro(-32602, 'Ferramenta desconhecida.')
        try:
            texto, com_erro = funcao(params.get('arguments') or {})
        except Exception:
            db.session.rollback()
            texto, com_erro = 'Erro ao executar a ferramenta.', True
        return ok({'content': [{'type': 'text', 'text': texto}], 'isError': com_erro})
    return erro(-32601, 'Método não suportado.')

@app.route('/mcp/<token>', methods=['POST', 'GET', 'DELETE'])
def mcp(token):
    esperado = os.environ.get('MCP_TOKEN', '')
    if len(esperado) < 24 or not hmac.compare_digest(token.encode('utf-8'), esperado.encode('utf-8')):
        return 'Not Found', 404
    if request.method != 'POST':
        return '', 405
    try:
        corpo = json.loads(request.get_data(as_text=True))
    except ValueError:
        return jsonify({'jsonrpc': '2.0', 'id': None, 'error': {'code': -32700, 'message': 'JSON inválido.'}}), 400
    if isinstance(corpo, list):
        respostas = [r for r in (_mcp_executar(m) for m in corpo) if r is not None]
        if not respostas:
            return '', 202
        return jsonify(respostas)
    resposta = _mcp_executar(corpo)
    if resposta is None:
        return '', 202
    return jsonify(resposta)

with app.app_context():
    db.create_all()
    seed_categorias()
    seed_contas()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
