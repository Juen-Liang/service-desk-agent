import asyncio, json, secrets, time, hashlib
from datetime import timedelta
from pathlib import Path
from fastapi import FastAPI, Depends, HTTPException, Request, Response, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, JSONResponse
from pydantic import BaseModel, Field, ConfigDict
from sqlalchemy import select, text, func
from .config import settings
from .db import Session, now, uid
from .models import User, Refresh, Ticket, Comment, Plan, Approval, Run, Event, Operation, Config, Document, DocumentVersion, Chunk, Evaluation
from .security import current_user, require_role, check_origin, passwords, access_token, digest, encrypt, redact
from .service import as_dict, public_run, visible, get_ticket, invalidate, start_run
from .policy import DEFAULT_POLICY
from .workflow import SYSTEM
from . import providers, business, knowledge

app = FastAPI(title='DeskFlow', version='0.1.0', dependencies=[Depends(check_origin)])
app.add_middleware(CORSMiddleware, allow_origins=[settings().frontend_origin], allow_credentials=True, allow_methods=['GET','POST','PUT'], allow_headers=['Content-Type','Authorization','Last-Event-ID'])

class Strict(BaseModel):
    model_config = ConfigDict(extra='forbid')

class Login(Strict):
    username: str = Field(max_length=80)
    password: str = Field(max_length=200)

def set_cookies(response, user, refresh):
    response.set_cookie('deskflow_access', access_token(user), httponly=True, secure=settings().cookie_secure, samesite='lax', max_age=1800, path='/api')
    response.set_cookie('deskflow_refresh', refresh, httponly=True, secure=settings().cookie_secure, samesite='lax', max_age=604800, path='/api/auth')

@app.get('/api/health')
def health():
    with Session() as db: db.execute(text('SELECT 1'))
    return {'status':'ok','version':'0.1.0'}

@app.post('/api/auth/login')
def login(p: Login, response: Response):
    with Session.begin() as db:
        u = db.scalar(select(User).where(User.username == p.username))
        if not u or not u.active or not passwords.verify(p.password, u.password): raise HTTPException(401,'用户名或密码错误')
        token = secrets.token_urlsafe(48)
        db.add(Refresh(digest=digest(token), user_id=u.id, expires_at=now()+timedelta(days=7)))
        set_cookies(response,u,token)
        return as_dict(u,('password',))

@app.post('/api/auth/refresh')
def refresh(request: Request, response: Response):
    with Session.begin() as db:
        r = db.scalar(select(Refresh).where(Refresh.digest==digest(request.cookies.get('deskflow_refresh',''))).with_for_update())
        if not r or r.expires_at <= now(): raise HTTPException(401,'登录已过期')
        u = db.get(User,r.user_id)
        if not u or not u.active: raise HTTPException(401,'账号已停用')
        db.delete(r)
        token=secrets.token_urlsafe(48)
        db.add(Refresh(digest=digest(token),user_id=u.id,expires_at=now()+timedelta(days=7)))
        set_cookies(response,u,token)
        return {'ok':True}

@app.post('/api/auth/logout')
def logout(request: Request, response: Response):
    with Session.begin() as db:
        r=db.get(Refresh,digest(request.cookies.get('deskflow_refresh','')))
        if r: db.delete(r)
    response.delete_cookie('deskflow_access',path='/api'); response.delete_cookie('deskflow_refresh',path='/api/auth')
    return {'ok':True}

@app.get('/api/auth/me')
def me(u=Depends(current_user)): return as_dict(u,('password',))

def scope(u,q):
    # Evaluation fixtures remain accessible through reports, not the everyday ticket inbox.
    q=q.where(Ticket.owner_id.in_(select(User.id).where(~User.username.like('eval-%'))))
    if u.role=='employee': return q.where(Ticket.owner_id==u.id)
    if u.role=='approver': return q.where((Ticket.department==u.department)|(Ticket.owner_id==u.id))
    return q

