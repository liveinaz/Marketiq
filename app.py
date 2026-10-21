import os, sqlite3, datetime as dt, json, math, time, io
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import streamlit as st
import plotly.graph_objects as go
from dotenv import load_dotenv
load_dotenv()

st.set_page_config(page_title='MarketIQ | AI Trade Research', page_icon='📈', layout='wide', initial_sidebar_state='expanded')
st.markdown('''<style>.block-container{padding-top:1rem;max-width:1300px}div[data-testid="stMetric"]{background:#14263b;border-radius:12px;padding:14px}div[data-testid="stMetric"] label,div[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#f2f6fa!important} .stApp{background:#081522;color:#e9f0f8}h1,h2,h3{letter-spacing:-.03em}div.stButton>button{border-radius:9px}</style>''', unsafe_allow_html=True)
DB = Path(os.environ.get('MARKETIQ_DB', str(Path(__file__).parent/'marketiq.db')))
DEFAULT=['AAPL','MSFT','NVDA','GOOGL','AMZN','META','TSLA','JPM','V','COST','SPY','QQQ']
API=os.getenv('FINNHUB_API_KEY','').strip()
OPENAI=os.getenv('OPENAI_API_KEY','').strip()
SUPABASE_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPABASE_SERVICE_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','')
REMOTE_BACKUP=bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)

def backup_headers():
    return {'apikey':SUPABASE_SERVICE_KEY,'Authorization':'Bearer '+SUPABASE_SERVICE_KEY,'Content-Type':'application/json'}

def remote_load():
    if not REMOTE_BACKUP:return False
    try:
        r=requests.get(SUPABASE_URL+'/rest/v1/marketiq_backup',params={'id':'eq.1','select':'payload'},headers=backup_headers(),timeout=12)
        r.raise_for_status()
        if not r.json():return False
        data=r.json()[0]['payload']
        with sqlite3.connect(DB) as c:
            for table in ('orders','watchlist','settings'):
                c.execute('DELETE FROM '+table)
                if table=='orders':
                    c.executemany('INSERT INTO orders(id,ts,symbol,side,shares,price,rationale) VALUES(?,?,?,?,?,?,?)',[(x['id'],x['ts'],x['symbol'],x['side'],x['shares'],x['price'],x['rationale']) for x in data.get(table,[])])
                elif table=='watchlist':c.executemany('INSERT INTO watchlist(symbol) VALUES(?)',[(x['symbol'],) for x in data.get(table,[])])
                else:c.executemany('INSERT INTO settings(key,value) VALUES(?,?)',[(x['key'],x['value']) for x in data.get(table,[])])
        return True
    except Exception:return False

def remote_save(conn):
    if not REMOTE_BACKUP:return True
    try:
        conn.commit()
        conn.row_factory=sqlite3.Row
        data={table:[dict(row) for row in conn.execute('SELECT * FROM '+table)] for table in ('orders','watchlist','settings')}
        r=requests.post(SUPABASE_URL+'/rest/v1/marketiq_backup',headers={**backup_headers(),'Prefer':'resolution=merge-duplicates,return=minimal'},json={'id':1,'payload':data},timeout=15)
        r.raise_for_status()
        return True
    except Exception:return False


