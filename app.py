import os, sqlite3, datetime as dt, json, time, re
from pathlib import Path
import numpy as np
import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
load_dotenv()

st.set_page_config(page_title='MarketIQ | AI Trade Research', page_icon='📈', layout='wide')
st.markdown('''<style>.block-container{padding-top:1rem;max-width:1300px}div[data-testid="stMetric"]{background:#14263b;border-radius:12px;padding:14px}div[data-testid="stMetric"] label,div[data-testid="stMetric"] [data-testid="stMetricValue"]{color:#f2f6fa!important}.stApp{background:#081522;color:#e9f0f8}</style>''',unsafe_allow_html=True)
DB=Path(os.getenv('MARKETIQ_DB',str(Path(__file__).parent/'marketiq.db')))
DEFAULT=['AAPL','MSFT','NVDA','GOOGL','AMZN','META','TSLA','JPM','V','COST','SPY','QQQ']
UNIVERSES={
 'Core 12':DEFAULT,
 'Technology & AI':['NVDA','AMD','AVGO','MSFT','AAPL','GOOGL','META','AMZN','ORCL','CRM','ADBE','PLTR','INTC','MU','QCOM','ARM','SMCI','DELL','IBM','NOW'],
 'Finance & payments':['JPM','BAC','WFC','C','GS','MS','SCHW','BLK','V','MA','AXP','PYPL','COIN','HOOD'],
 'Healthcare':['UNH','JNJ','LLY','ABBV','MRK','PFE','AMGN','GILD','ISRG','TMO','VRTX','REGN'],
 'Consumer & industrial':['COST','WMT','HD','LOW','NKE','MCD','SBUX','DIS','NFLX','TSLA','GM','F','CAT','DE','GE','BA','UPS','UBER'],
 'Energy & utilities':['XOM','CVX','COP','SLB','OXY','EOG','NEE','DUK','SO','XLE'],
 'ETFs':['SPY','QQQ','IWM','DIA','VTI','XLK','XLF','XLV','XLE','SMH']}
API=os.getenv('FINNHUB_API_KEY','').strip()
OPENAI=os.getenv('OPENAI_API_KEY','').strip()
SUPABASE_URL=os.getenv('SUPABASE_URL','').rstrip('/')
SUPABASE_SERVICE_KEY=os.getenv('SUPABASE_SERVICE_ROLE_KEY','')
REMOTE_BACKUP=bool(SUPABASE_URL and SUPABASE_SERVICE_KEY)
APP_PASSWORD=os.getenv('MARKETIQ_APP_PASSWORD','').strip()
if APP_PASSWORD:
    import hmac
    if not st.session_state.get('authenticated'):
        st.title('🔒 MarketIQ')
        with st.form('login'):
            password=st.text_input('App password',type='password')
            submitted=st.form_submit_button('Unlock')
        if submitted:
            if hmac.compare_digest(password,APP_PASSWORD):
                st.session_state['authenticated']=True;st.rerun()
            else:st.error('Incorrect password')
        st.stop()
else:
    st.warning('This app has no access password. If shared publicly, other visitors can change shared paper records. Set MARKETIQ_APP_PASSWORD in Streamlit Secrets before storing personal trading information.')

def backup_headers():
    return {'apikey':SUPABASE_SERVICE_KEY,'Authorization':'Bearer '+SUPABASE_SERVICE_KEY,'Content-Type':'application/json'}

def remote_load(conn):
    if not REMOTE_BACKUP:return False
    try:
        r=requests.get(SUPABASE_URL+'/rest/v1/marketiq_backup',params={'id':'eq.1','select':'payload'},headers=backup_headers(),timeout=12)
        r.raise_for_status()
        records=r.json()
        if not records:return False
        data=records[0]['payload']
        with conn:
            for table in ('orders','watchlist','settings'):
                conn.execute('DELETE FROM '+table)
                if table=='orders':
                    conn.executemany('INSERT INTO orders(id,ts,symbol,side,shares,price,rationale) VALUES(?,?,?,?,?,?,?)',[(x['id'],x['ts'],x['symbol'],x['side'],x['shares'],x['price'],x['rationale']) for x in data.get(table,[])])
                elif table=='watchlist':conn.executemany('INSERT INTO watchlist(symbol) VALUES(?)',[(x['symbol'],) for x in data.get(table,[])])
                else:conn.executemany('INSERT INTO settings(key,value) VALUES(?,?)',[(x['key'],x['value']) for x in data.get(table,[])])
        return True
    except Exception:return False

