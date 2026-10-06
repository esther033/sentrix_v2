"""Bounded pod-wide NetworkChaos capability probe, excluded from the dataset."""
import argparse
import importlib.util
import json
from pathlib import Path
import time
from telemetry_export import exclusive_export, save_json

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('attended',ROOT/'scripts/run-attended.py')
attended=importlib.util.module_from_spec(spec);spec.loader.exec_module(attended)

def probe(action):
    name='m2-capability-pod-'+action+'-'+str(int(time.time()))
    folder=ROOT/'audits'/name;folder.mkdir(parents=True)
    result={'complete':False,'action':action,'scope_kind':'service_instance','name':name}
    obj={'apiVersion':'chaos-mesh.org/v1alpha1','kind':'NetworkChaos',
         'metadata':{'name':name,'namespace':'sentrix'},
         'spec':{'action':action,'mode':'one','selector':{'namespaces':['sentrix'],'labelSelectors':{'app':'checkoutservice'}},'duration':'20s'}}
    obj['spec'][action]={'latency':'100ms','jitter':'10ms','correlation':'0'} if action=='delay' else {'loss':'5','correlation':'0'}
    attended.no_active_chaos()
    before=attended.snapshot()
    save_json(folder/'before.json',before)
    if not before['healthy']: raise RuntimeError('Unhealthy environment before probe')
    save_json(folder/'manifest.json',obj)
    applied=False;injected=False;recovered=False
    try:
        response=attended.kube('apply','-f','-',input=json.dumps(obj));applied=True
        save_json(folder/'apply.json',{'response':response,'observed_at':attended.now()})
        deadline=time.monotonic()+90
        while time.monotonic()<deadline:
            status=json.loads(attended.kube('get','networkchaos',name,'-n','sentrix','-o','json'))
            save_json(folder/'polls'/f'{time.time_ns()}.json',{'observed_at':attended.now(),'response':status})
            flags={c['type']:c['status'] for c in status.get('status',{}).get('conditions',[])}
            if flags.get('AllInjected')=='True': injected=True
            if flags.get('AllRecovered')=='True' and injected: recovered=True;break
            time.sleep(2)
        if not injected or not recovered: raise RuntimeError('Probe injection/removal not both observed')
        result['complete']=True
    except Exception as exc:
        result['error']=str(exc)
    finally:
        if applied and not recovered:
            try:
                attended.kube('annotate','networkchaos',name,'-n','sentrix','experiment.chaos-mesh.org/pause=true','--overwrite')
                for _ in range(30):
                    status=json.loads(attended.kube('get','networkchaos',name,'-n','sentrix','-o','json'))
                    save_json(folder/'emergency-removal.json',status,preserve=True)
                    if any(c.get('type')=='AllRecovered' and c.get('status')=='True' for c in status.get('status',{}).get('conditions',[])):
                        recovered=True;break
                    time.sleep(2)
            except Exception as exc:result['cleanup_error']=str(exc)
        result.update(injected=injected,recovered=recovered,finished_at=attended.now())
        save_json(folder/'result.json',result)
    print(json.dumps(result))
    return 0 if result['complete'] else 1

if __name__=='__main__':
    ap=argparse.ArgumentParser();ap.add_argument('--action',choices=['delay','loss'],required=True);args=ap.parse_args()
    with exclusive_export(ROOT/'runs'/'.attended-lock'):
        raise SystemExit(probe(args.action))
