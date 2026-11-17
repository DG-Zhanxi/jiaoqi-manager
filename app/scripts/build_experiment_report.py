from pathlib import Path
import json,csv,statistics,base64,html
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib import font_manager

ROOT=Path(__file__).resolve().parents[2];OUT=ROOT/'reports';FIG=OUT/'figures';FIG.mkdir(parents=True,exist_ok=True)
font=Path('C:/Windows/Fonts/msyh.ttc')
if font.exists():font_manager.fontManager.addfont(str(font));plt.rcParams['font.family']='Microsoft YaHei'
plt.rcParams.update({'axes.unicode_minus':False,'figure.dpi':150,'axes.spines.top':False,'axes.spines.right':False,'axes.labelcolor':'#344f42','text.color':'#193a2b','xtick.color':'#5d7467','ytick.color':'#5d7467'})
rid=(ROOT/'evidence/final-experiment-id.txt').read_text().strip();did=(ROOT/'evidence/delivery-experiment-id.txt').read_text().strip()
base=json.loads((ROOT/f'experiments/1/{rid}.json').read_text(encoding='utf-8'));delivery=json.loads((ROOT/f'experiments/1/{did}.json').read_text(encoding='utf-8'))
assert base['status']==delivery['status']=='completed'
rows=base['results']+delivery['results'];pairs={(r['case'],r['seed'],r['phase'],r['algorithm']):r for r in rows}
all_valid=sum(bool(r['validation']['valid']) for r in rows)
def paired_ratios(algorithm,baseline,phase,metric='makespan'):
 values=[]
 for r in rows:
  if r['phase']!=phase or r['algorithm']!=algorithm or not r['validation']['valid']:continue
  b=pairs[r['case'],r['seed'],phase,baseline]
  rv=r[metric] if metric in r else r['validation'][metric];bv=b[metric] if metric in b else b['validation'][metric]
  if bv>0:values.append(rv/bv)
 return values
static_improvement=(1-statistics.mean(paired_ratios('CP-SAT','FCFS','static')))*100
dynamic_improvement=(1-statistics.mean(paired_ratios('CP-SAT-global','FCFS-repair','dynamic')))*100
biw_base=pairs['BIW-43',0,'static','FCFS'];biw_cp=pairs['BIW-43',0,'static','CP-SAT'];biw_gain=(1-biw_cp['makespan']/biw_base['makespan'])*100

cases=sorted({r['case'] for r in base['results']})
ratios=[]
for c in cases:ratios.append(statistics.mean(pairs[c,s,'static','CP-SAT']['makespan']/pairs[c,s,'static','FCFS']['makespan'] for s in range(3)))
fig,ax=plt.subplots(figsize=(13,4.5));ax.bar(range(len(cases)),ratios,color=['#087f68' if c=='BIW-43' else '#6bb69a' for c in cases],width=.7);ax.axhline(1,color='#bd8a41',linestyle='--',lw=1,label='FCFS = 1');ax.set_xticks(range(len(cases)),[c.replace('HFUT-','') for c in cases],rotation=45,ha='right',fontsize=9);ax.set_ylabel('归一化生产跨度');ax.set_ylim(0,max(1.08,max(ratios)*1.08));ax.legend(frameon=False);ax.set_title('基础排产：CP-SAT 相对 FCFS 的生产跨度',loc='left',pad=18,fontsize=14);fig.tight_layout();fig.savefig(FIG/'01-static-comparison.png');fig.savefig(FIG/'01-static-comparison.svg');plt.close(fig)

dynamic_algs=['FCFS-repair','CP-SAT-global','CP-SAT-local'];normalized=[];changed=[]
for alg in dynamic_algs:
 normalized.append(statistics.mean(paired_ratios(alg,'FCFS-repair','dynamic')))
 changed.append(statistics.mean(r.get('changed_operations',0) for r in base['results'] if r['phase']=='dynamic' and r['algorithm']==alg))
