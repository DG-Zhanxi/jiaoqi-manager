from __future__ import annotations
from dataclasses import dataclass,field
from pathlib import Path
import json,time,math,random,os
from ortools.sat.python import cp_model

ROOT=Path(os.getenv('JIAOQI_ROOT',str(Path(__file__).resolve().parents[2])))
SCALE=10

@dataclass
class Case:
    name:str
    tasks:list[dict]
    resources:list[str]
    budget:int|None=None
    source:dict=field(default_factory=dict)
    outages:list[dict]=field(default_factory=list)
    fixed:dict[int,dict]=field(default_factory=dict)
    now:float=0

def industrial_case(budget:int|None=None)->Case:
    d=json.loads((ROOT/'data/processed/automotive_biw.json').read_text(encoding='utf-8'))
    tasks=[{'id':t['id'],'name':t['name'],'predecessors':t['predecessors'],'release':0,'due':None,'job':1,'options':[{'resource':t['resource'],'duration':m['duration'],'cost':m['labor_cost'],'mode':m['id']} for m in t['modes']]} for t in d['tasks']]
    return Case('BIW-43',tasks,d['resources'],d['budget'] if budget is None else budget,{'doi':d['source_doi'],'sha256':d['sha256'],'institution':d['institution'],'time_unit':'seconds'})

def hfut_cases()->list[Case]:
    from backend.adapters.hfut import HfutAdapter
    adapter=HfutAdapter(ROOT/'data/raw/open_alternatives/hfut/a dataset.xlsx')
    result=[]
    for instance in adapter.available_instances():
        p=adapter.parse(instance=instance.label)
        if not p.valid:
            raise ValueError(f'{instance.label}: {p.errors}')
        tasks=[];prev={};chain={}
        for k,op in enumerate(sorted(p.operations,key=lambda x:(int(x.job_key),x.sequence)),1):
            options=[{'resource':o.machine_code,'duration':o.duration_seconds+op.setup_seconds,'mode':0,'cost':0} for o in op.candidates]
            tasks.append({'id':k,'name':op.operation_key,'predecessors':[prev[op.job_key]] if op.job_key in prev else [],'release':0,'due':None,'job':int(op.job_key),'options':options})
            prev[op.job_key]=k;chain[op.job_key]=chain.get(op.job_key,0)+min(o['duration'] for o in options)
        # Fixed, documented due-date protocol for order-level experiments.
        for t in tasks:t['due']=round(chain[str(t['job'])]*1.5)
        result.append(Case('HFUT-'+instance.label,tasks,sorted({o['resource'] for t in tasks for o in t['options']}),source={'doi':'10.17632/mwrgpx7hhp.3','sha256':p.workbook_sha256,'instance':p.instance_key,'mapping':p.mapping,'due_date_rule':'1.5 * minimum total processing duration per job','batch_policy':'unsplit lot; each operation setup charged once'}))
    return result

def validate(case:Case,schedule:list[dict])->dict:
    errors=[];by={t['id']:t for t in schedule};expected={t['id']:t for t in case.tasks}
    if len(by)!=len(schedule):errors.append('duplicate task')
    if set(by)!=set(expected):errors.append('task coverage mismatch')
    labor=0
    for tid,x in by.items():
        t=expected.get(tid)
        if t is None:continue
        options=[o for o in t['options'] if o['resource']==x['resource'] and o['mode']==x.get('mode',0)]
        if len(options)!=1:errors.append(f'{tid}: invalid resource or mode');continue
        o=options[0];labor+=o['cost']
        if abs(x['end']-x['start']-o['duration'])>.051:errors.append(f'{tid}: duration mismatch')
        if x['start']+1e-6<t.get('release',0):errors.append(f'{tid}: before release')
        if tid not in case.fixed and x['start']+1e-6<case.now:errors.append(f'{tid}: before decision time')
        for p in t['predecessors']:
            if p not in by or by[p]['end']>x['start']+1e-6:errors.append(f'{tid}: precedence {p}')
        for event in case.outages:
            if x['resource']==event['resource'] and x['start']<event['end']-1e-6 and x['end']>event['start']+1e-6:errors.append(f'{tid}: outage conflict')
        if tid in case.fixed:
            f=case.fixed[tid]
            if f['resource']!=x['resource'] or abs(f['start']-x['start'])>.051 or f.get('mode',0)!=x.get('mode',0):errors.append(f'{tid}: fixed task changed')
    for resource in case.resources:
        ts=sorted((x for x in schedule if x['resource']==resource),key=lambda x:x['start'])
        for a,b in zip(ts,ts[1:]):
            if a['end']>b['start']+1e-6:errors.append(f'{resource}: overlap {a["id"]}/{b["id"]}')
    if case.budget is not None and labor>case.budget:errors.append('labor budget exceeded')
    completions={};dues={}
    for t in case.tasks:
        if t['id'] in by:completions[t['job']]=max(completions.get(t['job'],0),by[t['id']]['end'])
        if t.get('due') is not None:dues[t['job']]=t['due']
    tardy=sum(max(0,completions.get(j,0)-d) for j,d in dues.items())
    return {'valid':not errors,'errors':errors,'labor_used':labor,'makespan':max((t['end'] for t in schedule),default=0),'tardiness':tardy,'late_orders':sum(completions.get(j,0)>d for j,d in dues.items()),'checks':['coverage','duration','eligibility','precedence','resource_capacity','release','decision_time','outages','fixed_tasks','labor_budget']}

