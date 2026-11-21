import hashlib, json

def archive_decision(state):
    b = state['balance']
    settled = state.get('settlement') or {}
    ready = (b['row_balanced'] and b['time_balanced'] and b['energy_balanced']
             and not b['unknown_segments'] and not state['quarantine']
             and settled.get('bookable', False))
    reasons = []
    if not b['row_balanced']: reasons.append('行不守恒')
    if not b['time_balanced']: reasons.append('时间不守恒')
    if not b['energy_balanced']: reasons.append('能量不守恒')
    if b['unknown_segments']: reasons.append('存在未定片段')
    if state['quarantine']: reasons.append('存在隔离行')
    if settled and not settled.get('bookable', False): reasons.append('存在待核费用片段')
    return {'status': '可归档' if ready else '待复核',
            'seal': hashlib.sha256(json.dumps(state['energy_rows'], sort_keys=True).encode()).hexdigest(),
            'segments': len(state['energy_rows']), 'unknown': b['unknown_segments'],
            'amount_cny': (settled.get('totals') or {}).get('bookable_amount_cny'),
            'blockers': reasons}

def apply(state):
    state['archive'] = archive_decision(state)
    state['views']['archive'] = {
        'title': '运行凭证', 'rows': state['event_unions'],
        'metrics': {'切片': state['archive']['segments'], '未定': state['archive']['unknown'],
                    '入账金额': state['archive']['amount_cny'], '状态': state['archive']['status']}}
