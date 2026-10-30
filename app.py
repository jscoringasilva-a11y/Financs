from flask import Flask, render_template, request, jsonify, redirect, url_for, Response
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

class Parcelamento(db.Model):
    """Marca uma compra no cartão como parcelada em N vezes (a 1ª parcela vence no mês da compra).
    O valor total fica no lançamento; aqui só guardamos o número de parcelas."""
    id = db.Column(db.Integer, primary_key=True)
    lancamento_id = db.Column(db.Integer, db.ForeignKey('lancamento.id'), nullable=False, unique=True)
    parcelas = db.Column(db.Integer, nullable=False, default=1)

class ParcelamentoInicio(db.Model):
    """Opcional: mês em que a 1ª parcela vence, quando não é o mês da compra
    (ex.: compra em outubro com a fatura de outubro já fechada e 1ª parcela em dezembro)."""
    id = db.Column(db.Integer, primary_key=True)
    lancamento_id = db.Column(db.Integer, db.ForeignKey('lancamento.id'), nullable=False, unique=True)
    ano = db.Column(db.Integer, nullable=False)
    mes = db.Column(db.Integer, nullable=False)

def _parse_mes(txt):
    """'2026-12' -> (2026, 12). Vazio -> None. Inválido -> ValueError."""
    txt = (txt or '').strip()
    if not txt:
        return None
    t = txt.replace('/', '-')
    partes = t.split('-')
    if len(partes) != 2:
        raise ValueError('mês inválido')
    if len(partes[0]) != 4:          # aceita também MM/AAAA
        partes = partes[::-1]
    ano, mes = int(partes[0]), int(partes[1])
    if not (2000 <= ano <= 2100 and 1 <= mes <= 12):
        raise ValueError('mês inválido')
    return ano, mes

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
    # Transferências entre contas não são receita nem despesa de verdade
    lancamentos = [l for l in lancamentos if not _eh_transferencia(l)]

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
    Parcelamento.query.filter_by(lancamento_id=l.id).delete()
    ParcelamentoInicio.query.filter_by(lancamento_id=l.id).delete()
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
    # Sem transferências entre contas
    lancamentos_ano = [l for l in lancamentos_ano if not _eh_transferencia(l)]

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
    todas = Conta.query.order_by(Conta.id).all()
    resumos = []
    for c in todas:
        rec = sum(l.valor for l in c.lancamentos if l.tipo == 'receita')
        desp = sum(l.valor for l in c.lancamentos if l.tipo == 'despesa')
        saldo = round(rec - desp, 2) + 0.0
        exigivel = a_vencer = 0
        if c.tipo == 'cartao':
            # já vencido até o fim do mês atual; o resto são parcelas a vencer
            _, exigivel, _ = _fatura_cartao(c, _fim_mes(_hoje_brasil()), pagos_ate=date.max)
            exigivel = min(exigivel, max(0 - saldo, 0))
            a_vencer = max(round((0 - saldo) - exigivel, 2) + 0.0, 0.0)
        resumos.append({'conta': c, 'saldo': saldo, 'receitas': rec, 'despesas': desp,
                        'exigivel': exigivel, 'a_vencer': a_vencer})
    # Cartão de crédito não entra no saldo total: é dívida (fatura), mostrada à parte
    saldo_total = sum(r['saldo'] for r in resumos if r['conta'].tipo != 'cartao')
    fatura_total = sum(0 - r['saldo'] for r in resumos if r['conta'].tipo == 'cartao')
    tem_cartao = any(r['conta'].tipo == 'cartao' for r in resumos)
    categorias_despesa = Categoria.query.filter_by(tipo='despesa').order_by(Categoria.nome).all()
    hoje = date.today()
    return render_template('contas.html', resumos=resumos, hoje_str=hoje.strftime('%Y-%m-%d'),
                           saldo_total=saldo_total, fatura_total=fatura_total, tem_cartao=tem_cartao,
                           categorias_despesa=categorias_despesa)

