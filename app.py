"""My Finance — Mongolian personal finance tracker.

Start with: streamlit run app.py
All data is stored locally in finance.db (never committed to Git).
"""
from __future__ import annotations

import hashlib
import io
import json
import math
import re
import sqlite3
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st

DB = Path(__file__).resolve().parent / 'finance.db'
CATEGORIES = ['Хүнс', 'Орон сууц', 'Тээвэр', 'Эрүүл мэнд', 'Боловсрол',
              'Хувцас', 'Зугаа цэнгэл', 'Зээл', 'Хадгаламж', 'Бусад']


def connect():
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA foreign_keys = ON')
    return conn


def init_db():
    with connect() as c:
        c.executescript('''
        CREATE TABLE IF NOT EXISTS transactions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            day TEXT NOT NULL, description TEXT NOT NULL, amount REAL NOT NULL,
            category TEXT NOT NULL, source TEXT NOT NULL DEFAULT '',
            fingerprint TEXT NOT NULL UNIQUE, added_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS budgets (
            month TEXT NOT NULL, category TEXT NOT NULL, amount REAL NOT NULL,
            PRIMARY KEY (month, category)
        );
        CREATE TABLE IF NOT EXISTS goals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL, target REAL NOT NULL, current REAL NOT NULL DEFAULT 0,
            monthly REAL NOT NULL DEFAULT 0, annual_return REAL NOT NULL DEFAULT 0,
            deadline TEXT, loan_pct REAL NOT NULL DEFAULT 0,
            loan_annual_rate REAL NOT NULL DEFAULT 0, loan_years INTEGER NOT NULL DEFAULT 0
        );
        ''')


def month_shift(y: int, m: int, offset: int = 1) -> str:
    n = y * 12 + m - 1 + offset
    return f'{n // 12:04d}-{n % 12 + 1:02d}'


def fmt(n: float) -> str:
    return f'{n:,.0f} ₮'


def fingerprint(day, description, amount, reference='') -> str:
    # Duplicate lines within the SAME statement may be distinct transactions.
    # Include a bank's reference/identifier when present; otherwise use occurrence counter.
    s = json.dumps([str(day), str(description).strip().casefold(), round(float(amount), 2), str(reference)], ensure_ascii=False)
    return hashlib.sha256(s.encode('utf-8')).hexdigest()


def read_file(file) -> pd.DataFrame:
    data = file.getvalue()
    if file.name.lower().endswith('.csv'):
        for encoding in ('utf-8-sig', 'cp1251', 'utf-16'):
            try:
                return pd.read_csv(io.BytesIO(data), encoding=encoding, sep=None, engine='python')
            except (UnicodeError, pd.errors.ParserError):
                continue
        raise ValueError('CSV файлын кодчилол эсвэл бүтэц танигдсангүй.')
    return pd.read_excel(io.BytesIO(data))


def normalize(frame: pd.DataFrame, date_col, desc_col, amount_col,
              category_col, reference_col, debit_col, credit_col) -> pd.DataFrame:
    x = frame.copy()
    out = pd.DataFrame()
    out['day'] = pd.to_datetime(x[date_col], errors='coerce', dayfirst=False).dt.strftime('%Y-%m-%d')
    out['description'] = x[desc_col].fillna('').astype(str).str.strip()
    def numbers(series):
        return pd.to_numeric(series.astype(str).str.replace(',', '', regex=False)
                             .str.replace('₮', '', regex=False).str.replace(' ', '', regex=False), errors='coerce').fillna(0)
    if amount_col != '—':
        out['amount'] = numbers(x[amount_col])
    else:
        out['amount'] = numbers(x[credit_col]) - numbers(x[debit_col])
    out['category'] = x[category_col].fillna('Бусад').astype(str) if category_col != '—' else 'Бусад'
    out['ref'] = x[reference_col].fillna('').astype(str) if reference_col != '—' else ''
    out = out.dropna(subset=['day'])
    out = out[out['description'].ne('') & out['amount'].ne(0)].copy()
    # Distinguish multiple identical lines in one statement, and still dedupe reimports.
    out['occurrence'] = out.groupby(['day', 'description', 'amount', 'ref'], sort=False).cumcount().astype(str)
    out['fingerprint'] = out.apply(lambda r: fingerprint(r['day'], r['description'], r['amount'],
                                         str(r['ref']) + '#' + r['occurrence']), axis=1)
    return out


