"""Sequential attended pair: never inject when the fresh control failed."""
import argparse
import subprocess
import sys
from pathlib import Path
from datetime import datetime, timezone
from telemetry_export import save_json, exclusive_export
from view_integrity import safe_name

ROOT = Path(__file__).resolve().parents[1]

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--control-id',required=True);ap.add_argument('--fault-id',required=True)
    ap.add_argument('--control-profile',required=True);ap.add_argument('--fault-profile',required=True)
    ap.add_argument('--k6-bin',default='k6')
    args=ap.parse_args()
    for name in (args.control_id,args.fault_id):
        safe_name(name)
        if (ROOT/'runs'/name).exists(): raise ValueError('Refusing existing run: '+name)
    audit=ROOT/'audits'/('pair-'+args.fault_id)
    audit.mkdir(parents=True,exist_ok=False)
    state={'complete':False,'control_id':args.control_id,'fault_id':args.fault_id,'steps':{}}
    with exclusive_export(ROOT/'runs'/'.matched-pair-lock'):
        for run_id,profile,extra in [(args.control_id,args.control_profile,[]),(args.fault_id,args.fault_profile,['--control-run-id',args.control_id])]:
            state['phase']=run_id;state['updated_at']=datetime.now(timezone.utc).isoformat()
            save_json(audit/'status.json',state)
            with (audit/(run_id+'.log')).open('w',encoding='utf8') as log:
                rc=subprocess.run([sys.executable,'-B',str(ROOT/'scripts/run-attended.py'),'--run-id',run_id,'--profile',profile,'--k6-bin',args.k6_bin,'--policy-version','v3',*extra],cwd=ROOT,stdout=log,stderr=log).returncode
            state['steps'][run_id]=rc
            if rc:
                state['phase']='failed';save_json(audit/'status.json',state);return 1
        state['phase']='finished';state['complete']=True
        state['finished_at']=datetime.now(timezone.utc).isoformat()
        save_json(audit/'status.json',state)
    return 0

if __name__=='__main__': raise SystemExit(main())