def earliest_gap(blocked:list[tuple[float,float]],start:float,duration:float)->float:
    for a,b in sorted(blocked):
        if start+duration<=a+1e-7:break
        if start<b-1e-7 and start+duration>a+1e-7:start=b
    return start

def greedy(case:Case,rule='FCFS',seed=0)->dict:
    begin=time.perf_counter();placed={i:dict(t) for i,t in case.fixed.items()};remaining={t['id']:t for t in case.tasks if t['id'] not in placed};blocks={r:[] for r in case.resources};cost=0
    for t in case.tasks:
        if t['id'] in placed:
            x=placed[t['id']];blocks[x['resource']].append((x['start'],x['end']));cost+=next(o['cost'] for o in t['options'] if o['resource']==x['resource'] and o['mode']==x.get('mode',0))
    for d in case.outages:blocks[d['resource']].append((d['start'],d['end']))
    while remaining:
        ready=[t for t in remaining.values() if all(p in placed for p in t['predecessors'])]
        if not ready:raise ValueError('cyclic precedence graph')
        key=(lambda t:(min(o['duration'] for o in t['options']),t['id'])) if rule=='SPT' else (lambda t:(t.get('due') if t.get('due') is not None else math.inf,t['id'])) if rule=='EDD' else (lambda t:(t.get('release',0),t['id']))
        task=min(ready,key=key);release=max(case.now,task.get('release',0),max((placed[p]['end'] for p in task['predecessors']),default=0));choices=[]
        for o in task['options']:
            if case.budget is not None and cost+o['cost']>case.budget:continue
            start=earliest_gap(blocks[o['resource']],release,o['duration']);choices.append((start+o['duration'],o['cost'],start,o))
        if not choices:raise ValueError('budget has no feasible mode')
        end,extra,start,o=min(choices,key=lambda c:(c[0],c[1],c[3]['resource']));x={'id':task['id'],'resource':o['resource'],'mode':o['mode'],'start':round(start,1),'end':round(end,1)};placed[task['id']]=x;blocks[o['resource']].append((start,end));cost+=extra;del remaining[task['id']]
    schedule=sorted(placed.values(),key=lambda x:x['id']);v=validate(case,schedule)
    return {'algorithm':rule,'status':'FEASIBLE' if v['valid'] else 'INVALID','tasks':schedule,'makespan':v['makespan'],'wall_time':time.perf_counter()-begin,'validation':v,'seed':seed}

