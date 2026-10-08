import importlib.util
import json
import numpy as np
import pandas as pd
import pytest
from tesla_lab.calendar import sessions, exchange
from tesla_lab.data import Lake
from tesla_lab.episodes import make_labels, save_review
from tesla_lab.ledger import Ledger
from tesla_lab.worker import run_once


def research():
    assert importlib.util.find_spec('tesla_lab.research'), 'research evaluator missing'
    from tesla_lab import research
    return research


def synthetic_prices(start='2017-01-03', end='2023-12-29'):
    index=sessions(start,end)
    rng=np.random.default_rng(41)
    frames=[]
    for symbol in ['TSLA','SPY']:
        close=100*np.exp(np.cumsum(rng.normal(.0003,.015,len(index))))
        frames.append(pd.DataFrame({'symbol':symbol,'session':index.strftime('%Y-%m-%d'),
          'open':close,'high':close*1.01,'low':close*.99,'close':close,'volume':rng.integers(100000,200000,len(index)),
          'observed_at':[exchange().session_close(d) for d in index], 'available_at':pd.NaT}))
    return pd.concat(frames,ignore_index=True)


def synthetic_review(index):
    events=[]
    for n in range(90,len(index)-25,75):
        events.append({'onset':str(index[n].date()),'confirmation':str(index[n+3].date()),'end':str(index[n+10].date())})
    return {'rubric':'SYNTHETIC_TEST_ONLY','confirmation_sessions':3,'reviewed_intervals':[{'start':str(index[0].date()),'end':str(index[-1].date())}], 'episodes':events,'note':'Synthetic fixture, no market evidence'}


def test_features_use_prior_session_and_keep_gaps():
    r=research();d=synthetic_prices('2024-01-02','2024-05-31')
    f=r.build_features(d)
    changed=d.copy(); changed.loc[(changed.session=='2024-05-01') & (changed.symbol=='TSLA'),['open','high','low','close']]*=2
    g=r.build_features(changed)
    assert np.allclose(f.loc['2024-05-01'],g.loc['2024-05-01'],equal_nan=True)
    assert not np.allclose(f.loc['2024-05-02'],g.loc['2024-05-02'],equal_nan=True)
    missing=d.loc[~((d.symbol=='TSLA')&(d.session=='2024-04-15'))]
    m=r.build_features(missing)
    assert m.loc['2024-04-16'].isna().all()
    assert m.loc['2024-05-10'].isna().all()  # 21-session window still contains the missing day.


def test_labels_purged_through_confirmation_not_nominal_horizon():
    r=research(); index=sessions('2025-01-02','2025-04-30')
    review={'rubric':'test','confirmation_sessions':8,'reviewed_intervals':[{'start':'2025-01-02','end':'2025-04-30'}],
            'episodes':[{'onset':'2025-02-18','confirmation':'2025-02-21','end':'2025-03-03'}]}
    labels=make_labels(index,review,10)
    train=r.purged_before(labels,pd.Timestamp('2025-02-24'))
    assert (train.label_end<pd.Timestamp('2025-02-24')).all()
    assert pd.Timestamp('2025-02-10') not in train.index


def test_missing_review_blocks_worker_experiment(tmp_path):
    research();l=Ledger(tmp_path)
    did=Lake(l).put('equity_daily',synthetic_prices(),{'provider':'synthetic-test','availability':'retrospective','price_basis':'split_adjusted'})
    jid=l.enqueue('experiment',{'dataset_id':did,'review_id':None,'horizon':10})
    run_once(l)
    assert l.job(jid)['status']=='blocked'
    assert 'review' in l.job(jid)['error'].lower()


def test_frozen_synthetic_experiment_writes_reproducible_report(tmp_path):
    research();l=Ledger(tmp_path)
    d=synthetic_prices();index=sessions(d.session.min(),d.session.max())
    did=Lake(l).put('equity_daily',d,{'provider':'synthetic-test','availability':'retrospective','price_basis':'split_adjusted'})
    rid=save_review(l,synthetic_review(index))
    config={'dataset_id':did,'review_id':rid,'horizon':10,'alert_budget':4,'protocol':'daily-baseline-v1'}
    jid=l.enqueue('experiment',config);run_once(l)
    j=l.job(jid)
    assert j['status']=='succeeded', j['error']
    report=json.loads((tmp_path/j['result']['report_path']).read_text())
    assert report['evidence']=='retrospective_experimental'
    assert report['configuration']==config
    assert len(report['folds'])>=3
    assert report['metrics']['predictions']>100
    assert report['metrics']['false_alerts']>=0
    assert len(report['predictions'])==report['metrics']['predictions']
    for fold in report['folds']:
        assert fold['max_train_label_end']<fold['test_start']
    assert 'not_causal' in report['limitations']
    assert len(report['code_version']) == 64
    assert sum(b['count'] for b in report['calibration']) == report['metrics']['predictions']
    assert sum(b['positive_dates'] for b in report['calibration']) == report['metrics']['positive_dates']
    again=research().run_experiment(l,config)
    assert again['report_path']==j['result']['report_path']


def test_future_clock_rejects_verified_feature_row():
    r=research();d=synthetic_prices('2024-01-02','2024-05-31')
    d['available_at']=pd.to_datetime(d.observed_at,utc=True)+pd.Timedelta(hours=8)
    selected=(d.session=='2024-04-30')&(d.symbol=='TSLA')
    d.loc[selected,'available_at']=pd.Timestamp('2024-05-02T12:00:00Z')
    features=r.build_features(d,enforce_availability=True)
    assert features.loc['2024-05-01'].isna().all()
