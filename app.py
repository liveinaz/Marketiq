"""MarketIQ test application — research and simulated trading only."""
import datetime as dt
import hmac
import json
import hashlib
import logging
import math
import os
import re
import sqlite3
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import requests
import streamlit as st
from dotenv import load_dotenv

load_dotenv()
st.set_page_config(page_title='MarketIQ | AI Trade Research', page_icon='📈', layout='wide')
st.markdown('''<style>.block-container{padding-top:1rem;max-width:1300px}
div[data-testid="stMetric"]{background:#14263b;border-radius:12px;padding:14px}
div[data-testid="stMetric"] label,div[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#f2f6fa!important}
.stApp{background:#081522;color:#e9f0f8}
@media(max-width:640px){.block-container{padding-left:.75rem;padding-right:.75rem}h1{font-size:1.8rem!important}}
</style>''', unsafe_allow_html=True)

DB = Path(os.getenv('MARKETIQ_DB', str(Path(__file__).parent / 'marketiq.db')))
DEFAULT = ['AAPL','MSFT','NVDA','GOOGL','AMZN','META','TSLA','JPM','V','COST','SPY','QQQ']
UNIVERSES = {
    'Core 12': DEFAULT,
    'Technology & AI': ['NVDA','AMD','AVGO','MSFT','AAPL','GOOGL','META','AMZN','ORCL','CRM','ADBE','PLTR','INTC','MU','QCOM','ARM','SMCI','DELL','IBM','NOW'],
    'Finance & payments': ['JPM','BAC','WFC','C','GS','MS','SCHW','BLK','V','MA','AXP','PYPL','COIN','HOOD'],
    'Healthcare': ['UNH','JNJ','LLY','ABBV','MRK','PFE','AMGN','GILD','ISRG','TMO','VRTX','REGN'],
    'Consumer & industrial': ['COST','WMT','HD','LOW','NKE','MCD','SBUX','DIS','NFLX','TSLA','GM','F','CAT','DE','GE','BA','UPS','UBER'],
    'Energy & utilities': ['XOM','CVX','COP','SLB','OXY','EOG','NEE','DUK','SO','XLE'],
    'ETFs': ['SPY','QQQ','IWM','DIA','VTI','XLK','XLF','XLV','XLE','SMH'],
}

def secret(name):
    value = os.getenv(name, '')
    if not value:
        try:
            value = st.secrets.get(name, '')
        except Exception:
            pass
    return str(value or '').strip()

API = secret('FINNHUB_API_KEY')
OPENAI = secret('OPENAI_API_KEY')
SUPABASE_URL = secret('SUPABASE_URL').rstrip('/')
SUPABASE_SERVICE_KEY = secret('SUPABASE_SERVICE_ROLE_KEY')
REMOTE_BACKUP = bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
APP_VERSION = '2026.10.23-backup-controls'
logger = logging.getLogger('marketiq.backup')
APP_PASSWORD = secret('MARKETIQ_APP_PASSWORD')

if APP_PASSWORD:
    if not st.session_state.get('authenticated', False):
        st.title('🔒 MarketIQ')
        with st.form('login'):
            password = st.text_input('App password', type='password')
            submitted = st.form_submit_button('Unlock')
        if submitted:
            if hmac.compare_digest(password, APP_PASSWORD):
                st.session_state['authenticated'] = True
                st.rerun()
            else:
                st.error('Incorrect password')
        st.stop()
else:
    st.warning('No app password configured. Public visitors may change shared paper records. Configure MARKETIQ_APP_PASSWORD in Streamlit Secrets.')


def backup_failure(operation, exc):
    """Report safe diagnostics without exposing URLs, tokens or response bodies."""
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        response = exc.response
        status = response.status_code
        code = ''
        try:
            body = response.json()
            if isinstance(body, dict):
                code = str(body.get('code', ''))[:32]
        except (ValueError, TypeError):
            pass
        message = f'Supabase {operation} failed (HTTP {status}' + (f', code {code}' if code else '') + ')'
    else:
        message = f'Supabase {operation} failed ({type(exc).__name__})'
    logger.error('%s', message)
    st.session_state['backup_error'] = message


def backup_headers():
    return {'apikey': SUPABASE_SERVICE_KEY, 'Authorization': 'Bearer ' + SUPABASE_SERVICE_KEY, 'Content-Type': 'application/json'}


def local_snapshot(conn):
    """Create a consistent export of supported tables."""
    conn.commit()
    previous = conn.row_factory
    try:
        conn.row_factory = sqlite3.Row
        return {table: [dict(row) for row in conn.execute('SELECT * FROM ' + table)]
                for table in ('orders', 'watchlist', 'settings')}
    finally:
        conn.row_factory = previous


def valid_snapshot(data):
    """Reject incomplete or malformed snapshots rather than clearing tables."""
    if not isinstance(data, dict) or any(not isinstance(data.get(t), list) for t in ('orders', 'watchlist', 'settings')):
        return False
    fields = {'orders': ('id','ts','symbol','side','shares','price'),
              'watchlist': ('symbol',), 'settings': ('key','value')}
    return all(isinstance(row, dict) and all(key in row for key in fields[table])
               for table in fields for row in data[table])


