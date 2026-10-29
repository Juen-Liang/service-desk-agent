import json,sys,time,uuid
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[1]; path=root/'artifacts/restart-check.json'
credentials=json.loads((root/'data/demo-credentials.json').read_text(encoding='utf-8'))
with httpx.Client(base_url='http://127.0.0.1:8011',timeout=20) as client:
    who='employee' if sys.argv[1]=='prepare' else 'approver'
    client.post('/api/auth/login',json={'username':who,'password':credentials[who]['password']}).raise_for_status()
    if sys.argv[1]=='prepare':
        t=client.post('/api/tickets',json={'title':'断点恢复验收 · 重启后继续审批','description':'核验服务器重启后保留原方案和审批。','kind':'permission','fields':{'resource':'DEV_READONLY','purpose':'恢复验收'},'request_key':str(uuid.uuid4())}).json()
        r=client.post(f'/api/tickets/{t["id"]}/runs',json={'version':t['version'],'mode':'mock'});r.raise_for_status()
        for _ in range(100):
            d=client.get('/api/tickets/'+t['id']).json()
            if d['runs'][0]['status']=='waiting_approval': break
            time.sleep(.15)
        assert d['runs'][0]['status']=='waiting_approval',d
        p=d['plans'][0]
        path.write_text(json.dumps({'ticket_id':t['id'],'run_id':d['runs'][0]['id'],'tool_calls_before':d['runs'][0]['tool_calls'],'plan':p},ensure_ascii=False,indent=2),encoding='utf-8')
        print('已在待审批检查点保存，准备重启服务。')
    else:
        state=json.loads(path.read_text(encoding='utf-8'));p=state['plan']
        r=client.post(f'/api/approvals/{p["approval"]["id"]}/decision',json={'decision':'approved','note':'服务重启后批准','plan_digest':p['digest']});r.raise_for_status()
        for _ in range(120):
            d=client.get('/api/tickets/'+state['ticket_id']).json()
            if d['runs'][0]['status']=='completed': break
            time.sleep(.15)
        assert d['runs'][0]['id']==state['run_id'] and d['runs'][0]['status']=='completed',d
        assert d['runs'][0]['tool_calls']==state['tool_calls_before'],d
        assert len(d['plans'])==1 and d['plans'][0]['operation']['verified'],d
        state.update(passed=True,tool_calls_after=d['runs'][0]['tool_calls'],operation_id=d['plans'][0]['operation']['id'])
        path.write_text(json.dumps(state,ensure_ascii=False,indent=2),encoding='utf-8')
        print('重启恢复通过：同一运行、同一方案、已完成工具未重复调用、业务结果核验成功。')
