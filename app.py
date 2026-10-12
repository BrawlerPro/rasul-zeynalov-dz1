import os, json, hashlib, hmac, base64, time
from datetime import datetime, timezone
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pathlib import Path
import asyncpg
from fastapi import FastAPI, HTTPException, Request, Response, Depends, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from typing import Literal
from rules import can_transition, reaction_hours, sla_breached

DB_URL = os.getenv('DATABASE_URL', 'postgresql://rasul_zeynalov:changeme@localhost/rasul_zeynalov')
SECRET = os.getenv('TOKEN_SECRET', 'changeme').encode()
metrics = ContextVar('metrics', default=None)
pool = None

def password_hash(password):
    return hashlib.pbkdf2_hmac('sha256', password.encode(), b'rasul_zeynalov_coursework', 100_000).hex()

def token_for(user):
    payload = base64.urlsafe_b64encode(json.dumps({'id':user['id'],'name':user['name'],'role':user['role'],'expires':time.time()+3600}).encode()).decode()
    return payload + '.' + hmac.new(SECRET, payload.encode(), 'sha256').hexdigest()

def authenticated(request: Request):
    token = request.cookies.get('session') or request.headers.get('Authorization','').removeprefix('Bearer ')
    try:
        payload, signature = token.rsplit('.',1)
        if not hmac.compare_digest(signature, hmac.new(SECRET,payload.encode(),'sha256').hexdigest()): raise ValueError()
        user = json.loads(base64.urlsafe_b64decode(payload))
        if user['expires'] < time.time(): raise ValueError()
        return user
    except Exception:
        raise HTTPException(401,'Требуется авторизация')

@asynccontextmanager
async def lifespan(app):
    global pool
    pool = await asyncpg.create_pool(DB_URL, min_size=1, max_size=5, server_settings={'search_path':'rasul_zeynalov'})
    yield
    await pool.close()

app = FastAPI(title='rasul_zeynalov — служба поддержки',lifespan=lifespan)

@app.middleware('http')
async def timing(request, call_next):
    data={'db_ms':0.,'queries':0}; handle=metrics.set(data); start=time.perf_counter()
    try:
        response=await call_next(request)
        response.headers['X-App-Ms']=f'{(time.perf_counter()-start)*1000:.6f}'
        response.headers['X-DB-Ms']=f'{data["db_ms"]:.6f}'
        response.headers['X-DB-Queries']=str(data['queries'])
        response.headers['Cache-Control']='no-store'
        return response
    finally: metrics.reset(handle)

class DB:
    def __init__(self,conn): self.conn=conn
    async def query(self,method,sql,*args):
        start=time.perf_counter()
        try: return await getattr(self.conn,method)(sql,*args)
        finally:
            if (m:=metrics.get()) is not None:
                m['db_ms']+=(time.perf_counter()-start)*1000; m['queries']+=1
    async def fetch(self,sql,*args): return await self.query('fetch',sql,*args)
    async def one(self,sql,*args): return await self.query('fetchrow',sql,*args)
    async def scalar(self,sql,*args): return await self.query('fetchval',sql,*args)

async def database():
    async with pool.acquire() as conn: yield DB(conn)

class Login(BaseModel):
    login: str
    password: str
class TicketInput(BaseModel):
    title: str = Field(min_length=1,max_length=200)
    body: str = Field(min_length=1,max_length=5000)
    category_id: int = Field(gt=0)
class StatusInput(BaseModel):
    status: Literal['new','in_progress','closed']
class CommentInput(BaseModel):
    body: str = Field(min_length=1,max_length=5000)

@app.post('/api/login')
async def login(body:Login,response:Response,db=Depends(database)):
    user=await db.one('SELECT * FROM users WHERE login=$1',body.login)
    if not user or not hmac.compare_digest(user['password_hash'],password_hash(body.password)):
        raise HTTPException(401,'Неверный логин или пароль')
    token=token_for(user); response.set_cookie('session',token,httponly=True,samesite='strict',max_age=3600)
    return {'token':token,'user':{k:user[k] for k in ('id','name','role')}}

@app.get('/api/categories')
async def categories(user=Depends(authenticated),db=Depends(database)):
    return {'items':await db.fetch('SELECT * FROM categories ORDER BY id')}