fig,axs=plt.subplots(1,2,figsize=(11,4));colors=['#afc7ba','#087f68','#d4a25a']
axs[0].bar(dynamic_algs,normalized,color=colors,width=.55);axs[0].axhline(1,color='#899e91',linestyle='--',lw=1);axs[0].set_ylabel('相对规则修复的生产跨度');axs[0].set_title('异常后的完工表现',loc='left')
axs[1].bar(dynamic_algs,changed,color=colors,width=.55);axs[1].set_ylabel('平均变更工序数');axs[1].set_title('计划调整范围',loc='left')
for ax in axs:ax.tick_params(axis='x',labelsize=9)
fig.tight_layout();fig.savefig(FIG/'02-dynamic-tradeoff.png');fig.savefig(FIG/'02-dynamic-tradeoff.svg');plt.close(fig)

resource_order=['AGV','R1','R2','R3','R4','R5','R6','R7'];fig,ax=plt.subplots(figsize=(12,4.6))
for t in biw_cp['tasks']:
 y=resource_order.index(t['resource']);ax.barh(y,t['end']-t['start'],left=t['start'],height=.55,color='#167f61' if t['mode']==0 else '#d8a258',edgecolor='white',linewidth=.7)
 if t['end']-t['start']>45:ax.text((t['start']+t['end'])/2,y,str(t['id']),ha='center',va='center',fontsize=8,color='white' if t['mode']==0 else '#423418')
ax.set_yticks(range(8),resource_order);ax.invert_yaxis();ax.set_xlabel('时间 / 秒');ax.set_title('白车身焊装：43道任务的求解排程',loc='left',pad=18);ax.grid(axis='x',alpha=.15);ax.set_axisbelow(True);fig.tight_layout();fig.savefig(FIG/'03-biw-gantt.png');fig.savefig(FIG/'03-biw-gantt.svg');plt.close(fig)

delivery_algs=['FCFS','EDD','SPT','CP-SAT-delivery'];delivery_ratios=[statistics.mean(paired_ratios(a,'FCFS','delivery','tardiness')) for a in delivery_algs]
fig,ax=plt.subplots(figsize=(8,4));ax.bar(delivery_algs,delivery_ratios,color=['#b8cfc1','#91b7a2','#d4a25a','#087f68'],width=.55);ax.axhline(1,color='#8e9d94',linestyle='--',lw=1);ax.set_ylabel('归一化总延期');ax.set_title('交期策略对照：逐实例归一化后取平均',loc='left',pad=18);fig.tight_layout();fig.savefig(FIG/'04-delivery-comparison.png');fig.savefig(FIG/'04-delivery-comparison.svg');plt.close(fig)

csvpath=OUT/'全部实验指标.csv'
with csvpath.open('w',encoding='utf-8-sig',newline='') as f:
 writer=csv.writer(f);writer.writerow(['case','phase','algorithm','seed','status','valid','task_count','resource_count','makespan_seconds','tardiness_seconds','changed_operations','solve_seconds','labor_used','best_bound'])
 for r in rows:writer.writerow([r['case'],r['phase'],r['algorithm'],r['seed'],r['status'],r['validation']['valid'],r.get('task_count'),r.get('resource_count'),r['makespan'],r['validation']['tardiness'],r.get('changed_operations',''),r['wall_time'],r['validation'].get('labor_used'),r.get('best_bound')])

