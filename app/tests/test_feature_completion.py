import json
from datetime import datetime,timezone,timedelta
from backend.routers import account_ext,operations_ext,assistant_ext
from .conftest import register_and_login,create_workspace,create_resource,create_order,create_plan,order_payload,PASSWORD

def prepare(app):
    app.include_router(account_ext.router);app.include_router(operations_ext.router);app.include_router(assistant_ext.router)
    client=register_and_login(app,'completion_user');workspace=create_workspace(client,'完整功能验收');resource=create_resource(client,workspace['id'],'M1');return client,workspace,resource

def test_password_change_revokes_sessions_and_requires_old_password(app):
    c,w,r=prepare(app)
    assert c.post('/api/auth/password',json={'current_password':'incorrect','new_password':'ChangedPass123!'}).status_code==403
    assert c.post('/api/auth/password',json={'current_password':PASSWORD,'new_password':'ChangedPass123!'}).status_code==200
    assert c.get('/api/auth/me').status_code==401
    assert c.post('/api/auth/login',json={'username':'completion_user','password':PASSWORD}).status_code==401
    assert c.post('/api/auth/login',json={'username':'completion_user','password':'ChangedPass123!'}).status_code==200

def test_task_lock_unlock_and_freeze_window(app):
    c,w,r=prepare(app);wid=w['id']
    o=create_order(c,wid,order_payload('LOCK',release_at=(datetime.now(timezone.utc)+timedelta(minutes=5)).isoformat(),due_at=None,resources=[r['id']],durations=[60],operations=1))
    plan=create_plan(c,wid);op=o['operations'][0]['id']
    path=f'/api/workspaces/{wid}/operations/{op}/lock'
    assert c.post(path,json={'locked':True,'plan_id':plan['id']}).status_code==200
    assert c.get(f'/api/workspaces/{wid}/orders/{o["id"]}').json()['operations'][0]['fixed_start_at']
    assert c.post(path,json={'locked':False,'plan_id':plan['id']}).status_code==200
    response=c.post(f'/api/workspaces/{wid}/operations/freeze',json={'plan_id':plan['id'],'minutes':10,'from_at':plan['tasks'][0]['start_at']})
    assert response.status_code==200 and response.json()['count']==1

def test_calendar_multiple_shifts_preserved_and_overlap_rejected(app):
    c,w,r=prepare(app);path=f'/api/workspaces/{w["id"]}/resources/{r["id"]}'
    calendar=[{'weekday':0,'start_minute':480,'end_minute':720},{'weekday':0,'start_minute':780,'end_minute':1080}]
    assert c.patch(path,json={'calendar':calendar}).status_code==200
    c.patch(path,json={'name':'改名设备'}).raise_for_status()
    items=c.get(f'/api/workspaces/{w["id"]}/resources').json()['items'];assert items[0]['calendar']==calendar
    bad=[{'weekday':0,'start_minute':480,'end_minute':800},{'weekday':0,'start_minute':780,'end_minute':1080}]
    assert c.patch(path,json={'calendar':bad}).status_code==422

def test_preorder_preview_honors_material_arrival_without_saving(app):
    c,w,r=prepare(app);wid=w['id'];arrival=datetime.now(timezone.utc)+timedelta(hours=2)
    m=c.post(f'/api/workspaces/{wid}/materials',json={'name':'待到货部件','unit':'件','quantity':5,'available_at':arrival.isoformat()});m.raise_for_status()
    body=order_payload('PREVIEW',release_at=datetime.now(timezone.utc).isoformat(),due_at=None,resources=[r['id']],durations=[60],operations=1,materials=[{'material_id':m.json()['id'],'quantity':2}])
    response=c.post(f'/api/workspaces/{wid}/orders/preview?time_limit_seconds=3',json=body);assert response.status_code==200,response.text
    result=response.json()['result'];assert result['validation']['valid'] and result['tasks'][0]['start_epoch']>=int(arrival.timestamp())
    assert c.get(f'/api/workspaces/{wid}/orders').json()['total']==0

def test_assistant_history_is_scoped_and_hides_reasoning(app,tmp_path,monkeypatch):
    monkeypatch.setattr(assistant_ext,'THREADS',tmp_path)
    c,w,r=prepare(app);wid=w['id'];uid=c.get('/api/auth/me').json()['user']['id']
    tid=c.post(f'/api/workspaces/{wid}/assistant/new').json()['thread_id']
    path=tmp_path/f'{wid}_{uid}_{tid}.json'
    path.write_text(json.dumps({'messages':[{'role':'system','content':'system instructions'},{'role':'user','content':'查询工序'},{'role':'assistant','content':'共有三道工序','reasoning_content':'private reasoning'}]}),encoding='utf-8')
    history=c.get(f'/api/workspaces/{wid}/assistant/history').json()
    assert len(history['messages'])==2 and 'reasoning_content' not in json.dumps(history)