def remote_save(conn):
    if not REMOTE_BACKUP:return True
    try:
        conn.commit()
        conn.row_factory=sqlite3.Row
        data={table:[dict(row) for row in conn.execute('SELECT * FROM '+table)] for table in ('orders','watchlist','settings')}
        r=requests.post(SUPABASE_URL+'/rest/v1/marketiq_backup',headers={**backup_headers(),'Prefer':'resolution=merge-duplicates,return=minimal'},json={'id':1,'payload':data},timeout=15)
        r.raise_for_status();return True
    except Exception:return False

def db():
    conn=sqlite3.connect(DB,check_same_thread=False)
    conn.execute('CREATE TABLE IF NOT EXISTS watchlist(symbol TEXT PRIMARY KEY)')
    conn.execute('CREATE TABLE IF NOT EXISTS orders(id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, symbol TEXT, side TEXT, shares REAL, price REAL, rationale TEXT)')
    conn.execute('CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    if REMOTE_BACKUP and not st.session_state.get('remote_initialized'):
        st.session_state['remote_initialized']=True
        st.session_state['remote_loaded']=remote_load(conn)
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
        return {'price':float(d['c']),'prev':float(d.get('pc') or d['c']),'high':float(d.get('h') or 0),'low':float(d.get('l') or 0),'time':int(d.get('t') or 0)}
    return None

@st.cache_data(ttl=1800,show_spinner=False)
def fundamentals(sym):
    d=api('stock/metric',{'symbol':sym,'metric':'all'})
    return d.get('metric',{}) if isinstance(d,dict) else {}

@st.cache_data(ttl=900,show_spinner=False)
def news(sym):
    end=dt.date.today();start=end-dt.timedelta(days=10)
    d=api('company-news',{'symbol':sym,'from':str(start),'to':str(end)})
    return [x for x in d if x.get('headline')][:15] if isinstance(d,list) else []

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
    p=q['price'];prev=q['prev'];change=(p/prev-1)*100 if prev else 0
    fifty=m.get('50DayMovingAverage');two=m.get('200DayMovingAverage');pe=m.get('peTTM');growth=m.get('revenueGrowthTTMYoy');beta=m.get('beta')
    score=50.;factors=[];risks=[]
    if fifty and fifty>0:
        d=(p/fifty-1)*100;score+=np.clip(d*1.5,-12,12);factors.append(f'Price vs 50-day average: {d:+.1f}%')
    else:risks.append('50-day average unavailable')
    if two and two>0:
        d=(p/two-1)*100;score+=np.clip(d*.5,-10,10);factors.append(f'Price vs 200-day average: {d:+.1f}%')
    if growth is not None:
        try:score+=np.clip(float(growth)*.3,-10,10);factors.append(f'Revenue growth (YoY): {float(growth):+.1f}%')
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
    signal='Research buy candidate' if score>=68 else 'Review / potential exit' if score<=35 else 'Watch / hold'
    return {'score':score,'signal':signal,'change':change,'factors':factors,'risks':risks,'sentiment':s}

def analysis(sym):
    q=quote(sym)
    if not q:return None
    m=fundamentals(sym);ns=news(sym)
    return {**scoring(q,m,ns),'quote':q,'metric':m,'news':ns,'symbol':sym}

def explain_ai(a):
    if not OPENAI:return 'Optional AI narrative is disabled. Add OPENAI_API_KEY to enable it.'
    try:
        payload={'model':os.getenv('OPENAI_MODEL','gpt-4.1-mini'),'messages':[{'role':'system','content':'You are a cautious financial research assistant. Use ONLY supplied data, do not invent information, never promise returns, explicitly discuss uncertainty. This is research, not personalized investment advice. Keep under 180 words.'},{'role':'user','content':json.dumps({'symbol':a['symbol'],'price':a['quote']['price'],'signal':a['signal'],'score':a['score'],'factors':a['factors'],'risks':a['risks'],'headlines':[x.get('headline') for x in a['news'][:5]]})}],'temperature':0.2}
        r=requests.post('https://api.openai.com/v1/chat/completions',headers={'Authorization':'Bearer '+OPENAI,'Content-Type':'application/json'},json=payload,timeout=30)
        r.raise_for_status();return r.json()['choices'][0]['message']['content']
    except requests.exceptions.HTTPError as e:
        code=e.response.status_code if e.response is not None else 'unknown'
        return f'AI provider returned HTTP {code}. Check API billing, key, model access, and limits. (Credentials and raw response are not displayed.)'
    except Exception as e:return f'AI request failed ({type(e).__name__}). Check connectivity and configuration.'

def quote_age(q):
    if not q or not q.get('time'):return None
    return max(0,(dt.datetime.now(dt.timezone.utc)-dt.datetime.fromtimestamp(q['time'],dt.timezone.utc)).total_seconds()/3600)

def stamp(q):
    return dt.datetime.fromtimestamp(q['time'],dt.timezone.utc).strftime('%Y-%m-%d %H:%M UTC') if q and q.get('time') else 'Unknown'

conn=db()
with st.sidebar:
    st.title('📈 MarketIQ');st.caption('Research • Recommendations • Paper trading')
    page=st.radio('Navigate',['Overview','Stock Scanner','Stock Research','News Desk','Paper Trading','Trade Journal','Settings'])
    st.divider()
    if not API:st.warning('No market-data key. Add FINNHUB_API_KEY in Streamlit Secrets.')
    else:st.success('Market data configured')
    if REMOTE_BACKUP:st.success('Remote backup configured')
    else:st.warning('Local paper records may be erased by hosting restarts. Export journal regularly.')
    st.caption('No brokerage connection or live orders.')

saved_symbols=[x[0] for x in conn.execute('SELECT symbol FROM watchlist ORDER BY symbol')]
symbols=list(dict.fromkeys(DEFAULT+saved_symbols))
st.title({'Overview':'Market overview','Stock Scanner':'Stock scanner','Stock Research':'Company research','News Desk':'Market news desk','Paper Trading':'Paper trading','Trade Journal':'Trade journal','Settings':'App settings'}[page])
st.caption('Research only • Quotes may be delayed, incomplete, or stale • Trades must be entered independently at Fidelity')

if page=='Settings':
    st.subheader('Data & security')
    st.write('Set FINNHUB_API_KEY, optional OPENAI_API_KEY, and recommended MARKETIQ_APP_PASSWORD in Streamlit Secrets; never commit credentials to GitHub.')
    if APP_PASSWORD:st.success('Password protection enabled')
    else:st.error('No app password configured. This deployment is unsafe for shared personal trading records.')
    if REMOTE_BACKUP:st.success('Supabase backup configured')
    else:st.warning('No persistent remote backup configured. See README for Supabase setup.')
    st.subheader('Custom watchlist')
    new=st.text_input('Add ticker',placeholder='e.g. AMD').strip().upper()
    if st.button('Add to watchlist') and new:
        if re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}',new):
            conn.execute('INSERT OR IGNORE INTO watchlist VALUES(?)',(new,));conn.commit();remote_save(conn);st.rerun()
        else:st.error('Invalid ticker')
    st.write(', '.join(saved_symbols) if saved_symbols else 'No custom tickers yet. The Core 12 remain available.')
    if saved_symbols:
        remove=st.selectbox('Remove custom ticker',saved_symbols)
        if st.button('Remove selected'):
            conn.execute('DELETE FROM watchlist WHERE symbol=?',(remove,));conn.commit();remote_save(conn);st.rerun()
    st.subheader('Paper account')
    bal=st.number_input('Initial simulated cash ($)',min_value=100.,value=float(setting(conn,'starting_cash','25000')),step=1000.)
    if st.button('Save starting cash'):
        if conn.execute('SELECT COUNT(*) FROM orders').fetchone()[0]:st.error('Reset trades before changing starting cash.')
        else:putsetting(conn,'starting_cash',bal);st.success('Saved')
    if st.button('Reset paper account'):st.session_state['confirm_reset']=True
    if st.session_state.get('confirm_reset'):
        st.warning('This permanently deletes all simulated trades.')
        if st.button('Confirm delete all paper trades'):
            conn.execute('DELETE FROM orders');conn.commit();remote_save(conn);st.session_state['confirm_reset']=False;st.rerun()

