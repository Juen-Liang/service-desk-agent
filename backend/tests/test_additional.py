from datetime import timedelta
import uuid, time, json
import httpx
from contextlib import contextmanager
from sqlalchemy import select
from deskflow.config import ROOT
from deskflow.db import Session, now
from deskflow.models import Approval, Plan

@contextmanager
def client(name):
    credentials=json.loads((ROOT/'data/demo-credentials.json').read_text(encoding='utf-8'))
    c=httpx.Client(base_url='http://127.0.0.1:8011',timeout=20)
    c.post('/api/auth/login',json={'username':name,'password':credentials[name]['password']}).raise_for_status()
    try:
        yield c
    finally:
        c.close()

def create(c,kind='permission'):
    t=c.post('/api/tickets',json={'title':'附加权限验收','description':'用于审批和取消的边界验证','kind':kind,'fields':{'resource':'VPN_ACCESS','purpose':'验收'},'request_key':str(uuid.uuid4())}).json()
    c.post(f'/api/tickets/{t["id"]}/runs',json={'version':t['version'],'mode':'mock'}).raise_for_status()
    for _ in range(100):
        d=c.get('/api/tickets/'+t['id']).json()
        if d['runs'][0]['status']=='waiting_approval': return d
        time.sleep(.1)
    raise AssertionError(d)

def test_cancellation_prevents_approval_and_resume():
    with client('employee') as employee,client('approver') as approver:
        d=create(employee); t=d['ticket'];p=d['plans'][0];r=d['runs'][0]
        result=employee.post(f'/api/tickets/{t["id"]}/actions/cancel',json={'version':t['version']});assert result.status_code==200
        assert approver.post(f'/api/approvals/{p["approval"]["id"]}/decision',json={'decision':'approved','plan_digest':p['digest']}).status_code==409
        assert employee.post(f'/api/runs/{r["id"]}/resume').status_code==409
        assert employee.get('/api/tickets/'+t['id']).json()['plans'][0]['operation'] is None

def test_self_approval_and_expired_approval_blocked():
    with client('approver') as approver:
        d=create(approver);p=d['plans'][0]
        assert approver.post(f'/api/approvals/{p["approval"]["id"]}/decision',json={'decision':'approved','plan_digest':p['digest']}).status_code==403
    with client('employee') as employee,client('approver') as approver:
        d=create(employee);p=d['plans'][0]
        with Session.begin() as db: db.get(Approval,p['approval']['id']).expires_at=now()-timedelta(seconds=1)
        assert approver.post(f'/api/approvals/{p["approval"]["id"]}/decision',json={'decision':'approved','plan_digest':p['digest']}).status_code==409

def test_sse_last_event_id_replays_only_new_events():
    with client('employee') as c:
        d=create(c);ticket_id=d['ticket']['id']
        with c.stream('GET',f'/api/tickets/{ticket_id}/events') as response:
            first=next(int(line[4:]) for line in response.iter_lines() if line.startswith('id: '))
        with c.stream('GET',f'/api/tickets/{ticket_id}/events',headers={'Last-Event-ID':str(first)}) as response:
            following=next(int(line[4:]) for line in response.iter_lines() if line.startswith('id: '))
        assert following>first
