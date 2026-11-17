from __future__ import annotations
import csv,io,json,hashlib,math,os
from pathlib import Path
from datetime import datetime,timezone,timedelta
import openpyxl
from fastapi import APIRouter,Depends,UploadFile,File,HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session
from ..dependencies import WorkspaceContext,require_workspace_roles,get_db
from ..models import Order,Operation,OperationCandidate,Resource,Dataset
from ..schemas import OrderIn
from ..routers.orders import _validate_resources,_validate_materials,_add_operations,_add_materials
from ..services import audit
from ..timeutil import ensure_utc

router=APIRouter(prefix='/api/workspaces/{workspace_id}/datasets',tags=['imports'])
ROOT=Path(os.getenv('JIAOQI_ROOT',str(Path(__file__).resolve().parents[3])))

@router.post('/upload')
def upload(file:UploadFile=File(...),ctx:WorkspaceContext=Depends(require_workspace_roles('admin','planner')),db:Session=Depends(get_db)):
    raw=file.file.read(8*1024*1024+1)
    if len(raw)>8*1024*1024:raise HTTPException(413,'文件超过8MB')
    try:
        text=raw.decode('utf-8-sig')
        if (file.filename or '').lower().endswith('.json'):
            obj=json.loads(text);rows=obj if isinstance(obj,list) else obj.get('orders',[])
        elif (file.filename or '').lower().endswith('.csv'):
            grouped={}
            for index,r in enumerate(csv.DictReader(io.StringIO(text)),2):
                no=r.get('order_no','').strip()
                if not no:raise ValueError(f'第{index}行缺少order_no')
                item=grouped.setdefault(no,{'order_no':no,'product_name':r['product_name'],'quantity':int(r['quantity']),'release_at':r['release_at'],'due_at':r.get('due_at') or None,'priority':int(r.get('priority') or 0),'operations':[]})
                seq=int(r['sequence']);op=next((o for o in item['operations'] if o['sequence']==seq),None)
                if op is None:
                    op={'sequence':seq,'name':r.get('operation_name') or f'工序 {seq}','setup_seconds':int(r.get('setup_seconds') or 0),'candidates':[]};item['operations'].append(op)
                op['candidates'].append({'resource_id':int(r['resource_id']),'duration_seconds':int(r['duration_seconds'])})
            rows=list(grouped.values())
        else:raise ValueError('请选择JSON或CSV文件')
        if not 1<=len(rows)<=1000:raise ValueError('文件需包含1至1000个订单')
        parsed=[OrderIn.model_validate(r) for r in rows]
    except (ValueError,KeyError,UnicodeError,TypeError) as exc:raise HTTPException(422,f'文件格式错误：{str(exc)[:400]}')
    seen=set()
    for p in parsed:
        if p.order_no in seen:raise HTTPException(422,'文件中有重复订单号')
        seen.add(p.order_no)
        if db.scalar(select(Order.id).where(Order.workspace_id==ctx.workspace.id,Order.order_no==p.order_no)):raise HTTPException(409,f'订单 {p.order_no} 已存在')
        _validate_resources(db,ctx.workspace.id,p.operations);_validate_materials(db,ctx.workspace.id,p.materials)
    for p in parsed:
        o=Order(workspace_id=ctx.workspace.id,order_no=p.order_no,product_name=p.product_name,quantity=p.quantity,release_at=ensure_utc(p.release_at),due_at=ensure_utc(p.due_at),priority=p.priority,status=p.status,created_by=ctx.user.id);db.add(o);db.flush();_add_operations(db,o,p.operations);_add_materials(db,o,p.materials)
    audit.record(db,workspace_id=ctx.workspace.id,actor_id=ctx.user.id,action='dataset.upload',entity_type='dataset',details={'filename':Path(file.filename or 'upload').name,'sha256':hashlib.sha256(raw).hexdigest(),'order_count':len(parsed)});audit.bump_revision(db,ctx.workspace);db.commit()
    return {'order_count':len(parsed)}

