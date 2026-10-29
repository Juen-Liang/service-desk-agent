import json, sys, time
from pathlib import Path
import httpx
root=Path(__file__).resolve().parents[1]
credentials=json.loads((root/'data/demo-credentials.json').read_text(encoding='utf-8'))
with httpx.Client(base_url='http://127.0.0.1:8011',timeout=20) as client:
    r=client.post('/api/auth/login',json={'username':'admin','password':credentials['admin']['password']}); r.raise_for_status()
    r=client.post('/api/evaluations',json={'mode':'mock','label':'首版完整流程验收','split':'all'}); r.raise_for_status(); eid=r.json()['id']
    print('evaluation:',eid,flush=True)
    last=-1
    for _ in range(360):
        rows=client.get('/api/evaluations').json(); e=next(x for x in rows if x['id']==eid)
        if len(e['results'])!=last:
            last=len(e['results']); print('completed:',last,'passed:',e['summary'].get('passed'),flush=True)
        if e['status'] in ('completed','failed'):
            (root/'artifacts').mkdir(exist_ok=True)
            (root/'artifacts/evaluation.json').write_text(json.dumps(e,ensure_ascii=False,indent=2),encoding='utf-8')
            print(json.dumps(e['summary'],ensure_ascii=False),flush=True)
            for c in e['results']:
                if not c['passed']: print(json.dumps(c,ensure_ascii=False),flush=True)
            sys.exit(0 if all(c['passed'] for c in e['results']) else 1)
        time.sleep(1)
    raise RuntimeError('评测超时；已保存的逐条结果可在界面查看')