@app.route('/cartao/compra-parcelada/<int:id>', methods=['POST'])
def compra_parcelada(id):
    """Compra parcelada no cartão: o total entra na dívida do cartão (passivo) e na despesa do mês da
    compra; as parcelas de meses futuros ficam 'a vencer' e só são pagas quando vencem."""
    cartao = Conta.query.get_or_404(id)
    if cartao.tipo != 'cartao':
        return redirect(url_for('contas'))
    f = request.form
    try:
        valor = round(float(f['valor']), 2)
        n = int(f['parcelas'])
        data_c = datetime.strptime(f['data'], '%Y-%m-%d').date()
    except (KeyError, ValueError):
        return redirect(url_for('contas', erro='dados'))
    descricao = (f.get('descricao') or '').strip()[:190]
    if not descricao or valor <= 0 or not (2 <= n <= 48):
        return redirect(url_for('contas', erro='dados'))
    try:
        inicio = _parse_mes(f.get('primeira_parcela'))
    except ValueError:
        return redirect(url_for('contas', erro='dados'))
    if inicio and inicio < (data_c.year, data_c.month):
        return redirect(url_for('contas', erro='dados'))
    l = Lancamento(descricao=f'{descricao} ({n}x)', valor=valor, tipo='despesa', data=data_c,
                   categoria_id=int(f['categoria_id']) if f.get('categoria_id') else None,
                   conta_id=cartao.id)
    db.session.add(l)
    db.session.flush()
    db.session.add(Parcelamento(lancamento_id=l.id, parcelas=n))
    if inicio and inicio != (data_c.year, data_c.month):
        db.session.add(ParcelamentoInicio(lancamento_id=l.id, ano=inicio[0], mes=inicio[1]))
    db.session.commit()
    return redirect(url_for('contas'))

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
    if not nome or tipo not in ('corrente', 'poupanca', 'cartao'):
        return redirect(url_for('contas', erro='dados'))
    if _nome_conta_repetido(nome):
        return redirect(url_for('contas', erro='repetido'))
    padrao = '🐷' if tipo == 'poupanca' else '💳'
    icone = (request.form.get('icone') or padrao)[:10]
    cores = {'poupanca': '#10d98a', 'cartao': '#f0466e', 'corrente': '#4f8ef7'}
    db.session.add(Conta(nome=nome, tipo=tipo, icone=icone, cor=cores[tipo]))
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
    return round(total, 2) + 0.0

def _valor_conta_patrimonio(conta, ate=None):
    """Conta comum: o saldo. Cartão de crédito: a dívida em aberto (inclui parcelas a vencer), que é o saldo com sinal trocado."""
    saldo = _saldo_conta(conta, ate)
    return (round(0 - saldo, 2) + 0.0) if conta.tipo == 'cartao' else saldo

def _fim_mes(d):
    return date(d.year, d.month, calendar.monthrange(d.year, d.month)[1])

def _mapa_parcelas(cartao):
    """{id do lançamento: nº de parcelas} das compras parceladas deste cartão."""
    ids = [l.id for l in cartao.lancamentos if l.tipo == 'despesa']
    if not ids:
        return {}
    inicios = {i.lancamento_id: (i.ano, i.mes)
               for i in ParcelamentoInicio.query.filter(ParcelamentoInicio.lancamento_id.in_(ids)).all()}
    return {p.lancamento_id: (p.parcelas, inicios.get(p.lancamento_id))
            for p in Parcelamento.query.filter(Parcelamento.lancamento_id.in_(ids)).all()}

def _fatura_cartao(cartao, ate, pagos_ate=None):
    """Devolve (total em aberto, já vencido, a vencer) do cartão.
    - total: compras até `ate` menos os pagamentos até `pagos_ate` (inclui parcelas de meses futuros)
    - já vencido: compras à vista + parcelas cujo mês já chegou (até o mês de `ate`), menos os pagamentos
    - a vencer: parcelas de meses seguintes, que continuam em aberto"""
    pagos_ate = pagos_ate or ate
    mapa = _mapa_parcelas(cartao)
    compras = vencido = 0
    for l in cartao.lancamentos:
        if l.tipo != 'despesa' or l.data > ate:
            continue
        compras += l.valor
        n, inicio = mapa.get(l.id, (1, None))
        if n <= 1:
            vencido += l.valor
        else:
            a0, m0 = inicio if inicio else (l.data.year, l.data.month)
            k = (ate.year - a0) * 12 + ate.month - m0 + 1
            k = max(0, min(n, k))   # 0 = nenhuma parcela venceu ainda
            vencido += l.valor if k >= n else round(l.valor / n, 2) * k
    pagos = sum(l.valor for l in cartao.lancamentos if l.tipo == 'receita' and l.data <= pagos_ate)
    total = max(round(compras - pagos, 2) + 0.0, 0.0)
    exigivel = min(max(round(vencido - pagos, 2) + 0.0, 0.0), total)
    return total, exigivel, round(total - exigivel, 2) + 0.0

