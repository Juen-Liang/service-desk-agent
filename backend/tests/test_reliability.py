import asyncio, json, uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import httpx, pytest
from sqlalchemy import select
from deskflow.config import settings
from deskflow.db import now
from deskflow.security import sign_permit, encrypt
from deskflow.providers import embed, chat, ModelError
from deskflow.demo import DemoSession, Software, License, Grant

def demo_client(): return httpx.Client(base_url=settings().demo_url,headers={'Authorization':'Bearer '+settings().demo_service_secret},timeout=15)

def test_concurrent_last_license_and_expiry():
    scope='test-'+uuid.uuid4().hex[:12]; ids=[str(uuid.uuid4()),str(uuid.uuid4())]
    with demo_client() as c:
        for employee_id in ids:
            assert c.post('/seed',json={'id':employee_id,'name':'隔离并发验证','scope':scope}).status_code==200
    with DemoSession.begin() as db:
        software=db.scalar(select(Software).where(Software.scope==scope,Software.sku=='IDE_PRO'));software.capacity=1
    def assign(employee_id):
        op=str(uuid.uuid4());params={'employee_id':employee_id,'sku':'IDE_PRO'}
        with demo_client() as c: return c.post('/operations',json={'operation_id':op,'action':'assign_software','params':params,'permit':sign_permit(op,'assign_software',params)}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool: statuses=list(pool.map(assign,ids))
    assert sorted(statuses)==[200,409]
    with DemoSession() as db:
        assert len(db.scalars(select(License).where(License.software_id==software.id)).all())==1
    with DemoSession.begin() as db: db.add(Grant(id=str(uuid.uuid4()),employee_id=ids[0],resource='VPN_ACCESS',expires_at=now()-timedelta(seconds=1)))
    with demo_client() as c:
        assert not c.get('/employees/'+ids[0]).json()['permissions'][0]['active']
        assert c.post('/maintenance/expire').status_code==200
    with DemoSession() as db: assert db.scalar(select(Grant).where(Grant.employee_id==ids[0])).revoked_at

@pytest.mark.asyncio
@pytest.mark.parametrize('status',[400,401,403,429,500])
async def test_model_http_errors(monkeypatch,status):
    original=httpx.AsyncClient
    def factory(*a,**kw): return original(transport=httpx.MockTransport(lambda request:httpx.Response(status,json={'error':{'message':'denied'}})))
    monkeypatch.setattr(httpx,'AsyncClient',factory)
    with pytest.raises(ModelError,match=str(status)):
        await chat({'base_url':'https://model.invalid/v1','model':'test','encrypted_key':encrypt('test')},[{'role':'user','content':'test'}])

@pytest.mark.asyncio
async def test_embedding_success_and_dimension_validation(monkeypatch):
    original=httpx.AsyncClient
    vectors=[[.01]*1024]
    def factory(*a,**kw): return original(transport=httpx.MockTransport(lambda request:httpx.Response(200,json={'data':[{'index':0,'embedding':vectors[0]}]})))
    monkeypatch.setattr(httpx,'AsyncClient',factory)
    config={'base_url':'https://model.invalid/v1','model':'test','encrypted_key':encrypt('test')}
    assert len((await embed(config,['test']))[0])==1024
    vectors[0]=[1,2]
    with pytest.raises(ModelError,match='1024'): await embed(config,['test'])

@pytest.mark.asyncio
async def test_empty_stream_is_not_success(monkeypatch):
    original=httpx.AsyncClient
    monkeypatch.setattr(httpx,'AsyncClient',lambda *a,**kw:original(transport=httpx.MockTransport(lambda request:httpx.Response(200,text='data: [DONE]\n\n'))))
    with pytest.raises(ModelError,match='空结果'): await chat({'base_url':'https://model.invalid/v1','model':'test','encrypted_key':encrypt('test')},[{'role':'user','content':'test'}])