@app.get('/api/tickets')
def tickets(q: str='', status: str='', u=Depends(current_user)):
    with Session() as db:
        stmt=scope(u,select(Ticket))
        if q: stmt=stmt.where(Ticket.title.ilike('%'+q[:100]+'%'))
        if status: stmt=stmt.where(Ticket.status==status)
        return [as_dict(t)|{'owner_name':db.get(User,t.owner_id).name} for t in db.scalars(stmt.order_by(Ticket.updated_at.desc()).limit(250)).all()]

class NewTicket(Strict):
    title: str=Field(min_length=2,max_length=200)
    description: str=Field(min_length=2,max_length=10000)
    kind: str='auto'
    fields: dict=Field(default_factory=dict)
    request_key: str=Field(min_length=8,max_length=100)

@app.post('/api/tickets')
def create_ticket(p: NewTicket,u=Depends(current_user)):
    if p.kind not in ('auto','account','permission','software','device') or len(json.dumps(p.fields))>4000: raise HTTPException(422,'参数无效')
    hashed=digest(p.model_dump(exclude={'request_key'}))
    with Session.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:k,0))'),{'k':u.id+p.request_key})
        old=db.scalar(select(Ticket).where(Ticket.owner_id==u.id,Ticket.request_key==p.request_key))
        if old:
            if old.request_hash!=hashed: raise HTTPException(409,'幂等标识已用于不同内容')
            return as_dict(old)
        t=Ticket(id=uid(),owner_id=u.id,department=u.department,request_hash=hashed,**p.model_dump())
        db.add(t); db.flush()
        return as_dict(t)

@app.get('/api/tickets/{ticket_id}')
def detail(ticket_id: str,u=Depends(current_user)):
    with Session() as db:
        t=get_ticket(db,ticket_id,u)
        plans=db.scalars(select(Plan).where(Plan.ticket_id==t.id).order_by(Plan.created_at)).all()
        return {'ticket':as_dict(t)|{'owner_name':db.get(User,t.owner_id).name},
                'comments':[as_dict(c) for c in db.scalars(select(Comment).where(Comment.ticket_id==t.id).order_by(Comment.created_at)).all()],
                'plans':[as_dict(p)|{'approval':as_dict(a) if (a:=db.scalar(select(Approval).where(Approval.plan_id==p.id))) else None,'operation':as_dict(o) if (o:=db.get(Operation,p.id)) else None} for p in plans],
                'runs':[public_run(r) for r in db.scalars(select(Run).where(Run.ticket_id==t.id).order_by(Run.created_at.desc())).all()]}

class Mutation(Strict):
    version: int
    note: str=Field(default='',max_length=5000)
    fields: dict=Field(default_factory=dict)
    assignee_id: str|None=None