def _a_vencer(cartao, divida, ate=None):
    """Parte da dívida do cartão que ainda não venceu (parcelas de meses futuros)."""
    ref = ate or _fim_mes(_hoje_brasil())
    _, vencido, _ = _fatura_cartao(cartao, ref, pagos_ate=ate or date.max)
    return max(round(divida - vencido, 2) + 0.0, 0.0)

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
        conta = por_nome.get(_norm(i.nome))
        # Item manual só é trocado pelo saldo da conta se estiver no grupo certo:
        # conta comum -> ativo em "Dinheiro e contas"; cartão -> passivo em "Cartão de crédito"
        if conta is not None and not (
            (conta.tipo != 'cartao' and i.tipo == 'ativo' and i.grupo == 'Dinheiro e contas') or
            (conta.tipo == 'cartao' and i.tipo == 'passivo' and i.grupo == 'Cartão de crédito')):
            conta = None
        if conta is not None:
            ligadas.add(conta.id)
            valor, auto = _valor_conta_patrimonio(conta, ate), True
        else:
            valor, auto = i.valor, False
        lista.append(SimpleNamespace(
            id=i.id, nome=i.nome, tipo=i.tipo, grupo=i.grupo, valor=i.valor,
            valor_exib=valor, auto=auto, fixo=False, atualizado_em=i.atualizado_em,
            cartao=conta is not None and conta.tipo == 'cartao',
            a_vencer=_a_vencer(conta, valor, ate) if (conta is not None and conta.tipo == 'cartao') else 0))
    for c in todas:
        if c.id not in ligadas:
            eh_cartao = c.tipo == 'cartao'
            lista.append(SimpleNamespace(
                id=f'conta-{c.id}', nome=c.nome,
                tipo='passivo' if eh_cartao else 'ativo',
                grupo='Cartão de crédito' if eh_cartao else 'Dinheiro e contas', valor=0,
                valor_exib=_valor_conta_patrimonio(c, ate), auto=True, fixo=True,
                atualizado_em=None, cartao=eh_cartao,
                a_vencer=_a_vencer(c, _valor_conta_patrimonio(c, ate), ate) if eh_cartao else 0))
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

def _pagar_fatura(cartao, origem_id, fim_mes):
    """Paga o que JÁ VENCEU do cartão (compras à vista + parcelas até o mês fechado) com dinheiro
    da conta escolhida. O pagamento tem a data de HOJE (quando o dinheiro realmente sai): se for
    feito depois do fim do mês, a dívida daquele mês continua no passivo em 31/mês.
    Parcelas de meses futuros continuam em aberto. É uma transferência (conta -> cartão), não uma
    despesa nova: as compras já foram contadas quando lançadas. Se não há nada vencido, não faz nada."""
    origem = Conta.query.get(origem_id)
    if origem is None or origem.tipo == 'cartao':
        return
    hoje = _hoje_brasil()
    total_hoje, _, _ = _fatura_cartao(cartao, hoje)
    _, vencido, _ = _fatura_cartao(cartao, fim_mes, pagos_ate=date.max)
    valor = round(min(vencido, total_hoje), 2)
    if valor <= 0:
        return
    db.session.add_all([
        Lancamento(descricao=f'Transferência → {cartao.nome}', valor=valor, tipo='despesa',
                   data=hoje, conta_id=origem.id, observacao='Pagamento de fatura no fechamento'),
        Lancamento(descricao=f'Transferência ← {origem.nome}', valor=valor, tipo='receita',
                   data=hoje, conta_id=cartao.id, observacao='Pagamento de fatura no fechamento'),
    ])
    db.session.commit()

