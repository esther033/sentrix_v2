from datetime import datetime, timedelta, timezone
import unittest
import json
import tempfile
import yaml
from unittest.mock import patch
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
import evaluation_v2 as ev


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.start=datetime(2026,1,1,tzinfo=timezone.utc)
        self.base={'target_rps':2,'p50_ms':100,'p99_ms':200,'success_rate_pct':100}

    def rows(self,seconds,duration=100,success=True):
        return [{'event_time':(self.start+timedelta(seconds=i/2)).isoformat(),'duration_ms':duration,'success':success} for i in range(seconds*2)]

    def test_short_observation_unknown(self):
        self.assertEqual(ev.recovery(self.rows(60),self.start,self.base)[2],'unknown')

    def test_full_clean_followup_pass(self):
        self.assertEqual(ev.recovery(self.rows(140),self.start,self.base)[2],'pass')

    def test_gap_cannot_bridge_recovery(self):
        rows=self.rows(140)
        rows=[r for r in rows if not 30 < (ev.stamp(r)-self.start).total_seconds()<100]
        self.assertEqual(ev.recovery(rows,self.start,self.base)[2],'unknown')

    def test_timeout_sized_gap_is_not_sleep_but_long_gap_is(self):
        rows=self.rows(140)
        short=[r for r in rows if not 30 < (ev.stamp(r)-self.start).total_seconds()<35]
        self.assertTrue(ev.covered(short,self.start,self.start+timedelta(seconds=140),2))
        long=[r for r in rows if not 30 < (ev.stamp(r)-self.start).total_seconds()<50]
        self.assertFalse(ev.covered(long,self.start,self.start+timedelta(seconds=140),2))

    def test_sustained_slow_no_recovery(self):
        self.assertEqual(ev.recovery(self.rows(140,duration=500),self.start,self.base)[2],'fail')

    def test_sustained_failed_no_recovery(self):
        self.assertEqual(ev.recovery(self.rows(140,success=False),self.start,self.base)[2],'fail')

    def test_truncated_impact_window_not_confirmed(self):
        self.assertFalse(ev.impact(self.rows(10,success=False),self.start,self.start+timedelta(seconds=10),self.base)[0])

    def test_complete_impact_window_confirmed(self):
        self.assertTrue(ev.impact(self.rows(40,success=False),self.start,self.start+timedelta(seconds=40),self.base)[0])

    def test_control_mismatches_and_age_rejected(self):
        profile={'target_rps':2,'duration':'19m'}
        assessment={'valid':True,'errors':[],'profile':profile,'fingerprint':'same','ended_at':self.start.isoformat()}
        with patch.object(ev,'assess_run',return_value=assessment):
            self.assertTrue(ev.match_control('unused',profile,'same',self.start+timedelta(minutes=10))['valid'])
            self.assertFalse(ev.match_control('unused',{**profile,'target_rps':5},'same',self.start)['valid'])
            self.assertFalse(ev.match_control('unused',profile,'different',self.start)['valid'])
            self.assertFalse(ev.match_control('unused',profile,'same',self.start+timedelta(hours=3))['valid'])

    def test_traffic_surge_requires_declared_phase_matched_control(self):
        control = {'target_rps': 2, 'duration': '19m',
                   'traffic_phase_rps': {'warmup': 2, 'baseline': 2, 'fault': 2, 'recovery': 2, 'cooldown': 2}}
        surge = {**control, 'fault': {'type': 'traffic_surge'},
                 'traffic_phase_rps': {**control['traffic_phase_rps'], 'fault': 5},
                 'control_phase_rps': control['traffic_phase_rps']}
        assessment = {'valid': True, 'errors': [], 'profile': control, 'fingerprint': 'same', 'ended_at': self.start.isoformat()}
        with patch.object(ev, 'assess_run', return_value=assessment):
            self.assertTrue(ev.match_control('unused', surge, 'same', self.start + timedelta(minutes=10))['valid'])
            assessment['profile'] = {**control, 'traffic_phase_rps': {**control['traffic_phase_rps'], 'fault': 3}}
            self.assertFalse(ev.match_control('unused', surge, 'same', self.start + timedelta(minutes=10))['valid'])

    def test_run_validity_rejects_runtime_and_coverage_failures(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'run'; root.mkdir()
            rows=self.rows(180)
            for i,r in enumerate(rows): r.update(run_id='run',stage='TRANSACTION_SUMMARY',transaction_id=str(i))
            def save(name,value): (root/name).write_text(json.dumps(value))
            manifest={'workload':{'profile':{'duration':'3m','target_rps':2}},'evaluation_policy_version':ev.VERSION,'fault':{'injected':False}}
            (root/'run-manifest.yaml').write_text(yaml.safe_dump(manifest))
            def tx(data): (root/'transaction-results.jsonl').write_text('\n'.join(json.dumps(r) for r in data))
            tx(rows)
            save('runtime.json',{'workload_rc':0,'workload_elapsed_s':180,'errors':[]})
            save('workload-timeseries.json',[])
            save('quality-report.json',{k:{'status':'PASS'} for k in ('m1a_gate_a_transaction_telemetry','m1a_gate_b_k8s_event_pipeline')})
            for name in ('environment-before.json','environment-after.json'): save(name,{'fingerprint':'same','healthy':True,'restarts':{'pod':0}})
            with patch.object(ev,'validate_views',return_value={'complete':True}):
                self.assertTrue(ev.assess_run(root,control=True)['valid'])
                save('runtime.json',{'workload_rc':1,'workload_elapsed_s':180,'errors':[]})
                self.assertFalse(ev.assess_run(root,control=True)['valid'])
                save('runtime.json',{'workload_rc':0,'workload_elapsed_s':180,'errors':[]})
                tx(rows[:100]+rows[160:])
                self.assertIn('transaction coverage gap',ev.assess_run(root)['errors'])
                tx(rows)
                save('environment-after.json',{'fingerprint':'same','healthy':True,'restarts':{'pod':1}})
                self.assertIn('control pod restart/change',ev.assess_run(root,control=True)['errors'])


if __name__=='__main__': unittest.main()
