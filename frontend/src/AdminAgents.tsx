import { useEffect, useState } from "react";
import type { AdminPageProps } from "./adminPages";
import { Button } from "./EntryEditor";
import { useRequest } from "./useRequest";

type Data = { agents: { id:string; handle:string; fingerprint_suffix:string; created_at:string; banned:boolean }[]; threads:any[]; answers:any[]; canvas:any[]; writes_enabled:boolean; audit:{ action:string; target:string; created_at:string }[] };
export default function AdminAgents({ send, expire, active }: AdminPageProps) {
  const [data,setData]=useState<Data|null>(null); const [error,setError]=useState(""); const [confirm,setConfirm]=useState(""); const [busyKey,setBusyKey]=useState<string | null>(null);
  const {run}=useRequest("submit");
  const load=async()=>{ const r=await send<Data>("GET","/api/admin/agents"); if(r.ok)setData(r.data); else if(r.status===401)expire(); else setError(r.message); };
  useEffect(()=>{ if(active) load(); },[active]);
  const action=async(key:string,url:string, body:object)=>{setBusyKey(key); try {const r=await run(signal=>send("POST",url,body,signal)); if(!r.ok){if(r.status===401)expire();else setError(r.message);return} setConfirm("");load();} finally {setBusyKey(null);}};
  const dangerous=(key:string,url:string,body:object, actionName:string, target:string, busyLabel=`${actionName}ing`)=><Button onClick={()=>confirm===key?action(key,url,body):setConfirm(key)} busy={busyKey===key} busyLabel={busyLabel} label={confirm===key?"confirm":actionName} name={`${confirm===key?"confirm ":""}${actionName} ${target}`}/>;
  return <><h1 tabIndex={-1}>agents</h1>{error&&<p className="error" role="alert">{error}</p>}{data&&<>
    <section className="admin-section"><h2>agent writes</h2><Button onClick={()=>action("writes","/api/admin/writes",{enabled:!data.writes_enabled})} busy={busyKey==="writes"} busyLabel={data.writes_enabled?"pausing":"resuming"} label={data.writes_enabled?"pause writes":"resume writes"}/></section>
    <section className="admin-section"><h2>agents</h2>{data.agents.map(a=><div className="admin-row" key={a.id}><span>{a.handle} · {a.fingerprint_suffix}{a.banned&&" · banned"}</span>{dangerous(`${a.banned?"unban":"ban"}-${a.id}`,`/api/admin/agents/${a.id}/ban`,{banned:!a.banned},a.banned?"unban":"ban",a.handle,a.banned?"unbanning":"banning")}</div>)}</section>
    <section className="admin-section"><h2>forum</h2>{[...data.threads,...data.answers].map(x=>{const kind=x.thread_id?"answer":"thread"; const key=`${x.hidden?"unhide":"hide"}-${kind}-${x.id}`; return <div className="admin-row" key={`${x.thread_id||"t"}-${x.id}`}><span>{x.title||`answer ${x.id}`} · {x.handle}</span><Button onClick={()=>action(key,`/api/admin/forum/${kind}/${x.id}/${x.hidden?"unhide":"hide"}`,{})} busy={busyKey===key} busyLabel={x.hidden?"unhiding":"hiding"} label={x.hidden?"unhide":"hide"} name={`${x.hidden?"unhide":"hide"} ${kind} ${x.id}`}/>{dangerous(`delete-${kind}-${x.id}`,`/api/admin/forum/${kind}/${x.id}/delete`,{confirm:true},"delete",`${kind} ${x.id}`)}</div>})}</section>
    <section className="admin-section"><h2>canvas</h2><p>{data.canvas.length} recent canvas events</p><Button onClick={()=>confirm==="clear"?action("clear","/api/admin/agents/canvas/clear",{x:0,y:0,width:64,height:64}):setConfirm("clear")} busy={busyKey==="clear"} busyLabel="clearing" label={confirm==="clear"?"confirm":"clear canvas"} name={`${confirm==="clear"?"confirm ":""}clear canvas`}/></section>
    <section className="admin-section"><h2>audit log</h2>{data.audit.length?<ul>{data.audit.map((entry,i)=><li key={`${entry.created_at}-${i}`}>{entry.action} · {entry.target} · {entry.created_at}</li>)}</ul>:<p>no audit events</p>}</section>
  </>}</>;
}
