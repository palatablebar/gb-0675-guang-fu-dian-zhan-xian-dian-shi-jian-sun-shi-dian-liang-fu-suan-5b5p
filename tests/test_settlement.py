from copy import deepcopy
from decimal import Decimal

from app.engine import evaluate


def fragment_sum(s):
    return sum((Decimal(f['amount_cny']) for f in s['settlement']['fragments']
                if f['amount_cny'] is not None), Decimal(0))


# --- 定稿口径 -------------------------------------------------------------

def test_clean_bookable_amount(clean):
    totals = clean['settlement']['totals']
    assert totals['bookable_amount_cny'] == '9.17'
    assert totals['priced_loss_kwh'] == '26.6667'
    assert totals['pending_segments'] == 0
    assert clean['settlement']['status'] == '可入账'
    assert clean['settlement']['bookable'] is True


def test_instruction_basis_differs_across_tariff_change(clean):
    """跨费率变更时，逐段实际费率与整单指令费率金额必须不同且都留档。"""
    totals = clean['settlement']['totals']
    assert totals['instruction_amount_cny'] == '8.00'
    assert totals['difference_cny'] == '1.17'
    assert Decimal(totals['bookable_amount_cny']) != Decimal(totals['instruction_amount_cny'])
    # 指令口径按整张责任单沿用最早指令时点费率 0.30
    for row in clean['settlement']['instruction_rows']:
        assert row['price_cny_per_kwh'] == '0.30'
        assert row['code'] == 'OK'


def test_fragments_explain_total(clean):
    s = clean['settlement']
    assert fragment_sum(clean) == Decimal(s['totals']['bookable_amount_cny'])
    for row in s['responsibilities']:
        owner_total = sum((Decimal(f['amount_cny']) for f in s['fragments']
                           if f['owner_event'] == row['owner_event'] and f['amount_cny']), Decimal(0))
        assert owner_total == Decimal(row['priced_amount_cny'])
    assert sum(Decimal(r['priced_amount_cny']) for r in s['responsibilities']) == Decimal(s['totals']['bookable_amount_cny'])


def test_fragment_loss_sums_to_energy_rows(clean):
    s = clean['settlement']
    for row in clean['energy_rows']:
        parts = [Decimal(f['loss_kwh']) for f in s['fragments']
                 if f['device'] == row['device'] and f['start'] >= row['start'] and f['end'] <= row['end']]
        assert sum(parts) == Decimal(row['loss_kwh']).quantize(Decimal('0.0001'))


# --- 费率依据可追溯 -------------------------------------------------------

def test_tariff_split_at_boundary(clean):
    rows = [f for f in clean['settlement']['fragments'] if f['device'] == 'INV-A']
    # 10:15 费率切换把 10:10–10:20 片段切成两段
    assert [r['start'][11:16] for r in rows] == ['02:00', '02:10', '02:15', '02:20']
    assert [r['tariff_ids'] for r in rows] == [['T-2026Q4-A'], ['T-2026Q4-A'], ['T-2026Q4-B'], ['T-2026Q4-B']]
    assert [r['price_cny_per_kwh'] for r in rows] == ['0.30', '0.30', '0.40', '0.40']
    assert all(r['basis'] for r in rows)


def test_amount_uses_effective_price(clean):
    rows = {r['start'][11:16]: r for r in clean['settlement']['fragments'] if r['device'] == 'INV-A'}
    assert rows['02:15']['amount_cny'] == '1.33'   # 3.3334 kWh × 0.40
    assert rows['02:20']['amount_cny'] == '2.67'   # 6.6667 kWh × 0.40
    assert rows['02:00']['amount_cny'] == '2.00'   # 6.6667 kWh × 0.30


def test_fragment_time_explains_unions(clean):
    s = clean['settlement']
    assert s['time_explained'] is True
    assert s['fragment_seconds'] == sum(r['seconds'] for r in clean['event_unions'])


# --- 同一损失只计价一次 ---------------------------------------------------

def test_overlap_priced_once(clean):
    rows = clean['settlement']['fragments']
    assert sum(r['seconds'] for r in rows if r['device'] == 'INV-A') == 1800
    assert all(r['owner_event'] == 'EA1' for r in rows if r['device'] == 'INV-A')
    assert all(r['shared_with'] == ['EA2'] for r in rows if r['device'] == 'INV-A')
    ea2 = [r for r in clean['settlement']['responsibilities'] if r['owner_event'] == 'EA2']
    assert ea2 == []  # 协同事件不另立金额


def test_total_equals_loss_times_price_consistency(clean):
    # 已计价损失乘各自费率后四舍五入之和，必须等于入账总额
    expected = sum((Decimal(f['loss_kwh']) * Decimal(f['price_cny_per_kwh'])).quantize(Decimal('0.01'))
                   for f in clean['settlement']['fragments'])
    assert expected == Decimal(clean['settlement']['totals']['bookable_amount_cny'])


# --- 待核与费率冲突 -------------------------------------------------------

def test_dirty_pending_segments_not_booked(sample):
    s = sample['settlement']
    assert s['status'] == '待核'
    assert s['bookable'] is False
    assert s['totals']['pending_segments'] == 2
    # 待核时入账金额为 null，已计价部分单列为 priced_amount，不能当作可入账
    assert s['totals']['bookable_amount_cny'] is None
    assert Decimal(s['totals']['priced_amount_cny']) == Decimal('4.17')
    assert s['totals']['difference_cny'] is None
    assert {f['code'] for f in s['pending']} == {'NO_REFERENCE', 'TARGET_GAP'}
    assert all(f['amount_cny'] is None for f in s['pending'])
    assert all(f['price_cny_per_kwh'] is None for f in s['pending'])