def remote_snapshot():
    if not REMOTE_BACKUP:
        return None
    try:
        r = requests.get(SUPABASE_URL + '/rest/v1/marketiq_backup',
                         params={'id':'eq.1','select':'payload'}, headers=backup_headers(), timeout=12)
        r.raise_for_status()
        records = r.json()
        if not isinstance(records, list) or not records:
            st.session_state['backup_error'] = 'No cloud backup found.'
            return None
        data = records[0].get('payload') if isinstance(records[0], dict) else None
        if not valid_snapshot(data):
            st.session_state['backup_error'] = 'Cloud backup is incomplete or invalid; local records were not changed.'
            return None
        st.session_state.pop('backup_error', None)
        return data
    except (requests.RequestException, ValueError, KeyError, TypeError) as exc:
        backup_failure('read', exc)
        return None


def apply_snapshot(conn, data):
    if not valid_snapshot(data):
        st.session_state['backup_error'] = 'Invalid backup; no changes made.'
        return False
    try:
        with conn:
            for table in ('orders', 'watchlist', 'settings'):
                conn.execute('DELETE FROM ' + table)
                if table == 'orders':
                    conn.executemany('INSERT INTO orders(id,ts,symbol,side,shares,price,rationale) VALUES(?,?,?,?,?,?,?)',
                        [(x['id'],x['ts'],x['symbol'],x['side'],x['shares'],x['price'],x.get('rationale','')) for x in data[table]])
                elif table == 'watchlist':
                    conn.executemany('INSERT INTO watchlist(symbol) VALUES(?)', [(x['symbol'],) for x in data[table]])
                else:
                    conn.executemany('INSERT INTO settings(key,value) VALUES(?,?)',
                                     [(x['key'],x['value']) for x in data[table]])
        st.session_state.pop('backup_error', None)
        return True
    except (sqlite3.Error, KeyError, TypeError, ValueError) as exc:
        backup_failure('restore', exc)
        return False


def remote_load(conn):
    """Only auto-restore an empty local database, avoiding silent data loss."""
    existing = local_snapshot(conn)
    if any(existing[table] for table in ('orders','watchlist','settings')):
        st.session_state['restore_skipped'] = True
        return False
    data = remote_snapshot()
    if data is None:
        return False
    loaded = apply_snapshot(conn, data)
    if loaded:
        st.session_state['last_restore'] = dt.datetime.now(dt.timezone.utc).isoformat()
    return loaded


def remote_save(conn):
    if not REMOTE_BACKUP:
        return True
    try:
        data = local_snapshot(conn)
        r = requests.post(SUPABASE_URL + '/rest/v1/marketiq_backup',
                          headers={**backup_headers(),'Prefer':'resolution=merge-duplicates,return=minimal'},
                          params={'on_conflict':'id'}, json={'id':1,'payload':data}, timeout=15)
        r.raise_for_status()
        st.session_state['last_backup'] = dt.datetime.now(dt.timezone.utc).isoformat()
        st.session_state.pop('backup_error', None)
        return True
    except (requests.RequestException, ValueError, sqlite3.Error, TypeError) as exc:
        backup_failure('save', exc)
        return False