def cp_solve(case:Case,budget_seconds=5,seed=0,baseline:list[dict]|None=None,stability=False,delivery=False)->dict:
    begin=time.perf_counter();model=cp_model.CpModel();horizon=int((sum(max(o['duration'] for o in t['options']) for t in case.tasks)+max([case.now]+[e['end'] for e in case.outages])+1)*SCALE);starts={};ends={};choices={};intervals={r:[] for r in case.resources};labor=[]
    for t in case.tasks:
        i=t['id'];lo=round(max(t.get('release',0),0 if i in case.fixed else case.now)*SCALE);starts[i]=model.new_int_var(lo,horizon,f's{i}');ends[i]=model.new_int_var(lo,horizon,f'e{i}');lits=[]
        for k,o in enumerate(t['options']):
            present=model.new_bool_var(f'x{i}_{k}');d=round(o['duration']*SCALE);interval=model.new_optional_interval_var(starts[i],d,ends[i],present,f'op{i}_{k}');intervals[o['resource']].append(interval);lits.append(present);choices[i,k]=present;labor.append(o['cost']*present)
            if i in case.fixed:
                f=case.fixed[i];model.add(starts[i]==round(f['start']*SCALE));model.add(present==int(o['resource']==f['resource'] and o['mode']==f.get('mode',0)))
        model.add_exactly_one(lits)
    for t in case.tasks:
        for p in t['predecessors']:model.add(starts[t['id']]>=ends[p])
    for r,ints in intervals.items():
        # Merge overlapping unavailable windows before adding them as intervals.
        blocks=[]
        for e in sorted((e for e in case.outages if e['resource']==r),key=lambda x:x['start']):
            a,b=round(e['start']*SCALE),round(e['end']*SCALE)
            if blocks and a<=blocks[-1][1]:blocks[-1]=(blocks[-1][0],max(b,blocks[-1][1]))
            else:blocks.append((a,b))
        for a,b in blocks:ints.append(model.new_interval_var(a,b-a,b,f'outage_{r}_{a}'))
        model.add_no_overlap(ints)
    if case.budget is not None:model.add(sum(labor)<=case.budget)
    makespan=model.new_int_var(0,horizon,'makespan');model.add_max_equality(makespan,list(ends.values()));movement=[]
    if baseline:
        base={x['id']:x for x in baseline}
        for i,s in starts.items():
            if i in base:
                deviation=model.new_int_var(0,horizon,f'move{i}');model.add_abs_equality(deviation,s-round(base[i]['start']*SCALE));movement.append(deviation);model.add_hint(s,round(base[i]['start']*SCALE))
                if delivery:model.add_hint(ends[i],round(base[i]['end']*SCALE))
        if delivery:
            for task in case.tasks:
                if task['id'] in base:
                    b=base[task['id']]
                    for k,o in enumerate(task['options']):model.add_hint(choices[task['id'],k],int(o['resource']==b['resource'] and o['mode']==b.get('mode',0)))
    if delivery:
        tardiness=[]
        for job in {t['job'] for t in case.tasks}:
            group=[t for t in case.tasks if t['job']==job]
            if group[0].get('due') is None:continue
            completion=model.new_int_var(0,horizon,f'job{job}');model.add_max_equality(completion,[ends[t['id']] for t in group]);late=model.new_int_var(0,horizon,f'late{job}');model.add_max_equality(late,[0,completion-round(group[0]['due']*SCALE)]);tardiness.append(late)
        model.minimize(sum(tardiness) if tardiness else makespan)
    else:model.minimize(makespan+sum(movement)*10 if stability else makespan)
    solver=cp_model.CpSolver();solver.parameters.max_time_in_seconds=budget_seconds*.8 if delivery and tardiness else budget_seconds;solver.parameters.num_search_workers=1;solver.parameters.random_seed=seed
    solve_begin=time.perf_counter();status=solver.solve(model);status_name=solver.status_name(status);schedule=[]
    if delivery and tardiness and status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        primary_optimal=status==cp_model.OPTIMAL
        primary_value=solver.value(sum(tardiness))
        model.add(sum(tardiness)<=primary_value);model.minimize(makespan);model.clear_hints()
        for i in starts:model.add_hint(starts[i],solver.value(starts[i]));model.add_hint(ends[i],solver.value(ends[i]))
        for variable in choices.values():model.add_hint(variable,solver.value(variable))
        second=cp_model.CpSolver();second.parameters.max_time_in_seconds=max(.01,budget_seconds-(time.perf_counter()-solve_begin));second.parameters.num_search_workers=1;second.parameters.random_seed=seed
        secondary_status=second.solve(model)
        if secondary_status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
            solver=second;status=secondary_status
            status_name='OPTIMAL' if primary_optimal and secondary_status==cp_model.OPTIMAL else 'FEASIBLE'
        else:status_name='FEASIBLE'
    if status in (cp_model.OPTIMAL,cp_model.FEASIBLE):
        for t in case.tasks:
            for k,o in enumerate(t['options']):
                if solver.boolean_value(choices[t['id'],k]):schedule.append({'id':t['id'],'resource':o['resource'],'mode':o['mode'],'start':solver.value(starts[t['id']])/SCALE,'end':solver.value(ends[t['id']])/SCALE})
    v=validate(case,schedule);elapsed=time.perf_counter()-begin
    return {'algorithm':'CP-SAT-delivery' if delivery else 'CP-SAT-local' if stability else 'CP-SAT','status':status_name,'tasks':schedule,'makespan':v['makespan'] if schedule else None,'wall_time':elapsed,'best_bound':solver.best_objective_bound/SCALE if status in (cp_model.OPTIMAL,cp_model.FEASIBLE) and not stability and not delivery else None,'validation':v,'seed':seed,'time_limit_seconds':budget_seconds}

