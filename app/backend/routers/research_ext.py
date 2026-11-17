from __future__ import annotations
import json,threading,uuid
from pathlib import Path
from datetime import datetime,timezone
from fastapi import APIRouter,Depends,HTTPException
from pydantic import BaseModel,Field
from ..dependencies import WorkspaceContext,get_workspace_context,require_workspace_roles
from research.engine import ROOT,industrial_case,cp_solve
from research.runner import RESULTS,run_suite,write_atomic

router=APIRouter(prefix='/api/workspaces/{workspace_id}/research',tags=['research'])
_guard=threading.Lock();_running:set[int]=set()
class RunInput(BaseModel):
    suite:str=Field(default='biw',pattern='^(biw|hfut|all|delivery)$')
    time_limit_seconds:float=Field(default=5,ge=.1,le=30)
    seeds:int=Field(default=3,ge=1,le=5)
class IndustrialInput(BaseModel):
    budget:int=Field(default=80,ge=0,le=1000)
    time_limit_seconds:float=Field(default=5,ge=.1,le=30)

@router.get('/industrial')
def industrial(ctx:WorkspaceContext=Depends(get_workspace_context)):
    d=json.loads((ROOT/'data/processed/automotive_biw.json').read_text(encoding='utf-8'))
    return {k:v for k,v in d.items() if k!='source_file'}

@router.post('/industrial/solve')
def solve_industrial(payload:IndustrialInput,ctx:WorkspaceContext=Depends(require_workspace_roles('admin','planner'))):
    return cp_solve(industrial_case(payload.budget),payload.time_limit_seconds)

@router.get('/industrial/latest')
def latest_industrial(ctx:WorkspaceContext=Depends(get_workspace_context)):
    path=RESULTS/str(ctx.workspace.id)
    for p in sorted(path.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        try:d=json.loads(p.read_text(encoding='utf-8'))
        except (OSError,ValueError):continue
        if d.get('status')!='completed':continue
        rows=[r for r in d.get('results',[]) if r.get('case')=='BIW-43' and r.get('phase')=='static' and r.get('algorithm')=='CP-SAT' and r.get('validation',{}).get('valid')]
        if rows:return min(rows,key=lambda r:r['makespan'])
    return None

@router.get('/runs')
def runs(ctx:WorkspaceContext=Depends(get_workspace_context)):
    path=RESULTS/str(ctx.workspace.id);items=[]
    for p in sorted(path.glob('*.json'),key=lambda p:p.stat().st_mtime,reverse=True)[:30]:
        try:
            d=json.loads(p.read_text(encoding='utf-8'));items.append({k:v for k,v in d.items() if k not in ['results','traceback']})
        except (OSError,ValueError):continue
    return {'items':items}

@router.get('/runs/{run_id}')
def run_detail(run_id:str,ctx:WorkspaceContext=Depends(get_workspace_context)):
    if len(run_id)!=32 or any(c not in '0123456789abcdef' for c in run_id):raise HTTPException(404,'实验不存在')
    p=RESULTS/str(ctx.workspace.id)/f'{run_id}.json'
    if not p.exists():raise HTTPException(404,'实验不存在')
    return json.loads(p.read_text(encoding='utf-8'))

@router.post('/run',status_code=202)
def start_run(payload:RunInput,ctx:WorkspaceContext=Depends(require_workspace_roles('admin','planner'))):
    with _guard:
        if ctx.workspace.id in _running:raise HTTPException(409,'当前工作区已有实验运行中')
        _running.add(ctx.workspace.id)
    run_id=uuid.uuid4().hex;wid=ctx.workspace.id
    write_atomic(RESULTS/str(wid)/f'{run_id}.json',{'id':run_id,'workspace_id':wid,'suite':payload.suite,'status':'queued','created_at':datetime.now(timezone.utc).isoformat()})
    def worker():
        try:
            if payload.suite=='delivery':
                from research.delivery import run_delivery
                run_delivery(wid,payload.time_limit_seconds,payload.seeds,run_id)
            else:run_suite(wid,payload.suite,payload.time_limit_seconds,payload.seeds,run_id)
        finally:
            with _guard:_running.discard(wid)
    threading.Thread(target=worker,daemon=True,name='jiaoqi-research').start()
    return {'id':run_id,'status':'queued'}
