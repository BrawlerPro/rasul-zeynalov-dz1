import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient
from app import app, database, token_for, password_hash
from rules import can_transition, reaction_hours, sla_breached

T=datetime(2026,10,1,tzinfo=timezone.utc)
STAFF={'id':1,'name':'Расул','role':'staff'}
CUSTOMER={'id':2,'name':'Клиент','role':'customer'}
TICKET={'id':1,'user_id':2,'category_id':1,'title':'Тест','body':'Описание','status':'new','created_at':T,'author':'Клиент','category':'Сеть','sla_hours':4}

class FakeDB:
    def __init__(self, answers):
        self.answers=list(answers)
        self.conn=self
    async def fetch(self,*args): return self.answers.pop(0)
    async def one(self,*args): return self.answers.pop(0)
    async def scalar(self,*args): return self.answers.pop(0)
    def transaction(self): return self
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass

@pytest.fixture
def client():
    c=TestClient(app)
    yield c
    app.dependency_overrides.clear()
    c.close()

def configure(client, answers=(), user=STAFF):
    fake=FakeDB(answers)
    async def dep(): yield fake
    app.dependency_overrides[database]=dep
    if user: client.headers['Authorization']='Bearer '+token_for(user)
    return fake

@pytest.mark.parametrize('before,after,expected',[('new','in_progress',True),('in_progress','closed',True),('closed','in_progress',True),('new','closed',False),('new','new',False)])
def test_status_rules(before,after,expected):
    assert can_transition(before,after) is expected

def test_first_staff_answer_defines_reaction():
    assert reaction_hours(T,T+timedelta(hours=3),T+timedelta(hours=12))==3

def test_unanswered_ticket_uses_current_time():
    assert reaction_hours(T,None,T+timedelta(hours=5))==5

def test_sla_boundary_is_not_breach():
    assert not sla_breached(T,T+timedelta(hours=4),T,4)
    assert sla_breached(T,T+timedelta(hours=4,seconds=1),T,4)

@pytest.mark.parametrize('method,path,body',[
    ('GET','/api/categories',None),('GET','/api/tickets',None),('POST','/api/tickets',{'title':'a','body':'b','category_id':1}),
    ('GET','/api/tickets/1',None),('PATCH','/api/tickets/1/status',{'status':'in_progress'}),
    ('POST','/api/tickets/1/comments',{'body':'a'}),('GET','/api/summary',None)])
def test_operations_require_authorization(client,method,path,body):
    configure(client,user=None)
    assert client.request(method,path,json=body).status_code==401

@pytest.mark.parametrize('method,path,body',[
    ('GET','/api/tickets?page=0',None),('GET','/api/tickets?size=101',None),('GET','/api/tickets?status=bad',None),
    ('POST','/api/tickets',{'title':'','body':'a','category_id':1}),
    ('POST','/api/tickets',{'title':'a','body':'a','category_id':0}),
    ('PATCH','/api/tickets/1/status',{'status':'bad'}),
    ('POST','/api/tickets/1/comments',{'body':''})])
def test_invalid_parameters_return_422(client,method,path,body):
    configure(client)
    assert client.request(method,path,json=body).status_code==422

def test_login_contract(client):
    configure(client,[{**STAFF,'password_hash':password_hash('p')}],user=None)
    r=client.post('/api/login',json={'login':'rasul','password':'p'})
    assert r.status_code==200
    assert set(r.json())=={'token','user'}
    assert 'session=' in r.headers['set-cookie']

def test_bad_password(client):
    configure(client,[None],user=None)
    assert client.post('/api/login',json={'login':'bad','password':'p'}).status_code==401

def test_categories_contract(client):
    configure(client,[[{'id':1,'name':'Сеть','sla_hours':4}]])
    assert client.get('/api/categories').json()['items'][0]['sla_hours']==4

def test_list_returns_requested_page(client):
    configure(client,[42,[TICKET]])
    r=client.get('/api/tickets?page=2&size=1&status=new')
    assert r.status_code==200
    assert r.json()['page']==2 and len(r.json()['items'])==1 and r.json()['total']==42

def test_create_contract(client):
    configure(client,[1,TICKET])
    r=client.post('/api/tickets',json={'title':'Тест','body':'Описание','category_id':1})
    assert r.status_code==201 and r.json()['id']==1

def test_missing_category(client):
    configure(client,[None])
    assert client.post('/api/tickets',json={'title':'a','body':'b','category_id':999}).status_code==404

def test_detail_contract_and_first_employee_response(client):
    configure(client,[TICKET,[{'id':1,'user_id':2,'role':'customer','created_at':T+timedelta(hours=1),'body':'a','author':'Клиент'},
                             {'id':2,'user_id':1,'role':'staff','created_at':T+timedelta(hours=5),'body':'b','author':'Расул'}]])
    x=client.get('/api/tickets/1').json()
    assert x['reaction_hours']==5 and x['sla_breached'] and len(x['comments'])==2

def test_missing_ticket(client):
    configure(client,[None])
    assert client.get('/api/tickets/999').status_code==404

def test_customer_cannot_read_another_ticket(client):
    configure(client,[TICKET],user={**CUSTOMER,'id':3})
    assert client.get('/api/tickets/1').status_code==403

def test_change_status_contract(client):
    configure(client,[TICKET,{**TICKET,'status':'in_progress'}])
    r=client.patch('/api/tickets/1/status',json={'status':'in_progress'})
    assert r.status_code==200 and r.json()['status']=='in_progress'

def test_illegal_transition_is_rejected(client):
    configure(client,[TICKET])
    assert client.patch('/api/tickets/1/status',json={'status':'closed'}).status_code==409

def test_customer_cannot_change_status(client):
    configure(client,user=CUSTOMER)
    assert client.patch('/api/tickets/1/status',json={'status':'in_progress'}).status_code==403

def test_comment_contract(client):
    configure(client,[TICKET,{'id':5,'body':'Ответ','ticket_id':1,'user_id':1,'created_at':T}])
    r=client.post('/api/tickets/1/comments',json={'body':'Ответ'})
    assert r.status_code==201 and r.json()['ticket_id']==1

def test_closed_ticket_rejects_comment(client):
    configure(client,[{**TICKET,'status':'closed'}])
    assert client.post('/api/tickets/1/comments',json={'body':'Ответ'}).status_code==409

def test_summary_counts_open_and_breached(client):
    configure(client,[[{'id':1,'name':'Сеть','sla_hours':4}],
                      [TICKET,{**TICKET,'id':2,'status':'closed'},{**TICKET,'id':3,'status':'in_progress'}],
                      [{'ticket_id':1,'created_at':T+timedelta(hours=2)},{'ticket_id':3,'created_at':T+timedelta(hours=5)}]])
    x=client.get('/api/summary').json()
    assert x['total']==3 and x['open']==2 and x['categories'][0]['breached_share']==0.5

def test_customer_cannot_read_summary(client):
    configure(client,user=CUSTOMER)
    assert client.get('/api/summary').status_code==403