@app.get('/api/tickets')
async def tickets(page:int=Query(1,ge=1),size:int=Query(20,ge=1,le=100),status:Literal['new','in_progress','closed']|None=None,user=Depends(authenticated),db=Depends(database)):
    owner=None if user['role']=='staff' else user['id']
    where='WHERE ($1::text IS NULL OR t.status=$1) AND ($2::integer IS NULL OR t.user_id=$2)'
    total=await db.scalar('SELECT count(*) FROM tickets t '+where,status,owner)
    items=await db.fetch('SELECT t.*,u.name AS author,c.name AS category FROM tickets t JOIN users u ON u.id=t.user_id JOIN categories c ON c.id=t.category_id '+where+' ORDER BY t.id DESC LIMIT $3 OFFSET $4',status,owner,size,(page-1)*size)
    return {'items':items,'total':total,'page':page,'size':size}

@app.post('/api/tickets',status_code=201)
async def create(body:TicketInput,user=Depends(authenticated),db=Depends(database)):
    if not await db.scalar('SELECT id FROM categories WHERE id=$1',body.category_id): raise HTTPException(404,'Категория не найдена')
    return dict(await db.one('INSERT INTO tickets(user_id,category_id,title,body) VALUES($1,$2,$3,$4) RETURNING *',user['id'],body.category_id,body.title,body.body))

async def get_ticket(db,id,user,lock=False):
    t=await db.one('SELECT t.*,u.name AS author,c.name AS category,c.sla_hours FROM tickets t JOIN users u ON u.id=t.user_id JOIN categories c ON c.id=t.category_id WHERE t.id=$1'+(' FOR UPDATE OF t' if lock else ''),id)
    if not t: raise HTTPException(404,'Заявка не найдена')
    if user['role']!='staff' and t['user_id']!=user['id']: raise HTTPException(403,'Нет доступа к чужой заявке')
    return t

@app.get('/api/tickets/{id}')
async def detail(id:int,user=Depends(authenticated),db=Depends(database)):
    ticket=await get_ticket(db,id,user)
    comments=await db.fetch('SELECT c.*,u.name AS author,u.role FROM comments c JOIN users u ON u.id=c.user_id WHERE c.ticket_id=$1 ORDER BY c.created_at,c.id',id)
    first=next((c['created_at'] for c in comments if c['role']=='staff'),None)
    now=datetime.now(timezone.utc)
    return {'ticket':dict(ticket),'comments':comments,'reaction_hours':reaction_hours(ticket['created_at'],first,now),'sla_breached':sla_breached(ticket['created_at'],first,now,ticket['sla_hours']),'awaiting_response':first is None}

@app.patch('/api/tickets/{id}/status')
async def status_change(id:int,body:StatusInput,user=Depends(authenticated),db=Depends(database)):
    if user['role']!='staff': raise HTTPException(403,'Статус меняет сотрудник')
    async with db.conn.transaction():
        ticket=await get_ticket(db,id,user,True)
        if not can_transition(ticket['status'],body.status): raise HTTPException(409,'Недопустимый переход статуса')
        return dict(await db.one('UPDATE tickets SET status=$2 WHERE id=$1 RETURNING *',id,body.status))

@app.post('/api/tickets/{id}/comments',status_code=201)
async def comment(id:int,body:CommentInput,user=Depends(authenticated),db=Depends(database)):
    async with db.conn.transaction():
        ticket=await get_ticket(db,id,user,True)
        if ticket['status']=='closed': raise HTTPException(409,'Переписка закрытой заявки запрещена')
        return dict(await db.one('INSERT INTO comments(ticket_id,user_id,body) VALUES($1,$2,$3) RETURNING *',id,user['id'],body.body))

@app.get('/api/summary')
async def summary(user=Depends(authenticated),db=Depends(database)):
    if user['role']!='staff': raise HTTPException(403,'Сводка доступна сотрудникам')
    cats=await db.fetch('SELECT * FROM categories ORDER BY id')
    rows=await db.fetch('SELECT id,category_id,created_at,status FROM tickets')
    answers=await db.fetch("SELECT c.ticket_id,c.created_at FROM comments c JOIN users u ON u.id=c.user_id WHERE u.role='staff' ORDER BY c.created_at")
    first={}
    for c in answers: first.setdefault(c['ticket_id'],c['created_at'])
    groups={c['id']:{'category':c['name'],'sla_hours':c['sla_hours'],'open':0,'breached':0} for c in cats}
    now=datetime.now(timezone.utc)
    for t in rows:
        if t['status']!='closed':
            g=groups[t['category_id']];g['open']+=1
            g['breached']+=int(sla_breached(t['created_at'],first.get(t['id']),now,g['sla_hours']))
    for g in groups.values(): g['breached_share']=g['breached']/g['open'] if g['open'] else 0
    return {'total':len(rows),'open':sum(g['open'] for g in groups.values()),'categories':list(groups.values()),'as_of':now}

@app.get('/',response_class=HTMLResponse,include_in_schema=False)
async def index(): return Path('static/index.html').read_text(encoding='utf8')
