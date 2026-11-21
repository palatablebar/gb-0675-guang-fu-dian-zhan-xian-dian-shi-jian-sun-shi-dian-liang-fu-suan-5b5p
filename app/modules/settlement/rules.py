"""结算计价：把「损失电量」折算为可直接入账的金额。

定稿口径（见 docs/DATA-DICTIONARY.md）：
- 入账金额按**损失时段实际生效费率**逐段计价，费用片段可逐段追到费率依据；
- 夜班的「整张责任单沿用指令下达时费率」保留为对照口径，只展示、不入账；
- 同一损失只计价一次：并集已按设备去重，费用片段按主责任单归属一次；
- 费率冲突、无费率、损失未定等片段一律进入待核，不计入可入账金额。

金额与损失的分段分摊都保证 `各费用片段之和 == 汇总金额`，可直接解释总额：
损失按累计量取整分摊（单调非负），金额逐段取整到分。
"""
from collections import defaultdict
from decimal import Decimal, ROUND_HALF_UP
from app.coerce import number, instant
from app.errors import BusinessError

QLOSS = Decimal('0.0001')
QMONEY = Decimal('0.01')


def qloss(v): return v.quantize(QLOSS, rounding=ROUND_HALF_UP)
def qmoney(v): return v.quantize(QMONEY, rounding=ROUND_HALF_UP)
def txt(v): return format(v, 'f') if isinstance(v, Decimal) else v
def dec(v): return Decimal(str(v))


def load_tariffs(context):
    """解析费率表。价格缺失的条目保留，覆盖到该时点即判 TARIFF_INVALID。"""
    tariffs, issues = [], []
    for raw in context.get('tariffs') or []:
        tid = str(raw.get('id') or '')
        zone = raw.get('zone') or None
        try:
            start, end = instant(raw['start']), instant(raw['end'])
            if not start < end: raise ValueError
        except (BusinessError, KeyError, ValueError, TypeError):
            issues.append({'id': tid, 'code': 'TARIFF_TIME_INVALID', 'detail': '费率有效期缺失、无效或倒置'})
            continue
        price = None
        try:
            price = number(raw.get('price_cny_per_kwh'))
            if price < 0: raise BusinessError('INVALID_RANGE', '费率为负')
        except BusinessError:
            issues.append({'id': tid, 'code': 'TARIFF_PRICE_INVALID', 'detail': '费率价格缺失或无效'})
            price = None
        tariffs.append({'id': tid, 'zone': zone, 'start': start, 'end': end,
                        'price': price, 'source': str(raw.get('source') or '')})
    return tariffs, issues


def _applies(tariff, zone):
    return tariff['zone'] is None or tariff['zone'] == zone


def resolve_tariff(tariffs, zone, at):
    """取 at 时刻对 zone 生效的费率。区域专用优先于全局；多价并存判冲突。"""
    t = instant(at)
    active = [x for x in tariffs if x['start'] <= t < x['end'] and _applies(x, zone)]
    if not active:
        return {'code': 'NO_TARIFF', 'price': None, 'ids': [], 'source': '',
                'detail': '该时段没有生效费率'}
    specific = [x for x in active if x['zone']]
    use = specific or active
    ids = sorted({x['id'] for x in use})
    sources = sorted({x['source'] for x in use if x['source']})
    if any(x['price'] is None for x in use):
        return {'code': 'TARIFF_INVALID', 'price': None, 'ids': ids,
                'source': ' / '.join(sources), 'detail': '生效费率价格无效'}
    prices = {x['price'] for x in use}
    if len(prices) > 1:
        return {'code': 'TARIFF_CONFLICT', 'price': None, 'ids': ids,
                'source': ' / '.join(sources), 'detail': '同一时刻存在多个不同费率：' + '、'.join(ids)}
    return {'code': 'OK', 'price': prices.pop(), 'ids': ids,
            'source': ' / '.join(sources), 'detail': ''}


def _boundaries(tariffs, zone, start, end):
    points = set()
    for x in tariffs:
        if not _applies(x, zone): continue
        for edge in (x['start'], x['end']):
            if start < edge < end: points.add(edge)
    return sorted(points)


def _split(start, end, points):
    cursor, out = start, []
    for edge in points:
        if start < edge < end:
            out.append((cursor, edge)); cursor = edge
    out.append((cursor, end))
    return out


def _instruction_time(state, event_id):
    raw = (state['context'].get('instruction_times') or {}).get(event_id)
    if raw:
        try: return instant(raw)
        except BusinessError: return None
    for row in state['records']:
        if row['kind'] == 'EVENT' and row['id'] == event_id:
            return instant(row['start'])
    return None


