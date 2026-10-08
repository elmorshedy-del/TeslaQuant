import concurrent.futures
import importlib.util
import sqlite3
import pandas as pd
import pytest


def api():
    assert importlib.util.find_spec('tesla_lab.ledger'), 'persistent ledger missing'
    from tesla_lab.ledger import Ledger
    from tesla_lab.data import Lake
    return Ledger, Lake


def bars():
    return pd.DataFrame([dict(symbol='TSLA', session='2025-01-02', open=100., high=102., low=99., close=101., volume=1000., observed_at='2025-01-02T21:00:00Z', available_at='2025-01-03T02:00:00Z')])


def test_duplicate_submissions_have_one_durable_job(tmp_path):
    Ledger, _ = api()
    l = Ledger(tmp_path)
    a = l.enqueue('inspect', {'x': 1})
    assert a == Ledger(tmp_path).enqueue('inspect', {'x': 1})
    assert len(l.jobs()) == 1


def test_only_one_concurrent_worker_claims(tmp_path):
    Ledger, _ = api()
    l = Ledger(tmp_path)
    l.enqueue('inspect', {})
    with concurrent.futures.ThreadPoolExecutor(4) as p:
        claims = list(p.map(lambda n: Ledger(tmp_path).claim(str(n)), range(4)))
    assert sum(c is not None for c in claims) == 1


def test_expired_worker_cannot_finish_reclaimed_job(tmp_path):
    Ledger, _ = api()
    l = Ledger(tmp_path)
    jid = l.enqueue('inspect', {})
    old = l.claim('old')
    with l.connect() as c:
        c.execute('UPDATE jobs SET lease_until=0 WHERE id=?', (jid,))
    new = l.claim('new')
    assert new['attempts'] == 2
    with pytest.raises(ValueError, match='lease'):
        l.finish(jid, old['token'], 'succeeded', {})
    l.finish(jid, new['token'], 'succeeded', {'done': True})
    assert Ledger(tmp_path).job(jid)['result']['done']


def test_dataset_versions_are_immutable_and_queryable(tmp_path):
    Ledger, Lake = api()
    lake = Lake(Ledger(tmp_path))
    d = bars()
    a = lake.put('equity_daily', d, {'provider':'test','availability':'verified','price_basis':'split_adjusted'})
    b = lake.put('equity_daily', d.assign(close=100.), {'provider':'test','availability':'verified','price_basis':'split_adjusted'})
    assert a != b
    assert lake.read(a).close.iloc[0] == 101.
    assert lake.query(a, 'SELECT count(*) AS n FROM data').n.iloc[0] == 1
    assert lake.put('equity_daily', d, {'provider':'test','availability':'verified','price_basis':'split_adjusted'}) == a


@pytest.mark.parametrize('mutation', ['clock','holiday','duplicate','ohlc'])
def test_bad_daily_rows_rejected(tmp_path, mutation):
    Ledger, Lake = api()
    d = bars()
    if mutation == 'clock': d['available_at'] = '2025-01-02T20:00:00Z'
    if mutation == 'holiday': d['session'] = '2025-01-01'
    if mutation == 'duplicate': d = pd.concat([d,d])
    if mutation == 'ohlc': d['high'] = 50.
    with pytest.raises(ValueError):
        Lake(Ledger(tmp_path)).put('equity_daily', d, {'provider':'test','availability':'verified','price_basis':'split_adjusted'})