def db():
    conn=sqlite3.connect(DB,check_same_thread=False)
    conn.execute('CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, rationale TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    if REMOTE_BACKUP and not st.session_state.get('remote_initialized'):
        st.session_state['remote_initialized']=True
        st.session_state['remote_loaded']=remote_load()
    return conn

def setting(conn,key,default):
    r=conn.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone()
    return r[0] if r else default

def putsetting(conn,key,val):
    conn.execute('INSERT OR REPLACE INTO settings VALUES(?,?)',(key,str(val)));conn.commit();remote_save(conn)

@st.cache_data(ttl=180,show_spinner=False)
def api(path,params):
    if not API or API=='your_finnhub_key_here':return None
    try:
        r=requests.get('https://finnhub.io/api/v1/'+path,params={**params,'token':API},timeout=12)
        if r.status_code!=200:return None
        return r.json()
    except (requests.RequestException,ValueError):return None

@st.cache_data(ttl=300,show_spinner=False)
def quote(sym):
    d=api('quote',{'symbol':sym})
    if isinstance(d,dict) and float(d.get('c') or 0)>0:
        return {'price':float(d['c']),'prev':float(d.get('pc') or d['c']),'high':float(d.get('h') or 0),'low':float(d.get('l') or 0),'time':int(d.get('t') or 0),'live':True}
    return None

@st.cache_data(ttl=1800,show_spinner=False)
def fundamentals(sym):
    d=api('stock/metric',{'symbol':sym,'metric':'all'})
    return d.get('metric',{}) if isinstance(d,dict) else {}

@st.cache_data(ttl=900,show_spinner=False)
def news(sym):
    end=dt.date.today(); start=end-dt.timedelta(days=10)
    d=api('company-news',{'symbol':sym,'from':str(start),'to':str(end)})
    if isinstance(d,list):return [x for x in d if x.get('headline')][:15]
    return []

POS=('beat','growth','upgrade','record','strong','surge','profit','partnership','raises','approval','expands','bullish')
NEG=('miss','downgrade','lawsuit','investigation','decline','cuts','warning','loss','weak','recall','bearish','fraud')
def sentiment(items):
    if not items:return None
    vals=[]
    for n in items:
        s=(n.get('headline','')+' '+n.get('summary','')).lower()
        vals.append(sum(w in s for w in POS)-sum(w in s for w in NEG))
    return float(np.clip(np.mean(vals),-2,2))

def scoring(q,m,ns):
    # Transparent heuristic; no predictive probability claim.
    p=q['price']; prev=q['prev']; change=(p/prev-1)*100 if prev else 0
    fifty=m.get('50DayMovingAverage');two=m.get('200DayMovingAverage'); pe=m.get('peTTM'); growth=m.get('revenueGrowthTTMYoy'); beta=m.get('beta')
    score=50.; factors=[]; risks=[]
    if fifty and fifty>0:
        d=(p/fifty-1)*100; score+=np.clip(d*1.5,-12,12);factors.append(f'Price vs 50-day average: {d:+.1f}%')
    else: risks.append('50-day average unavailable')
    if two and two>0:
        d=(p/two-1)*100;score+=np.clip(d*.5,-10,10);factors.append(f'Price vs 200-day average: {d:+.1f}%')
    if growth is not None:
        try: score+=np.clip(float(growth)*.3,-10,10);factors.append(f'Revenue growth (YoY): {float(growth):+.1f}%')
        except (ValueError,TypeError):pass
    if pe is not None:
        try:
            pe=float(pe)
            if pe>70:score-=8;risks.append('High trailing P/E')
            elif 0<pe<30:score+=4
            elif pe<=0:score-=7;risks.append('Non-positive earnings / P/E')
            factors.append(f'Trailing P/E: {pe:.1f}')
        except (ValueError,TypeError):pass
    s=sentiment(ns)
    if s is not None:score+=np.clip(s*7,-12,12);factors.append(f'Headline keyword sentiment: {s:+.2f} (rough indicator)')
    else:risks.append('No recent news feed available')
    if beta is not None:
        try:
            if float(beta)>1.8:risks.append('High market sensitivity (beta)')
        except (ValueError,TypeError):pass
    score=int(round(np.clip(score,0,100)))
    if score>=68:signal='Research buy candidate'
    elif score<=35:signal='Review / potential exit'
    else:signal='Watch / hold'
    return {'score':score,'signal':signal,'change':change,'factors':factors,'risks':risks,'sentiment':s}

def analysis(sym):
    q=quote(sym)
    if not q:return None
    m=fundamentals(sym);ns=news(sym)
    return {**scoring(q,m,ns),'quote':q,'metric':m,'news':ns,'symbol':sym}

def explain_ai(a):
    if not OPENAI:return 'Optional AI narrative is disabled. Add OPENAI_API_KEY to enable it; the transparent rule-based score is still available.'
    try:
        payload={'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'messages':[{'role':'system','content':'You are a cautious financial research assistant. Use ONLY supplied data, do not invent information, never promise returns, explicitly discuss uncertainty. This is research, not personalized investment advice. Keep under 180 words.'},{'role':'user','content':json.dumps({'symbol':a['symbol'],'price':a['quote']['price'],'signal':a['signal'],'score':a['score'],'factors':a['factors'],'risks':a['risks'],'headlines':[x.get('headline') for x in a['news'][:5]]})}],'temperature':0.2}
        r=requests.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':'Bearer '+OPENAI,'Content-Type':'application/json'},json=payload,timeout=30)
        r.raise_for_status();return r.json()['choices'][0]['message']['content']
    except Exception as e:return 'AI summary unavailable; check your API configuration and network connection.'

conn=db()
with st.sidebar:
    st.title('📈 MarketIQ')
    st.caption('Research • Recommendations • Paper trading')
    page=st.radio('Navigate',['Overview','Stock Scanner','Stock Research','News Desk','Paper Trading','Trade Journal','Settings'])
    st.divider()
    if not API or API=='your_finnhub_key_here':st.warning('No market-data key. Add FINNHUB_API_KEY in Settings or .env to load prices.')
    else:st.success('Market data configured')
    st.caption('No Fidelity credentials required. No live brokerage orders are possible.')
    if REMOTE_BACKUP:st.success('Remote backup configured')
    else:st.warning('Paper records are stored on temporary hosting. Export the journal regularly; configure remote backup before relying on history.')

symbols=[x[0] for x in conn.execute('SELECT symbol FROM watchlist ORDER BY symbol').fetchall()] or DEFAULT
st.title({'Overview':'Market overview','Stock Scanner':'Stock scanner','Stock Research':'Company research','News Desk':'Market news desk','Paper Trading':'Paper trading','Trade Journal':'Trade journal','Settings':'App settings'}[page])
st.caption('Research only • Quotes may be delayed, incomplete, or stale • All real trades must be entered independently at Fidelity')

if page=='Settings':
    st.subheader('Connect market data')
    st.markdown('Get a Finnhub API key at [finnhub.io](https://finnhub.io). Set `FINNHUB_API_KEY` in your host environment or `.env` file, then restart. Free-tier rate limits and news access vary. Never paste brokerage credentials here.')
    st.markdown('Optional: set `OPENAI_API_KEY` for AI-written research summaries. The core scoring engine works without it.')
    st.subheader('Persistent paper-trade backup')
    if REMOTE_BACKUP:st.success('Supabase backup configured. Keep your app private: service-role keys must never be in GitHub.')
    else:st.warning('This deployment uses temporary SQLite storage. To enable persistent backups, create the Supabase table described in README and add SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY in Streamlit Secrets. Do not share the app publicly with personal trading data.')
    st.subheader('Watchlist')
    new=st.text_input('Add ticker',placeholder='e.g. AMD').strip().upper()
    if st.button('Add to watchlist') and new:
        if new.replace('.','').replace('-','').isalnum() and len(new)<=12:
            conn.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(new,));conn.commit();remote_save(conn);st.rerun()
        else:st.error('Invalid symbol')
    st.write(', '.join(symbols))
    remove=st.selectbox('Remove ticker',symbols)
    if st.button('Remove selected'):
        for s in symbols:conn.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(s,))
        conn.execute('DELETE FROM watchlist WHERE symbol=?',(remove,));conn.commit();remote_save(conn);st.rerun()
    st.subheader('Paper account')
    bal=st.number_input('Initial simulated cash ($)',min_value=100.,value=float(setting(conn,'starting_cash','25000')),step=1000.)
    if st.button('Save starting cash'):
        if conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:st.error('Cannot change starting cash after trades have been recorded. Reset paper account first.')
        else:putsetting(conn,'starting_cash',bal);st.success('Saved')
    if st.button('Reset paper account',type='secondary'):
        st.session_state['confirm_reset']=True
    if st.session_state.get('confirm_reset'):
        st.warning('This permanently deletes your simulated trades.')
        if st.button('Confirm delete all paper trades'):
            conn.execute('DELETE FROM orders');conn.commit();remote_save(conn);st.session_state['confirm_reset']=False;st.rerun()