def _owner(state, event_ids):
    """主责任单：最早下达指令者，其次编号。协同责任单只标注、不重复计价。"""
    def key(event_id):
        t = _instruction_time(state, event_id)
        return (t is None, t.timestamp() if t else 0.0, event_id)
    return sorted(event_ids, key=key)[0] if event_ids else ''


def _allocate(row, points):
    """按累计秒数分摊损失并量化，片段之和严格等于原段且各段非负。

    用「累计精确量取整后相减」而不是逐段独立取整再让末段吸收尾差：
    后者在片段极多或量极小时可能分摊出负值，前者天然单调、非负。
    """
    start, end = instant(row['start']), instant(row['end'])
    slices = _split(start, end, points)
    total = qloss(dec(row['loss_kwh']))
    if len(slices) == 1:
        return slices, [total]
    span = Decimal(str((end - start).total_seconds()))
    parts, done, previous = [], 0, Decimal(0)
    for a, b in slices[:-1]:
        done += (b - a).total_seconds()
        cumulative = qloss(total * Decimal(str(done)) / span)
        parts.append(cumulative - previous)
        previous = cumulative
    parts.append(total - previous)
    return slices, parts


def build(state):
    tariffs, issues = load_tariffs(state['context'])
    fragments = []
    for row in state['energy_rows']:
        zone = state['context']['devices'][row['device']]['zone']
        start, end = instant(row['start']), instant(row['end'])
        owner = _owner(state, row['event_ids'])
        shared = [x for x in row['event_ids'] if x != owner]
        if row['loss_kwh'] is None:
            fragments.append({
                'device': row['device'], 'start': row['start'], 'end': row['end'],
                'seconds': row['seconds'], 'loss_kwh': None, 'event_ids': list(row['event_ids']),
                'owner_event': owner, 'shared_with': shared,
                'tariff_ids': [], 'price_cny_per_kwh': None, 'amount_cny': None,
                'code': row['code'] if row['code'] != 'LOSS' else 'LOSS_UNKNOWN',
                'basis': '', 'reason': '损失电量未定，不能计价',
            })
            continue
        points = _boundaries(tariffs, zone, start, end)
        slices, losses = _allocate(row, points)
        for (a, b), loss in zip(slices, losses):
            at = a.isoformat()
            tariff = resolve_tariff(tariffs, zone, at)
            frag = {'device': row['device'], 'start': at, 'end': b.isoformat(),
                    'seconds': int((b - a).total_seconds()), 'loss_kwh': txt(loss),
                    'event_ids': list(row['event_ids']), 'owner_event': owner,
                    'shared_with': shared, 'tariff_ids': tariff['ids'],
                    'price_cny_per_kwh': txt(tariff['price']), 'amount_cny': None,
                    'code': 'PRICED', 'basis': tariff['source'], 'reason': ''}
            if tariff['code'] == 'OK':
                amount = qmoney(loss * tariff['price'])
                frag['amount_cny'] = txt(amount)
                frag['code'] = 'NO_LOSS' if loss == 0 else 'PRICED'
            else:
                frag['code'] = tariff['code']
                frag['reason'] = tariff['detail']
            fragments.append(frag)

    priced = [f for f in fragments if f['amount_cny'] is not None]
    pending = [f for f in fragments if f['amount_cny'] is None]
    amount_total = sum((dec(f['amount_cny']) for f in priced), Decimal(0))
    priced_loss = sum((dec(f['loss_kwh']) for f in priced), Decimal(0))
    # 待核片段的损失不一定已知：只要有一段未定，待核损失就不给合计，避免把未知呈现为 0。
    pending_known = [f for f in pending if f['loss_kwh'] is not None]
    pending_loss = (sum((dec(f['loss_kwh']) for f in pending_known), Decimal(0))
                    if len(pending_known) == len(pending) else None)

    responsibilities = _responsibilities(fragments)
    instruction = _instruction(state, tariffs)
    instruction_amount = instruction['amount_cny']
    difference = (amount_total - dec(instruction_amount)) if (instruction_amount is not None and not pending) else None

    fragment_seconds = sum(f['seconds'] for f in fragments)
    bookable = not pending

    return {
        'basis': {
            'bookable': 'actual', 'bookable_label': '损失时段实际生效费率',
            'comparison': 'instruction', 'comparison_label': '指令下达时费率（整张责任单）',
            'rule': '入账金额按逐段实际生效费率；指令费率仅作对照。跨费率变更时两者金额不同，差额单列。',
        },
        'fragments': fragments,
        'responsibilities': responsibilities,
        'instruction_rows': instruction['rows'],
        'instruction_complete': instruction['complete'],
        'pending': pending,
        'tariff_issues': issues,
        'fragment_seconds': fragment_seconds,
        'time_explained': fragment_seconds == sum(r['seconds'] for r in state['energy_rows']),
        'bookable': bookable,
        'status': '可入账' if bookable else '待核',
        'totals': {
            'priced_amount_cny': txt(qmoney(amount_total)),
            'bookable_amount_cny': txt(qmoney(amount_total)) if bookable else None,
            'instruction_amount_cny': instruction_amount,
            'difference_cny': txt(qmoney(difference)) if difference is not None else None,
            'priced_loss_kwh': txt(qloss(priced_loss)),
            'pending_loss_kwh': txt(qloss(pending_loss)) if pending_loss is not None else None,
            'priced_segments': len(priced),
            'pending_segments': len(pending),
            'pending_seconds': sum(f['seconds'] for f in pending),
        },
    }