def packaging_case()->Case:
    import openpyxl,hashlib
    from datetime import datetime
    path=ROOT/'data/raw/open_alternatives/packaging/4-Width.xlsx';w=openpyxl.load_workbook(path,read_only=True,data_only=True)
    try:
        machines=[r for r in list(w['1-Machine'].values)[2:] if isinstance(r[0],(int,float))]
        routes={int(r[0]):str(r[1]).split('-') for r in list(w['3-Routing'].values)[2:] if isinstance(r[0],(int,float))}
        products={int(r[0]):(int(r[2]),int(r[3])) for r in list(w['5-Product Type'].values)[2:] if isinstance(r[0],(int,float))}
        orders=[r for r in list(w['6-Data Order'].values)[2:] if isinstance(r[0],(int,float))]
    finally:w.close()
    parse=lambda v:v if isinstance(v,datetime) else datetime.fromisoformat(str(v))
    origin=min(parse(r[4]) for r in orders);tasks=[];groups={'P1':[],'P2':[],'P2b':[],'P3':[],'P4':[],'P5':[]}
    for m in machines:
        name=m[1];p='P1' if name.startswith('Printing') else 'P2' if name.startswith('Drylaminasi') else 'P3' if name.startswith('Extrusion') else 'P4' if name.startswith('Sliting') else 'P5';groups[p].append(m)
        if p=='P2':groups['P2b'].append(m)
    for order in orders:
        colors,route=products[int(order[2])];prior=None
        for seq,process in enumerate(routes[route],1):
            options=[{'resource':m[2],'duration':math.ceil((float(order[3])/(float(m[4])*float(m[5]))+float(m[6]))*60),'mode':0,'cost':0} for m in groups[process] if process!='P1' or int(m[3])>=colors]
            tid=len(tasks)+1;tasks.append({'id':tid,'name':f'PK-{int(order[0])}-{seq}','job':int(order[0]),'release':(parse(order[4])-origin).total_seconds(),'due':(parse(order[5])-origin).total_seconds(),'predecessors':[prior] if prior else [],'options':options});prior=tid
    return Case('Packaging-59',tasks,[m[2] for m in machines],source={'doi':'10.17632/h66hb89k6z.1','sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'origin':origin.isoformat(),'duration_rule':'ceil((running meters / effective m/min speed + setup minutes) * 60)','orders':len(orders)})

def disruption_case(case:Case,base:list[dict],seed:int)->Case:
    rnd=random.Random(seed);eligible=sorted(base,key=lambda x:x['start'])[max(1,len(base)//3):max(2,2*len(base)//3)];target=rnd.choice(eligible);now=target['start']
    outage_duration=max(1,math.ceil((target['end']-target['start'])*.6*SCALE-1e-9)/SCALE)
    outage={'resource':target['resource'],'start':now,'end':round(now+outage_duration,1)}
    fixed={x['id']:x for x in base if x['start']<now-1e-6}
    # A failure begins exactly at this resource's next operation boundary; running tasks elsewhere remain fixed.
    return Case(case.name+'-停机',case.tasks,case.resources,case.budget,case.source,[outage],fixed,now)

def local_solve(case:Case,baseline:list[dict],time_limit=5,seed=0)->dict:
    begin=time.perf_counter();by={t['id']:t for t in case.tasks};affected={x['id'] for x in baseline if any(x['resource']==e['resource'] and x['end']>e['start'] for e in case.outages)}
    changed=True
    while changed:
        old=set(affected)
        affected.update(t['id'] for t in case.tasks if any(p in affected for p in t['predecessors']))
        used={o['resource'] for i in affected for o in by[i]['options']}
        affected.update(x['id'] for x in baseline if x['resource'] in used and x['start']>=case.now)
        changed=old!=affected
    fixed={**case.fixed,**{x['id']:x for x in baseline if x['id'] not in affected}}
    restricted=Case(case.name,case.tasks,case.resources,case.budget,case.source,case.outages,fixed,case.now)
    result=cp_solve(restricted,time_limit*.6,seed,baseline,True);result['neighborhood_size']=len(affected)
    if not result['validation']['valid']:
        remain=max(.01,time_limit-(time.perf_counter()-begin));result=cp_solve(case,remain,seed,baseline,True);result['fallback']='expanded to full future tasks'
    result['wall_time']=time.perf_counter()-begin;result['algorithm']='CP-SAT-local';result['validation']=validate(case,result['tasks']);result['changed_operations']=sum(1 for x in result['tasks'] if any(b['id']==x['id'] and (b['resource']!=x['resource'] or abs(b['start']-x['start'])>.05) for b in baseline));return result
