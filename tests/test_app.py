from pathlib import Path
from streamlit.testing.v1 import AppTest
from tesla_lab.ledger import Ledger
from tesla_lab.worker import run_once


APP=Path(__file__).resolve().parents[1]/'app.py'


def test_four_views_render_and_job_survives_reopening(tmp_path,monkeypatch):
    assert APP.exists(), 'research interface missing'
    monkeypatch.setenv('TESLA_DATA_DIR',str(tmp_path))
    app=AppTest.from_file(str(APP),default_timeout=15).run()
    assert not app.exception
    for view in ['Experiments','Evidence','Current Forecast','Episodes / Data']:
        app.sidebar.radio[0].set_value(view).run()
        assert not app.exception
    app.button(key='inspect_job').click().run()
    l=Ledger(tmp_path);assert len(l.jobs())==1
    run_once(l)
    reopened=AppTest.from_file(str(APP),default_timeout=15).run()
    assert not reopened.exception
    assert Ledger(tmp_path).jobs()[0]['status']=='succeeded'
