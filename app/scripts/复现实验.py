from pathlib import Path
import argparse,os,sys,json
p=argparse.ArgumentParser();p.add_argument('--project-root',default=str(Path(__file__).resolve().parents[2]));p.add_argument('--suite',choices=['biw','hfut','all','delivery','full'],default='biw');p.add_argument('--seconds',type=float,default=5);p.add_argument('--seeds',type=int,default=3);p.add_argument('--workspace',type=int,default=4);a=p.parse_args();root=Path(a.project_root).resolve();os.environ['JIAOQI_ROOT']=str(root);sys.path.insert(0,str(root/'app'))
from research.runner import run_suite
from research.delivery import run_delivery
results=[]
if a.suite in ['biw','hfut','all','full']:results.append(run_suite(a.workspace,'all' if a.suite=='full' else a.suite,a.seconds,a.seeds))
if a.suite in ['delivery','full']:results.append(run_delivery(a.workspace,a.seconds,a.seeds))
for r in results:
 assert r['status']=='completed',r.get('error')
 assert all(x['validation']['valid'] for x in r['results'])
 print(json.dumps({'运行编号':r['id'],'运行次数':len(r['results']),'校验通过':sum(x['validation']['valid'] for x in r['results'])},ensure_ascii=False),flush=True)
if a.suite=='full':(root/'experiments/reproduction-last.json').write_text(json.dumps({'base':results[0]['id'],'delivery':results[1]['id'],'workspace':a.workspace},ensure_ascii=False,indent=2),encoding='utf-8')