elif page in ('Overview','Stock Scanner'):
    st.info('Select a curated universe, or your custom watchlist. This is not a whole-market scan. Scores are unvalidated heuristics, not probabilities.')
    choices=['Core 12','My custom watchlist']+list(UNIVERSES.keys())[1:]+['All curated (large)']
    universe=st.selectbox('Stock universe',choices)
    if universe=='My custom watchlist':pool=saved_symbols
    elif universe=='All curated (large)':pool=list(dict.fromkeys(x for values in UNIVERSES.values() for x in values))
    else:pool=UNIVERSES[universe]
    if not pool:st.info('Add tickers in Settings to use your custom watchlist.')
    batch=st.slider('Maximum stocks per scan',5,40,12,1)
    st.caption('Each stock can require up to three market-data requests. Large scans may hit Finnhub limits. Cached results are reused when possible.')
    selected=st.multiselect('Choose tickers',pool,default=pool[:min(batch,len(pool))])
    if len(selected)>batch:st.warning(f'Only the first {batch} selected tickers will be scanned.')
    if st.button('Refresh market data'):
        api.clear();quote.clear();fundamentals.clear();news.clear();st.rerun()
    rows=[]
    with st.spinner('Checking available market data...'):
        for sym in selected[:batch]:
            a=analysis(sym)
            if a:
                age=quote_age(a['quote'])
                rows.append({'Ticker':sym,'Price':a['quote']['price'],'Day %':round(a['change'],2),'Score / 100':a['score'],'Research signal':a['signal'],'Quote UTC':stamp(a['quote']),'Age (hours)':round(age,1) if age is not None else None,'Data gaps':len(a['risks'])})
    if rows:
        df=pd.DataFrame(rows)
        c1,c2,c3=st.columns(3)
        minscore=c1.slider('Minimum score',0,100,0)
        minprice=c2.number_input('Minimum price ($)',min_value=0.,value=0.,step=5.)
        maxprice=c3.number_input('Maximum price ($; 0 = no limit)',min_value=0.,value=0.,step=10.)
        direction=st.selectbox('Daily movement',['Any','Gainers only','Decliners only'])
        df=df[(df['Score / 100']>=minscore)&(df['Price']>=minprice)]
        if maxprice>0:df=df[df['Price']<=maxprice]
        if direction=='Gainers only':df=df[df['Day %']>0]
        elif direction=='Decliners only':df=df[df['Day %']<0]
        df=df.sort_values('Score / 100',ascending=False)
        if page=='Overview':
            a,b,c=st.columns(3);a.metric('Stocks matching',len(df));b.metric('Research candidates',int((df['Score / 100']>=68).sum()));c.metric('Top score',int(df['Score / 100'].max()) if len(df) else '—')
        st.dataframe(df,use_container_width=True,hide_index=True)
        st.download_button('Export scanner CSV',df.to_csv(index=False),'marketiq_scanner.csv','text/csv')
        if any((r['Age (hours)'] or 0)>24 for r in rows):st.warning('Some quotes are more than 24 hours old. This may be normal after weekends/holidays, but verify before making decisions.')
        if len(rows)<len(selected[:batch]):st.warning('Some selected tickers returned no valid quote (possibly API limits or unavailable symbols).')
    else:st.warning('No valid quotes. Check Finnhub API key, tickers, or API quota.')