@app.post('/api/tickets/{ticket_id}/actions/{action}')
def mutate(ticket_id: str,action: str,p: Mutation,u=Depends(current_user)):
    if action not in ('comment','claim','assign','takeover','resolve','confirm','reopen','cancel'): raise HTTPException(404,'未知操作')
    with Session.begin() as db:
        t=get_ticket(db,ticket_id,u,True)
        if t.version!=p.version: raise HTTPException(409,'工单已更新，请刷新')
        if action in ('claim','assign','takeover','resolve'): require_role(u,'support')
        elif u.id!=t.owner_id and u.role!='support': raise HTTPException(403,'仅申请人或处理人员可操作')
        if action=='comment':
            if not p.note.strip() or t.status in ('closed','cancelled'): raise HTTPException(422,'请填写内容并确认工单未关闭')
            if len(json.dumps(p.fields))>4000: raise HTTPException(422,'参数过长')
            # New information invalidates any outstanding action proposal, not only structured fields.
            invalidate(db,t,'申请人补充信息，需重新核对')
            t.fields={**t.fields,**p.fields}; t.status='pending'
        elif action in ('claim','assign','takeover'):
            target=db.get(User,p.assignee_id or u.id)
            if not target or target.role!='support' or not target.active: raise HTTPException(422,'处理人员不可用')
            invalidate(db,t,'人工接管'); t.assignee_id=target.id; t.status='waiting_human'
        elif action=='resolve':
            if not p.note.strip(): raise HTTPException(422,'请填写实际维修或处理结果')
            invalidate(db,t,'人工处理完成'); t.status='waiting_confirmation'
        elif action=='confirm':
            if t.status!='waiting_confirmation': raise HTTPException(409,'尚未完成处理')
            t.status='closed'
        elif action=='reopen': invalidate(db,t,'重新打开'); t.status='pending'
        elif action=='cancel': invalidate(db,t,'取消工单'); t.status='cancelled'
        t.version+=1; t.updated_at=now()
        labels={'claim':'认领','assign':'转派','takeover':'人工接管','confirm':'确认解决','reopen':'重新打开','cancel':'取消'}
        db.add(Comment(ticket_id=t.id,author_id=u.id,author_name=u.name,body=p.note or labels.get(action,'工单更新')))
        db.add(Event(ticket_id=t.id,kind='ticket_updated',data={'action':action,'status':t.status,'actor':u.name}))
        return as_dict(t)

class Start(Strict):
    version: int
    mode: str='mock'

@app.post('/api/tickets/{ticket_id}/runs')
def start(ticket_id: str,p: Start,u=Depends(current_user)):
    with Session.begin() as db:
        t=get_ticket(db,ticket_id,u,True)
        if u.id!=t.owner_id and u.role!='support': raise HTTPException(403,'无权分析')
        if t.version!=p.version: raise HTTPException(409,'工单已更新，请刷新')
        r=start_run(db,t,p.mode); db.flush(); return public_run(r)

@app.post('/api/runs/{run_id}/{action}')
def control(run_id: str,action: str,u=Depends(current_user)):
    if action not in ('pause','resume'): raise HTTPException(404,'未知操作')
    with Session.begin() as db:
        r=db.get(Run,run_id)
        if not r: raise HTTPException(404,'运行不存在')
        t=get_ticket(db,r.ticket_id,u,True)
        if u.id!=t.owner_id and u.role!='support': raise HTTPException(403,'无权控制')
        r=db.scalar(select(Run).where(Run.id==run_id).with_for_update().execution_options(populate_existing=True))
        if r.generation!=t.generation: raise HTTPException(409,'运行已被新操作替代')
        allowed=('queued','running') if action=='pause' else ('paused','failed')
        if r.status not in allowed: raise HTTPException(409,'当前状态不允许此操作')
        r.status='paused' if action=='pause' else 'queued'; r.error='用户暂停' if action=='pause' else ''
        r.lease_token=None; r.lease_until=None
        return public_run(r)

@app.get('/api/tickets/{ticket_id}/events')
async def events(ticket_id: str,request: Request,after: int=0,u=Depends(current_user)):
    with Session() as db: get_ticket(db,ticket_id,u)
    try: cursor=max(after,int(request.headers.get('last-event-id','0')))
    except ValueError: raise HTTPException(422,'无效事件序号')
    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            with Session() as db:
                live=db.get(User,u.id)
                if not live or not live.active: return
                get_ticket(db,ticket_id,live)
                rows=[as_dict(e) for e in db.scalars(select(Event).where(Event.ticket_id==ticket_id,Event.id>cursor).order_by(Event.id).limit(100)).all()]
            for row in rows:
                cursor=row['id']; yield f'id: {cursor}\ndata: {json.dumps(row,ensure_ascii=False)}\n\n'
            if not rows: yield ': heartbeat\n\n'
            await asyncio.sleep(1)
    return StreamingResponse(stream(),media_type='text/event-stream',headers={'Cache-Control':'no-cache','X-Accel-Buffering':'no'})