def calculate_goal(goal: dict) -> dict:
    """Contributions toward down payment (target less financed amount), monthly compounding."""
    target = max(0., float(goal['target']))
    loan_pct = min(100., max(0., float(goal['loan_pct'])))
    cash_needed = target * (1 - loan_pct / 100)
    current = max(0., float(goal['current']))
    monthly = max(0., float(goal['monthly']))
    rate = max(0., float(goal['annual_return'])) / 1200
    remaining = max(0., cash_needed - current)
    if remaining <= 0:
        months = 0
    elif monthly <= 0 and rate <= 0:
        months = None
    elif rate == 0:
        months = math.ceil(remaining / monthly)
    elif monthly == 0 and current == 0:
        months = None
    else:
        # closed-form solution to current*(1+r)^n + monthly*((1+r)^n-1)/r >= cash_needed
        ratio = (cash_needed + monthly / rate) / (current + monthly / rate)
        months = math.ceil(math.log(ratio) / math.log1p(rate)) if ratio > 0 else None
    loan = target * loan_pct / 100
    r = max(0., float(goal['loan_annual_rate'])) / 1200
    n = max(0, int(goal['loan_years'])) * 12
    payment = (loan / n if r == 0 else loan * r / (1 - (1+r)**(-n))) if loan > 0 and n > 0 else 0
    return dict(cash_needed=cash_needed, remaining=remaining, months=months,
                loan=loan, loan_payment=payment)


st.set_page_config(page_title='Миний санхүү', page_icon='💰', layout='wide')
init_db()
st.title('💰 Миний санхүү')
st.caption('Банкны хуулга · Сарын төсөв · Урт хугацааны зорилго | Мэдээлэл зөвхөн энэ компьютерийн SQLite санд хадгалагдана.')
page = st.sidebar.radio('Цэс', ['📊 Хяналтын самбар', '📥 Хуулга импортлох', '🧾 Гүйлгээ ба ангилал',
                                '📅 Сарын төсөв', '🎯 Урт хугацааны зорилго'])

with connect() as c:
    transactions = pd.read_sql_query('SELECT id, day, description, amount, category, source FROM transactions ORDER BY day DESC, id DESC', c)
    budgets = pd.read_sql_query('SELECT * FROM budgets', c)
    goals = pd.read_sql_query('SELECT * FROM goals ORDER BY id', c)

if page == '📊 Хяналтын самбар':
    st.subheader('Ерөнхий үзүүлэлт')
    current_month = date.today().strftime('%Y-%m')
    this_month = transactions[transactions.day.str.startswith(current_month)]
    income = this_month.loc[this_month.amount > 0, 'amount'].sum()
    expense = -this_month.loc[this_month.amount < 0, 'amount'].sum()
    a,b,c = st.columns(3)
    a.metric('Энэ сарын орлого', fmt(income))
    b.metric('Энэ сарын зарлага', fmt(expense))
    c.metric('Энэ сарын үлдэгдэл', fmt(income-expense))
    if not transactions.empty:
        chart = transactions.assign(month=transactions.day.str[:7],
                                    income=transactions.amount.clip(lower=0),
                                    expense=-transactions.amount.clip(upper=0))
        st.subheader('Сар бүрийн орлого, зарлага')
        st.bar_chart(chart.groupby('month')[['income','expense']].sum())
        st.subheader('Сүүлийн гүйлгээнүүд')
        st.dataframe(transactions.head(15), use_container_width=True, hide_index=True)
    else:
        st.info('Эхлээд банкны хуулга импортлоно уу.')

elif page == '📥 Хуулга импортлох':
    st.subheader('Банкны хуулга нэмэх')
    st.write('CSV эсвэл XLSX файл оруулна. Дахин импортлоход хуучин гүйлгээг устгахгүй, давхардлыг алгасна.')
    file = st.file_uploader('Хуулга сонгох (.csv, .xlsx)', type=['csv','xlsx'])
    if file:
        try:
            frame = read_file(file)
            if frame.empty: st.warning('Файл хоосон байна.')
            else:
                st.write('Өгөгдлийн эхний мөрүүд')
                st.dataframe(frame.head(8), use_container_width=True)
                cols = list(frame.columns)
                def pick(label, choices, default=0):
                    return st.selectbox(label, choices, index=default)
                a,b = st.columns(2)
                with a:
                    date_col = pick('Огнооны багана', cols)
                    desc_col = pick('Гүйлгээний утгын багана', cols, min(1,len(cols)-1))
                    category_col = pick('Ангиллын багана (байвал)', ['—'] + cols)
                with b:
                    amount_col = pick('Дүнгийн багана (эсвэл — сонгоно)', ['—'] + cols)
                    debit_col = pick('Зарлагын багана (дүнгийн багана — бол)', ['—'] + cols)
                    credit_col = pick('Орлогын багана (дүнгийн багана — бол)', ['—'] + cols)
                    reference_col = pick('Гүйлгээний ID / лавлагаа (байвал)', ['—'] + cols)
                if st.button('Импортлох', type='primary'):
                    if amount_col == '—' and (debit_col == '—' or credit_col == '—'):
                        st.error('Дүн эсвэл орлого/зарлагын хоёр баганыг сонгоно уу.')
                    else:
                        clean = normalize(frame, date_col, desc_col, amount_col, category_col,
                                          reference_col, debit_col, credit_col)
                        inserted = 0
                        with connect() as c:
                            for r in clean.itertuples(index=False):
                                res = c.execute('INSERT OR IGNORE INTO transactions(day,description,amount,category,source,fingerprint) VALUES (?,?,?,?,?,?)',
                                                (r.day, r.description, float(r.amount), r.category if r.category.strip() else 'Бусад', file.name, r.fingerprint))
                                inserted += res.rowcount
                        st.success(f'{inserted} шинэ гүйлгээ нэмлээ. {len(clean)-inserted} давхардал алгаслаа.')
                        st.rerun()
        except Exception as ex:
            st.error(f'Файл боловсруулахад алдаа: {ex}')
    st.caption('Анхааруулга: Импортын үед орлого эерэг (+), зарлага сөрөг (−) утгатай байна. Банкны багануудыг тохируулна уу.')