def db():
    conn = sqlite3.connect(DB, check_same_thread=False)
    conn.execute('CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, rationale TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    if REMOTE_BACKUP and not st.session_state.get('remote_initialized'):
        st.session_state['remote_initialized'] = True
        st.session_state['remote_loaded'] = remote_load(conn)
    return conn


def setting(conn, key, default):
    row = conn.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def save_changes(conn):
    conn.commit()
    return remote_save(conn)


@st.cache_data(ttl=180, show_spinner=False)
def api(path, params):
    if not API or API == 'your_finnhub_key_here':
        return None
    try:
        r = requests.get('https://finnhub.io/api/v1/' + path, params={**params,'token':API}, timeout=12)
        if r.status_code == 429:
            return {'_marketiq_error':'rate_limit'}
        if r.status_code != 200:
            return None
        return r.json()
    except (requests.RequestException, ValueError):
        return None


@st.cache_data(ttl=300, show_spinner=False)
def quote(sym):
    d = api('quote', {'symbol':sym})
    try:
        if isinstance(d, dict) and float(d.get('c') or 0) > 0:
            return {'price':float(d['c']), 'prev':float(d.get('pc') or d['c']), 'high':float(d.get('h') or 0), 'low':float(d.get('l') or 0), 'time':int(d.get('t') or 0)}
    except (TypeError, ValueError):
        pass
    return None


@st.cache_data(ttl=1800, show_spinner=False)
def fundamentals(sym):
    d = api('stock/metric', {'symbol':sym,'metric':'all'})
    return d.get('metric', {}) if isinstance(d, dict) else {}


@st.cache_data(ttl=900, show_spinner=False)
def news(sym):
    end = dt.date.today()
    start = end - dt.timedelta(days=10)
    d = api('company-news', {'symbol':sym,'from':str(start),'to':str(end)})
    return [x for x in d if isinstance(x,dict) and x.get('headline')][:15] if isinstance(d,list) else []


POS = ('beat','growth','upgrade','record','strong','surge','profit','partnership','raises','approval','expands','bullish')
NEG = ('miss','downgrade','lawsuit','investigation','decline','cuts','warning','loss','weak','recall','bearish','fraud')


def sentiment(items):
    if not items:
        return None
    values = []
    for n in items:
        s = (str(n.get('headline') or '') + ' ' + str(n.get('summary') or '')).lower()
        values.append(sum(w in s for w in POS) - sum(w in s for w in NEG))
    return float(np.clip(np.mean(values), -2, 2))


def finite_number(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (ValueError, TypeError):
        return None


def scoring(q, m, ns):
    p, prev = q['price'], q['prev']
    change = (p / prev - 1) * 100 if prev else 0
    fifty = finite_number(m.get('50DayMovingAverage'))
    two = finite_number(m.get('200DayMovingAverage'))
    pe = finite_number(m.get('peTTM'))
    growth = finite_number(m.get('revenueGrowthTTMYoy'))
    beta = finite_number(m.get('beta'))
    score = 50.0
    factors, risks = [], []
    if fifty and fifty > 0:
        delta = (p / fifty - 1) * 100
        score += np.clip(delta * 1.5, -12, 12)
        factors.append(f'Price vs 50-day average: {delta:+.1f}%')
    else:
        risks.append('50-day average unavailable')
    if two and two > 0:
        delta = (p / two - 1) * 100
        score += np.clip(delta * .5, -10, 10)
        factors.append(f'Price vs 200-day average: {delta:+.1f}%')
    if growth is not None:
        score += np.clip(growth * .3, -10, 10)
        factors.append(f'Revenue growth (YoY): {growth:+.1f}%')
    if pe is not None:
        if pe > 70:
            score -= 8
            risks.append('High trailing P/E')
        elif 0 < pe < 30:
            score += 4
        elif pe <= 0:
            score -= 7
            risks.append('Non-positive earnings / P/E')
        factors.append(f'Trailing P/E: {pe:.1f}')
    s = sentiment(ns)
    if s is not None:
        score += np.clip(s * 7, -12, 12)
        factors.append(f'Headline keyword sentiment: {s:+.2f} (rough indicator)')
    else:
        risks.append('No recent news feed available')
    if beta is not None and beta > 1.8:
        risks.append('High market sensitivity (beta)')
    score = int(round(np.clip(score, 0, 100)))
    signal = 'Research buy candidate' if score >= 68 else 'Review / potential exit' if score <= 35 else 'Watch / hold'
    return {'score':score,'signal':signal,'change':change,'factors':factors,'risks':risks,'sentiment':s}


def analysis(sym):
    q = quote(sym)
    if not q:
        return None
    m, ns = fundamentals(sym), news(sym)
    return {**scoring(q,m,ns), 'quote':q,'metric':m,'news':ns,'symbol':sym}


def ai_request(data, system, max_words=180):
    if not OPENAI:
        return 'Optional AI narrative is disabled. Add OPENAI_API_KEY in Streamlit Secrets.'
    try:
        payload = {'model':secret('OPENAI_MODEL') or 'gpt-4.1-mini', 'messages':[{'role':'system','content':system + f' Keep under {max_words} words.'},{'role':'user','content':json.dumps(data)}], 'temperature':0.2}
        r = requests.post('https://api.openai.com/v1/chat/completions', headers={'Authorization':'Bearer '+OPENAI,'Content-Type':'application/json'}, json=payload, timeout=30)
        r.raise_for_status()
        return r.json()['choices'][0]['message']['content']
    except requests.exceptions.HTTPError as e:
        code = e.response.status_code if e.response is not None else 'unknown'
        return f'AI provider returned HTTP {code}. Check API billing, key, model access and limits.'
    except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
        return 'AI request failed. Check connection and configuration.'


def explain_ai(a):
    data = {'symbol':a['symbol'],'price':a['quote']['price'],'signal':a['signal'],'score':a['score'],'factors':a['factors'],'risks':a['risks'],'headlines':[x.get('headline') for x in a['news'][:5]]}
    return ai_request(data, 'You are a cautious financial research assistant. Use ONLY supplied data, do not invent information, never promise returns, explicitly discuss uncertainty. This is research, not personalized investment advice.')


def quote_age(q):
    if not q or not q.get('time'):
        return None
    return max(0, (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(q['time'],dt.timezone.utc)).total_seconds() / 3600)


def stamp(q):
    return dt.datetime.fromtimestamp(q['time'],dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if q and q.get('time') else 'Unknown'


@st.cache_data(ttl=3600, show_spinner=False)
def price_history(sym, period):
    import urllib.parse
    allowed = {'1wk':'5d','1mo':'1mo','3mo':'3mo','6mo':'6mo','1yr':'1y'}
    if period not in allowed or not re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}',sym):
        return None
    try:
        url = 'https://query1.finance.yahoo.com/v8/finance/chart/' + urllib.parse.quote(sym.replace('.','-'),safe='')
        r = requests.get(url, params={'range':allowed[period],'interval':'1d'}, headers={'User-Agent':'Mozilla/5.0 MarketIQ research prototype'}, timeout=12)
        r.raise_for_status()
        result = r.json()['chart']['result'][0]
        frame = pd.DataFrame({'Date':pd.to_datetime(result['timestamp'],unit='s',utc=True), 'Close':pd.to_numeric(result['indicators']['quote'][0]['close'],errors='coerce')}).dropna()
        return frame.sort_values('Date').reset_index(drop=True) if len(frame) >= 2 else None
    except (requests.RequestException, KeyError, IndexError, ValueError, TypeError):
        return None


def chart_stock(sym):
    st.subheader('Price history')
    period = st.selectbox('Chart period', ['1wk','1mo','3mo','6mo','1yr'], index=2)
    history = price_history(sym, period)
    if history is None:
        st.info('Historical chart unavailable from the optional source. This does not mean the stock price is zero.')
        return
    fig = go.Figure()
    fig.add_trace(go.Scatter(x=history['Date'], y=history['Close'], name='Daily close', mode='lines', line={'width':2}))
    if len(history) >= 20:
        fig.add_trace(go.Scatter(x=history['Date'], y=history['Close'].rolling(20).mean(), name='20-day average', mode='lines', line={'dash':'dash'}))
    if len(history) >= 50:
        fig.add_trace(go.Scatter(x=history['Date'], y=history['Close'].rolling(50).mean(), name='50-day average', mode='lines', line={'dash':'dot'}))
    fig.update_layout(template='plotly_dark', height=330, margin=dict(l=8,r=8,t=12,b=8), yaxis_title='USD', legend=dict(orientation='h',y=-.2), paper_bgcolor='#081522', plot_bgcolor='#081522')
    st.plotly_chart(fig, width="stretch")
    st.caption('Historical daily closes from an unofficial third-party source; may be delayed or unavailable.')


def stock_metrics(a):
    m, q = a['metric'], a['quote']
    return {'Ticker':a['symbol'],'Price':q['price'],'Day %':round(a['change'],2),'Score':a['score'], 'Revenue growth %':finite_number(m.get('revenueGrowthTTMYoy')), 'P/E':finite_number(m.get('peTTM')), 'Beta':finite_number(m.get('beta')), 'Sentiment':a['sentiment'], 'Risks':len(a['risks']), '50d average':finite_number(m.get('50DayMovingAverage')), '200d average':finite_number(m.get('200DayMovingAverage'))}


def portfolio_snapshot(orders, start):
    cash, qty, basis, realized, history = float(start), {}, {}, 0.0, []
    for _, o in orders.iterrows():
        sym, n, price, side = str(o['symbol']), float(o['shares']), float(o['price']), o['side']
        previous, oldbasis = qty.get(sym,0.0), basis.get(sym,0.0)
        if side == 'BUY':
            cash -= n * price
            qty[sym], basis[sym] = previous+n, oldbasis+n*price
        elif side == 'SELL' and previous >= n > 0:
            avg = oldbasis / previous
            realized += n * (price-avg)
            cash += n * price
            qty[sym], basis[sym] = max(0.0,previous-n), max(0.0,oldbasis-n*avg)
        history.append({'Trade date':str(o['ts'])[:10],'Cash after trade':round(cash,2),'Realized P/L to date':round(realized,2)})
    return cash, qty, basis, realized, pd.DataFrame(history)


conn = db()
PAGES = ['Overview','Stock Scanner','Stock Research','Compare Stocks','News Desk','Paper Trading','Trade Journal','Settings']
with st.sidebar:
    st.title('📈 MarketIQ')
    st.caption('Research • Recommendations • Paper trading')
    page = st.radio('Navigate', PAGES, key='nav_page')
    st.divider()
    if API:
        st.success('Market data configured')
    else:
        st.warning('No market-data key. Add FINNHUB_API_KEY in Streamlit Secrets.')
    if REMOTE_BACKUP:
        st.success('Supabase credentials configured')
        if st.session_state.get('backup_error'):
            st.warning(st.session_state['backup_error'])
    else:
        st.warning('Local paper records may be erased by hosting restarts. Export journal regularly.')
    st.caption('No brokerage connection or live orders.')

saved_symbols = [x[0] for x in conn.execute('SELECT symbol FROM watchlist ORDER BY symbol')]
symbols = list(dict.fromkeys(DEFAULT + saved_symbols))
titles = {'Overview':'Market overview','Stock Scanner':'Stock scanner','Stock Research':'Company research','Compare Stocks':'Compare stocks','News Desk':'Market news desk','Paper Trading':'Paper trading','Trade Journal':'Trade journal','Settings':'App settings'}
st.title(titles[page])
st.caption('Research only • Quotes may be delayed, incomplete, or stale • Trades must be entered independently at Fidelity')

if page == 'Settings':
    st.subheader('Data & security')
    st.write('Configure FINNHUB_API_KEY, optional OPENAI_API_KEY and MARKETIQ_APP_PASSWORD in Streamlit Secrets; never commit credentials to GitHub.')
    if APP_PASSWORD:
        st.success('Password protection enabled')
    else:
        st.error('No app password configured. Unsafe for shared personal trading records.')
    if REMOTE_BACKUP:
        st.success('Supabase credentials detected (connection not yet verified)')
        if st.session_state.get('backup_error'):
            st.error(st.session_state['backup_error'])
    else:
        st.warning('No persistent remote backup configured. See README for Supabase setup.')
    st.subheader('Backup & restore')
    st.caption('Version: ' + APP_VERSION + ' • Supabase stores one current snapshot; historical cloud versions are not yet enabled.')
    local_data = local_snapshot(conn)
    st.write(f"Local records: {len(local_data['watchlist'])} custom tickers, "
             f"{len(local_data['orders'])} paper trades, {len(local_data['settings'])} settings.")
    st.download_button('Download local backup JSON',
                       json.dumps(local_data, indent=2, default=str),
                       'marketiq_local_backup.json', 'application/json', key='download_local_backup')
    if st.session_state.get('last_backup'):
        st.success('Last confirmed cloud save this session: ' + st.session_state['last_backup'])
    if st.session_state.get('last_restore'):
        st.info('Last cloud restore this session: ' + st.session_state['last_restore'])
    if st.session_state.get('restore_skipped'):
        st.info('Automatic restore skipped because local records already existed. This prevents accidental replacement.')
    if REMOTE_BACKUP:
        if st.button('Test Supabase and preview cloud backup'):
            st.session_state['cloud_preview'] = remote_snapshot()
            st.session_state['restore_confirm'] = False
        cloud_data = st.session_state.get('cloud_preview')
        if cloud_data is not None:
            st.success('Supabase connection verified; cloud backup is readable.')
            st.write(f"Cloud records: {len(cloud_data['watchlist'])} custom tickers, "
                     f"{len(cloud_data['orders'])} paper trades, {len(cloud_data['settings'])} settings.")
            st.write('Cloud tickers: ' + (', '.join(x['symbol'] for x in cloud_data['watchlist']) or '(none)'))
            st.download_button('Download cloud backup JSON',
                               json.dumps(cloud_data, indent=2, default=str),
                               'marketiq_cloud_backup.json', 'application/json', key='download_cloud_backup')
            if st.button('Prepare manual restore'):
                st.session_state['restore_confirm'] = True
            if st.session_state.get('restore_confirm'):
                st.error('Restoring REPLACES local paper trades, watchlist and settings with the cloud snapshot. Download your local backup first.')
                acknowledged = st.checkbox('I understand that local data will be replaced', key='restore_ack')
                if st.button('Confirm restore from Supabase', disabled=not acknowledged):
                    if apply_snapshot(conn, cloud_data):
                        st.session_state['last_restore'] = dt.datetime.now(dt.timezone.utc).isoformat()
                        st.session_state['restore_confirm'] = False
                        st.session_state.pop('cloud_preview', None)
                        st.success('Restored from Supabase.')
                        st.rerun()
        if st.button('Back up local data to Supabase now'):
            if remote_save(conn):
                st.success('Cloud backup saved successfully.')
            else:
                st.error(st.session_state.get('backup_error', 'Cloud backup failed.'))
    else:
        st.warning('Supabase is not configured. Download a local backup regularly.')
    st.subheader('Custom watchlist')
    new = st.text_input('Add ticker', placeholder='e.g. AMD').strip().upper()
    if st.button('Add to watchlist') and new:
        if re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}',new):
            conn.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(new,))
            saved = save_changes(conn)
            if REMOTE_BACKUP and not saved:
                st.error('Saved locally, but remote backup failed.')
            else:
                st.rerun()
        else:
            st.error('Invalid ticker')
    st.write(', '.join(saved_symbols) if saved_symbols else 'No custom tickers yet. The Core 12 remain available.')
    if saved_symbols:
        remove = st.selectbox('Remove custom ticker',saved_symbols)
        if st.button('Remove selected'):
            conn.execute('DELETE FROM watchlist WHERE symbol=?',(remove,))
            saved = save_changes(conn)
            if REMOTE_BACKUP and not saved:
                st.error('Removed locally, but remote backup failed.')
            else:
                st.rerun()
    st.subheader('Paper account')
    bal = st.number_input('Initial simulated cash ($)', min_value=100.0, value=float(setting(conn,'starting_cash','25000')), step=1000.0)
    if st.button('Save starting cash'):
        if conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:
            st.error('Reset trades before changing starting cash.')
        else:
            conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',('starting_cash',str(bal)))
            saved = save_changes(conn)
            st.success('Saved' if saved else 'Saved locally; remote backup failed.')
    if st.button('Reset paper account'):
        st.session_state['confirm_reset'] = True
    if st.session_state.get('confirm_reset'):
        st.warning('This permanently deletes all simulated trades.')
        if st.button('Confirm delete all paper trades'):
            conn.execute('DELETE FROM orders')
            saved = save_changes(conn)
            st.session_state['confirm_reset'] = False
            if REMOTE_BACKUP and not saved:
                st.error('Deleted locally, but remote backup failed.')
            else:
                st.rerun()