elif page in ('Stock Research','News Desk'):
    pool=list(dict.fromkeys(symbols+[s for v in UNIVERSES.values() for s in v]))
    sym=st.selectbox('Select ticker',pool)
    custom=st.text_input('Or enter any ticker',placeholder='e.g. AMD').strip().upper()
    if custom:
        if re.fullmatch(r'[A-Z0-9][A-Z0-9.\-]{0,11}',custom):sym=custom
        else:st.error('Ticker format invalid')
    a=analysis(sym)
    if not a:st.error('No quote available. Check ticker and market-data quota.')
    else:
        q=a['quote'];c1,c2,c3=st.columns(3)
        c1.metric(sym+' price',f"${q['price']:,.2f}",f"{a['change']:+.2f}%")
        c2.metric('Research score',f"{a['score']}/100");c3.metric('Research signal',a['signal'])
        st.caption('Quote as of '+stamp(q))
        age=quote_age(q)
        if age is None or age>24:st.warning('Quote timestamp missing or older than 24 hours. Verify current price.')
        if page=='Stock Research':
            st.subheader('Why this score?')
            for f in a['factors']:st.write('• '+f)
            if a['risks']:
                st.subheader('Data gaps and risks')
                for r in a['risks']:st.write('• '+r)
            st.caption('Rule-based score is not a probability of profit. Targets and stops are not fabricated.')
            if st.button('Generate optional AI research narrative'):st.info(explain_ai(a))
            st.link_button('Open Fidelity — place trades manually','https://www.fidelity.com/')
        st.subheader('Recent company headlines')
        if not a['news']:st.warning('No news available for this ticker or plan.')
        for item in a['news'][:12]:
            title=item.get('headline','Untitled');url=item.get('url','')
            if url.startswith('https://'):st.markdown(f'**[{title}]({url})**')
            else:st.write('**'+title+'**')
            st.caption(item.get('source','Source unknown')+' • '+(dt.datetime.fromtimestamp(item.get('datetime',0),dt.timezone.utc).strftime('%Y-%m-%d') if item.get('datetime') else 'Date unknown'))

