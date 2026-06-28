from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from datetime import datetime, date
import json
from collections import defaultdict
import calendar

app = Flask(__name__)
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///financa.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
app.secret_key = 'financa-pessoal-secret-2024'

db = SQLAlchemy(app)

# ─── MODELS ───────────────────────────────────────────────────────────────────

class Categoria(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    nome = db.Column(db.String(100), nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'receita' ou 'despesa'
    icone = db.Column(db.String(10), default='💰')
    cor = db.Column(db.String(20), default='#6366f1')
    lancamentos = db.relationship('Lancamento', backref='categoria', lazy=True)

class Lancamento(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    descricao = db.Column(db.String(200), nullable=False)
    valor = db.Column(db.Float, nullable=False)
    tipo = db.Column(db.String(10), nullable=False)  # 'receita' ou 'despesa'
    data = db.Column(db.Date, nullable=False, default=date.today)
    categoria_id = db.Column(db.Integer, db.ForeignKey('categoria.id'), nullable=True)
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

    return render_template('lancamentos.html',
        lancamentos=lancamentos,
        categorias=categorias,
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
        observacao=data.get('observacao', '')
    )
    db.session.add(l)
    db.session.commit()
    return redirect(request.referrer or url_for('index'))

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

    anos_disponiveis = db.session.query(
        db.func.strftime('%Y', Lancamento.data)
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

with app.app_context():
    db.create_all()
    seed_categorias()

if __name__ == '__main__':
    app.run(debug=True, host='0.0.0.0', port=5000)
