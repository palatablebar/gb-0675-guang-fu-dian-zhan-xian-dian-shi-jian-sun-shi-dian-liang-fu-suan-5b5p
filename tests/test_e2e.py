from fastapi.testclient import TestClient
from app import main,store
from app.engine import evaluate
import pytest

def client(tmp_path,monkeypatch):
    monkeypatch.setattr(main,'DB_PATH',str(tmp_path/'test.sqlite'));return TestClient(main.app)
def test_api_idempotent(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();b=c.post('/api/runs',json={}).json();assert a['manifest']['run_id']==b['manifest']['run_id'];assert len(c.get('/api/runs').json())==1
def test_stale_review(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);a=c.post('/api/runs',json={}).json();url='/api/runs/'+a['manifest']['run_id']+'/review';assert c.post(url,json={'revision':1}).status_code==200;assert c.post(url,json={'revision':1}).status_code==409
def test_seven_entries(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    for k in main.MODULES:
        assert c.post('/api/'+k,json={}).status_code==200
        assert c.get('/'+k).status_code==200
def test_bad_dataset(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.post('/api/runs',json={'dataset':'../bad'}).status_code==422
def test_missing_run(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);assert c.get('/api/runs/none').status_code==404
def test_restart_store(tmp_path,sample):
    p=str(tmp_path/'durable.sqlite');store.save(p,sample);assert store.get(p,sample['manifest']['run_id'])['manifest']['sha256']==sample['manifest']['sha256']
def test_bad_revision(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch);s=c.post('/api/runs',json={}).json();assert c.post('/api/runs/'+s['manifest']['run_id']+'/review',json={'revision':0}).status_code==422

def test_clean_navigation_run_preserved(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    for k in main.MODULES:
        html=c.get('/'+k+'?run_id='+rid).text
        assert '?run_id='+rid in html
        assert rid in html
    assert c.get('/api/export/'+rid).status_code==200

def test_custom_csv_snapshot_and_navigation(tmp_path,monkeypatch):
    from app.config import ROOT
    c=client(tmp_path,monkeypatch)
    original=(ROOT/'sample_data/clean.csv').read_text()
    # 改成独立于所有仓内样例的证据编号，追踪其跨页与重跑行为。
    first=original.splitlines()[1].split(',')[0]
    custom=original.replace(first,first+'-CUSTOM')
    result=c.post('/api/runs',json={'csv_text':custom}).json()
    rid=result['manifest']['run_id']
    assert '-CUSTOM' in str(result['records']) or '-CUSTOM' in str(result['quarantine'])
    for key in main.MODULES:
        response=c.get('/'+key+'?run_id='+rid)
        assert response.status_code==200
        assert '?run_id='+rid in response.text
    repeated=c.post('/api/runs/'+rid+'/rerun',json={})
    assert repeated.status_code==200
    assert repeated.json()['manifest']['run_id']==rid
    assert repeated.json()['manifest']['sha256']==result['manifest']['sha256']


def test_settlement_entry_and_amount(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    assert c.post('/api/settlement',json={'dataset':'clean'}).status_code==200
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    html=c.get('/settlement?run_id='+rid).text
    assert run['settlement']['totals']['bookable_amount_cny'] in html
    assert 'T-2026Q4-B' in html                     # 费率依据可见
    assert '待核' in html

def test_rerun_keeps_reviewed_amount(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    reviewed=c.post('/api/runs/'+rid+'/review',json={'revision':1}).json()
    assert reviewed['status']=='已复核'
    again=c.post('/api/runs/'+rid+'/rerun',json={}).json()
    assert again['manifest']['run_id']==rid
    assert again['revision']==reviewed['revision']
    assert again['status']=='已复核'
    assert again['settlement']['totals']==run['settlement']['totals']
    assert len(c.get('/api/runs').json())==1

def test_failed_switch_keeps_previous_run(tmp_path,monkeypatch):
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    bad=c.post('/api/runs',json={'dataset':'../bad'})
    assert bad.status_code==422
    history=c.get('/api/runs').json()
    assert [h['id'] for h in history]==[rid]
    assert c.get('/api/runs/'+rid).json()['settlement']['totals']==run['settlement']['totals']

def test_export_contains_settlement(tmp_path,monkeypatch):
    import zipfile,io,json
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    z=zipfile.ZipFile(io.BytesIO(c.get('/api/export/'+rid).content))
    payload=json.loads(z.read('settlement.json'))
    assert payload['totals']['bookable_amount_cny']=='9.17'
    assert payload['totals']['instruction_amount_cny']=='8.00'
    assert len(payload['fragments'])==7

def test_rule_bump_never_overwrites_reviewed_settlement(tmp_path,monkeypatch):
    """规则版本变化时重跑必须生成为独立批次，旧已复核结算原样保留。"""
    c=client(tmp_path,monkeypatch)
    run=c.post('/api/runs',json={'dataset':'clean'}).json()
    rid=run['manifest']['run_id']
    c.post('/api/runs/'+rid+'/review',json={'revision':1})
    from app import manifest
    monkeypatch.setattr(manifest,'RULE_VERSION','2099.01.01-r99')
    new=c.post('/api/runs/'+rid+'/rerun',json={}).json()
    assert new['manifest']['run_id']!=rid
    assert new['manifest']['rule_version']=='2099.01.01-r99'
    old=c.get('/api/runs/'+rid).json()
    assert old['status']=='已复核' and old['revision']==2
    assert old['settlement']['totals']==run['settlement']['totals']
    ids={h['id'] for h in c.get('/api/runs').json()}
    assert ids=={rid,new['manifest']['run_id']}