elif page in ('Paper Trading','Trade Journal'):
    start=float(setting(conn,'starting_cash','25000'))
    orders=pd.read_sql_query('SELECT * FROM orders ORDER BY id ASC',conn)
    cash=start;hold={};costs={}
    for _,o in orders.iterrows():
        s=o['symbol'];qty=float(o['shares']);price=float(o['price']);side=o['side'];old=hold.get(s,0.)
        if side=='BUY':cash-=qty*price;hold[s]=old+qty;costs[s]=costs.get(s,0.)+qty*price
        else:
            cash+=qty*price;hold[s]=old-qty
            costs[s]=max(0.,costs.get(s,0.)-qty*(costs.get(s,0.)/old if old>0 else 0))
    holdings=[];market_value=0.;missing=[]
    for s,qty in hold.items():
        if qty<=1e-8:continue
        q=quote(s)
        if q:market_value+=qty*q['price']
        else:missing.append(s)
        holdings.append({'Ticker':s,'Shares':round(qty,5),'Cost basis $':round(costs.get(s,0.),2),'Last quote $':q['price'] if q else None,'Market value $':round(qty*q['price'],2) if q else None,'Quote UTC':stamp(q)})
    c1,c2,c3=st.columns(3)
    c1.metric('Simulated cash',f'${cash:,.2f}');c2.metric('Priced holdings',f'${market_value:,.2f}');c3.metric('Account value*',f'${cash+market_value:,.2f}')
    if missing:st.warning('Account value incomplete: missing quotes for '+', '.join(missing))
    st.caption('*Simulated value based on available quotes; excludes commissions, spreads, slippage, dividends, and taxes.')
    if holdings:st.dataframe(pd.DataFrame(holdings),hide_index=True,use_container_width=True)
    if page=='Paper Trading':
        st.subheader('Record simulated order')
        sym=st.selectbox('Ticker',symbols,key='paper_ticker');q=quote(sym)
        if st.session_state.get('price_ticker')!=sym:
            st.session_state['paper_price']=float(q['price']) if q else 100.
            st.session_state['price_ticker']=sym
        if st.button('Use latest available quote'):
            if q:st.session_state['paper_price']=float(q['price']);st.rerun()
        st.caption('Quote timestamp: '+stamp(q)+' • Quotes may be delayed')
        with st.form('paper_order'):
            side=st.radio('Action',['BUY','SELL'],horizontal=True)
            qty=st.number_input('Shares',min_value=0.0001,value=1.,step=1.,format='%.4f')
            price=st.number_input('Simulated execution price ($)',min_value=0.01,key='paper_price',step=0.01)
            reason=st.text_area('Trade rationale')
            if st.form_submit_button('Record PAPER trade',type='primary'):
                if side=='BUY' and qty*price>cash+1e-6:st.error('Insufficient simulated cash.')
                elif side=='SELL' and qty>hold.get(sym,0.)+1e-6:st.error('Insufficient simulated shares; short selling disabled.')
                else:
                    conn.execute('INSERT INTO orders(ts,symbol,side,shares,price,rationale) VALUES(?,?,?,?,?,?)',(dt.datetime.now(dt.timezone.utc).isoformat(),sym,side,qty,price,reason))
                    conn.commit();saved=remote_save(conn)
                    if REMOTE_BACKUP and not saved:st.error('Remote backup failed. Download journal CSV.')
                    else:st.success('Paper trade recorded — no brokerage order placed.')
                    st.rerun()
        st.link_button('Open Fidelity separately','https://www.fidelity.com/')
    st.subheader('Simulated trade history')
    if len(orders):
        if not REMOTE_BACKUP:st.warning('Download journal regularly. Hosting restarts can erase local storage.')
        st.dataframe(orders.sort_values('id',ascending=False),hide_index=True,use_container_width=True)
        st.download_button('Download journal CSV',orders.to_csv(index=False),'marketiq_paper_journal.csv','text/csv')
    else:st.info('No simulated trades yet.')

st.divider();st.caption('MarketIQ prototype • Not investment advice • No performance guarantee • Never executes live orders')