@app.get('/api/approvals')
def approvals(u=Depends(current_user)):
    with Session() as db:
        rows=db.execute(select(Approval,Plan,Ticket).join(Plan,Approval.plan_id==Plan.id).join(Ticket,Plan.ticket_id==Ticket.id).order_by(Plan.created_at.desc())).all()
        return [as_dict(a)|{'plan':as_dict(p),'ticket':as_dict(t)} for a,p,t in rows if visible(u,t)]

class Decision(Strict):
    decision: str
    note: str=Field(default='',max_length=2000)
    plan_digest: str

@app.post('/api/approvals/{approval_id}/decision')
def decision(approval_id: str,p: Decision,u=Depends(current_user)):
    require_role(u,'approver')
    if p.decision not in ('approved','rejected','returned'): raise HTTPException(422,'审批决定无效')
    with Session.begin() as db:
        a=db.get(Approval,approval_id)
        if not a: raise HTTPException(404,'审批不存在')
        plan=db.get(Plan,a.plan_id); t=get_ticket(db,plan.ticket_id,u,True)
        a=db.scalar(select(Approval).where(Approval.id==approval_id).with_for_update())
        policy=db.get(Config,'policy')
        if u.id==t.owner_id or u.department!=t.department: raise HTTPException(403,'不能审批本人或其他部门申请')
        if plan.digest!=p.plan_digest or plan.generation!=t.generation or plan.policy_version!=(policy.version if policy else 1): raise HTTPException(409,'方案或策略已变化，请重新分析')
        if a.status==p.decision and a.approver_id==u.id: return as_dict(a)
        if a.status!='pending' or a.expires_at<=now(): raise HTTPException(409,'审批已处理或过期')
        a.status=p.decision; a.approver_id=u.id; a.note=p.note; a.decided_at=now()
        r=db.scalar(select(Run).where(Run.ticket_id==t.id,Run.generation==plan.generation))
        if p.decision=='approved':
            if r and r.status=='waiting_approval': r.status='queued'
            t.status='processing'
        else:
            invalidate(db,t,'审批拒绝或退回'); t.status='waiting_human' if p.decision=='rejected' else 'waiting_info'
        t.version+=1
        db.add(Comment(ticket_id=t.id,author_id=u.id,author_name=u.name,body=f'审批 {p.decision}：{p.note}'))
        db.add(Event(ticket_id=t.id,kind='ticket_updated',data={'decision':p.decision}))
        return as_dict(a)

def public_profile(v): return {k:x for k,x in v.items() if k!='encrypted_key'}|{'has_key':bool(v.get('encrypted_key'))}

@app.get('/api/settings')
def configs(u=Depends(current_user)):
    require_role(u,'admin')
    with Session() as db:
        rows={c.key:c for c in db.scalars(select(Config)).all()}
        return {k:public_profile(rows[k].value) if k in rows else {} for k in ('chat','embedding')}|{'policy':{**DEFAULT_POLICY,**(rows['policy'].value if 'policy' in rows else {})}}

class Profile(Strict):
    base_url: str=Field(max_length=500)
    model: str=Field(max_length=200)
    api_key: str=Field(default='',max_length=3000)
    timeout: int=Field(default=45,ge=5,le=120)
    max_tokens: int=Field(default=1500,ge=100,le=8000)
    prompt: str=Field(default=SYSTEM,max_length=12000)
    prompt_version: str=Field(default='v1',max_length=80)

def profile_value(db,kind,p):
    old=db.get(Config,kind)
    v=p.model_dump(exclude={'api_key'})
    v['encrypted_key']=encrypt(p.api_key.strip()) if p.api_key.strip() else (old.value.get('encrypted_key','') if old else '')
    try: providers.validate_profile(v)
    except ValueError as e: raise HTTPException(422,str(e))
    return v