def _responsibilities(fragments):
    rows = defaultdict(lambda: {'amount': Decimal(0), 'seconds': 0, 'fragments': 0,
                                'pending': 0, 'shared': set(), 'devices': set()})
    for f in fragments:
        bucket = rows[f['owner_event']]
        bucket['seconds'] += f['seconds']; bucket['fragments'] += 1
        bucket['devices'].add(f['device']); bucket['shared'].update(f['shared_with'])
        if f['amount_cny'] is not None: bucket['amount'] += dec(f['amount_cny'])
        else: bucket['pending'] += 1
    return [{'owner_event': k, 'priced_amount_cny': txt(qmoney(v['amount'])), 'seconds': v['seconds'],
             'fragments': v['fragments'], 'pending_segments': v['pending'],
             'shared_events': sorted(v['shared']), 'devices': sorted(v['devices'])}
            for k, v in sorted(rows.items())]


def _instruction(state, tariffs):
    """指令口径：每个责任单（并集）以其最早指令时点的费率整段计价一次。"""
    groups = defaultdict(list)
    for row in state['energy_rows']:
        groups[(row['device'], tuple(sorted(row['event_ids'])))].append(row)
    rows, total, complete = [], Decimal(0), True
    for (device, event_ids), members in sorted(groups.items()):
        zone = state['context']['devices'][device]['zone']
        start = min(m['start'] for m in members); end = max(m['end'] for m in members)
        known = all(m['loss_kwh'] is not None for m in members)
        loss = qloss(sum((dec(m['loss_kwh']) for m in members), Decimal(0))) if known else None
        resolutions = [(eid, _instruction_time(state, eid)) for eid in event_ids]
        bad_time = [eid for eid, t in resolutions if t is None]
        codes, prices, ids, sources = set(), set(), set(), set()
        for eid, t in resolutions:
            if t is None: continue
            r = resolve_tariff(tariffs, zone, t.isoformat())
            codes.add(r['code']); ids.update(r['ids']);
            if r['price'] is not None: prices.add(r['price'])
            if r['source']: sources.add(r['source'])
        if bad_time:
            code, detail = 'INSTRUCTION_TIME_MISSING', '缺少指令下达时点：' + '、'.join(bad_time)
        elif codes - {'OK'}:
            code = sorted(codes - {'OK'})[0]; detail = '指令时点费率不可用'
        elif len(prices) > 1:
            code, detail = 'TARIFF_CONFLICT', '协同责任单指令费率不一致'
        elif not known:
            code, detail = 'LOSS_PENDING', '责任单内损失未定'
        else:
            code, detail = 'OK', ''
        price = prices.pop() if len(prices) == 1 else None
        amount = qmoney(loss * price) if code == 'OK' else None
        if amount is None: complete = False
        else: total += amount
        rows.append({'device': device, 'start': start, 'end': end,
                     'event_ids': list(event_ids), 'loss_kwh': txt(loss) if loss is not None else None,
                     'tariff_ids': sorted(ids), 'price_cny_per_kwh': txt(price) if price is not None else None,
                     'amount_cny': txt(amount) if amount is not None else None,
                     'basis': ' / '.join(sorted(sources)), 'code': code, 'reason': detail})
    return {'rows': rows, 'amount_cny': txt(qmoney(total)) if complete else None,
            'complete': complete}


def apply(state):
    state['settlement'] = build(state)
    s = state['settlement']; t = s['totals']
    state['views']['settlement'] = {
        'title': '结算计价', 'fragments': s['fragments'], 'pending': s['pending'],
        'responsibilities': s['responsibilities'], 'instruction': s['instruction_rows'],
        'totals': t, 'basis': s['basis'], 'status': s['status'],
        'metrics': {'入账金额': t['bookable_amount_cny'], '已计价金额': t['priced_amount_cny'],
                    '已计价片段': t['priced_segments'], '待核片段': t['pending_segments'],
                    '状态': s['status']},
    }
