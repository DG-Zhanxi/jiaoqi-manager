import json
from datetime import datetime,timedelta,timezone
from fastapi.testclient import TestClient
from backend.routers import import_ext,research_ext,assistant_ext
from .conftest import register_and_login,create_workspace,create_resource,order_payload,create_order,create_plan

def setup(app):
    app.include_router(import_ext.router);app.include_router(research_ext.router);app.include_router(assistant_ext.router)
    c=register_and_login(app,'extension_admin');w=create_workspace(c,'扩展验收');r=create_resource(c,w['id'],'M1')
    return c,w,r

def test_json_import_validates_entire_batch_before_writing(app):
    c,w,r=setup(app);now=datetime.now(timezone.utc).isoformat()
    good=order_payload('IMPORT-A',release_at=now,due_at=None,resources=[r['id']],durations=[60],operations=1)
    bad={**good,'order_no':'IMPORT-B','quantity':0}
    response=c.post(f'/api/workspaces/{w["id"]}/datasets/upload',files={'file':('orders.json',json.dumps({'orders':[good,bad]}),'application/json')})
    assert response.status_code==422
    assert c.get(f'/api/workspaces/{w["id"]}/orders').json()['total']==0
    response=c.post(f'/api/workspaces/{w["id"]}/datasets/upload',files={'file':('orders.json',json.dumps({'orders':[good]}),'application/json')})
    assert response.status_code==200 and response.json()['order_count']==1

def test_compare_persists_distinct_candidates(app):
    c,w,r=setup(app);create_order(c,w['id'],order_payload('COMPARE',release_at=datetime.now(timezone.utc).isoformat(),due_at=None,resources=[r['id']],durations=[60],operations=1))
    response=c.post(f'/api/workspaces/{w["id"]}/schedule/compare',json={'time_limit_seconds':3})
    assert response.status_code==200,response.text
    candidates=response.json()['candidates'];assert candidates and all(x.get('plan_id') for x in candidates)
    saved=c.get(f'/api/workspaces/{w["id"]}/plans').json()['items']
    assert len(saved)==len(candidates)

def test_new_event_changes_revision_and_applies_to_normal_solver(app):
    c,w,r=setup(app);now=datetime.now(timezone.utc)
    create_order(c,w['id'],order_payload('EVENT',release_at=now.isoformat(),due_at=None,resources=[r['id']],durations=[60],operations=1))
    before=c.get(f'/api/workspaces/{w["id"]}').json()['data_revision']
    event=c.post(f'/api/workspaces/{w["id"]}/events',json={'event_type':'machine_failure','title':'停机','occurred_at':now.isoformat(),'known_at':now.isoformat(),'duration_seconds':300,'resource_id':r['id']})
    assert event.status_code==201,event.text
    assert c.get(f'/api/workspaces/{w["id"]}').json()['data_revision']>before
    solved=c.post(f'/api/workspaces/{w["id"]}/schedule/solve',json={'time_limit_seconds':3}).json()['result']
    assert solved['validation']['valid']
    assert solved['tasks'][0]['start_epoch']>=int(now.timestamp())+300

def test_research_files_require_membership_and_valid_id(app):
    c,w,r=setup(app)
    assert c.get(f'/api/workspaces/{w["id"]}/research/runs/not-a-run').status_code==404
    outsider=register_and_login(app,'extension_outsider')
    assert outsider.get(f'/api/workspaces/{w["id"]}/research/industrial').status_code==404
    assert c.get(f'/api/workspaces/{w["id"]}/research/industrial').json()['budget']==80

def test_operator_cannot_create_solver_job_or_change_model(app):
    c,w,r=setup(app);operator=register_and_login(app,'extension_operator')
    c.post(f'/api/workspaces/{w["id"]}/members',json={'username':'extension_operator','role':'operator'}).raise_for_status()
    assert operator.post(f'/api/workspaces/{w["id"]}/schedule/jobs',json={}).status_code==403
    assert operator.post('/api/integrations/kimi',json={'model':'kimi-k3.1-pro'}).status_code==403
