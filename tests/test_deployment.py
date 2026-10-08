import importlib.util
import json
from pathlib import Path
import subprocess
import tarfile
from io import BytesIO
import pytest
from tesla_lab.ledger import Ledger
from tesla_lab.backup import create_backup, restore_backup
from tesla_lab.worker import run_once

ROOT=Path(__file__).resolve().parents[1]


def test_setup_refuses_public_mode_without_site_and_password(tmp_path):
    script=ROOT/'deploy/setup.sh'
    assert script.exists(), 'deployment setup missing'
    p=subprocess.run(['bash',str(script),'public'],cwd=tmp_path,env={'PATH':'/usr/bin:/bin'},capture_output=True,text=True)
    assert p.returncode!=0
    assert 'LAB_DOMAIN' in p.stderr


def test_backup_rejects_tampered_bytes(tmp_path):
    l=Ledger(tmp_path/'data');l.enqueue('inspect',{})
    archive=create_backup(l,tmp_path/'good.tar.gz')
    bad=tmp_path/'bad.tar.gz'
    with tarfile.open(archive,'r:gz') as source, tarfile.open(bad,'w:gz') as target:
        for member in source.getmembers():
            body=source.extractfile(member).read()
            if member.name=='ledger.sqlite3':body=body[:-1]+bytes([body[-1]^1])
            member.size=len(body);target.addfile(member,BytesIO(body))
    with pytest.raises(ValueError,match='checksum'):
        restore_backup(bad,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()


def test_original_forecasts_do_not_overwrite_each_other(tmp_path):
    l=Ledger(tmp_path)
    first=l.append_forecast({'session':'2025-01-02','model':'test','probability':.1,'input_version':'a'})
    second=l.append_forecast({'session':'2025-01-02','model':'test','probability':.2,'input_version':'b'})
    assert first!=second
    with l.connect() as c:
        assert c.execute('SELECT count(*) FROM forecasts').fetchone()[0]==2


def test_repeated_worker_check_creates_new_operational_job(tmp_path,monkeypatch):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv('TESLA_DATA_DIR',str(tmp_path))
    app=AppTest.from_file(str(ROOT/'app.py'),default_timeout=15).run()
    app.button(key='inspect_job').click().run()
    l=Ledger(tmp_path);run_once(l)
    app.button(key='inspect_job').click().run()
    assert len(l.jobs())==2