elif page in ('Overview','Stock Scanner'):
    st.info('Scanner ranks only the selected watchlist/universe, not the entire U.S. market. Scores are transparent heuristics, not AI probabilities or proven predictions.')
    chosen=st.multiselect('Scan tickers',symbols,default=symbols[:min(len(symbols),12)])
    if st.button('Refresh market data'):api.clear();quote.clear();fundamentals.clear();news.clear();st.rerun()
    rows=[]
    with st.spinner('Checking available market data...'):
        for sym in chosen:
            a=analysis(sym)
            if a:rows.append({'Ticker':sym,'Price':a['quote']['price'],'Day %':round(a['change'],2),'Score / 100':a['score'],'Research signal':a['signal'],'Quote UTC':dt.datetime.fromtimestamp(a['quote']['time'],dt.timezone.utc).strftime('%Y-%m-%d %H:%M') if a['quote']['time'] else 'Unknown'})
    if rows:
        df=pd.DataFrame(rows).sort_values('Score / 100',ascending=False)
        if page=='Overview':
            c1,c2,c3=st.columns(3);c1.metric('Stocks checked',len(df));c2.metric('Research candidates',int((df['Score / 100']>=68).sum()));c3.metric('Top score',int(df['Score / 100'].max()))
        st.dataframe(df,use_container_width=True,hide_index=True)
        st.download_button('Export scanner CSV',df.to_csv(index=False),'marketiq_scanner.csv','text/csv')
    else:st.warning('No valid quotes returned. Configure Finnhub in Settings, verify tickers and API quota, then retry.')
    st.caption('A quote timestamp indicates when the data provider last updated it; it does not guarantee real-time exchange coverage.')

