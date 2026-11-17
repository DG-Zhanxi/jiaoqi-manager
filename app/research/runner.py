from __future__ import annotations
import json,platform,time,uuid,statistics,traceback
from pathlib import Path
from datetime import datetime,timezone
from .engine import ROOT,industrial_case,hfut_cases,greedy,cp_solve,disruption_case,local_solve,validate

RESULTS=ROOT/'experiments'

def write_atomic(path:Path,data:dict):
    path.parent.mkdir(parents=True,exist_ok=True)
    tmp=path.with_suffix('.tmp')
    tmp.write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    # Windows readers and antivirus scans can briefly hold the destination open.
    for attempt in range(12):
        try:
            tmp.replace(path)
            return
        except PermissionError:
            if attempt == 11:
                raise
            time.sleep(min(0.02 * (attempt + 1), 0.2))

def run_suite(workspace_id:int,suite='all',time_limit=5,seeds=3,run_id:str|None=None)->dict:
    import ortools
    run_id=run_id or uuid.uuid4().hex
    out=RESULTS/str(workspace_id)/f'{run_id}.json'
    payload={'id':run_id,'workspace_id':workspace_id,'suite':suite,'status':'running','created_at':datetime.now(timezone.utc).isoformat(),'environment':{'python':platform.python_version(),'platform':platform.platform(),'ortools':ortools.__version__},'protocol':{'revision':2,'time_limit_seconds':time_limit,'seeds':list(range(seeds)),'workers':1,'static_algorithms':['FCFS','EDD','SPT','CP-SAT'],'dynamic_algorithms':['FCFS-repair','CP-SAT-global','CP-SAT-local'],'dynamic_event':'resource outage at operation boundary; duration=60% selected task duration rounded UP to 0.1 second','known_at':'outage start; fixed all tasks started before decision time','hfut_batch_policy':'unsplit lot','hfut_due_date_rule':'1.5 * minimum chain duration; not a supplied customer date'},'results':[],'summary':[]}
    write_atomic(out,payload);begin=time.perf_counter()
    try:
        cases=[]
        if suite in ('all','biw'):cases.append(industrial_case())
        if suite in ('all','hfut'):cases.extend(hfut_cases())
        for case in cases:
            for seed in range(seeds):
                baseline=None
                for rule in ['FCFS','EDD','SPT','CP-SAT']:
                    result=cp_solve(case,time_limit,seed) if rule=='CP-SAT' else greedy(case,rule,seed)
                    result.update({'case':case.name,'phase':'static','source':case.source,'task_count':len(case.tasks),'resource_count':len(case.resources)})
                    payload['results'].append(result)
                    if rule=='FCFS':baseline=result['tasks']
                    write_atomic(out,payload)
                event_case=disruption_case(case,baseline,seed)
                for rule in ['FCFS-repair','CP-SAT-global','CP-SAT-local']:
                    if rule=='FCFS-repair':result=greedy(event_case,'FCFS',seed)
                    elif rule=='CP-SAT-global':result=cp_solve(event_case,time_limit,seed,baseline)
                    else:result=local_solve(event_case,baseline,time_limit,seed)
                    result['algorithm']=rule;result['validation']=validate(event_case,result['tasks'])
                    result['changed_operations']=sum(1 for x in result['tasks'] if any(b['id']==x['id'] and (b['resource']!=x['resource'] or abs(b['start']-x['start'])>.05) for b in baseline))
                    result.update({'case':case.name,'phase':'dynamic','event':event_case.outages,'decision_time':event_case.now,'fixed_task_ids':list(event_case.fixed),'source':case.source,'task_count':len(case.tasks),'resource_count':len(case.resources)})
                    payload['results'].append(result);write_atomic(out,payload)
        algorithms=sorted({r['algorithm'] for r in payload['results']})
        for algorithm in algorithms:
            rows=[r for r in payload['results'] if r['algorithm']==algorithm];valid=[r for r in rows if r['validation']['valid']]
            payload['summary'].append({'algorithm':algorithm,'count':len(rows),'feasible_rate':len(valid)/len(rows),'makespan_mean':statistics.mean(r['makespan'] for r in valid) if valid else 0,'solve_seconds_mean':statistics.mean(r['wall_time'] for r in rows),'changed_operations_mean':statistics.mean(r.get('changed_operations',0) for r in valid) if valid else 0})
        payload['status']='completed';payload['duration_seconds']=time.perf_counter()-begin;payload['finished_at']=datetime.now(timezone.utc).isoformat();payload['case_count']=len(cases)
    except Exception as exc:
        payload['status']='failed';payload['error']=str(exc);payload['traceback']=traceback.format_exc()
    write_atomic(out,payload);return payload

if __name__=='__main__':
    import argparse
    p=argparse.ArgumentParser();p.add_argument('--suite',default='all',choices=['all','biw','hfut']);p.add_argument('--seconds',type=float,default=5);p.add_argument('--seeds',type=int,default=3);p.add_argument('--workspace',type=int,default=1);a=p.parse_args()
    r=run_suite(a.workspace,a.suite,a.seconds,a.seeds);print(json.dumps({k:v for k,v in r.items() if k not in ['results','traceback']},ensure_ascii=False,indent=2))