@app.put('/api/settings/{kind}')
def save_profile(kind: str,p: Profile,u=Depends(current_user)):
    require_role(u,'admin')
    if kind not in ('chat','embedding'): raise HTTPException(404,'未知配置')
    with Session.begin() as db:
        v=profile_value(db,kind,p); r=db.get(Config,kind)
        if r: r.value=v; r.version+=1
        else: db.add(Config(key=kind,value=v))
        return public_profile(v)

@app.post('/api/settings/{kind}/test')
async def test_profile(kind: str,p: Profile,u=Depends(current_user)):
    require_role(u,'admin'); start=time.monotonic()
    if kind not in ('chat','embedding'): raise HTTPException(404,'未知配置')
    try:
        with Session() as db: v=profile_value(db,kind,p)
        if kind=='embedding':
            result=await providers.embed(v,['IT 服务台连接测试']); detail=f'返回 {len(result)} 条 {len(result[0])} 维向量'
        else:
            result,_=await providers.chat(v,[{'role':'user','content':'请简短回复连接成功'}]); detail=result['content'][:300]
        return {'ok':True,'detail':detail,'elapsed_ms':round((time.monotonic()-start)*1000)}
    except Exception as e: return {'ok':False,'detail':redact(e.detail if isinstance(e,HTTPException) else e),'elapsed_ms':round((time.monotonic()-start)*1000)}

class PolicyUpdate(Strict):
    max_permission_hours: int=Field(default=168,ge=1,le=168)
    permission_resources: list[str]=['VPN_ACCESS','DEV_READONLY']
    software_skus: list[str]=['IDE_PRO','DIAGRAM_TOOL']
    approval_hours: int=Field(default=24,ge=1,le=72)
    max_rounds: int=Field(default=8,ge=1,le=50)
    max_tools: int=Field(default=20,ge=1,le=100)
    prompt_version: str='v1'

@app.put('/api/policy')
def policy(p: PolicyUpdate,u=Depends(current_user)):
    require_role(u,'admin'); v=p.model_dump()
    if not set(p.permission_resources)<={'VPN_ACCESS','DEV_READONLY'} or not set(p.software_skus)<={'IDE_PRO','DIAGRAM_TOOL'}: raise HTTPException(422,'超出预置目录')
    with Session.begin() as db:
        r=db.get(Config,'policy')
        if not r: r=Config(key='policy',value=dict(DEFAULT_POLICY),version=1); db.add(r)
        if any(r.value.get(k)!=v[k] for k in ('max_permission_hours','permission_resources','software_skus','approval_hours')): r.version+=1
        r.value=v; return {'version':r.version,'policy':v}

@app.get('/api/users')
def users(u=Depends(current_user)):
    require_role(u,'admin','support')
    with Session() as db: return [as_dict(x,('password',)) for x in db.scalars(select(User).where(~User.username.like('eval-%'))).all()]

class UserEdit(Strict):
    name: str=Field(min_length=1,max_length=80)
    department: str=Field(min_length=1,max_length=80)
    role: str
    active: bool=True

@app.put('/api/users/{user_id}')
def edit_user(user_id: str,p: UserEdit,u=Depends(current_user)):
    require_role(u,'admin')
    if p.role not in ('employee','support','approver','admin'): raise HTTPException(422,'角色无效')
    if user_id==u.id and (p.role!='admin' or not p.active): raise HTTPException(409,'不能停用自己')
    with Session.begin() as db:
        row=db.get(User,user_id)
        if not row: raise HTTPException(404,'用户不存在')
        for k,v in p.model_dump().items(): setattr(row,k,v)
        return as_dict(row,('password',))

@app.get('/api/knowledge')
def docs(u=Depends(current_user)):
    with Session() as db:
        return [as_dict(d)|{'versions':[as_dict(v) for v in db.scalars(select(DocumentVersion).where(DocumentVersion.document_id==d.id).order_by(DocumentVersion.number.desc())).all()]} for d in db.scalars(select(Document).order_by(Document.created_at.desc())).all()]

