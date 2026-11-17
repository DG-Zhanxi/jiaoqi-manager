export class ApiError extends Error{status:number;constructor(message:string,status:number){super(message);this.status=status}}
function csrf(){return document.cookie.split('; ').find(c=>c.startsWith('jiaoqi_csrf='))?.split('=').slice(1).join('=')||''}
export async function api<T=unknown>(path:string,init:RequestInit={}):Promise<T>{
 const h=new Headers(init.headers); if(init.body&&!(init.body instanceof FormData))h.set('Content-Type','application/json');
 if(init.method&&!['GET','HEAD'].includes(init.method))h.set('X-CSRF-Token',decodeURIComponent(csrf()));
 const r=await fetch('/api'+path,{...init,headers:h,credentials:'include'});
 const d=r.status===204?null:await r.json().catch(()=>null);
 if(!r.ok){const detail=d?.detail??d?.error?.message??d?.error??d?.message;throw new ApiError(typeof detail==='string'?detail:Array.isArray(detail)?detail.map((x:{msg?:string})=>x.msg).join('；'):`请求失败 (${r.status})`,r.status)}
 return d as T;
}
export const post=<T=unknown>(p:string,body:unknown={})=>api<T>(p,{method:'POST',body:JSON.stringify(body)});
export const patch=<T=unknown>(p:string,body:unknown)=>api<T>(p,{method:'PATCH',body:JSON.stringify(body)});
export const list=<T>(d:T[]|{items?:T[];data?:T[]}):T[]=>Array.isArray(d)?d:d?.items??d?.data??[];
export const wsPath=(id:number,path='')=>`/workspaces/${id}${path}`;
export function downloadJSON(name:string,data:unknown){const a=document.createElement('a');const u=URL.createObjectURL(new Blob([JSON.stringify(data,null,2)],{type:'application/json'}));a.href=u;a.download=name;a.click();setTimeout(()=>URL.revokeObjectURL(u),1000)}