elif page in ('Overview','Stock Scanner'):
    st.info('Select a curated universe or your custom watchlist. This is not a whole-market scan. Scores are unvalidated heuristics, not probabilities.')
    choices = ['Core 12','My custom watchlist'] + list(UNIVERSES.keys())[1:] + ['All curated (large)']
    universe = st.selectbox('Stock universe', choices)
    if universe == 'My custom watchlist':
        pool = saved_symbols
    elif universe == 'All curated (large)':
        pool = list(dict.fromkeys(x for values in UNIVERSES.values() for x in values))
    else:
        pool = UNIVERSES[universe]
    if not pool:
        st.info('Add tickers in Settings to use your custom watchlist.')
    batch = st.slider('Maximum stocks per scan', 5, 15, 12, 1)
    st.caption('Scans are capped at 15 symbols to reduce free-tier API rate-limit risk. Each stock can require up to three Finnhub requests.')
    selected = st.multiselect('Choose tickers', pool, default=pool[:min(batch,len(pool))], key='scan_selected')
    if len(selected) > batch:
        st.warning(f'Only the first {batch} selected tickers will be scanned.')
    if st.button('Refresh market data'):
        quote.clear()
        api.clear()
        st.rerun()
    rows = []
    with st.spinner('Checking available market data...'):
        for sym in selected[:batch]:
            a = analysis(sym)
            if a:
                age = quote_age(a['quote'])
                fields = {k:v for k,v in stock_metrics(a).items() if k in ('Revenue growth %','P/E','Beta','Sentiment')}
                rows.append({'Ticker':sym,'Price':a['quote']['price'],'Day %':round(a['change'],2),'Score / 100':a['score'],'Research signal':a['signal'],'Quote UTC':stamp(a['quote']),'Age (hours)':round(age,1) if age is not None else None,'Data gaps':len(a['risks']),**fields})
    if rows:
        df = pd.DataFrame(rows)
        c1, c2, c3 = st.columns(3)
        minscore = c1.slider('Minimum score', 0, 100, 0)
        minprice = c2.number_input('Minimum price ($)', min_value=0.0, value=0.0, step=5.0)
        maxprice = c3.number_input('Maximum price ($; 0 = no limit)', min_value=0.0, value=0.0, step=10.0)
        direction = st.selectbox('Daily movement', ['Any','Gainers only','Decliners only'])
        research_filter = st.selectbox('Opportunity filter', ['All','Positive revenue growth','P/E between 0 and 30','Positive news sentiment','Lower beta (under 1.5)','Score at least 68'])
        sortby = st.selectbox('Rank results by', ['Research score','Daily gain','Revenue growth','Lowest positive P/E'])
        df = df[(df['Score / 100'] >= minscore) & (df['Price'] >= minprice)]
        if maxprice > 0:
            df = df[df['Price'] <= maxprice]
        if direction == 'Gainers only':
            df = df[df['Day %'] > 0]
        elif direction == 'Decliners only':
            df = df[df['Day %'] < 0]
        if research_filter == 'Positive revenue growth':
            df = df[df['Revenue growth %'] > 0]
        elif research_filter == 'P/E between 0 and 30':
            df = df[(df['P/E'] > 0) & (df['P/E'] < 30)]
        elif research_filter == 'Positive news sentiment':
            df = df[df['Sentiment'] > 0]
        elif research_filter == 'Lower beta (under 1.5)':
            df = df[df['Beta'] < 1.5]
        elif research_filter == 'Score at least 68':
            df = df[df['Score / 100'] >= 68]
        if sortby == 'Daily gain':
            df = df.sort_values('Day %',ascending=False)
        elif sortby == 'Revenue growth':
            df = df.sort_values('Revenue growth %',ascending=False,na_position='last')
        elif sortby == 'Lowest positive P/E':
            df = df.assign(_pe=df['P/E'].where(df['P/E'] > 0,float('inf'))).sort_values('_pe').drop(columns='_pe')
        else:
            df = df.sort_values('Score / 100',ascending=False)
        if page == 'Overview':
            a, b, c = st.columns(3)
            a.metric('Stocks matching',len(df))
            b.metric('Research candidates',int((df['Score / 100'] >= 68).sum()))
            c.metric('Top score',int(df['Score / 100'].max()) if len(df) else '—')
        st.dataframe(df,width="stretch",hide_index=True)
        if not df.empty:
            st.selectbox('Research a scanner result',df['Ticker'].tolist(),key='scan_research_ticker')

            def open_detailed_research():
                ticker = st.session_state['scan_research_ticker']
                st.session_state['research_ticker'] = ticker
                st.session_state['nav_page'] = 'Stock Research'

            st.button('Open detailed research →',type='primary',on_click=open_detailed_research)
        st.download_button('Export scanner CSV',df.to_csv(index=False),'marketiq_scanner.csv','text/csv')
        if any((r['Age (hours)'] or 0) > 24 for r in rows):
            st.warning('Some quotes are over 24 hours old. This can happen after weekends/holidays; verify before trading.')
        if len(rows) < len(selected[:batch]):
            st.warning('Some tickers returned no valid quote. Check symbols or wait before retrying due to API limits.')
    else:
        st.warning('No valid quotes. Check Finnhub API key, tickers, or API quota.')

