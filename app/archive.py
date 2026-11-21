import io, csv, json, zipfile, hashlib
from app.errors import BusinessError

def serialize(result):
    if not result.get('manifest') or not result.get('views'):
        raise BusinessError('INCOMPLETE_RUN','批次尚未完成')
    settlement=result.get('settlement') or {}
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w',compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr('manifest.json',json.dumps({**result['manifest'],'revision':result.get('revision',1),'status':result.get('status','待复核')},ensure_ascii=False,indent=2))
        archive.writestr('evidence.json',json.dumps({'counts':result['counts'],'views':result['views'],'quarantine':result['quarantine']},ensure_ascii=False,indent=2))
        archive.writestr('settlement.json',json.dumps({'status':settlement.get('status'),
            'bookable':settlement.get('bookable'),'basis':settlement.get('basis'),
            'totals':settlement.get('totals'),'fragments':settlement.get('fragments'),
            'responsibilities':settlement.get('responsibilities'),
            'instruction_rows':settlement.get('instruction_rows'),
            'tariff_issues':settlement.get('tariff_issues')},ensure_ascii=False,indent=2))
    return buffer.getvalue()
