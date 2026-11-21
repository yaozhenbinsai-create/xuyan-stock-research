"""Explicit isolated UI fixture. Zero external data/model requests; cleans its own schema."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import json,os,tempfile,threading,time,uuid
from datetime import date,timedelta
from unittest.mock import patch
import uvicorn
from foundation import assets,configuration,database as store
from research import engine,model_transport,originals
from tools.mysql_instance import create_isolated_database,drop_isolated_database

def main():
    schema='stock_test_ui_'+uuid.uuid4().hex[:8]
    stop_file=Path(os.environ.get('STOCK_UI_STOP_FILE',store.data_root()/'stop-isolated-ui'))
    if stop_file.exists():stop_file.unlink()
    create_isolated_database(schema)
    try:
        with tempfile.TemporaryDirectory(prefix='stock-ui-fixture-') as tmp,patch.dict(os.environ,{'STOCK_DATA_ROOT':tmp,'STOCK_MYSQL_DATABASE':schema,'STOCK_DISABLE_WORKER':'1','STOCK_UI_ACCEPTANCE':'1'}):
            from tools.admin_account import main as setup_admin
            setup_admin()
            days=[];d=date.fromisoformat(store.today())
            if store.now()[11:16]<'15:00':
                d-=timedelta(days=1)
            while len(days)<65:
                if d.weekday()<5:days.append(d.isoformat())
                d-=timedelta(days=1)
            days.reverse();today=days[-1];calendar=days[:]
            d=date.fromisoformat(today)+timedelta(days=1)
            while len(calendar)<75:
                if d.weekday()<5:calendar.append(d.isoformat())
                d+=timedelta(days=1)
            assets.publish('calendar',[{'day':x,'symbol':'*','trading':True,'markets':['sh','sz']} for x in calendar],'bs_calendar')
            assets.publish('basics',[{'day':today,'symbol':'sh.600000','name':'隔离示例公司','status':'1','board':'隔离样本'}],'bs_universe')
            bars=[]
            for i,x in enumerate(days):
                previous=10+(i-1)*.03 if i else 10;close=10+i*.03
                if i==64:close*=1.05
                bars.append({'day':x,'symbol':'sh.600000','open':previous,'preclose':previous,'high':max(close,previous)+.01,'low':min(close,previous)-.01,'close':close,'volume':300 if i==64 else 100,'amount':3000 if i==64 else 1000,'is_st':False,'suspended':False,'adjust':'raw','volume_unit':'shares'})
            assets.publish('daily',bars,'bs_daily_all')
            assets.publish('announcements',[{'day':today,'symbol':'sh.600000','key':'fixture-filing','title':'2026年第一季度报告 · 隔离样本',
                            'published':today+'T11:00:00+08:00','url':'https://static.cninfo.com.cn/fixture.pdf',
                            'text':'【PDF 第 5 页】\n单位：万元\n营业收入 1500\n主营业务为储能设备生产。'}],'cninfo_text')
            m=configuration.save('models',{'name':'隔离模型替身','model_identifier':'fixture-only','protocol':'openai','base_url':'https://example.invalid/v1','api_key':'isolated-not-real'})
            configuration.save('plans',{'name':'隔离流程方案','model_ids':[m['id']]})
            from test_research import model_report
            from foundation.main import app
            def answer(rid,plan,system,payload):
                r=model_report(payload['bundle'],review_state='remove' if payload['kind']=='review' else 'observe')
                return store.dumps(r),'isolated-no-request'
            sample=f'<meta name="ArticleTitle" content="隔离政策原件"><meta name="PubDate" content="{today}"><div id="UCAP-CONTENT"><p>'+('推进储能设备产业的示例政策表述。'*12)+'</p></div>'
            with patch.object(model_transport,'complete',side_effect=answer),patch.object(originals,'_download',return_value=sample):
                run=engine.submit({'idempotency_key':uuid.uuid4().hex,'market_day':today})
                engine.process(run['id'])
                server=uvicorn.Server(uvicorn.Config(app,host='127.0.0.1',port=int(os.environ.get('STOCK_UI_PORT','8014')),log_level='warning'))
                def worker():
                    while not server.should_exit:
                        if stop_file.exists():server.should_exit=True;break
                        with store.db(readonly=True) as c:row=c.execute("SELECT id FROM research_runs WHERE status='queued' LIMIT 1").fetchone()
                        if row:engine.process(row['id'])
                        time.sleep(.3)
                thread=threading.Thread(target=worker,daemon=True);thread.start()
                print('ISOLATED_UI_READY',schema,flush=True)
                server.run();thread.join(timeout=2)
    finally:
        drop_isolated_database(schema)
        if stop_file.exists():stop_file.unlink()

if __name__=='__main__':main()