elif page in ('Stock Research','News Desk'):
    sym=st.selectbox('Select ticker',symbols)
    a=analysis(sym)
    if not a:st.error('No quote available. Add a valid market-data API key and try again.')
    else:
        q=a['quote'];c1,c2,c3=st.columns(3);c1.metric(sym+' price',f"${q['price']:,.2f}",f"{a['change']:+.2f}%");c2.metric('Research score',f"{a['score']}/100");c3.metric('Research signal',a['signal'])
        st.caption('Quote as of '+(dt.datetime.fromtimestamp(q['time'],dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if q['time'] else 'unknown time'))
        if page=='Stock Research':
            st.subheader('Why this score?')
            for f in a['factors']:st.write('• '+f)
            if a['risks']:
                st.subheader('Data gaps and risks')
                for r in a['risks']:st.write('• '+r)
            st.caption('Rule-based scoring is NOT a probability of profit. Price targets/stops are intentionally not fabricated.')
            if st.button('Generate optional AI research narrative'):st.info(explain_ai(a))
            st.link_button('Open Fidelity — place trades manually','https://www.fidelity.com/')
        st.subheader('Recent company headlines')
        if not a['news']:st.warning('No news available from the provider for this ticker or plan.')
        for item in a['news'][:12]:
            title=item.get('headline','Untitled');url=item.get('url','')
            if url.startswith('https://'):st.markdown(f'**[{title}]({url})**')
            else:st.write('**'+title+'**')
            st.caption(item.get('source','Source unknown')+' • '+(dt.datetime.fromtimestamp(item.get('datetime',0),dt.timezone.utc).strftime('%Y-%m-%d') if item.get('datetime') else 'Date unknown'))

elif page in ('Paper Trading','Trade Journal'):
    start=float(setting(conn,'starting_cash','25000'))
    orders=pd.read_sql_query('SELECT * FROM orders ORDER BY id ASC',conn)
    cash=start;hold={};costs={};
    for _,o in orders.iterrows():
        s=o['symbol'];qty=float(o['shares']);price=float(o['price']);side=o['side'];old=hold.get(s,0.)
        if side=='BUY':
            cash-=qty*price;hold[s]=old+qty;costs[s]=costs.get(s,0.)+qty*price
        else:
            cash+=qty*price;hold[s]=old-qty
            costs[s]=max(0.,costs.get(s,0.)-qty*(costs.get(s,0.)/old if old>0 else 0))
    holdings=[];market_value=0.;missing=[]
    for s,qty in hold.items():
        if qty<=1e-8:continue
        q=quote(s)
        if q:market_value+=qty*q['price']
        else:missing.append(s)
        holdings.append({'Ticker':s,'Shares':round(qty,5),'Cost basis $':round(costs.get(s,0.),2),'Last quote $':q['price'] if q else None,'Market value $':round(qty*q['price'],2) if q else None})
    c1,c2,c3=st.columns(3);c1.metric('Simulated cash',f'${cash:,.2f}');c2.metric('Priced holdings',f'${market_value:,.2f}');c3.metric('Account value*',f'${cash+market_value:,.2f}')
    if missing:st.warning('Account value is incomplete: missing current quotes for '+', '.join(missing))
    st.caption('*Simulated valuation based on latest available provider quotes; excludes commissions, spreads, slippage, dividends, and taxes.')
    if holdings:st.dataframe(pd.DataFrame(holdings),hide_index=True,use_container_width=True)
    if page=='Paper Trading':
        st.subheader('Record simulated order')
        sym=st.selectbox('Ticker',symbols,key='paper_ticker')
        q=quote(sym)
        if st.session_state.get('price_ticker') != sym:
            st.session_state['paper_price']=float(q['price']) if q else 100.
            st.session_state['price_ticker']=sym
        if st.button('Use latest available quote',help='Resets the editable simulated price to the latest provider quote'):
            if q:st.session_state['paper_price']=float(q['price']);st.rerun()
        st.caption('Quote timestamp: '+(dt.datetime.fromtimestamp(q['time'],dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if q and q['time'] else 'unavailable')+' • Quotes may be delayed')
        with st.form('paper_order'):
            side=st.radio('Action',['BUY','SELL'],horizontal=True)
            qty=st.number_input('Shares',min_value=0.0001,value=1.,step=1.,format='%.4f')
            price=st.number_input('Simulated execution price ($)',min_value=0.01,key='paper_price',step=0.01)
            reason=st.text_area('Trade rationale',placeholder='What data supports this decision?')
            if st.form_submit_button('Record PAPER trade',type='primary'):
                if side=='BUY' and qty*price>cash+1e-6:st.error('Insufficient simulated cash.')
                elif side=='SELL' and qty>hold.get(sym,0.)+1e-6:st.error('Insufficient simulated shares; short selling disabled.')
                else:
                    conn.execute('INSERT INTO orders(ts,symbol,side,shares,price,rationale) VALUES(?,?,?,?,?,?)',(dt.datetime.now(dt.timezone.utc).isoformat(),sym,side,qty,price,reason));conn.commit();saved=remote_save(conn);st.success('Paper trade recorded — no brokerage order was placed.');
                    if REMOTE_BACKUP and not saved:st.error('Remote backup failed. Download your journal CSV before leaving.')
                    st.rerun()
        st.link_button('Open Fidelity separately','https://www.fidelity.com/')
    st.subheader('Simulated trade history')
    if len(orders):
        if not REMOTE_BACKUP:st.warning('Download your journal CSV regularly. Streamlit local storage can be erased during restarts.')
        st.dataframe(orders.sort_values('id',ascending=False),hide_index=True,use_container_width=True)
        st.download_button('Download journal CSV',orders.to_csv(index=False),'marketiq_paper_journal.csv','text/csv')
    else:st.info('No simulated trades yet.')

st.divider();st.caption('MarketIQ prototype • Not investment advice • No guarantee of performance • No connection to Fidelity accounts • Never executes live orders')
