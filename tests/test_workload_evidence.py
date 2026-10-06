import sys
from pathlib import Path
from datetime import datetime, timezone, timedelta
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]/'scripts'))
from workload_evidence import confirm_surge

class EvidenceTests(unittest.TestCase):
    def fixture(self):
        names=('warmup','baseline','fault','recovery','cooldown')
        profile={'duration':'150s','target_rps':2,'traffic_phase_rps':dict.fromkeys(names,2)}
        profile['traffic_phase_rps']['fault']=5
        rows=[]; origin=datetime(2026,1,1,tzinfo=timezone.utc)
        for n,name in enumerate(names):
            profile[name+'_seconds']=30
            start=origin+timedelta(seconds=n*30); rate=profile['traffic_phase_rps'][name]
            for i in range(30*rate):
                rows.append({'workload_scenario':'browse_cart_checkout_'+name,'scenario_start_time':start.isoformat(),'event_time':(start+timedelta(seconds=i/rate)).isoformat()})
        return profile,rows

    def test_confirmed_only_with_observed_rates_and_return(self):
        p,rows=self.fixture(); r=confirm_surge({},rows,p)
        self.assertEqual(r['removal_confidence'],'confirmed')
        self.assertEqual(r['phase_evidence']['fault']['actual_rps'],5)
        self.assertEqual(r['phase_evidence']['recovery']['actual_rps'],2)

    def test_scheduled_transition_without_recovery_records_rejected(self):
        p,rows=self.fixture()
        with self.assertRaises(ValueError):
            confirm_surge({},[r for r in rows if not r['workload_scenario'].endswith('recovery')],p)

    def test_excess_recovery_rate_cannot_pass_lower_bound_only(self):
        p,rows=self.fixture()
        rows += [r for r in rows if r['workload_scenario'].endswith('recovery')]
        with self.assertRaises(ValueError): confirm_surge({},rows,p)

    def test_underachieved_surge_rejected(self):
        p,rows=self.fixture(); rows=[r for i,r in enumerate(rows) if not r['workload_scenario'].endswith('fault') or i%2]
        with self.assertRaises(ValueError): confirm_surge({},rows,p)