def test_no_tariff_keeps_pending(clean):
    s = evaluate({'dataset': 'clean', 'overrides': {'tariffs': []}})
    assert s['settlement']['status'] == '待核'
    assert {f['code'] for f in s['settlement']['fragments']} == {'NO_TARIFF'}
    assert s['settlement']['totals']['bookable_amount_cny'] is None
    assert s['settlement']['totals']['priced_amount_cny'] == '0.00'
    assert s['settlement']['totals']['pending_seconds'] == 3000


def test_conflicting_tariff_not_priced(clean):
    tariffs = deepcopy(clean['context']['tariffs']) + [{
        'id': 'T-DUP', 'zone': None, 'start': '2026-10-15T10:10:00+08:00',
        'end': '2026-10-15T10:20:00+08:00', 'price_cny_per_kwh': '0.55', 'source': '临时价'}]
    s = evaluate({'dataset': 'clean', 'overrides': {'tariffs': tariffs}})
    disputed = [f for f in s['settlement']['fragments'] if f['code'] == 'TARIFF_CONFLICT']
    assert disputed and all(f['amount_cny'] is None for f in disputed)
    assert all('T-DUP' in f['tariff_ids'] for f in disputed)
    # 未被冲突覆盖的片段仍可计价：10:00–10:10 两段 + INV-A 10:20–10:30
    assert Decimal(s['settlement']['totals']['priced_amount_cny']) == Decimal('5.67')
    assert s['settlement']['totals']['bookable_amount_cny'] is None
    assert s['settlement']['totals']['pending_segments'] == len(disputed)
    assert s['settlement']['status'] == '待核'


def test_invalid_tariff_price_not_priced(clean):
    tariffs = deepcopy(clean['context']['tariffs'])
    tariffs[1]['price_cny_per_kwh'] = '未知'
    s = evaluate({'dataset': 'clean', 'overrides': {'tariffs': tariffs}})
    assert 'TARIFF_INVALID' in {f['code'] for f in s['settlement']['fragments']}
    assert s['settlement']['bookable'] is False
    assert s['settlement']['totals']['difference_cny'] is None


def test_broken_tariff_window_reported(clean):
    tariffs = deepcopy(clean['context']['tariffs'])
    tariffs.append({'id': 'T-BAD', 'start': '2026-10-15T12:00:00+08:00',
                    'end': '2026-10-15T11:00:00+08:00', 'price_cny_per_kwh': '0.5'})
    s = evaluate({'dataset': 'clean', 'overrides': {'tariffs': tariffs}})
    assert any(i['id'] == 'T-BAD' and i['code'] == 'TARIFF_TIME_INVALID' for i in s['settlement']['tariff_issues'])


def test_zone_specific_tariff_wins(clean):
    tariffs = deepcopy(clean['context']['tariffs']) + [{
        'id': 'T-EAST', 'zone': '东区', 'start': '2026-10-15T10:00:00+08:00',
        'end': '2026-10-15T10:30:00+08:00', 'price_cny_per_kwh': '0.50', 'source': '东区协议价'}]
    s = evaluate({'dataset': 'clean', 'overrides': {'tariffs': tariffs}})
    east = [f for f in s['settlement']['fragments'] if f['device'] == 'INV-A']
    west = [f for f in s['settlement']['fragments'] if f['device'] == 'INV-B']
    assert all(f['tariff_ids'] == ['T-EAST'] and f['price_cny_per_kwh'] == '0.50' for f in east)
    assert all('T-EAST' not in f['tariff_ids'] for f in west)
    assert all(f['code'] == 'PRICED' for f in east + west)


def test_zero_loss_fragment_priced_zero(clean):
    from app.modules.loss.rules import integrate
    segment = dict(clean['estimates'][0], actual_kw='110')
    assert integrate(segment)['code'] == 'NO_LOSS'


# --- 归档联动 -------------------------------------------------------------

def test_clean_archive_carries_amount(clean):
    assert clean['archive']['status'] == '可归档'
    assert clean['archive']['amount_cny'] == clean['settlement']['totals']['bookable_amount_cny']


def test_pending_blocks_archive(clean):
    s = deepcopy(clean)
    s['settlement']['bookable'] = False
    from app.modules.archive.rules import archive_decision
    assert archive_decision(s)['status'] == '待复核'


def test_many_boundaries_allocate_without_negative_fragments():
    from datetime import timedelta
    from app.coerce import instant
    from app.modules.settlement.rules import _allocate
    row = {'start': '2026-10-15T02:00:00+00:00', 'end': '2026-10-15T02:30:00+00:00', 'loss_kwh': '0.0003'}
    start = instant(row['start'])
    points = [start + timedelta(seconds=s) for s in range(1, 1800)]
    slices, parts = _allocate(row, points)
    assert len(parts) == 1800
    assert min(parts) >= 0
    assert sum(parts) == Decimal('0.0003')


def test_pending_loss_unknown_not_zero(sample):
    """待核片段里含未定损失时，待核损失合计为 null，不能呈现成 0。"""
    s = sample['settlement']
    assert s['totals']['pending_loss_kwh'] is None
    assert all(f['loss_kwh'] is None for f in s['pending'])
