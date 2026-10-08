from io import BytesIO
import importlib.util
import json
from pathlib import Path
import tarfile
import zipfile
import pandas as pd
import pytest
from tesla_lab.ledger import Ledger
from tesla_lab.data import Lake
from tesla_lab.calendar import sessions


def module(name):
    assert importlib.util.find_spec('tesla_lab.'+name), f'{name} module missing'
    return __import__('tesla_lab.'+name, fromlist=['*'])


def audit_zip(path):
    equity=pd.DataFrame([dict(symbol='TSLA',date='2025-01-02',open=100,high=102,low=99,close=101,volume=1000,interpolated=False)])
    gamma=pd.DataFrame([dict(date=20250102,close=150,gex_total=123),dict(date=20250101,close=100,gex_total=12)])
    with zipfile.ZipFile(path,'w') as z:
        z.writestr('tesla_validation/equity_prices.csv',equity.to_csv(index=False))
        z.writestr('tesla_validation/TSLA_gex_source.csv',gamma.to_csv(index=False))
        z.writestr('tesla_validation/provenance.json',json.dumps({'retrospective_revision_risk':'Recomputed history; availability unknown'}))
    return path


def test_audit_import_keeps_revision_limits_and_quarantine(tmp_path):
    p=module('providers')
    l=Ledger(tmp_path/'store')
    result=p.import_audit(l,audit_zip(tmp_path/'audit.zip'))
    lake=Lake(l)
    assert lake.info(result['equity_daily'])['metadata']['availability']=='retrospective'
    assert lake.read(result['equity_daily']).available_at.isna().all()
    g=lake.read(result['gamma_daily'])
    assert len(g)==1
    assert g.quality.iloc[0]=='quarantined_spot_mismatch'
    assert result['off_calendar_gamma_rows']==1
    assert len(l.datasets())==2
    assert len(p.reference_annotations(l))==9


def test_alpaca_paginates_and_pins_consolidated_adjusted_feed(tmp_path):
    p=module('providers')
    calls=[]
    class Response:
        def __init__(self,body): self.body=body
        def raise_for_status(self): pass
        def json(self): return self.body
        @property
        def content(self): return json.dumps(self.body).encode()
    class Session:
        def get(self,url,**kwargs):
            calls.append(kwargs)
            date='2025-01-02' if len(calls)==1 else '2025-01-03'
            return Response({'bars':{'TSLA':[{'t':date+'T05:00:00Z','o':100,'h':102,'l':99,'c':101,'v':1000}]}, 'next_page_token':'second' if len(calls)==1 else None})
    result=p.Alpaca('id','secret',session=Session()).fetch_daily(['TSLA'],'2025-01-02','2025-01-03')
    assert len(result)==2
    assert calls[0]['params']['feed']=='sip'
    assert calls[0]['params']['adjustment']=='split'
    assert calls[1]['params']['page_token']=='second'
    assert (result.available_at>=result.observed_at).all()


def review():
    return {'rubric':'price-only-test-v1','confirmation_sessions':0,'reviewed_intervals':[{'start':'2025-01-02','end':'2025-02-28'}],
            'episodes':[{'onset':'2025-01-16','confirmation':'2025-01-21','end':'2025-01-24'}], 'note':'All paths reviewed'}


def test_reviews_append_versions_and_never_label_unreviewed_dates(tmp_path):
    e=module('episodes'); l=Ledger(tmp_path)
    a=e.save_review(l,review())
    r=review(); r['note']='Revised review'; b=e.save_review(l,r)
    assert a!=b
    assert e.get_review(l,a)['note']=='All paths reviewed'
    index=sessions('2024-12-02','2025-03-28')
    labels=e.make_labels(index,review(),10)
    assert labels.loc['2024-12-20','target']!=labels.loc['2024-12-20','target']
    assert labels.loc['2025-01-08','target']==1
    assert labels.loc['2025-02-27','target']!=labels.loc['2025-02-27','target']
    assert labels.loc['2025-01-24','target']!=labels.loc['2025-01-24','target']  # active episode


def test_holiday_episode_dates_are_rejected(tmp_path):
    e=module('episodes'); r=review();r['episodes'][0]['onset']='2025-01-20'
    with pytest.raises(ValueError,match='session'):
        e.save_review(Ledger(tmp_path),r)


def test_backup_restore_preserves_jobs_and_dataset_hashes(tmp_path):
    b=module('backup'); p=module('providers')
    l=Ledger(tmp_path/'store'); p.import_audit(l,audit_zip(tmp_path/'audit.zip'))
    jid=l.enqueue('inspect',{})
    archive=b.create_backup(l,tmp_path/'backup.tar.gz')
    b.restore_backup(archive,tmp_path/'restored')
    restored=Ledger(tmp_path/'restored')
    assert restored.job(jid)['status']=='queued'
    assert Lake(restored).read(restored.datasets('equity_daily')[0]['id']).close.iloc[0]==101


def test_restore_rejects_path_traversal(tmp_path):
    b=module('backup')
    path=tmp_path/'unsafe.tar.gz'
    with tarfile.open(path,'w:gz') as tar:
        body=b'bad'; member=tarfile.TarInfo('../escape');member.size=len(body);tar.addfile(member,BytesIO(body))
    with pytest.raises(ValueError): b.restore_backup(path,tmp_path/'target')
    assert not (tmp_path/'escape').exists()


def test_failed_worker_job_does_not_poison_next_job(tmp_path):
    w=module('worker');l=Ledger(tmp_path)
    bad=l.enqueue('unknown',{})
    good=l.enqueue('inspect',{})
    w.run_once(l);w.run_once(l)
    assert l.job(bad)['status']=='failed'
    assert l.job(good)['status']=='succeeded'