@app.post('/api/knowledge/upload')
async def upload(file: UploadFile=File(...),document_id: str=Form(default=''),u=Depends(current_user)):
    require_role(u,'admin','support'); name=Path(file.filename or '').name[:200]
    if Path(name).suffix.lower() not in ('.md','.txt','.docx','.pdf'): raise HTTPException(422,'仅支持 MD/TXT/DOCX/文本 PDF')
    data=await file.read(10*1024*1024+1)
    if not data or len(data)>10*1024*1024: raise HTTPException(422,'资料必须为 1 字节至 10 MB')
    hashed=hashlib.sha256(data).hexdigest()
    with Session.begin() as db:
        db.execute(text('SELECT pg_advisory_xact_lock(hashtextextended(:k,0))'),{'k':'upload:'+hashed})
        old=db.scalar(select(DocumentVersion).where(DocumentVersion.file_hash==hashed,DocumentVersion.document_id==document_id)) if document_id else db.scalar(select(DocumentVersion).where(DocumentVersion.file_hash==hashed))
        if old: return as_dict(old)
        if document_id:
            d=db.scalar(select(Document).where(Document.id==document_id).with_for_update())
            if not d: raise HTTPException(404,'资料不存在')
        else: d=Document(id=uid(),name=name); db.add(d); db.flush()
        number=(db.scalar(select(func.max(DocumentVersion.number)).where(DocumentVersion.document_id==d.id)) or 0)+1
        v=DocumentVersion(id=uid(),document_id=d.id,number=number,file_hash=hashed,filename=name)
        directory=settings().data_dir/'uploads'; directory.mkdir(parents=True,exist_ok=True); (directory/v.id).write_bytes(data)
        db.add(v); db.flush(); return as_dict(v)

@app.get('/api/knowledge/versions/{version_id}')
def chunks(version_id: str,u=Depends(current_user)):
    with Session() as db:
        v=db.get(DocumentVersion,version_id)
        if not v: raise HTTPException(404,'版本不存在')
        return {'version':as_dict(v),'chunks':[as_dict(c,('embedding','search_vector')) for c in db.scalars(select(Chunk).where(Chunk.version_id==v.id).order_by(Chunk.ordinal)).all()]}

@app.post('/api/knowledge/versions/{version_id}/retry')
def retry(version_id: str,u=Depends(current_user)):
    require_role(u,'admin','support')
    with Session.begin() as db:
        old=db.get(DocumentVersion,version_id)
        if not old: raise HTTPException(404,'版本不存在')
        if old.status in ('queued','processing'): return as_dict(old)
        d=db.scalar(select(Document).where(Document.id==old.document_id).with_for_update())
        n=(db.scalar(select(func.max(DocumentVersion.number)).where(DocumentVersion.document_id==d.id)) or 0)+1
        v=DocumentVersion(id=uid(),document_id=d.id,number=n,file_hash=old.file_hash,filename=old.filename)
        source=settings().data_dir/'uploads'/old.id; (source.parent/v.id).write_bytes(source.read_bytes())
        db.add(v); db.flush(); return as_dict(v)

@app.get('/api/knowledge/search')
async def search(q: str,u=Depends(current_user)):
    if not 1<=len(q)<=1000: raise HTTPException(422,'请输入 1–1000 字符')
    return await knowledge.search(q)

@app.get('/api/business')
async def overview(employee_id: str='',u=Depends(current_user)):
    target=employee_id or u.id
    if target!=u.id: require_role(u,'admin','support')
    return {'employee':await business.call('GET',f'/employees/{target}'),'software':await business.call('GET',f'/software/{target}')}

class Scenario(Strict):
    employee_id: str
    scenario: str='normal'

