"""Verify workload intervention from recorded k6 scenario starts and arrivals."""
from datetime import timedelta
from evaluation_v2 import phase_plan, stamp, covered


def verify_phases(rows, profile):
    plan = phase_plan(profile)
    reports = {}
    origin = None
    offset = 0
    for name, duration, rate in plan:
        selected = [r for r in rows if r.get('workload_scenario') == 'browse_cart_checkout_' + name]
        if not selected:
            raise ValueError('Missing observed workload phase: ' + name)
        starts = {r.get('scenario_start_time') for r in selected}
        if len(starts) != 1 or None in starts:
            raise ValueError('Inconsistent scenario start: ' + name)
        start = stamp({'event_time': next(iter(starts))})
        if origin is None:
            origin = start
        if abs((start - origin).total_seconds() - offset) > .1:
            raise ValueError('Scenario schedule mismatch: ' + name)
        end = start + timedelta(seconds=duration)
        if any(not start <= stamp(r) <= end + timedelta(seconds=1) for r in selected):
            raise ValueError('Arrival outside phase: ' + name)
        count = sum(start <= stamp(r) < end for r in selected)
        if not .9 * rate * duration <= count <= 1.1 * rate * duration:
            raise ValueError('Phase rate outside 90-110%: ' + name)
        if not covered(selected, start, end, rate):
            raise ValueError('Phase coverage gap: ' + name)
        reports[name] = {'started_at': start.isoformat(), 'ended_at': end.isoformat(),
                         'target_rps': rate, 'actual_rps': count / duration, 'count': count}
        offset += duration
    return reports


def confirm_surge(record, rows, profile):
    reports = verify_phases(rows, profile)
    rates = profile['traffic_phase_rps']
    if rates['fault'] <= profile['target_rps'] or any(rates[p] != profile['target_rps'] for p in ('warmup','baseline','recovery','cooldown')):
        raise ValueError('Surge must return to its baseline rate')
    return {**record, 'started_at': reports['fault']['started_at'],
            'ended_at': reports['recovery']['started_at'],
            'removal_confidence': 'confirmed', 'injector_status': 'succeeded',
            'timing_source': 'k6 scenario schedule corroborated by observed transaction arrivals',
            'phase_evidence': reports}