def _calcular_fechamento(ano, mes):
    inicio = date(ano, mes, 1)
    fim = date(ano, mes, calendar.monthrange(ano, mes)[1])

    do_mes = Lancamento.query.filter(Lancamento.data >= inicio, Lancamento.data <= fim).all()
    receitas = sum(l.valor for l in do_mes if l.tipo == 'receita' and not _eh_transferencia(l))
    despesas = sum(l.valor for l in do_mes if l.tipo == 'despesa' and not _eh_transferencia(l))

    # Saldo das contas até o último dia do mês (mesma lógica da tela Contas)
    ate_fim = Lancamento.query.filter(Lancamento.conta_id.isnot(None), Lancamento.data <= fim).all()
    # Cartão de crédito não é dinheiro em conta: fica fora do saldo (aparece como passivo)
    saldo_contas = sum(l.valor if l.tipo == 'receita' else -l.valor
                       for l in ate_fim if l.conta is None or l.conta.tipo != 'cartao')

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

    # Cartões com fatura em aberto: cada um pode ser pago com uma conta ao fechar o mês
    cartoes_fatura = []
    for c in Conta.query.filter_by(tipo='cartao').order_by(Conta.id).all():
        fatura = max(0 - _saldo_conta(c), 0)
        if fatura > 0:
            cartoes_fatura.append({'conta': c, 'fatura': fatura})
    contas_pagamento = Conta.query.filter(Conta.tipo != 'cartao').order_by(Conta.id).all()

    return render_template('fechamento.html',
        cartoes_fatura=cartoes_fatura,
        contas_pagamento=contas_pagamento,
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
        fim_mes = date(ano, mes, calendar.monthrange(ano, mes)[1])
        for cartao in Conta.query.filter_by(tipo='cartao').order_by(Conta.id).all():
            origem_id = request.form.get(f'pagar_com_{cartao.id}', type=int)
            if origem_id:
                _pagar_fatura(cartao, origem_id, fim_mes)
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
    if len(parciais) == 1:
        return parciais[0]
    # Todas as palavras do que foi dito aparecem no nome (ex.: "cartao pj" -> "Cartão de Crédito PJ")
    palavras = set(alvo.split())
    por_palavras = [i for i in itens if palavras and palavras <= set(_norm(i.nome).split())]
    return por_palavras[0] if len(por_palavras) == 1 else None

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
                'parcelas': {'type': 'integer', 'description': 'Só para despesa no cartão de crédito: número de parcelas (2 a 48). O valor informado é o TOTAL da compra. Padrão 1 (à vista).'},
                'primeira_parcela': {'type': 'string', 'description': 'Opcional, só com parcelas: mês em que a 1ª parcela vence, no formato AAAA-MM (ex.: 2026-12). Se omitido, é o mês da compra.'},
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
        parcelas = int(args.get('parcelas') or 1)
    except (ValueError, TypeError):
        return 'Número de parcelas inválido.', True
    if parcelas < 1 or parcelas > 48:
        return 'O número de parcelas deve ficar entre 1 e 48.', True
    inicio = None
    if parcelas > 1:
        if tipo != 'despesa' or conta is None or conta.tipo != 'cartao':
            return 'Compra parcelada só vale para despesa no cartão de crédito. Informe a conta do cartão.', True
        try:
            inicio = _parse_mes(str(args.get('primeira_parcela') or ''))
        except ValueError:
            return "Mês da 1ª parcela inválido. Use o formato AAAA-MM (ex.: 2026-12).", True
        if inicio and inicio < (data_l.year, data_l.month):
            return 'A 1ª parcela não pode ser anterior ao mês da compra.', True
        descricao = f'{descricao[:190]} ({parcelas}x)'

    try:
        l = Lancamento(
            descricao=descricao, valor=valor, tipo=tipo, data=data_l,
            categoria_id=categoria.id if categoria else None,
            conta_id=conta.id if conta else None,
            observacao=str(args.get('observacao') or '')[:500],
        )
        db.session.add(l)
        if parcelas > 1:
            db.session.flush()
            db.session.add(Parcelamento(lancamento_id=l.id, parcelas=parcelas))
            if inicio and inicio != (data_l.year, data_l.month):
                db.session.add(ParcelamentoInicio(lancamento_id=l.id, ano=inicio[0], mes=inicio[1]))
        db.session.commit()
    except Exception:
        db.session.rollback()
        return 'Não consegui salvar o lançamento.', True

    texto = (f"Lançamento registrado: {tipo} de R$ {brl_num(valor)} — {descricao} "
             f"({data_l.strftime('%d/%m/%Y')}). "
             f"Categoria: {categoria.nome if categoria else 'sem categoria'}. "
             f"Conta: {conta.nome if conta else 'sem conta'}.")
    if parcelas > 1:
        texto += (f" Parcelado em {parcelas}x de aproximadamente R$ {brl_num(valor / parcelas)}: "
                  "o total entra na dívida do cartão e as parcelas futuras ficam a vencer.")
        if inicio:
            texto += f" A 1ª parcela vence em {inicio[1]:02d}/{inicio[0]}."
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
    faturas = 0
    for c in todas:
        rec = sum(l.valor for l in c.lancamentos if l.tipo == 'receita')
        desp = sum(l.valor for l in c.lancamentos if l.tipo == 'despesa')
        saldo = round(rec - desp, 2) + 0.0
        if c.tipo == 'cartao':
            fatura = round(0 - saldo, 2) + 0.0
            faturas += fatura
            _, vencido, _ = _fatura_cartao(c, _fim_mes(_hoje_brasil()), pagos_ate=date.max)
            vencido = min(vencido, max(fatura, 0))
            a_vencer = max(round(fatura - vencido, 2) + 0.0, 0.0)
            linhas.append(f'{c.nome} (cartão de crédito): dívida em aberto {_rs(fatura)}, sendo já vencido {_rs(vencido)} e parcelas a vencer {_rs(a_vencer)} (compras {_rs(desp)} | pagamentos {_rs(rec)})')
        else:
            total += saldo
            linhas.append(f'{c.nome}: saldo {_rs(saldo)} (entradas {_rs(rec)} | saídas {_rs(desp)})')
    linhas.append(f'Saldo total das contas (sem o cartão): {_rs(total)}')
    if any(c.tipo == 'cartao' for c in todas):
        linhas.append(f'Fatura total dos cartões: {_rs(faturas)}')
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

# ─── BACKUP EM EXCEL ──────────────────────────────────────────────────────────
# Baixar: gera uma planilha com tudo. Restaurar: lê a planilha e SÓ ACRESCENTA o que não existe
# no banco (nunca apaga nem altera o que já está lá).

BACKUP_COLS_LANC = ['ID', 'Data', 'Descrição', 'Valor', 'Tipo', 'Categoria', 'Tipo da categoria',
                    'Conta', 'Observação', 'Parcelas', '1ª parcela (AAAA-MM)']

def _dt_txt(v):
    return v.strftime('%Y-%m-%d %H:%M') if v else ''

def _gerar_backup_xlsx():
    import io
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    def folha(nome, cab, linhas, larguras, fmts=None):
        ws = wb.create_sheet(nome)
        ws.append(cab)
        for c in ws[1]:
            c.font = Font(bold=True, color='FFFFFF')
            c.fill = PatternFill('solid', fgColor='1E3340')
        for lin in linhas:
            ws.append(lin)
        for row in ws.iter_rows(min_row=2):
            for c in row:
                if isinstance(c.value, str) and c.data_type == 'f':
                    c.data_type = 's'          # texto que começa com "=" não vira fórmula
        for col, fmt in (fmts or {}).items():
            for row in ws.iter_rows(min_row=2, min_col=col, max_col=col):
                row[0].number_format = fmt
        for i, w in enumerate(larguras, 1):
            ws.column_dimensions[get_column_letter(i)].width = w
        ws.freeze_panes = 'A2'
        return ws

    lancs = Lancamento.query.order_by(Lancamento.data, Lancamento.id).all()
    parc = {p.lancamento_id: p.parcelas for p in Parcelamento.query.all()}
    inic = {i.lancamento_id: f'{i.ano}-{i.mes:02d}' for i in ParcelamentoInicio.query.all()}

    def linha_lanc(l):
        return [l.id, l.data, l.descricao, round(l.valor, 2), l.tipo,
                l.categoria.nome if l.categoria else '', l.categoria.tipo if l.categoria else '',
                l.conta.nome if l.conta else '', l.observacao or '',
                parc.get(l.id, ''), inic.get(l.id, '')]

    larg = [8, 12, 46, 14, 10, 24, 16, 26, 40, 10, 20]
    fm = {2: 'dd/mm/yyyy', 4: '#,##0.00'}

    ws0 = wb.create_sheet('LEIA-ME')
    ws0.append(['Backup do app de Finanças'])
    ws0['A1'].font = Font(bold=True, size=14)
    ws0.append([f'Gerado em {_hoje_brasil().strftime("%d/%m/%Y")}'])
    ws0.append([''])
    ws0.append(['Para restaurar: no app, menu Backup > Restaurar. Só entra o que estiver faltando no banco.'])
    ws0.append(['A restauração lê as abas: Lançamentos, Contas, Categorias, Metas, Patrimônio e Fechamentos.'])
    ws0.append(['As abas por mês (ex.: 2026-10) e o Resumo mensal são só para consulta e são ignoradas ao restaurar.'])
    ws0.append(['Não troque os nomes das abas nem os títulos das colunas.'])
    ws0.column_dimensions['A'].width = 110

    # Resumo mensal (sem transferências entre contas)
    resumo = defaultdict(lambda: [0.0, 0.0])
    for l in lancs:
        if _eh_transferencia(l):
            continue
        resumo[(l.data.year, l.data.month)][0 if l.tipo == 'receita' else 1] += l.valor
    folha('Resumo mensal', ['Mês', 'Receitas', 'Despesas', 'Resultado'],
          [[f'{a}-{m:02d}', round(r, 2), round(d, 2), round(r - d, 2)] for (a, m), (r, d) in sorted(resumo.items())],
          [12, 16, 16, 16], {2: '#,##0.00', 3: '#,##0.00', 4: '#,##0.00'})

    folha('Lançamentos', BACKUP_COLS_LANC, [linha_lanc(l) for l in lancs], larg, fm)

    por_mes = defaultdict(list)
    for l in lancs:
        por_mes[f'{l.data.year}-{l.data.month:02d}'].append(l)
    for chave in sorted(por_mes):
        folha(chave, BACKUP_COLS_LANC, [linha_lanc(l) for l in por_mes[chave]], larg, fm)

    folha('Contas', ['Nome', 'Tipo', 'Ícone', 'Cor'],
          [[c.nome, c.tipo, c.icone or '', c.cor or ''] for c in Conta.query.order_by(Conta.id).all()],
          [30, 12, 8, 12])
    folha('Categorias', ['Nome', 'Tipo', 'Ícone', 'Cor'],
          [[c.nome, c.tipo, c.icone or '', c.cor or ''] for c in Categoria.query.order_by(Categoria.tipo, Categoria.nome).all()],
          [30, 12, 8, 12])
    folha('Metas', ['Nome', 'Valor alvo', 'Valor atual', 'Prazo', 'Ícone', 'Ativa'],
          [[m.nome, round(m.valor_alvo, 2), round(m.valor_atual or 0, 2), m.prazo or '', m.icone or '',
            'Sim' if m.ativa else 'Não'] for m in Meta.query.order_by(Meta.id).all()],
          [36, 16, 16, 12, 8, 8], {2: '#,##0.00', 3: '#,##0.00', 4: 'dd/mm/yyyy'})
    folha('Patrimônio', ['Nome', 'Tipo', 'Grupo', 'Valor', 'Atualizado em'],
          [[p.nome, p.tipo, p.grupo, round(p.valor or 0, 2), _dt_txt(p.atualizado_em)]
           for p in Patrimonio.query.order_by(Patrimonio.id).all()],
          [36, 10, 24, 16, 18], {4: '#,##0.00'})
    folha('Fechamentos', ['Ano', 'Mês', 'Receitas', 'Despesas', 'Saldo contas', 'Total ativos',
                          'Total passivos', 'Manual', 'Detalhes', 'Fechado em'],
          [[f.ano, f.mes, f.receitas if f.receitas is not None else '', f.despesas if f.despesas is not None else '',
            f.saldo_contas if f.saldo_contas is not None else '', f.total_ativos, f.total_passivos,
            'Sim' if f.manual else 'Não', f.detalhes or '', _dt_txt(f.fechado_em)]
           for f in Fechamento.query.order_by(Fechamento.ano, Fechamento.mes).all()],
          [8, 8, 14, 14, 14, 14, 14, 8, 40, 18])

    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()

def _ler_folha(wb, nome):
    if nome not in wb.sheetnames:
        return None
    linhas = list(wb[nome].iter_rows(values_only=True))
    if not linhas:
        return []
    cab = [str(c or '').strip() for c in linhas[0]]
    saida = []
    for r in linhas[1:]:
        if r is None or all(c in (None, '') for c in r):
            continue
        saida.append({cab[i]: r[i] for i in range(min(len(cab), len(r))) if cab[i]})
    return saida

def _data_celula(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v or '').strip()
    for fmt in ('%d/%m/%Y', '%Y-%m-%d', '%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError('data inválida')

def _datahora_celula(v):
    if isinstance(v, datetime):
        return v
    try:
        return datetime.strptime(str(v or '').strip(), '%Y-%m-%d %H:%M')
    except ValueError:
        return None

def _num_ou_none(v):
    if v in (None, ''):
        return None
    return _parse_valor(v)

def _sim(v):
    return v is True or _norm(v) in ('sim', 'true', '1', 'verdadeiro')

def _restaurar_backup(arquivo):
    """Lê a planilha de backup e acrescenta ao banco SÓ o que ainda não existe. Devolve (resumo, avisos)."""
    from openpyxl import load_workbook
    from collections import Counter
    wb = load_workbook(arquivo, data_only=True, read_only=True)
    if _ler_folha(wb, 'Lançamentos') is None:
        raise ValueError('Esta planilha não parece um backup do app (não achei a aba "Lançamentos").')

    resumo = {}
    avisos = []

    def conta_res(nome, novos, ja, ign=0):
        resumo[nome] = {'novos': novos, 'ja': ja, 'ign': ign}

    # Categorias
    cats = {(_norm(c.nome), c.tipo): c for c in Categoria.query.all()}
    novos = ja = ign = 0
    for r in _ler_folha(wb, 'Categorias') or []:
        nome, tipo = str(r.get('Nome') or '').strip()[:100], _norm(r.get('Tipo'))
        if not nome or tipo not in ('receita', 'despesa'):
            ign += 1
            continue
        if (_norm(nome), tipo) in cats:
            ja += 1
            continue
        c = Categoria(nome=nome, tipo=tipo, icone=str(r.get('Ícone') or '💰')[:10], cor=str(r.get('Cor') or '#6366f1')[:20])
        db.session.add(c)
        cats[(_norm(nome), tipo)] = c
        novos += 1
    conta_res('Categorias', novos, ja, ign)

    # Contas
    contas = {_norm(c.nome): c for c in Conta.query.all()}
    novos = ja = ign = 0
    for r in _ler_folha(wb, 'Contas') or []:
        nome, tipo = str(r.get('Nome') or '').strip()[:100], _norm(r.get('Tipo'))
        if not nome or tipo not in ('corrente', 'poupanca', 'cartao'):
            ign += 1
            continue
        if _norm(nome) in contas:
            ja += 1
            continue
        c = Conta(nome=nome, tipo=tipo, icone=str(r.get('Ícone') or '🏦')[:10], cor=str(r.get('Cor') or '#4f8ef7')[:20])
        db.session.add(c)
        contas[_norm(nome)] = c
        novos += 1
    conta_res('Contas', novos, ja, ign)
    db.session.flush()

    # Lançamentos (compara data + descrição + valor + tipo + conta; repetidos legítimos são respeitados)
    existentes = Counter()
    for l in Lancamento.query.all():
        existentes[(l.data, _norm(l.descricao), round(l.valor, 2), l.tipo, _norm(l.conta.nome if l.conta else ''))] += 1
    novos = ja = ign = 0
    for r in _ler_folha(wb, 'Lançamentos'):
        try:
            data_l = _data_celula(r.get('Data'))
            desc = str(r.get('Descrição') or '').strip()[:200]
            valor = round(_parse_valor(r.get('Valor')), 2)
            tipo = _norm(r.get('Tipo'))
            if not desc or valor <= 0 or tipo not in ('receita', 'despesa'):
                raise ValueError('linha inválida')
            nome_conta = str(r.get('Conta') or '').strip()
            nome_cat = str(r.get('Categoria') or '').strip()
            parcelas = int(_parse_valor(r['Parcelas'])) if r.get('Parcelas') not in (None, '') else 1
            inicio = _parse_mes(str(r.get('1ª parcela (AAAA-MM)') or ''))
        except (ValueError, TypeError, KeyError):
            ign += 1
            continue
        chave = (data_l, _norm(desc), valor, tipo, _norm(nome_conta))
        if existentes[chave] > 0:
            existentes[chave] -= 1
            ja += 1
            continue
        conta = None
        if nome_conta:
            conta = contas.get(_norm(nome_conta))
            if conta is None:
                conta = Conta(nome=nome_conta[:100], tipo='corrente')
                db.session.add(conta)
                contas[_norm(nome_conta)] = conta
        cat = None
        if nome_cat:
            tipo_cat = _norm(r.get('Tipo da categoria')) or tipo
            cat = cats.get((_norm(nome_cat), tipo_cat))
            if cat is None:
                cat = Categoria(nome=nome_cat[:100], tipo=tipo_cat if tipo_cat in ('receita', 'despesa') else tipo)
                db.session.add(cat)
                cats[(_norm(nome_cat), cat.tipo)] = cat
        l = Lancamento(descricao=desc, valor=valor, tipo=tipo, data=data_l,
                       categoria=cat, conta=conta, observacao=str(r.get('Observação') or '')[:500])
        db.session.add(l)
        if parcelas > 1:
            db.session.flush()
            db.session.add(Parcelamento(lancamento_id=l.id, parcelas=parcelas))
            if inicio and inicio != (data_l.year, data_l.month):
                db.session.add(ParcelamentoInicio(lancamento_id=l.id, ano=inicio[0], mes=inicio[1]))
        novos += 1
    conta_res('Lançamentos', novos, ja, ign)
    db.session.flush()

    # Metas
    existentes = {(_norm(m.nome), round(m.valor_alvo, 2)) for m in Meta.query.all()}
    novos = ja = ign = 0
    for r in _ler_folha(wb, 'Metas') or []:
        try:
            nome = str(r.get('Nome') or '').strip()[:200]
            alvo = round(_parse_valor(r.get('Valor alvo')), 2)
            atual = _num_ou_none(r.get('Valor atual')) or 0
            prazo = _data_celula(r['Prazo']) if r.get('Prazo') not in (None, '') else None
            if not nome:
                raise ValueError
        except (ValueError, TypeError, KeyError):
            ign += 1
            continue
        if (_norm(nome), alvo) in existentes:
            ja += 1
            continue
        db.session.add(Meta(nome=nome, valor_alvo=alvo, valor_atual=atual, prazo=prazo,
                            icone=str(r.get('Ícone') or '🎯')[:10], ativa=_sim(r.get('Ativa'))))
        existentes.add((_norm(nome), alvo))
        novos += 1
    conta_res('Metas', novos, ja, ign)

    # Patrimônio (itens digitados à mão)
    existentes = {(_norm(p.nome), p.tipo, p.grupo) for p in Patrimonio.query.all()}
    novos = ja = ign = 0
    for r in _ler_folha(wb, 'Patrimônio') or []:
        try:
            nome = str(r.get('Nome') or '').strip()[:200]
            tipo = _norm(r.get('Tipo'))
            grupo = str(r.get('Grupo') or '').strip()[:50]
            valor = _parse_valor(r.get('Valor'))
            if not nome or tipo not in ('ativo', 'passivo') or not grupo:
                raise ValueError
        except (ValueError, TypeError):
            ign += 1
            continue
        if (_norm(nome), tipo, grupo) in existentes:
            ja += 1
            continue
        db.session.add(Patrimonio(nome=nome, tipo=tipo, grupo=grupo, valor=valor,
                                  atualizado_em=_datahora_celula(r.get('Atualizado em')) or datetime.utcnow()))
        existentes.add((_norm(nome), tipo, grupo))
        novos += 1
    conta_res('Patrimônio', novos, ja, ign)

    # Fechamentos
    existentes = {(f.ano, f.mes) for f in Fechamento.query.all()}
    novos = ja = ign = 0
    for r in _ler_folha(wb, 'Fechamentos') or []:
        try:
            ano, mes = int(_parse_valor(r.get('Ano'))), int(_parse_valor(r.get('Mês')))
            ativos = _parse_valor(r.get('Total ativos'))
            passivos = _parse_valor(r.get('Total passivos'))
            if not (2000 <= ano <= 2100 and 1 <= mes <= 12):
                raise ValueError
        except (ValueError, TypeError):
            ign += 1
            continue
        if (ano, mes) in existentes:
            ja += 1
            continue
        db.session.add(Fechamento(
            ano=ano, mes=mes, receitas=_num_ou_none(r.get('Receitas')), despesas=_num_ou_none(r.get('Despesas')),
            saldo_contas=_num_ou_none(r.get('Saldo contas')), total_ativos=ativos, total_passivos=passivos,
            manual=_sim(r.get('Manual')), detalhes=str(r.get('Detalhes') or '') or None,
            fechado_em=_datahora_celula(r.get('Fechado em')) or datetime.utcnow()))
        existentes.add((ano, mes))
        novos += 1
    conta_res('Fechamentos', novos, ja, ign)

    wb.close()
    return resumo, avisos

def _contagens_backup():
    return [('Lançamentos', Lancamento.query.count()), ('Contas', Conta.query.count()),
            ('Categorias', Categoria.query.count()), ('Metas', Meta.query.count()),
            ('Itens do patrimônio', Patrimonio.query.count()), ('Fechamentos', Fechamento.query.count())]

@app.route('/backup')
def backup():
    return render_template('backup.html', resultado=None, erro=None, contagens=_contagens_backup())

@app.route('/backup/baixar')
def backup_baixar():
    try:
        dados = _gerar_backup_xlsx()
    except ImportError:
        return render_template('backup.html', resultado=None, contagens=_contagens_backup(),
                               erro='A biblioteca de Excel ainda não foi instalada. Suba também o requirements.txt e aguarde o deploy.')
    nome = f'backup-financas-{_hoje_brasil().strftime("%Y-%m-%d")}.xlsx'
    return Response(dados, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                    headers={'Content-Disposition': f'attachment; filename="{nome}"'})

@app.route('/backup/restaurar', methods=['POST'])
def backup_restaurar():
    import io
    arq = request.files.get('arquivo')
    resultado, erro = None, None
    if not arq or not arq.filename:
        erro = 'Escolha o arquivo de backup (.xlsx).'
    elif not arq.filename.lower().endswith('.xlsx'):
        erro = 'O arquivo precisa ser uma planilha .xlsx (o backup baixado aqui).'
    else:
        conteudo = arq.read(15 * 1024 * 1024 + 1)
        if len(conteudo) > 15 * 1024 * 1024:
            erro = 'Arquivo grande demais.'
        else:
            try:
                resultado, _ = _restaurar_backup(io.BytesIO(conteudo))
                db.session.commit()
            except ImportError:
                db.session.rollback()
                erro = 'A biblioteca de Excel ainda não foi instalada. Suba também o requirements.txt e aguarde o deploy.'
            except ValueError as e:
                db.session.rollback()
                erro = str(e)
            except Exception:
                db.session.rollback()
                erro = 'Não consegui ler esta planilha. Nada foi alterado no banco.'
    return render_template('backup.html', resultado=resultado, erro=erro, contagens=_contagens_backup())

def seed_cartao():
    """Cria os cartões PF e PJ uma única vez (só se ainda não existir nenhum cartão)."""
    if Conta.query.filter_by(tipo='cartao').count() == 0:
        for nome, icone in (('Cartão de Crédito PF', '👤'), ('Cartão de Crédito PJ', '🏢')):
            if not _nome_conta_repetido(nome):
                db.session.add(Conta(nome=nome, tipo='cartao', icone=icone, cor='#f0466e'))
        db.session.commit()

with app.app_context():
    db.create_all()
    seed_categorias()
    seed_contas()
    seed_cartao()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)