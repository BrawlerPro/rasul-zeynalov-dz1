import asyncio, sys, json, hashlib, os
from pathlib import Path
import asyncpg
from app import DB_URL,password_hash

async def seed(size):
    assert size in ('small','work')
    users,categories,tickets=(50,5,300) if size=='small' else (3000,10,50000)
    conn=await asyncpg.connect(DB_URL)
    async with conn.transaction():
        await conn.execute(Path('schema.sql').read_text())
        await conn.execute('TRUNCATE comments,tickets,categories,users RESTART IDENTITY CASCADE')
        ph=password_hash(os.getenv('SEED_PASSWORD', 'changeme'))
        await conn.execute("INSERT INTO users SELECT i,CASE WHEN i=1 THEN 'Зейналов Расул' ELSE 'Пользователь '||i END,CASE WHEN i=1 THEN 'rasul' ELSE 'user'||i END,$1,CASE WHEN i=1 OR i%5=0 THEN 'staff' ELSE 'customer' END FROM generate_series(1,$2) i",ph,users)
        names=['Доступ к системе','Почта','Сеть','Программное обеспечение','Оборудование','Учётная запись','Документы','Печать','VPN','Прочее']
        await conn.executemany('INSERT INTO categories VALUES($1,$2,$3)',[(i+1,names[i],(i%3+1)*4) for i in range(categories)])
        await conn.execute("INSERT INTO tickets SELECT i,2+((i-1)%($2-1)),1+((i-1)%$3),'Заявка № '||i,'Описание обращения '||i,CASE i%3 WHEN 0 THEN 'closed' WHEN 1 THEN 'new' ELSE 'in_progress' END,TIMESTAMPTZ '2026-10-01 08:00:00+00' + (i%1000)*INTERVAL '1 minute' FROM generate_series(1,$1) i",tickets,users,categories)
        # Exactly three comments per ticket; the first staff response can violate category SLA.
        await conn.execute("INSERT INTO comments SELECT (t.id-1)*3+j,t.id,CASE WHEN j=1 THEN t.user_id ELSE 1 END,CASE WHEN j=1 THEN 'Уточнение пользователя' ELSE 'Ответ сотрудника' END,t.created_at+CASE WHEN j=1 THEN INTERVAL '30 minutes' ELSE (j+ t.id%20)*INTERVAL '1 hour' END FROM tickets t CROSS JOIN generate_series(1,3) j")
        for table in ('users','categories','tickets','comments'):
            await conn.execute(f"SELECT setval(pg_get_serial_sequence('{table}','id'),(SELECT max(id) FROM {table}))")
    await conn.execute('ANALYZE')
    counts={t:await conn.fetchval(f'SELECT count(*) FROM {t}') for t in ('users','categories','tickets','comments')}
    hashes={}
    for t in counts:
        value=await conn.fetchval(f"SELECT md5(string_agg(row_to_json(x)::text, E'\\n' ORDER BY id)) FROM (SELECT * FROM {t}) x")
        hashes[t]=value
    result={'prefix':'rasul_zeynalov','dataset':size,'counts':counts,'md5':hashes,'postgres':await conn.fetchval('SELECT version()')}
    Path('results').mkdir(exist_ok=True)
    Path(f'results/seed-{size}.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(json.dumps(result,ensure_ascii=False,indent=2));await conn.close()

if __name__=='__main__':asyncio.run(seed(sys.argv[1]))
