from datetime import datetime,timedelta
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..dependencies import WorkspaceContext,require_workspace_roles,get_db
from ..models import Operation,Order,Plan,PlanTask
from ..services import audit
from ..timeutil import utcnow,ensure_utc

router=APIRouter(prefix='/api/workspaces/{workspace_id}/operations',tags=['task-locks'])
class LockRequest(BaseModel):
    locked:bool
    plan_id:int=Field(ge=1)
class FreezeRequest(BaseModel):
    plan_id:int=Field(ge=1)
    minutes:int=Field(ge=1,le=1440)
    from_at:datetime|None=None

def get_plan(db,ctx,pid):
    p=db.get(Plan,pid)
    if not p or p.workspace_id!=ctx.workspace.id:raise HTTPException(404,'计划不存在')
    return p

@router.post('/{operation_id}/lock')
def lock(operation_id:int,body:LockRequest,ctx:WorkspaceContext=Depends(require_workspace_roles('admin','planner')),db:Session=Depends(get_db)):
    plan=get_plan(db,ctx,body.plan_id);op=db.get(Operation,operation_id)
    if not op or db.get(Order,op.order_id).workspace_id!=ctx.workspace.id:raise HTTPException(404,'工序不存在')
    if op.status!='planned':raise HTTPException(409,'已开始执行的任务不能修改锁定状态')
    task=next((t for t in plan.tasks if t.operation_id==operation_id),None)
    if not task:raise HTTPException(404,'该工序不在所选计划中')
    op.fixed_start_at=task.start_at if body.locked else None;op.fixed_resource_id=task.resource_id if body.locked else None
    audit.record(db,workspace_id=ctx.workspace.id,actor_id=ctx.user.id,action='task.lock' if body.locked else 'task.unlock',entity_type='operation',entity_id=op.id,details={'plan_id':plan.id});audit.bump_revision(db,ctx.workspace);db.commit()
    return {'operation_id':op.id,'locked':body.locked}

@router.post('/freeze')
def freeze(body:FreezeRequest,ctx:WorkspaceContext=Depends(require_workspace_roles('admin','planner')),db:Session=Depends(get_db)):
    plan=get_plan(db,ctx,body.plan_id);start=ensure_utc(body.from_at) if body.from_at else utcnow();end=start+timedelta(minutes=body.minutes);ids=[]
    for t in plan.tasks:
        if start<=ensure_utc(t.start_at)<end:
            op=db.get(Operation,t.operation_id)
            if op and op.status=='planned':op.fixed_start_at=t.start_at;op.fixed_resource_id=t.resource_id;ids.append(op.id)
    if ids:audit.bump_revision(db,ctx.workspace)
    audit.record(db,workspace_id=ctx.workspace.id,actor_id=ctx.user.id,action='tasks.freeze',entity_type='plan',entity_id=plan.id,details={'operation_ids':ids,'from':start.isoformat(),'to':end.isoformat()});db.commit()
    return {'count':len(ids),'operation_ids':ids}