elif page in ('Stock Research','News Desk'):
    pool = list(dict.fromkeys(symbols + [s for v in UNIVERSES.values() for s in v]))
    selected_research = st.session_state.pop('research_ticker',None)
    if selected_research and selected_research not in pool:
        pool = [selected_research] + pool
    # Apply scanner selection before constructing the widget, avoiding Streamlit state errors.
    if selected_research and st.session_state.get('research_selector') != selected_research:
        st.session_state['research_selector'] = selected_research
    if st.session_state.get('research_selector') not in pool:
        st.session_state['research_selector'] = pool[0]
    sym = st.selectbox('Select ticker',pool,key='research_selector')
    custom = st.text_input('Or enter any ticker',placeholder='e.g. AMD').strip().upper()
    if custom:
        if re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}',custom):
            sym = custom
        else:
            st.error('Ticker format invalid')
    a = analysis(sym)
    if not a:
        st.error('No quote available. Check ticker and market-data quota.')
    else:
        q = a['quote']
        c1,c2,c3 = st.columns(3)
        c1.metric(sym+' price',f"${q['price']:,.2f}",f"{a['change']:+.2f}%")
        c2.metric('Research score',f"{a['score']}/100")
        c3.metric('Research signal',a['signal'])
        st.caption('Quote as of ' + stamp(q))
        age = quote_age(q)
        if age is None or age > 24:
            st.warning('Quote timestamp missing or older than 24 hours. Verify current price.')
        if page == 'Stock Research':
            chart_stock(sym)
            st.subheader('Why this score?')
            for factor in a['factors']:
                st.write('• ' + factor)
            if a['risks']:
                st.subheader('Data gaps and risks')
                for risk in a['risks']:
                    st.write('• ' + risk)
            st.caption('Rule-based score is not a probability of profit. Targets and stops are not fabricated.')
            if st.button('Generate optional AI research narrative'):
                st.info(explain_ai(a))
            st.link_button('Open Fidelity — place trades manually','https://www.fidelity.com/')
        st.subheader('Recent company headlines')
        if not a['news']:
            st.warning('No news available for this ticker or plan.')
        for item in a['news'][:12]:
            title = str(item.get('headline') or 'Untitled')
            url = str(item.get('url') or '')
            if url.startswith('https://'):
                st.markdown(f'**[{title}]({url})**')
            else:
                st.write('**' + title + '**')
            timestamp = item.get('datetime')
            try:
                date = dt.datetime.fromtimestamp(timestamp,dt.timezone.utc).strftime('%Y-%m-%d') if timestamp else 'Date unknown'
            except (TypeError, ValueError, OverflowError):
                date = 'Date unknown'
            st.caption(str(item.get('source') or 'Source unknown') + ' • ' + date)