elif page == '🧾 Гүйлгээ ба ангилал':
    st.subheader('Гүйлгээний ангилал засах')
    if transactions.empty: st.info('Одоогоор гүйлгээ алга.')
    else:
        months = sorted(transactions.day.str[:7].unique(), reverse=True)
        selected = st.selectbox('Сар', months)
        subset = transactions[transactions.day.str.startswith(selected)].copy()
        category_opts = sorted(set(CATEGORIES) | set(transactions.category))
        edited = st.data_editor(subset, disabled=['id','day','description','amount','source'],
                                column_config={'category': st.column_config.SelectboxColumn('Ангилал', options=category_opts, required=True)},
                                hide_index=True, use_container_width=True, key='editor')
        if st.button('Ангилал хадгалах'):
            with connect() as c:
                for r in edited.itertuples(index=False):
                    c.execute('UPDATE transactions SET category=? WHERE id=?', (r.category, int(r.id)))
            st.success('Хадгаллаа.')
            st.rerun()
        st.download_button('Бүх гүйлгээг CSV болгох', transactions.to_csv(index=False).encode('utf-8-sig'),
                           'transactions.csv', 'text/csv')

elif page == '📅 Сарын төсөв':
    st.subheader('Дараа сарын төсөв ба гүйцэтгэл')
    today = date.today()
    next_month = month_shift(today.year, today.month)
    all_months = sorted(set(budgets.month) | set(transactions.day.str[:7]) | {next_month}, reverse=True)
    selected_month = st.selectbox('Төсөв харах/төлөвлөх сар', all_months,
                                  index=all_months.index(next_month))
    st.caption('Ангилал бүрийн төлөвлөсөн дээд зарлага болон импортолсон гүйцэтгэлийг харьцуулна.')
    categories = sorted(set(CATEGORIES) | set(transactions.category) | set(budgets.category))
    planned = budgets[budgets.month == selected_month].set_index('category').amount.to_dict()
    actual = (-transactions[(transactions.day.str.startswith(selected_month)) & (transactions.amount < 0)]
                .groupby('category').amount.sum()).to_dict()
    # The planned monthly contribution to goals is budgeted as a reserved saving.
    goal_saving = float(goals.monthly.sum()) if not goals.empty else 0.
    rows = [{'Ангилал': cat, 'Төсөв (₮)': float(planned.get(cat, 0)),
             'Гүйцэтгэл (₮)': float(actual.get(cat, 0))} for cat in categories]
    display = pd.DataFrame(rows)
    updated = st.data_editor(display, disabled=['Ангилал','Гүйцэтгэл (₮)'],
                            column_config={'Төсөв (₮)': st.column_config.NumberColumn(min_value=0, step=10000)},
                            use_container_width=True, hide_index=True)
    if st.button('Төсөв хадгалах', type='primary'):
        with connect() as c:
            for r in updated.itertuples(index=False, name=None):
                c.execute('INSERT INTO budgets(month,category,amount) VALUES (?,?,?) ON CONFLICT(month,category) DO UPDATE SET amount=excluded.amount',
                          (selected_month, str(r[0]), max(0, float(r[1]))))
        st.success('Төсөв хадгалагдлаа.')
        st.rerun()
    updated['Зөрүү (₮)'] = updated['Төсөв (₮)'] - updated['Гүйцэтгэл (₮)']
    st.write('**Төсөв ба бодит гүйцэтгэлийн зөрүү (+ = хэмнэлт):**')
    st.dataframe(updated, use_container_width=True, hide_index=True)
    a,b,c = st.columns(3)
    a.metric('Нийт төсөв', fmt(updated['Төсөв (₮)'].sum()))
    b.metric('Бодит зарлага', fmt(updated['Гүйцэтгэл (₮)'].sum()))
    c.metric('Зорилгод сар бүр хуваарилах', fmt(goal_saving))
    if goal_saving > 0:
        st.info('🎯 Зорилго бүрийн сарын хуримтлал төсвийн төлөвлөлттэй уялдсан. Хадгаламжийн зардлын мөрийг тусад нь нэмсэн бол давхар тооцохгүйгээр төлөвлөөрэй.')
        st.metric('Нийт төсөв + зорилгын хуримтлал (хадгаламжийг төсөвт оруулаагүй үед)', fmt(updated['Төсөв (₮)'].sum()+goal_saving))

