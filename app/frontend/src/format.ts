export function dateTime(s?:string|null){if(!s)return '—';const d=new Date(s);return Number.isNaN(d.getTime())?s:new Intl.DateTimeFormat('zh-CN',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(d)}
export function timeOnly(s:string){return new Intl.DateTimeFormat('zh-CN',{hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false,timeZone:'Asia/Shanghai'}).format(new Date(s))}
export function duration(s:number){if(s<60)return `${Math.round(s*10)/10}秒`;if(s<3600)return `${Math.round(s/60*10)/10}分`;return `${Math.round(s/3600*10)/10}小时`}
export function localInput(s?:string|null){const d=s?new Date(s):new Date();return new Date(d.getTime()-d.getTimezoneOffset()*60000).toISOString().slice(0,16)}
export const isoInput=(v:string)=>new Date(v).toISOString();
export const statusNames:Record<string,string>={draft:'草案',submitted:'待审批',approved:'已批准',rejected:'已驳回',published:'已发布',expired:'已过期',confirmed:'待生产',planned:'待开工',running:'运行中',completed:'已完成',cancelled:'已取消',failed:'失败',queued:'排队中',valid:'已校验',OPTIMAL:'最优解',FEASIBLE:'可行解',INFEASIBLE:'无可行解',UNKNOWN:'尚未找到解',MODEL_INVALID:'模型无效'};
export const roleNames:Record<string,string>={admin:'管理员',planner:'计划员',reviewer:'审批负责人',operator:'执行人员'};
export const strategies:Record<string,string>={delivery_first:'交期优先',stability_first:'少改动',overtime_limited:'限时排产'};
