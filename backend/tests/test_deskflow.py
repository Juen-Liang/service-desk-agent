import json, time, uuid, asyncio
from pathlib import Path
import pytest, httpx
from sqlalchemy import select
from deskflow.config import ROOT, settings
from deskflow.db import Session, uid, now
from deskflow.models import User, Ticket, Plan, Approval, Operation, Config, Run, Chunk
from deskflow.security import passwords, access_token, sign_permit
from deskflow.policy import DEFAULT_POLICY, check_policy
from deskflow.evaluation import cases
from deskflow.knowledge import split_blocks, terms

BASE='http://127.0.0.1:8011'

@pytest.fixture(scope='session')
def clients():
    credentials=json.loads((ROOT/'data/demo-credentials.json').read_text(encoding='utf-8'))
    clients={}
    for name in ('admin','support','approver','employee','employee2'):
        c=httpx.Client(base_url=BASE,timeout=20)
        r=c.post('/api/auth/login',json={'username':name,'password':credentials[name]['password']}); assert r.status_code==200,r.text
        clients[name]=c
    yield clients
    for c in clients.values(): c.close()

def new_ticket(clients,kind='account',fields=None,owner='employee'):
    r=clients[owner].post('/api/tickets',json={'title':'验收 '+kind,'description':'独立验收工单，核查完整业务流程。','kind':kind,'fields':fields or {},'request_key':str(uuid.uuid4())})
    assert r.status_code==200,r.text
    return r.json()

def start(clients,t,mode='mock'):
    r=clients['employee'].post(f'/api/tickets/{t["id"]}/runs',json={'version':t['version'],'mode':mode}); assert r.status_code==200,r.text
    return r.json()

def wait_ticket(clients,t,states=('waiting_confirmation','waiting_approval','waiting_human','waiting_info')):
    deadline=time.monotonic()+20
    while time.monotonic()<deadline:
        r=clients['employee'].get('/api/tickets/'+t['id']); assert r.status_code==200,r.text
        d=r.json()
        if d['ticket']['status'] in states and (not d['runs'] or d['runs'][0]['status'] not in ('queued','running')): return d
        if d['runs'] and d['runs'][0]['status']=='failed': pytest.fail(d['runs'][0]['error'])
        time.sleep(.15)
    pytest.fail('运行未完成：'+json.dumps(d,ensure_ascii=False))

def reset_employee(clients,scenario='normal'):
    u=clients['employee'].get('/api/auth/me').json()
    r=clients['admin'].post('/api/business/scenario',json={'employee_id':u['id'],'scenario':scenario}); assert r.status_code==200,r.text

@pytest.mark.parametrize('case',cases(),ids=lambda c:c['id'])
def test_policy_matrix(case):
    e={'id':'self','active':case['scenario']!='inactive','privileged':case['scenario']=='privileged','locked':case['scenario']!='unlocked','lock_reason':'security' if case['scenario']=='security_lock' else 'failed_login','devices':[{'id':'own'}]}
    d=check_policy(case['kind'],case['fields'],e,DEFAULT_POLICY)
    expected={'waiting_human':'manual','waiting_info':'clarify','waiting_confirmation':'execute' if case['kind']=='account' else 'approve'}[case['expected']]
    if case['decision']=='rejected' or case['scenario']=='no_seats': expected='approve'
    assert d['decision']==expected

def test_dataset_split():
    assert len(cases())==30
    assert sum(c['split']=='holdout' for c in cases())==10

def test_chunk_source_and_chinese_tokens():
    chunks=split_blocks([('规则内容。'*500,{'line':7,'heading':'权限'})])
    assert len(chunks)>1
    assert all(c[1]['start']['line']==7 for c in chunks)
    assert '权限' in terms('申请临时权限 VPN_ACCESS')

def test_login_isolation_idempotency(clients):
    t=new_ticket(clients)
    assert clients['employee2'].get('/api/tickets/'+t['id']).status_code==404
    assert clients['employee'].get('/api/settings').status_code==403
    p={'title':'幂等测试','description':'相同请求只建一个工单','request_key':str(uuid.uuid4())}
    a=clients['employee'].post('/api/tickets',json=p); b=clients['employee'].post('/api/tickets',json=p)
    assert a.json()['id']==b.json()['id']
    p['title']='不同内容'
    assert clients['employee'].post('/api/tickets',json=p).status_code==409

def test_unlock_and_confirmation(clients):
    reset_employee(clients)
    t=new_ticket(clients); start(clients,t); d=wait_ticket(clients,t)
    assert d['ticket']['status']=='waiting_confirmation'
    assert d['plans'][0]['operation']['verified'] is True
    assert clients['employee'].get('/api/business').json()['employee']['locked'] is False
    r=clients['employee'].post('/api/tickets/'+t['id']+'/actions/confirm',json={'version':d['ticket']['version']}); assert r.status_code==200,r.text
    assert r.json()['status']=='closed'

