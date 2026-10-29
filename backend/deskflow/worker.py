import asyncio, contextlib, logging, sys
from datetime import timedelta
from sqlalchemy import select, update, text
from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
from langgraph.types import Command
from .config import settings
from .db import Session, now, uid, engine
from .models import Run, Ticket, Event, DocumentVersion, Evaluation
from .workflow import build_graph, Paused, Obsolete
from .security import redact
from . import knowledge, business

log=logging.getLogger('deskflow.worker')

def claim():
    with Session.begin() as db:
        r=db.scalar(select(Run).where((Run.status=='queued')|((Run.status=='running')&(Run.lease_until<now()))).order_by(Run.created_at).with_for_update(skip_locked=True).limit(1))
        if not r: return None
        r.status='running'; r.lease_token=uid(); r.lease_until=now()+timedelta(seconds=settings().lease_seconds)
        return r.id,r.lease_token

def renew(run_id,token):
    with Session.begin() as db:
        db.execute(update(Run).where(Run.id==run_id,Run.lease_token==token,Run.status=='running').values(lease_until=now()+timedelta(seconds=settings().lease_seconds)))

async def process(run_id,token,saver):
    async def heartbeat():
        while True:
            await asyncio.sleep(15)
            await asyncio.to_thread(renew,run_id,token)
    pulse=asyncio.create_task(heartbeat())
    graph=build_graph(saver,token); config={'configurable':{'thread_id':run_id},'recursion_limit':500}
    try:
        snapshot=await graph.aget_state(config)
        interrupted=any(t.interrupts for t in snapshot.tasks)
        initial=Command(resume={'continue':True}) if interrupted else None if snapshot.values else {'run_id':run_id}
        result=await graph.ainvoke(initial,config)
        with Session.begin() as db:
            r=db.get(Run,run_id)
            if r.lease_token==token and r.status=='running':
                if result.get('__interrupt__'):
                    # Approval may arrive between graph interrupt and status persistence.
                    from .models import Plan, Approval
                    p=db.scalar(select(Plan).where(Plan.ticket_id==r.ticket_id,Plan.generation==r.generation))
                    a=db.scalar(select(Approval).where(Approval.plan_id==p.id)) if p else None
                    r.status='queued' if a and a.status=='approved' else 'waiting_approval'
                    r.phase='approval'
                else: r.status='completed'; r.finished_at=now()
    except Obsolete:
        pass
    except Exception as exc:
        with Session.begin() as db:
            r=db.get(Run,run_id)
            if r and r.lease_token==token and r.status=='running':
                r.status='paused' if isinstance(exc,Paused) else 'failed'
                r.error=redact(exc)
                db.add(Event(ticket_id=r.ticket_id,run_id=r.id,kind='error',data={'message':r.error,'status':r.status}))
        log.warning('run %s: %s',run_id,redact(exc))
    finally:
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError): await pulse

async def runner(saver):
    while True:
        task=await asyncio.to_thread(claim)
        if task: await process(*task,saver)
        else: await asyncio.sleep(.5)

async def maintenance(saver):
    # Session advisory lock allows only one ingestion/evaluation scheduler across worker processes.
    connection=engine.connect()
    if not connection.execute(text('SELECT pg_try_advisory_lock(77182026)')).scalar():
        connection.close(); return
    connection.commit()
    try:
        with Session.begin() as db:
            db.execute(update(DocumentVersion).where(DocumentVersion.status=='processing').values(status='queued'))
            db.execute(update(Evaluation).where(Evaluation.status=='running').values(status='queued'))
        while True:
            with Session() as db:
                v=db.scalar(select(DocumentVersion).where(DocumentVersion.status=='queued').order_by(DocumentVersion.created_at).limit(1))
                e=db.scalar(select(Evaluation).where(Evaluation.status=='queued').order_by(Evaluation.created_at).limit(1))
                version_id=v.id if v else None; evaluation_id=e.id if e else None
            if version_id: await knowledge.ingest(version_id)
            if evaluation_id:
                from .evaluation import evaluate
                await evaluate(evaluation_id,saver)
            try: await business.call('POST','/maintenance/expire')
            except business.BusinessError: pass
            await asyncio.sleep(2)
    finally: connection.close()

async def main():
    async with AsyncPostgresSaver.from_conn_string(settings().checkpoint_dsn) as saver:
        await saver.setup()
        await asyncio.gather(maintenance(saver),*[runner(saver) for _ in range(settings().worker_concurrency)])

if __name__=='__main__':
    logging.basicConfig(level=logging.INFO)
    logging.getLogger('httpx').setLevel(logging.WARNING)
    if sys.platform=='win32': asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    asyncio.run(main())