elif page == 'Compare Stocks':
    st.caption('Compare two or three tickers. Data may be stale or incomplete.')
    universe = list(dict.fromkeys(symbols + [t for values in UNIVERSES.values() for t in values]))
    picks = st.multiselect('Stocks to compare',universe,default=['NVDA','MSFT'],max_selections=3)
    manual = st.text_input('Additional ticker (optional; replaces last selection if already at three)',placeholder='e.g. SMCI').strip().upper()
    if manual and re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}',manual):
        picks = list(dict.fromkeys((picks[:2] if len(picks) >= 3 else picks) + [manual]))
    elif manual:
        st.error('Ticker format invalid')
    if len(picks) < 2:
        st.info('Choose at least two stocks to compare.')
    else:
        valid = [a for a in (analysis(t) for t in picks) if a]
        if len(valid) < 2:
            st.warning('At least two valid quotes needed. Check tickers and API limits.')
        else:
            comp = pd.DataFrame([stock_metrics(a) for a in valid]).set_index('Ticker').T
            st.dataframe(comp,width="stretch")
            st.caption('Missing fields are unknown, not zero. Scores are heuristic, not expected returns.')
            for a in valid:
                with st.expander(a['symbol'] + ' — supporting factors and risks'):
                    st.write('**Factors:** ' + ('; '.join(a['factors']) or 'No factors available'))
                    st.write('**Risks:** ' + ('; '.join(a['risks']) or 'No flagged risks'))
                    st.caption('Quote as of ' + stamp(a['quote']))
            if st.button('Generate AI comparison'):
                data = [{'ticker':a['symbol'],'score':a['score'],'price':a['quote']['price'],'factors':a['factors'],'risks':a['risks'],'sentiment':a['sentiment']} for a in valid]
                st.info(ai_request(data,'Compare the supplied stocks using only the provided data. Describe strengths, weaknesses, data gaps and uncertainty. Do not invent price targets or promise a winner.',250))