elif page == '🎯 Урт хугацааны зорилго':
    st.subheader('Байр, машин болон бусад зорилго')
    st.write('Хуримтлал + сар бүрийн орлогоос хийх нэмэлт + хадгаламжийн өгөөж + зээлийн санхүүжилтийг хослуулан тооцно.')
    with st.form('add_goal', clear_on_submit=True):
        a,b = st.columns(2)
        with a:
            name = st.text_input('Зорилгын нэр', placeholder='Жишээ: 2 өрөө байр')
            target = st.number_input('Нийт үнэ (₮)', min_value=0., value=150_000_000., step=1_000_000.)
            current = st.number_input('Одоо хуримтлуулсан (₮)', min_value=0., value=10_000_000., step=100_000.)
            monthly = st.number_input('Сар бүр хуримтлуулах (₮)', min_value=0., value=1_000_000., step=100_000.)
        with b:
            annual_return = st.number_input('Хадгаламжийн жилийн өгөөж (%)', 0., 100., 10., 0.5)
            loan_pct = st.slider('Зээлээр санхүүжүүлэх хувь (%)', 0, 100, 70)
            loan_annual_rate = st.number_input('Зээлийн жилийн хүү (%)', 0., 100., 8., 0.5)
            loan_years = st.number_input('Зээлийн хугацаа (жил)', 0, 40, 20)
            deadline = st.date_input('Зорилтот огноо', value=date.today().replace(year=date.today().year+3))
        save = st.form_submit_button('Зорилго нэмэх', type='primary')
        if save:
            if not name.strip() or target <= 0:
                st.error('Зорилгын нэр болон үнийг зөв оруулна уу.')
            else:
                with connect() as c:
                    c.execute('INSERT INTO goals(name,target,current,monthly,annual_return,deadline,loan_pct,loan_annual_rate,loan_years) VALUES (?,?,?,?,?,?,?,?,?)',
                              (name.strip(),target,current,monthly,annual_return,deadline.isoformat(),loan_pct,loan_annual_rate,loan_years))
                st.success('Зорилго нэмлээ.')
                st.rerun()
    if goals.empty: st.info('Зорилго нэмэгдээгүй байна.')
    else:
        st.markdown('### Зорилгуудын тооцоолол')
        total_monthly = 0
        for row in goals.to_dict('records'):
            result = calculate_goal(row)
            total_monthly += row['monthly']
            with st.expander(f"🎯 {row['name']} — {fmt(row['target'])}", expanded=True):
                a,b,c = st.columns(3)
                a.metric('Урьдчилгаа/бэлнээр бүрдүүлэх', fmt(result['cash_needed']))
                b.metric('Нэмэлт бүрдүүлэх шаардлагатай', fmt(result['remaining']))
                c.metric('Зээлийн сарын төлөлт (ойролцоогоор)', fmt(result['loan_payment']))
                if result['months'] is None:
                    st.warning('Энэ төлөвлөгөөгөөр зорилгод хүрэх боломжгүй. Сар бүрийн хуримтлалаа нэмнэ үү.')
                else:
                    year = date.today().year + (date.today().month-1 + result['months']) // 12
                    month = (date.today().month-1 + result['months']) % 12 + 1
                    st.success(f"Төлөвлөгөөгөөр {result['months']} сарын дараа ({year:04d}-{month:02d}) бэлэн мөнгөний зорилтод хүрнэ.")
                    if row['deadline'] and f'{year:04d}-{month:02d}' > row['deadline'][:7]:
                        st.warning(f"Таны заасан {row['deadline']} хугацаанд амжихгүй байж болзошгүй.")
                if st.button('Зорилго устгах', key=f"delete_{row['id']}"):
                    with connect() as c: c.execute('DELETE FROM goals WHERE id=?', (int(row['id']),))
                    st.rerun()
        st.info(f'Эдгээр зорилгод сар бүр нийт **{fmt(total_monthly)}** хуваарилах шаардлагатай. Энэ дүнг «Сарын төсөв» хэсэгт харуулна.')
        st.caption('Зээлийн тооцоонд тэнцүү сарын төлбөр, тогтмол хүү ашигласан; шимтгэл, татвар, даатгал, үнийн өсөлт ороогүй.')