@app.post('/api/business/scenario')
async def seed_scenario(p: Scenario,u=Depends(current_user)):
    require_role(u,'admin')
    with Session() as db:
        e=db.get(User,p.employee_id)
        if not e or e.username not in ('employee','employee2','support','approver','admin'): raise HTTPException(422,'仅可初始化预置演示账号')
    return await business.call('POST','/seed',{'id':e.id,'name':e.name,'department':e.department,'scenario':p.scenario})

@app.get('/api/runs')
def all_runs(u=Depends(current_user)):
    with Session() as db: return [public_run(r) for r in db.scalars(select(Run).where(Run.ticket_id.in_(scope(u,select(Ticket.id)))).order_by(Run.created_at.desc()).limit(200)).all()]

@app.get('/api/runs/{run_id}/trace')
def trace(run_id: str,u=Depends(current_user)):
    with Session() as db:
        r=db.get(Run,run_id)
        if not r: raise HTTPException(404,'运行不存在')
        get_ticket(db,r.ticket_id,u)
        return {'run':public_run(r),'events':[as_dict(e) for e in db.scalars(select(Event).where(Event.run_id==r.id).order_by(Event.id)).all()]}

@app.get('/api/notifications')
def notifications(u=Depends(current_user)):
    with Session() as db: return [as_dict(e) for e in db.scalars(select(Event).where(Event.ticket_id.in_(scope(u,select(Ticket.id))),Event.kind.in_(['answer','plan','error','ticket_updated'])).order_by(Event.id.desc()).limit(30)).all()]

class EvalStart(Strict):
    mode: str='mock'
    label: str=Field(default='基线 v1',max_length=100)
    split: str='all'

@app.get('/api/evaluations')
def evaluations(u=Depends(current_user)):
    require_role(u,'admin','support')
    with Session() as db: return [as_dict(e,('config_snapshot',)) for e in db.scalars(select(Evaluation).order_by(Evaluation.created_at.desc())).all()]

@app.post('/api/evaluations')
def create_eval(p: EvalStart,u=Depends(current_user)):
    require_role(u,'admin')
    if p.mode not in ('mock','real') or p.split not in ('all','dev','holdout'): raise HTTPException(422,'无效模式或数据集')
    with Session.begin() as db:
        profile=db.get(Config,'chat'); config=dict(profile.value) if profile else {}
        if p.mode=='real':
            try: providers.validate_profile(config)
            except ValueError as e: raise HTTPException(422,str(e))
        from .evaluation import cases
        config['knowledge_version_ids']=knowledge.active_versions(db)
        rules=db.get(Config,'policy')
        e=Evaluation(id=uid(),mode=p.mode,label=p.label,config_snapshot={'chat':config,'split':p.split,'dataset_hash':digest(cases()),'policy_version':rules.version if rules else 1})
        db.add(e); db.flush(); return as_dict(e,('config_snapshot',))

@app.get('/api/evaluations/{evaluation_id}/report')
def report(evaluation_id: str,u=Depends(current_user)):
    require_role(u,'admin','support')
    with Session() as db:
        e=db.get(Evaluation,evaluation_id)
        if not e: raise HTTPException(404,'评测不存在')
        lines=[f'# {e.label}',f'模式：{e.mode}（mock 为模拟流程，不代表真实模型效果）',f'状态：{e.status}','## 汇总','```json',json.dumps(e.summary,ensure_ascii=False,indent=2),'```','## 逐条结果']
        for r in e.results: lines += ['',f"### {r['case_id']} {r.get('name','')}",f"通过：{r.get('passed')}；预期：{r.get('expected')}；实际：{r.get('actual')}",f"说明：{r.get('error','')}"]
        return Response('\n'.join(lines),media_type='text/markdown',headers={'Content-Disposition':'attachment; filename="deskflow-evaluation.md"'})

@app.exception_handler(business.BusinessError)
async def business_error(request,exc): return JSONResponse(status_code=503,content={'detail':redact(exc)})
