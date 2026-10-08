from io import BytesIO
import json
from pathlib import Path
import tarfile
import threading
import zipfile
import pytest
from tesla_lab.ledger import Ledger
from tesla_lab.data import Lake
from tesla_lab.episodes import save_review, get_review
from tesla_lab import backup, data
from tesla_lab.providers import import_audit
from test_ingestion import audit_zip


def test_backup_ignores_concurrent_uncommitted_staging_object(tmp_path,monkeypatch):
    l=Ledger(tmp_path/'data')
    paused,release=threading.Event(),threading.Event()
    original_sync=data.os.fsync
    original_digest=backup.digest
    errors=[]
    def pause_writer(fd):
        original_sync(fd)
        if threading.current_thread().name=='pending-object-writer':
            paused.set()
            if not release.wait(5):raise TimeoutError('Writer was not released')
    monkeypatch.setattr(data.os,'fsync',pause_writer)
    def write():
        try:Lake(l).raw(b'uncommitted object',{'type':'test'})
        except Exception as e:errors.append(e)
    writer=threading.Thread(target=write,name='pending-object-writer');writer.start()
    assert paused.wait(2)
    def complete_after_staging_hash(path):
        result=original_digest(path)
        if Path(path).name.startswith('tmp'):
            release.set();writer.join(timeout=2)
        return result
    monkeypatch.setattr(backup,'digest',complete_after_staging_hash)
    try:
        archive=backup.create_backup(l,tmp_path/'snapshot.tar.gz')
        with tarfile.open(archive) as tar:
            assert not any(m.name.startswith('raw/') for m in tar.getmembers())
    finally:
        release.set();writer.join(timeout=2)
    assert not errors


def test_spaces_failure_retries_same_snapshot_and_retains_only_seven(tmp_path,monkeypatch):
    l=Ledger(tmp_path/'data');folder=l.root/'backups';folder.mkdir()
    for n in range(8):(folder/f'2020010{n+1}T000000Z.tar.gz').write_bytes(b'old')
    monkeypatch.setenv('SPACES_BUCKET','test')
    clock=[1000.];monkeypatch.setattr(backup.time,'time',lambda:clock[0])
    attempts=[]
    def upload(path):
        attempts.append(path)
        if len(attempts)==1:raise ConnectionError('simulated outage')
        return {'verified':True}
    monkeypatch.setattr(backup,'upload_spaces',upload)
    class Stop:
        waits=[]
        def is_set(self):return len(self.waits)>=2
        def wait(self,delay):self.waits.append(delay);clock[0]+=delay;return self.is_set()
    stop=Stop();backup.backup_loop(l,stop)
    assert len(attempts)==2 and attempts[0]==attempts[1]
    assert stop.waits[0]>=300
    assert len(list(folder.glob('*.tar.gz')))==7


@pytest.mark.parametrize('value',['MISSING',-1,1.5,True,61,'5'])
def test_invalid_confirmation_contract_rejected_before_persistence(tmp_path,value):
    body={'rubric':'test','reviewed_intervals':[{'start':'2025-01-02','end':'2025-02-28'}],'episodes':[]}
    if value!='MISSING':body['confirmation_sessions']=value
    l=Ledger(tmp_path)
    with pytest.raises(ValueError,match='confirmation'):
        save_review(l,body)
    with l.connect() as c:assert c.execute('SELECT count(*) FROM reviews').fetchone()[0]==0


def test_non_iso_review_dates_are_normalized_before_event_identity(tmp_path):
    l=Ledger(tmp_path)
    body={'rubric':'test','confirmation_sessions':0,'reviewed_intervals':[{'start':'2025-1-2','end':'2025-2-28'}],
          'episodes':[{'onset':'2025-1-16','confirmation':'2025-1-21','end':'2025-1-24'}]}
    rid=save_review(l,body)
    saved=get_review(l,rid)
    assert saved['episodes'][0]['onset']=='2025-01-16'
    assert saved['reviewed_intervals'][0]['start']=='2025-01-02'


def test_non_object_review_gets_readable_error(tmp_path):
    with pytest.raises(ValueError,match='object'):save_review(Ledger(tmp_path),[])


def test_audit_missing_declared_checksum_member_is_rejected(tmp_path):
    path=audit_zip(tmp_path/'original.zip');broken=tmp_path/'broken.zip'
    with zipfile.ZipFile(path) as source,zipfile.ZipFile(broken,'w') as target:
        for name in source.namelist():
            body=source.read(name)
            if name.endswith('provenance.json'):
                p=json.loads(body);p['sha256']={'missing.csv':'a'*64};body=json.dumps(p).encode()
            target.writestr(name,body)
    with pytest.raises(ValueError,match='checksum'):
        import_audit(Ledger(tmp_path/'store'),broken)