def test_approval_permissions_and_receipt_replay(clients):
    reset_employee(clients)
    t=new_ticket(clients,'permission',{'resource':'VPN_ACCESS','purpose':'测试审批闭环','hours':24}); start(clients,t)
    d=wait_ticket(clients,t); p=d['plans'][0]; a=p['approval']
    body={'decision':'approved','note':'测试批准','plan_digest':p['digest']}
    assert clients['employee'].post(f'/api/approvals/{a["id"]}/decision',json=body).status_code==403
    assert clients['admin'].post(f'/api/approvals/{a["id"]}/decision',json=body).status_code==403
    assert clients['approver'].post(f'/api/approvals/{a["id"]}/decision',json={**body,'plan_digest':'tampered'}).status_code==409
    response=clients['approver'].post(f'/api/approvals/{a["id"]}/decision',json=body); assert response.status_code==200,response.text
    d=wait_ticket(clients,t,('waiting_confirmation',)); op=d['plans'][0]['operation']; assert op['verified']
    with httpx.Client(base_url=settings().demo_url,headers={'Authorization':'Bearer '+settings().demo_service_secret}) as c:
        payload={'operation_id':op['id'],'action':p['action'],'params':p['params'],'permit':sign_permit(op['id'],p['action'],p['params'])}
        replay=c.post('/operations',json=payload); assert replay.status_code==200,replay.text
        assert replay.json()==op['receipt']
        changed={**p['params'],'hours':48}; payload.update(params=changed,permit=sign_permit(op['id'],p['action'],changed))
        assert c.post('/operations',json=payload).status_code==409

def test_changed_request_invalidates_approval(clients):
    t=new_ticket(clients,'permission',{'resource':'DEV_READONLY','purpose':'审批绑定','hours':24}); start(clients,t); d=wait_ticket(clients,t)
    p=d['plans'][0]
    response=clients['employee'].post(f'/api/tickets/{t["id"]}/actions/comment',json={'version':d['ticket']['version'],'note':'修改为 48 小时','fields':{'hours':48}}); assert response.status_code==200
    assert clients['approver'].post(f'/api/approvals/{p["approval"]["id"]}/decision',json={'decision':'approved','plan_digest':p['digest']}).status_code==409

def test_handoff_and_manual_resolution(clients):
    t=new_ticket(clients,'device'); start(clients,t); d=wait_ticket(clients,t)
    assert d['ticket']['status']=='waiting_human'
    response=clients['support'].post(f'/api/tickets/{t["id"]}/actions/resolve',json={'version':d['ticket']['version'],'note':'已检查并更换损坏线缆，请确认。'})
    assert response.status_code==200,response.text
    assert response.json()['status']=='waiting_confirmation'

def test_budget_resume_preserves_run(clients):
    original=clients['admin'].get('/api/settings').json()['policy']
    try:
        clients['admin'].put('/api/policy',json={**original,'max_rounds':1})
        t=new_ticket(clients,'device'); r=start(clients,t)
        for _ in range(100):
            d=clients['employee'].get('/api/tickets/'+t['id']).json()
            if d['runs'][0]['status']=='paused': break
            time.sleep(.1)
        assert d['runs'][0]['status']=='paused',d
        old_tools=d['runs'][0]['tool_calls']
        clients['admin'].put('/api/policy',json=original)
        assert clients['employee'].post('/api/runs/'+r['id']+'/resume').status_code==200
        d=wait_ticket(clients,t); assert d['runs'][0]['id']==r['id']
        assert d['runs'][0]['tool_calls']==old_tools
    finally: clients['admin'].put('/api/policy',json=original)

def test_profile_errors_return_result_without_secret(clients):
    r=clients['admin'].post('/api/settings/embedding/test',json={'base_url':'http://127.0.0.1:9/v1','model':'test','api_key':'sk-test-hidden','timeout':5})
    assert r.status_code==200 and r.json()['ok'] is False
    assert 'sk-test-hidden' not in r.text
    config=clients['admin'].get('/api/settings').json()
    assert not config['embedding'].get('has_key')

def test_knowledge_version_keeps_old_chunks(clients):
    name='test-'+uuid.uuid4().hex+'.md'
    r=clients['admin'].post('/api/knowledge/upload',files={'file':(name,'# 期限\n临时权限最多 168 小时。'.encode(),'text/markdown')}); assert r.status_code==200,r.text
    v=r.json()
    for _ in range(80):
        old=clients['employee'].get('/api/knowledge/versions/'+v['id']).json()
        if old['chunks']: break
        time.sleep(.1)
    assert old['chunks']
    assert clients['admin'].post('/api/knowledge/versions/'+v['id']+'/retry').status_code==200
    assert clients['employee'].get('/api/knowledge/versions/'+v['id']).json()['chunks']==old['chunks']

def test_csrf_and_origin(clients):
    assert clients['employee'].post('/api/auth/logout',headers={'Origin':'https://untrusted.invalid'}).status_code==403