@router.post('/import/packaging')
def packaging(ctx:WorkspaceContext=Depends(require_workspace_roles('admin','planner')),db:Session=Depends(get_db)):
    if db.scalar(select(Order.id).where(Order.workspace_id==ctx.workspace.id)):
        raise HTTPException(409,'请在空工作区导入这组生产订单')
    if db.scalar(select(Dataset.id).where(Dataset.workspace_id==ctx.workspace.id,Dataset.source_type=='packaging')):raise HTTPException(409,'当前工作区已导入包装生产数据')
    path=ROOT/'data/raw/open_alternatives/packaging/4-Width.xlsx';w=openpyxl.load_workbook(path,read_only=True,data_only=True)
    try:
        machines=[r for r in list(w['1-Machine'].values)[2:] if isinstance(r[0],(int,float))]
        routes={int(r[0]):str(r[1]).split('-') for r in list(w['3-Routing'].values)[2:] if isinstance(r[0],(int,float))}
        products={int(r[0]):{'width':int(r[1]),'colors':int(r[2]),'route':int(r[3])} for r in list(w['5-Product Type'].values)[2:] if isinstance(r[0],(int,float))}
        orders=[r for r in list(w['6-Data Order'].values)[2:] if isinstance(r[0],(int,float))]
    finally:w.close()
    groups={'P1':[],'P2':[],'P2b':[],'P3':[],'P4':[],'P5':[]};labels={'P1':'印刷','P2':'一次复合','P2b':'二次复合','P3':'挤出','P4':'分切','P5':'制袋'};resource_map={}
    for r in machines:
        name=str(r[1]);process='P1' if name.startswith('Printing') else 'P2' if name.startswith('Drylaminasi') else 'P3' if name.startswith('Extrusion') else 'P4' if name.startswith('Sliting') else 'P5'
        code='PK-'+str(r[2]);res=db.scalar(select(Resource).where(Resource.workspace_id==ctx.workspace.id,Resource.code==code))
        if res:raise HTTPException(409,f'资源编号 {code} 已存在，请使用独立工作区导入')
        res=Resource(workspace_id=ctx.workspace.id,code=code,name=f'{labels[process]} {r[2]}',resource_type='machine',capabilities=[labels[process]],is_active=True);db.add(res);db.flush();resource_map[str(r[2])]=res;groups[process].append(r)
        if process=='P2':groups['P2b'].append(r)
    digest=hashlib.sha256(path.read_bytes()).hexdigest();ds=Dataset(workspace_id=ctx.workspace.id,name='包装生产订单',source_type='packaging',sheet_name='6-Data Order',instance_key='Packaging-2022',source_uri='https://data.mendeley.com/datasets/h66hb89k6z/1',source_doi='10.17632/h66hb89k6z.1',license='CC BY 4.0',workbook_sha256=digest,status='valid',mapping={'source_file':'4-Width.xlsx','duration_rule':'ceil((Running Meter / (MinSpeed * Ratio Speed) + SetupTime) * 60) seconds','speed_unit':'meters/minute','setup_unit':'minutes','eligibility':'process category; printing machine colors >= product colors','width_policy':'film width retained by product type; source has no machine width capacity column','material_policy':'source has no lot-level arrival quantities; no inventory constraints inferred'},row_counts={'orders':len(orders),'resources':len(machines)},created_by=ctx.user.id);db.add(ds);db.flush();count=0
    for r in orders:
        no='PK-'+str(int(r[0])).zfill(3)
        if db.scalar(select(Order.id).where(Order.workspace_id==ctx.workspace.id,Order.order_no==no)):raise HTTPException(409,f'订单 {no} 已存在')
        product=products[int(r[2])];meter=float(r[3]);parse=lambda v: v if isinstance(v,datetime) else datetime.fromisoformat(str(v))
        release=parse(r[4]).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc);due=parse(r[5]).replace(tzinfo=timezone(timedelta(hours=8))).astimezone(timezone.utc)
        o=Order(workspace_id=ctx.workspace.id,order_no=no,product_name=f'包装产品 {int(r[2]):02d}',quantity=int(meter),release_at=release,due_at=due,priority=int(r[1]),status='confirmed',dataset_id=ds.id,external_job_key=str(int(r[0])),created_by=ctx.user.id);db.add(o);db.flush()
        for seq,process in enumerate(routes[product['route']],1):
            op=Operation(order_id=o.id,sequence=seq,name=labels[process],setup_seconds=0,status='planned');db.add(op);db.flush();opts=0
            for m in groups[process]:
                if process=='P1' and int(m[3])<product['colors']:continue
                speed=float(m[4])*float(m[5])
                seconds=math.ceil((meter/speed+float(m[6]))*60)
                db.add(OperationCandidate(operation_id=op.id,resource_id=resource_map[str(m[2])].id,duration_seconds=seconds));opts+=1
            if not opts:raise HTTPException(422,f'{no} 的 {labels[process]} 没有可用设备')
        count+=1
    audit.record(db,workspace_id=ctx.workspace.id,actor_id=ctx.user.id,action='dataset.import.packaging',entity_type='dataset',entity_id=ds.id,details={'orders':count,'sha256':digest});audit.bump_revision(db,ctx.workspace);db.commit()
    return {'dataset_id':ds.id,'order_count':count,'resource_count':len(machines)}
