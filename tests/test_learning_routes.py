"""The explanatory guide is separate, offline-capable and database-independent."""
from fastapi.testclient import TestClient
from gridmind.config import Settings
from gridmind.main import create_app


def test_guide_assets_and_notes_are_accessible_with_unavailable_operational_backend(tmp_path):
    config=Settings(_env_file=None,database_url=f"sqlite:///{(tmp_path/'guide.db').as_posix()}",
                    event_backend='local',event_directory=str(tmp_path/'events'),api_key='local-test-key')
    with TestClient(create_app(config)) as client:
        client.app.state.ready=False
        response=client.get('/learn/')
        assert response.status_code==200
        assert '<html' in response.text and 'GRIDMIND' in response.text.upper()
        assert "script-src 'self'" in response.headers['content-security-policy']
        assert client.get('/learn/guide.css').status_code==200
        assert client.get('/learn/guide.js').status_code==200
        notes=client.get('/learning-notes')
        assert notes.status_code==200
        assert 'attachment' in notes.headers['content-disposition']
        assert 'normalization' in notes.text.lower() and 'atomic' in notes.text.lower()
        design=client.get('/docs/DBMS.md')
        assert design.status_code==200 and '14 tables' in design.text
        assert client.get('/docs').status_code==200
        assert client.get('/api/assets').status_code==401
        assert client.get('/api/assets',headers={'X-API-Key':'local-test-key'}).status_code==503
        assert client.get('/learn/../../.env').status_code==404
