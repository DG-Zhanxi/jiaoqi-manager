from backend.scheduling.domain import Scenario,Operation,Order,Resource,Candidate
from backend.scheduling.engine import solve
from research.engine import industrial_case,greedy,disruption_case,cp_solve,local_solve

def one_op(resources=None,candidates=None,release=1000):
    return Scenario(operations=(Operation(1,1,'A','op',1,0,release,None,0,1,tuple(candidates or [Candidate(1,100)])),),resources=tuple(resources or [Resource(1,'M1')]),orders=(Order(1,'A',1000,None,0),),horizon_start_epoch=1000,horizon_end_epoch=2000)

def test_default_completion_limit_is_not_zero():
    result=solve(one_op(),strategy='overtime_limited',time_limit_seconds=3,workers=1)
    assert result.has_solution and result.validation['valid']

def test_overlapping_downtime_and_closed_calendar_are_merged():
    resource=Resource(1,'M1',downtime_windows=((1100,1200),),availability_windows=((1000,1150),(1250,2000)),calendar_defined=True)
    result=solve(one_op([resource]),time_limit_seconds=3,workers=1)
    assert result.has_solution and result.validation['valid']

def test_unusable_alternative_does_not_invalidate_fast_machine():
    case=one_op([Resource(1,'M1'),Resource(2,'M2')],[Candidate(1,5000),Candidate(2,100)],release=1800)
    result=solve(case,time_limit_seconds=3,workers=1)
    assert result.has_solution and result.tasks[0].resource_id==2

def test_industrial_outage_quantization_and_dynamic_validation():
    case=industrial_case();base=greedy(case)['tasks']
    for seed in [0,2]:
        d=disruption_case(case,base,seed)
        assert greedy(d)['validation']['valid']
        assert cp_solve(d,.5,seed,base)['validation']['valid']
        assert local_solve(d,base,1,seed)['validation']['valid']

def test_calendar_uses_shanghai_business_hours_and_empty_means_closed():
    from datetime import datetime,timezone
    from backend.services.scheduling import calendar_windows
    start=int(datetime(2026,11,16,tzinfo=timezone.utc).timestamp())
    defined,windows=calendar_windows([{'weekday':0,'start_minute':480,'end_minute':1080}],start,start+86400)
    assert defined and windows==((start,start+36000),)
    assert calendar_windows([],start,start+86400)==(True,())