elif page in ('Paper Trading','Trade Journal'):
    start = float(setting(conn,'starting_cash','25000'))
    orders = pd.read_sql_query('SELECT * FROM orders ORDER BY id ASC',conn)
    cash, hold, costs, realized, trade_timeline = portfolio_snapshot(orders,start)
    holdings, market_value, missing, unrealized = [], 0.0, [], 0.0
    for s, qty in hold.items():
        if qty <= 1e-8:
            continue
        q = quote(s)
        if q:
            market_value += qty * q['price']
            unrealized += qty * q['price'] - costs.get(s,0.0)
        else:
            missing.append(s)
        holdings.append({'Ticker':s,'Shares':round(qty,5),'Cost basis $':round(costs.get(s,0.0),2),'Last quote $':q['price'] if q else None,'Market value $':round(qty*q['price'],2) if q else None,'Quote UTC':stamp(q)})
    c1,c2,c3 = st.columns(3)
    c1.metric('Simulated cash',f'${cash:,.2f}')
    c2.metric('Priced holdings',f'${market_value:,.2f}')
    c3.metric('Account value*',f'${cash+market_value:,.2f}' if not missing else 'Incomplete')
    if missing:
        st.warning('Account value incomplete: missing quotes for ' + ', '.join(missing))
    p1,p2,p3 = st.columns(3)
    p1.metric('Realized P/L',f'${realized:+,.2f}')
    p2.metric('Unrealized P/L*',f'${unrealized:+,.2f}' if not missing else 'Incomplete')
    p3.metric('Total P/L*',f'${cash+market_value-start:+,.2f}' if not missing else 'Incomplete')
    if len(trade_timeline):
        st.subheader('Paper account activity')
        st.line_chart(trade_timeline.set_index('Trade date')[['Cash after trade','Realized P/L to date']])
        st.caption('Cash and realized P/L after trades, NOT a historical portfolio equity curve.')
    st.caption('*Simulated value based on available quotes; excludes commissions, spreads, slippage, dividends and taxes.')
    if holdings:
        portfolio_df = pd.DataFrame(holdings)
        portfolio_df['Unrealized P/L $'] = portfolio_df['Market value $'] - portfolio_df['Cost basis $']
        st.dataframe(portfolio_df,hide_index=True,width="stretch")
        st.download_button('Export holdings CSV',portfolio_df.to_csv(index=False),'marketiq_holdings.csv','text/csv')
    if page == 'Paper Trading':
        st.subheader('Record simulated order')
        paper_options = list(dict.fromkeys(symbols + [t for values in UNIVERSES.values() for t in values] + [s for s,n in hold.items() if n > 0]))
        sym = st.selectbox('Ticker',paper_options,key='paper_ticker')
        q = quote(sym)
        if st.session_state.get('price_ticker') != sym:
            st.session_state['paper_price'] = float(q['price']) if q else 100.0
            st.session_state['price_ticker'] = sym
        if st.button('Use latest available quote'):
            if q:
                st.session_state['paper_price'] = float(q['price'])
                st.rerun()
        st.caption('Quote timestamp: ' + stamp(q) + ' • Quotes may be delayed')
        with st.form('paper_order'):
            side = st.radio('Action',['BUY','SELL'],horizontal=True)
            qty = st.number_input('Shares',min_value=0.0001,value=1.0,step=1.0,format='%.4f')
            price = st.number_input('Simulated execution price ($)',min_value=0.01,key='paper_price',step=0.01)
            reason = st.text_area('Trade rationale')
            if st.form_submit_button('Record PAPER trade',type='primary'):
                if side == 'BUY' and qty*price > cash+1e-6:
                    st.error('Insufficient simulated cash.')
                elif side == 'SELL' and qty > hold.get(sym,0.0)+1e-6:
                    st.error('Insufficient simulated shares; short selling disabled.')
                else:
                    conn.execute('INSERT INTO orders(ts,symbol,side,shares,price,rationale) VALUES(?,?,?,?,?,?)',(dt.datetime.now(dt.timezone.utc).isoformat(),sym,side,qty,price,reason))
                    saved = save_changes(conn)
                    if REMOTE_BACKUP and not saved:
                        st.error('Trade recorded locally, but remote backup failed. Export journal now.')
                    else:
                        st.success('Paper trade recorded — no brokerage order placed.')
                    st.rerun()
        st.link_button('Open Fidelity separately','https://www.fidelity.com/')
    st.subheader('Simulated trade history')
    if len(orders):
        if not REMOTE_BACKUP:
            st.warning('Download journal regularly. Hosting restarts can erase local storage.')
        st.dataframe(orders.sort_values('id',ascending=False),hide_index=True,width="stretch")
        st.download_button('Download journal CSV',orders.to_csv(index=False),'marketiq_paper_journal.csv','text/csv')
    else:
        st.info('No simulated trades yet.')

st.divider()
st.caption('MarketIQ prototype • Not investment advice • No performance guarantee • Never executes live orders')
