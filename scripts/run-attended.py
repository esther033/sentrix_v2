"""Bounded, logged control/fault execution. Never reuses an existing run ID."""
import argparse
import ctypes
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import urllib.request
import yaml

from evaluation_v2 import seconds, match_control, assess_run
from telemetry_export import atomic_write, save_json, digest, exclusive_export
from view_integrity import safe_name
from workload_evidence import confirm_surge

ROOT = Path(__file__).resolve().parents[1]


def now():
    return datetime.now(timezone.utc).isoformat()


def kube(*args, input=None):
    p = subprocess.run(["kubectl","--request-timeout=10s",*args],input=input,text=True,capture_output=True,timeout=15)
    if p.returncode:
        raise RuntimeError(p.stderr)
    return p.stdout


def snapshot():
    pods = json.loads(kube("get","pods","-n","sentrix","-o","json"))
    ns = json.loads(kube("get","namespace","sentrix","-o","json"))
    nodes = json.loads(kube("get","nodes","-o","json"))
    objects = json.loads(kube("get","deployments,statefulsets,services,configmaps","-n","sentrix","-o","json"))
    configs = []
    for o in objects["items"]:
        spec = o.get("spec",{})
        if o["kind"] in ("Deployment","StatefulSet"):
            spec = {"replicas":spec.get("replicas"),"template":spec["template"]}
        configs.append({"kind":o["kind"],"name":o["metadata"]["name"],"spec":spec,"data":o.get("data"),"binaryData":o.get("binaryData")})
    images = sorted({(p["metadata"].get("labels",{}).get("app",p["metadata"]["name"]),c["name"],c.get("imageID")) for p in pods["items"] for c in p["status"].get("containerStatuses",[])})
    restarts = {p["metadata"]["uid"]+"/"+c["name"]:c["restartCount"] for p in pods["items"] for c in p["status"].get("containerStatuses",[])}
    fingerprint = digest({"namespace":ns["metadata"]["uid"],"nodes":sorted(n["metadata"]["uid"] for n in nodes["items"]),"config":sorted(configs,key=lambda o:(o['kind'],o['name'])),"images":images})
    healthy = bool(pods['items']) and all(p['status'].get('phase')=='Running' and all(c.get('ready') for c in p['status'].get('containerStatuses',[])) for p in pods['items'])
    return {"captured_at":now(),"fingerprint":fingerprint,"healthy":healthy,"restarts":restarts,"pods":pods,"objects":objects}


def no_active_chaos():
    data = json.loads(kube("get","stresschaos,networkchaos,podchaos","-n","sentrix","-o","json"))
    for item in data["items"]:
        if not any(c.get("type")=="AllRecovered" and c.get("status")=="True" for c in item.get("status",{}).get("conditions",[])):
            raise RuntimeError("Unrecovered Chaos object: "+item['metadata']['name'])
    return data


def terminate(proc):
    if proc.poll() is None:
        if os.name=="nt":
            subprocess.run(["taskkill","/PID",str(proc.pid),"/T","/F"],capture_output=True,timeout=15)
        else:
            proc.terminate()
        proc.wait(timeout=15)


def observation_gap_seconds(previous_wall, current_wall):
    """Return elapsed wall time between supervisor observations.

    Wall time advances while Windows is suspended, unlike a normal two-second
    poll.  Keeping this separate makes a post-resume data gap explicit rather
    than reporting it only as a generic stage timeout.
    """
    return max(0.0, current_wall - previous_wall)


