import asyncio, json, math, time, sys, statistics, platform, hashlib, os
from pathlib import Path
from datetime import datetime, timezone
import httpx, asyncpg
from app import DB_URL

REPEATS=30
WARMUP=5
def stats(values):
    s=sorted(values)
    return {'p50':statistics.median(s),'p95':s[math.ceil(.95*len(s))-1],'max':max(s)}

async def run(dataset):
    print(f'Dataset: {dataset}; warmup={WARMUP}; repeats={REPEATS}',flush=True)
    print('DATA  OPERATION     p50_ms     p95_ms     max_ms',flush=True)
    conn=await asyncpg.connect(DB_URL,server_settings={'search_path':'rasul_zeynalov'})
    before={t:await conn.fetchval(f'SELECT count(*) FROM {t}') for t in ('users','categories','tickets','comments')}
    source_hash=hashlib.sha256(Path('app.py').read_bytes()).hexdigest()
    result={'dataset':dataset,'timestamp_utc':datetime.now(timezone.utc).isoformat(),'repeats':REPEATS,'warmup':WARMUP,'counts_before':before,'app_sha256':source_hash,'operations':{}}
    async with httpx.AsyncClient(base_url='http://api:8000',timeout=60) as c:
        auth=await c.post('/api/login',json={'login':'rasul','password':os.getenv('SEED_PASSWORD', 'changeme')});auth.raise_for_status()
        c.headers['Authorization']='Bearer '+auth.json()['token']
        operations=[
            ('login','POST','/api/login',{'login':'rasul','password':os.getenv('SEED_PASSWORD', 'changeme')},200),
            ('categories','GET','/api/categories',None,200),
            ('list','GET','/api/tickets?page=1&size=20&status=new',None,200),
            ('create','POST','/api/tickets',{'title':'Измерение','body':'Запись серии','category_id':1},201),
            ('detail','GET','/api/tickets/2',None,200),
            ('status','PATCH','/api/tickets/1/status',{'status':'in_progress'},200),
            ('comment','POST','/api/tickets/2/comments',{'body':'Ответ измерения'},201),
            ('summary','GET','/api/summary',None,200)]
        for name,method,path,body,expected in operations:
            records=[]
            for n in range(WARMUP+REPEATS):
                start=time.perf_counter()
                r=await c.request(method,path,json=body) if body else await c.request(method,path)
                elapsed=(time.perf_counter()-start)*1000
                assert r.status_code==expected,(name,r.status_code,r.text)
                payload=r.json()
                record={'http_ms':elapsed,'server_ms':float(r.headers['X-App-Ms']),'db_ms':float(r.headers['X-DB-Ms']),'queries':int(r.headers['X-DB-Queries']),'status':r.status_code,'bytes':len(r.content)}
                record['code_ms']=record['server_ms']-record['db_ms']
                if n>=WARMUP: records.append(record)
                # Restore outside the timed HTTP operation, so every repeat sees the same logical data.
                if name=='create': await conn.execute('DELETE FROM tickets WHERE id=$1',payload['id'])
                if name=='comment': await conn.execute('DELETE FROM comments WHERE id=$1',payload['id'])
                if name=='status': await conn.execute("UPDATE tickets SET status='new' WHERE id=1")
            result['operations'][name]={'raw':records,**{k:stats([x[k] for x in records]) for k in ('http_ms','server_ms','db_ms','code_ms')},'queries':sorted(set(x['queries'] for x in records)),'response_bytes':records[0]['bytes']}
            h=result['operations'][name]['http_ms']
            print(f'{dataset:<5} {name:<10} {h["p50"]:10.3f} {h["p95"]:10.3f} {h["max"]:10.3f}',flush=True)
        # Verify business scenarios against the running service with the real PostgreSQL database.
        for path,expected in [('/api/tickets?page=0',422),('/api/tickets/9999999',404)]:
            assert (await c.get(path)).status_code==expected
        assert (await c.patch('/api/tickets/1/status',json={'status':'closed'})).status_code==409
        assert (await c.post('/api/tickets/3/comments',json={'body':'Запрещено'})).status_code==409
        x=(await c.get('/api/tickets/2')).json()
        assert len(x['comments'])==3 and x['reaction_hours']==4
        x=(await c.get('/api/summary')).json()
        assert x['total']==before['tickets'] and x['open']==before['tickets']-before['tickets']//3
        result['live_checks']='passed'
        result['summary']=x
    result['counts_after']={t:await conn.fetchval(f'SELECT count(*) FROM {t}') for t in before}
    assert result['counts_after']==before
    result['writes']={'tickets_inserted_and_deleted':35,'comments_inserted_and_deleted':35,'status_updates_and_restores':35}
    result['versions']={'python':platform.python_version(),'postgres':await conn.fetchval('SELECT version()')}
    Path(f'results/benchmark-{dataset}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print('LAYERS: mean of the same 30 measured requests (ms)',flush=True)
    print('OPERATION   SERVER_MS       DB_MS     CODE_MS   QUERIES',flush=True)
    for name in ('list','detail','summary'):
        op=result['operations'][name]
        avg={k:statistics.mean(x[k] for x in op['raw']) for k in ('server_ms','db_ms','code_ms')}
        print(f'{name:<10} {avg["server_ms"]:10.3f} {avg["db_ms"]:10.3f} {avg["code_ms"]:10.3f}   {op["queries"]}',flush=True)
    print(f'Live checks: {result["live_checks"]}; row counts preserved: {result["counts_before"]==result["counts_after"]}',flush=True)
    print(f'Saved: results/benchmark-{dataset}.json',flush=True)
    await conn.close()

if __name__=='__main__': asyncio.run(run(sys.argv[1]))