def table(head,body):return '<div class="table"><table><thead><tr>'+''.join('<th>'+html.escape(str(v))+'</th>' for v in head)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>'+html.escape(str(v))+'</td>' for v in r)+'</tr>' for r in body)+'</tbody></table></div>'
def img(name,caption):return '<figure><img src="data:image/png;base64,'+base64.b64encode((FIG/name).read_bytes()).decode()+'" alt="'+caption+'"><figcaption>'+caption+'</figcaption></figure>'
summary_rows=[]
for phase,algs,ref in [('static',['FCFS','EDD','SPT','CP-SAT'],'FCFS'),('dynamic',dynamic_algs,'FCFS-repair'),('delivery',delivery_algs,'FCFS')]:
 for alg in algs:
  group=[r for r in rows if r['phase']==phase and r['algorithm']==alg];valid=[r for r in group if r['validation']['valid']]
  metric='tardiness' if phase=='delivery' else 'makespan'
  summary_rows.append([{'static':'基础排产','dynamic':'异常重排','delivery':'交期优化'}[phase],alg,len(group),f'{len(valid)}/{len(group)}',f'{statistics.mean(paired_ratios(alg,ref,phase,metric)):.4f}',f'{statistics.mean(r["wall_time"] for r in group):.3f}'])
pack=[r for r in delivery['results'] if r['case']=='Packaging-59' and r['seed']==0]
codearts=json.loads((ROOT/'evidence/codearts-session-evidence.json').read_text(encoding='utf-8'))
checks=json.loads((ROOT/'evidence/browser-checks.json').read_text(encoding='utf-8'))
style='''body{font:15px/1.85 "Microsoft YaHei",sans-serif;color:#203e2e;background:#f3f6f4;margin:0}main{max-width:1120px;margin:32px auto;background:white;padding:45px 55px;border:1px solid #dce5df}h1{font-size:32px;margin:0 0 12px}h2{font-size:22px;border-top:1px solid #dde7e0;margin-top:35px;padding-top:25px}h3{font-size:17px}p{margin:12px 0;color:#435a4b}.meta{font-size:13px;color:#6b8173}.metrics{display:grid;grid-template-columns:repeat(4,1fr);border:1px solid #dce5df;margin:25px 0}.metrics div{padding:18px;border-right:1px solid #dce5df}.metrics div:last-child{border:0}.metrics strong{font-size:29px;color:#087f68;display:block}.metrics span{font-size:12px;color:#6c8173}table{width:100%;border-collapse:collapse;font-size:13px}th,td{padding:10px 12px;text-align:left;border-bottom:1px solid #e4ebe6;vertical-align:top}th{background:#edf4ef;white-space:nowrap}.table{overflow:auto;margin:15px 0}img{max-width:100%;display:block}figure{margin:25px 0}figcaption{font-size:12px;color:#6f8075;margin:8px 0}a{color:#087f68}pre{white-space:pre-wrap;background:#f4f7f5;border:1px solid #e0e8e2;padding:15px;font-size:12px}code{font-family:Consolas,monospace;font-size:12px}.status{color:#087f68;font-weight:bold}li{margin:8px 0}footer{margin-top:35px;padding-top:20px;border-top:1px solid #dce5df;font-size:12px;color:#6b8173}@media(max-width:700px){main{padding:24px 18px;margin:0}.metrics{grid-template-columns:repeat(2,1fr)}.metrics div{border-bottom:1px solid #dce5df}h1{font-size:25px}}@media print{body{background:white}main{border:0;margin:0;padding:0}figure,table{break-inside:avoid}}'''
doc=f'''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>交期管家实验与系统验收报告</title><style>{style}</style></head><body><main><div class="meta">交期管家 · 系统与算法实验 · 2026年11月17日</div><h1>生产调度与交期策略实验报告</h1><p>围绕生产资源约束、订单交期、异常重排和人机协作预算，使用公开数据构建统一模型，执行独立可行性校验，并通过本地系统完成交互验收。</p><div class="metrics"><div><strong>{len(rows)}</strong><span>实验运行次数</span></div><div><strong>{all_valid}/{len(rows)}</strong><span>约束校验通过</span></div><div><strong>{static_improvement:.2f}%</strong><span>基础排产平均跨度下降</span></div><div><strong>43 / 8</strong><span>焊装任务 / 资源类型</span></div></div>
<h2>1. 数据与模型</h2>{table(['数据集','规模与字段','来源'],[['车间批量调度','20个实例；作业、批量、工序、可选设备与加工参数','合肥工业大学，10.17632/mwrgpx7hhp.3'],['汽车白车身焊装','43道任务、8类资源；依赖关系、两类执行模式、工时预算80','长春工业大学，10.17632/gswbxhjmnw.1'],['包装生产订单','59个订单、15台设备、237道工序；原始入单日期、交期和产品路线','Masmur Tarigan，10.17632/h66hb89k6z.1']])}
<p>HFUT模型采用固定批次安排。标准表工序时间按适配器的分钟标尺转成秒；Exam表按单件时长乘批量，并计入常量准备时间。交期对照统一取每个作业最短工艺链时间的1.5倍，释放时刻为0。比较均在相同的固定批次、常量准备时间条件下进行。</p><p>焊装数据直接提取作者公开文件中的任务参数、资源映射及前置关系，时间精度为0.1秒。协作模式每次消耗10个预算单位，总预算80。包装数据保留2022年的原始入单与交付日期，按工艺组、印刷色数、加工速度和准备时间构建工序；物料幅宽作为产品属性保留。</p>
<h2>2. 实验设置</h2>{table(['项目','设置'],[['基础与重排实验','21个实例 × 3次重复 × 7种方法，共441次'],['交期策略实验','20个车间实例与1个包装实例 × 3次重复 × 4种方法，共252次'],['求解资源','单工作线程；每次求解预算5秒；种子0、1、2'],['停机事件','在基准任务开始边界发生，停机时长为该任务时长的60%，向上对齐至0.1秒'],['重排约束','保留决策时刻前已开始任务；后续任务不早于决策时刻'],['交期目标','第一阶段最小化总延期；第二阶段在已获得的延期上界内压缩生产跨度'],['独立校验','任务覆盖、加工时长、设备资格、前置关系、资源互斥、释放时间、停机窗口、固定任务和预算']])}
<p>归一化指标按相同实例与种子配对，再对实例等权汇总。基础排产以FCFS为1，异常重排以FCFS-repair为1，交期对照以FCFS总延期为1。确定性规则的重复运行用于记录耗时，其排程保持一致。OPTIMAL仅用于求解器已完成相应目标证明的结果。</p>
<h2>3. 基础排产结果</h2><p>CP-SAT在63组静态配对中，平均归一化生产跨度为{1-static_improvement/100:.4f}，相对FCFS下降{static_improvement:.2f}%。这是本次实例、模型和计算预算下的比较结果。</p>{img('01-static-comparison.png','图1　各实例CP-SAT生产跨度与FCFS之比；虚线为FCFS基准。')}
<h3>白车身焊装单元</h3><p>FCFS排程跨度为{biw_base['makespan']:.1f}秒，CP-SAT为{biw_cp['makespan']:.1f}秒，下降{biw_gain:.2f}%。该解满足43道任务的依赖、8类资源互斥及预算约束，求解状态为{biw_cp['status']}。</p>{img('03-biw-gantt.png','图2　焊装任务排程；颜色区分普通模式与协作模式，条内数字为任务编号。')}
<h2>4. 异常重排结果</h2><p>全局重排的平均归一化跨度为{normalized[1]:.4f}，局部重排为{normalized[2]:.4f}；二者的平均变更工序数分别为{changed[1]:.2f}和{changed[2]:.2f}。局部重排保留更多原安排，表现为跨度与调整范围之间的取舍。</p>{img('02-dynamic-tradeoff.png','图3　异常后的生产跨度与任务变更数量。')}
<h2>5. 交期策略结果</h2>{img('04-delivery-comparison.png','图4　交期策略按逐实例总延期归一化后比较。')}
<p>包装大实例的5秒结果如下。CP-SAT-delivery在该预算下保留了FCFS初始化对应的可行安排；SPT在该实例的总延期指标上更低。结果表明，策略选择需要结合实例规模、目标与计算预算。</p>{table(['算法','生产跨度 / 秒','总延期 / 秒','状态'],[[r['algorithm'],f'{r["makespan"]:.1f}',f'{r["validation"]["tardiness"]:.1f}',r['status']] for r in pack])}
<h2>6. 汇总指标</h2>{table(['阶段','方法','次数','校验通过','归一化指标','平均耗时 / 秒'],summary_rows)}
<h2>7. 系统功能验收</h2>{table(['功能','验收内容'],[['账号与权限','注册、登录、退出、密码修改、工作区成员、角色隔离、禁止自审批'],['订单与资源','订单CRUD、保存前试算、物料到货约束、设备及分段工作日历、文件批量导入'],['排产与异常','CP-SAT异步求解、取消、任务锁定、时间窗口冻结、异常影响与重排、事件恢复'],['计划执行','候选方案保存与比较、审批、发布前复核、版本冲突检测、开工完工记录'],['智能助手','Kimi实际工具调用、图像识别、多轮会话与历史恢复、服务端凭据配置'],['三维车间','Three.js网格、视角操作、设备拾取、真实求解任务驱动的时间轴回放'],['数据实验','实例选择、重复实验、原始结果导出、各方法指标与运行记录']])}
<p>自动化测试记录见交付文件。桌面验收采用1586×992视口，移动端采用390×844视口；检查包括页面导航、订单筛选、试算、任务锁定、日历编辑、模型回放与移动布局。浏览器记录中的页面脚本错误数为{len(checks['page_errors'])}。</p>
<h2>8. 开发与复现</h2><p>核心后端通过华为云码道CLI 26.11.1开发，并保留{len(codearts['events'])}条会话事件与{len(codearts['file_mapping'])}个文件操作映射。随后完成接口联调、独立校验、测试修复与前端实现。会话记录、源代码哈希与测试结果随工程保存。</p><pre>runtime\\portable\\python.exe -m research.runner --suite all --seconds 5 --seeds 3 --workspace 1\nruntime\\portable\\python.exe -m research.delivery\nruntime\\portable\\python.exe -m pytest -q</pre><p>基础与重排汇总记录编号：<code>{rid}</code>；交期实验记录编号：<code>{did}</code>。边界精度修订前的原始运行文件保留，最终汇总引用修订后通过校验的焊装结果，其他实例保持原始运行记录。源数据哈希、处理规则、种子、求解状态和逐任务安排均可追溯。</p>
<h2>数据引用</h2><ol><li>Lele Li. Research on discrete intelligent workshop lot-streaming scheduling with variable sublots under engineer to order. <a href="https://doi.org/10.17632/mwrgpx7hhp.3">10.17632/mwrgpx7hhp.3</a>，CC BY 4.0。</li><li>Qi Xia. Schedule of automotive manufacturing considering man-hour budget. <a href="https://doi.org/10.17632/gswbxhjmnw.1">10.17632/gswbxhjmnw.1</a>，CC BY 4.0。</li><li>Masmur Tarigan. Data for Flexible Packaging Scheduling. <a href="https://doi.org/10.17632/h66hb89k6z.1">10.17632/h66hb89k6z.1</a>，CC BY 4.0。</li></ol><footer>交期管家 · 本地系统与实验交付。统计图由实际结果文件生成。</footer></main></body></html>'''
(OUT/'实验报告.html').write_text(doc,encoding='utf-8')
summary={'runs':len(rows),'valid':all_valid,'distinct_cases':len({r['case'] for r in rows}),'static_makespan_improvement_percent':static_improvement,'dynamic_global_improvement_percent':dynamic_improvement,'biw_makespan_seconds':biw_cp['makespan'],'biw_fcfs_seconds':biw_base['makespan'],'biw_improvement_percent':biw_gain,'source_runs':[rid,did]}
(OUT/'实验汇总.json').write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding='utf-8')
print(json.dumps(summary,ensure_ascii=False,indent=2))