def run(args):
    safe_name(args.run_id)
    profile = json.loads(Path(args.profile).read_text())
    duration = seconds(profile["duration"])
    if sum(profile[k] for k in ('warmup_seconds','baseline_seconds','fault_seconds','recovery_seconds','cooldown_seconds'))!=duration:
        raise ValueError("Phase lengths do not match duration")
    fault = profile.get("fault")
    workload_fault = bool(fault and fault.get("type") == "traffic_surge")
    if fault and not args.control_run_id:
        raise ValueError("Matching control required")
    run_dir = ROOT/"runs"/args.run_id
    if run_dir.exists(): raise ValueError("Run exists; resume postprocessing only")
    no_active_chaos()
    before = snapshot()
    if not before["healthy"]: raise ValueError("Pods not healthy")
    for port,path in ((8080,"/"),(3200,"/ready"),(9090,"/-/ready"),(3100,"/loki/api/v1/labels")):
        with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}",timeout=10) as r: r.read()
    if fault:
        match = match_control(ROOT/"runs"/args.control_run_id,profile,before["fingerprint"],datetime.now(timezone.utc))
        if not match["valid"]: raise ValueError("Control rejected: "+str(match["errors"]))
    run_dir.mkdir()
    save_json(run_dir/"environment-before.json",before)
    state = {"run_id":args.run_id,"started_at":now(),"errors":[],"steps":{}}
    fault_record = None
    proc = None
    log = (run_dir/"supervisor.log").open("w",encoding="utf-8")
    if os.name=="nt": ctypes.windll.kernel32.SetThreadExecutionState(0x80000001)
    def stage(script, extra, timeout):
        state['phase'] = script
        child = subprocess.Popen([sys.executable,"-B",str(ROOT/script),"--run-id",args.run_id,*extra],stdout=log,stderr=log)
        deadline = time.monotonic() + timeout
        previous_wall = time.time()
        try:
            while child.poll() is None:
                current_wall = time.time()
                gap = observation_gap_seconds(previous_wall, current_wall)
                if gap > 15:
                    state['observation_gap_seconds'] = round(gap, 3)
                    raise RuntimeError('Supervisor observation gap >15s during postprocessing (possible suspend)')
                previous_wall = current_wall
                state['heartbeat_at'] = now()
                state['stage_pid'] = child.pid
                save_json(run_dir/'runtime.json',state)
                if time.monotonic() > deadline:
                    raise TimeoutError('Postprocessing deadline: ' + script)
                time.sleep(2)
        except BaseException:
            terminate(child)
            raise
        state['stage_pid'] = None
        state["steps"][script] = child.returncode
        save_json(run_dir/"runtime.json",state)
        return child.returncode
    try:
        started = last_poll = time.monotonic()
        proc = subprocess.Popen([sys.executable,"-B",str(ROOT/"recorder/transaction-recorder.py"),"--run-id",args.run_id,"--profile",args.profile,"--base-url","http://127.0.0.1:8080","--k6-bin",args.k6_bin],stdout=log,stderr=log)
        state["recorder_pid"] = proc.pid
        while proc.poll() is None:
            current = time.monotonic(); elapsed=current-started
            if current-last_poll>15: raise RuntimeError("Supervisor poll gap >15s (possible suspend)")
            last_poll=current
            if elapsed>duration+60: raise RuntimeError("Workload hard deadline exceeded")
            console = run_dir/"k6-console-output.log"
            if elapsed>60 and (not console.exists() or time.time()-console.stat().st_mtime>60):
                raise RuntimeError("Workload console inactive >60s")
            if fault and fault_record is None and elapsed>=profile["warmup_seconds"]+profile["baseline_seconds"]:
                name=args.run_id+"-fault"
                safe_name(name)
                if len(name)>63: raise ValueError("Chaos name too long")
                if workload_fault:
                    kind = "workload"
                    planned = {"phase": "fault", "target_rps": profile["traffic_phase_rps"]["fault"],
                               "duration_seconds": profile["fault_seconds"]}
                    atomic_write(run_dir/"injected-manifest.yaml",yaml.safe_dump(planned,sort_keys=False))
                    fault_record={"fault_kind":kind,"fault_name":name,"type":fault['type'],"root_cause_service":fault['root_cause_service'],"started_at":now(),"ended_at":None,"removal_confidence":"scheduled_pending_actual_rate_validation","injector_status":"succeeded","target_pod_uid":None,"workload_phase":planned}
                else:
                    template=(ROOT/fault["template"]).read_text()
                    substitutions={
                        "NAME": name,
                        "SOURCE_SERVICE": fault["root_cause_service"],
                        "TARGET_SERVICE": fault["root_cause_service"],
                        "DURATION": str(profile['fault_seconds'])+'s',
                        **fault.get('params',{}),
                    }
                    for key,value in substitutions.items(): template=template.replace('{{'+key+'}}',str(value))
                    kind=yaml.safe_load(template)['kind'].lower()
                    atomic_write(run_dir/"injected-manifest.yaml",template)
                    fault_record={"fault_kind":kind,"fault_name":name,"type":fault['type'],"root_cause_service":fault['root_cause_service'],"started_at":None,"ended_at":None,"removal_confidence":"unknown","injector_status":"unknown","target_pod_uid":None}
                save_json(run_dir/"fault-record.json",fault_record)
                if workload_fault:
                    atomic_write(run_dir/"apply-response.txt","workload phase scheduled by k6\n")
                else:
                    response=kube('apply','-f','-',input=template)
                    atomic_write(run_dir/"apply-response.txt",response)
                state['fault_apply_elapsed']=elapsed
            if fault_record and not fault_record['ended_at']:
                if workload_fault:
                    if elapsed-state['fault_apply_elapsed'] >= profile['fault_seconds']:
                        fault_record['ended_at']=now()
                        fault_record['removal_confidence']='confirmed_by_workload_schedule_pending_actual_rate_validation'
                        fault_record['raw_removal_response']={"observed_at":now(),"next_phase":"recovery","target_rps":profile['traffic_phase_rps']['recovery']}
                else:
                    obj=json.loads(kube('get',fault_record['fault_kind'],fault_record['fault_name'],'-n','sentrix','-o','json'))
                    save_json(run_dir/'fault-polls'/f'{time.time_ns()}.json',{'observed_at':now(),'response':obj})
                    conditions={c['type']:c['status'] for c in obj.get('status',{}).get('conditions',[])}
                    if conditions.get('AllInjected')=='True' and not fault_record['started_at']:
                        fault_record['started_at']=now(); fault_record['raw_inject_status']=obj
                        target=json.loads(kube('get','pods','-n','sentrix','-l','app='+fault['root_cause_service'],'-o','json'))
                        fault_record['target_pod_uid']=target['items'][0]['metadata']['uid']
                    if fault_record['started_at'] and conditions.get('AllRecovered')=='True':
                        fault_record['ended_at']=now(); fault_record['removal_confidence']='confirmed'; fault_record['injector_status']='succeeded'; fault_record['raw_removal_response']=obj
                save_json(run_dir/'fault-record.json',fault_record)
                if not workload_fault and elapsed-state['fault_apply_elapsed']>60 and not fault_record['started_at']: raise RuntimeError('Injection not confirmed')
                if elapsed-state['fault_apply_elapsed']>profile['fault_seconds']+120 and not fault_record['ended_at']: raise RuntimeError('Fault removal not confirmed')
            state['elapsed_s']=round(elapsed,2); state['heartbeat_at']=now()
            save_json(run_dir/'runtime.json',state)
            time.sleep(2)
        state['workload_rc']=proc.returncode
        state['workload_elapsed_s']=time.monotonic()-started
        if proc.returncode: state['errors'].append('recorder failed')
        if workload_fault and proc.returncode == 0 and fault_record:
            summaries = [json.loads(line) for line in (run_dir/'transaction-results.jsonl').read_text(encoding='utf-8').splitlines() if line]
            summaries = [r for r in summaries if r['stage'] == 'TRANSACTION_SUMMARY']
            fault_record = confirm_surge(fault_record, summaries, profile)
            save_json(run_dir/'fault-record.json', fault_record, preserve=True)
        if fault and (not fault_record or not fault_record['ended_at']): state['errors'].append('fault lifecycle incomplete')
    except Exception as exc:
        state['errors'].append(str(exc))
        if proc: terminate(proc)
    finally:
        if fault_record and not workload_fault and not fault_record['ended_at']:
            try:
                kube('annotate',fault_record['fault_kind'],fault_record['fault_name'],'-n','sentrix','experiment.chaos-mesh.org/pause=true','--overwrite')
                for _ in range(30):
                    obj=json.loads(kube('get',fault_record['fault_kind'],fault_record['fault_name'],'-n','sentrix','-o','json'))
                    save_json(run_dir/'emergency-removal.json',{'observed_at':now(),'response':obj},preserve=True)
                    if any(c.get('type')=='AllRecovered' and c.get('status')=='True' for c in obj.get('status',{}).get('conditions',[])): break
                    time.sleep(2)
            except Exception as exc: state['errors'].append('Emergency recovery: '+str(exc))
        try: save_json(run_dir/'environment-after.json',snapshot())
        except Exception as exc: state['errors'].append('Post-snapshot: '+str(exc))
        save_json(run_dir/'runtime.json',state)
    try:
        stage('scripts/generate-run-manifest.py',['--profile',args.profile,'--evaluation-policy-version',args.policy_version],90)
        stage('smoke/quality-check.py',[],120)
        for _ in range(2):
            if stage('scripts/process-telemetry.py',['--trace-workers',str(profile.get('trace_workers', 1))],4350)==0: break
        assessment=assess_run(run_dir,control=not bool(fault))
        save_json(run_dir/'run-assessment-v2.json',assessment)
        if fault:
            stage('scripts/generate-incident-record.py',['--incident-id','inc-'+args.run_id,'--experiment-id','exp-'+args.run_id,'--control-run-id',args.control_run_id],180)
        state['finished_at']=now(); state['assessment_valid']=assessment['valid']
        state['phase']='finished'
        state['complete']=bool(assessment['valid'] and not state['errors'] and all(v==0 for v in state['steps'].values()))
    except Exception as exc:
        state['errors'].append('Postprocessing: '+str(exc))
        state['phase']='failed'; state['complete']=False; state['finished_at']=now()
    finally:
        save_json(run_dir/'runtime.json',state)
        log.close()
        if os.name=='nt': ctypes.windll.kernel32.SetThreadExecutionState(0x80000000)
    print(json.dumps(state,indent=2))
    return 0 if state.get('assessment_valid') and not state['errors'] and all(v==0 for v in state['steps'].values()) else 1


if __name__=='__main__':
    ap=argparse.ArgumentParser(); ap.add_argument('--run-id',required=True); ap.add_argument('--profile',required=True); ap.add_argument('--control-run-id'); ap.add_argument('--k6-bin',default='k6'); ap.add_argument('--policy-version',default='v3')
    args=ap.parse_args()
    with exclusive_export(ROOT/'runs'/'.attended-lock'):
        raise SystemExit(run(args))
