from pathlib import Path
from datetime import datetime,timezone
import uuid,time,json,statistics,platform
from .engine import ROOT,hfut_cases,packaging_case,greedy,cp_solve
from .runner import write_atomic,RESULTS

def run_delivery(workspace_id=1,time_limit=5,seeds=3,run_id=None):
    rid=run_id or uuid.uuid4().hex;out=RESULTS/str(workspace_id)/f'{rid}.json';d={'id':rid,'workspace_id':workspace_id,'suite':'delivery','status':'running','created_at':datetime.now(timezone.utc).isoformat(),'protocol':{'revision':2,'objective':'two-stage: minimize total tardiness (80% time); then minimize makespan without increasing attained tardiness','optimal_status':'OPTIMAL only when both stages proved optimal','workers':1,'seeds':list(range(seeds)),'time_limit_seconds':time_limit,'hfut_due_rule':'1.5 * shortest operation chain','packaging_due_rule':'original release and deadline columns','warm_start':'FCFS feasible schedule'},'results':[],'summary':[]}
    write_atomic(out,d)
    try:
        cases=hfut_cases()+[packaging_case()]
        for case in cases:
            for seed in range(seeds):
                baseline=None
                for alg in ['FCFS','EDD','SPT','CP-SAT-delivery']:
                    if alg=='CP-SAT-delivery':r=cp_solve(case,time_limit,seed,baseline=baseline,delivery=True)
                    else:r=greedy(case,alg,seed)
                    if alg=='FCFS':baseline=r['tasks']
                    r.update({'case':case.name,'phase':'delivery','source':case.source,'task_count':len(case.tasks),'resource_count':len(case.resources)});d['results'].append(r);write_atomic(out,d)
        for alg in ['FCFS','EDD','SPT','CP-SAT-delivery']:
            rs=[r for r in d['results'] if r['algorithm']==alg];valid=[r for r in rs if r['validation']['valid']]
            d['summary'].append({'algorithm':alg,'count':len(rs),'feasible_rate':len(valid)/len(rs),'makespan_mean':statistics.mean(r['makespan'] for r in valid) if valid else 0,'tardiness_mean':statistics.mean(r['validation']['tardiness'] for r in valid) if valid else 0,'solve_seconds_mean':statistics.mean(r['wall_time'] for r in rs)})
        d['status']='completed';d['case_count']=len(cases);d['finished_at']=datetime.now(timezone.utc).isoformat()
    except Exception as exc:d['status']='failed';d['error']=str(exc)
    write_atomic(out,d);return d

if __name__=='__main__':
    r=run_delivery();print(json.dumps({k:v for k,v in r.items() if k!='results'},ensure_ascii=False,indent=2))
