import json
from html.parser import HTMLParser
import struct

import pytest
from flask import abort
from solarbank_dashboard import auth
from solarbank_dashboard.app import create_app
from solarbank_dashboard.auth import AuthSettings
from solarbank_dashboard.config import Settings


class Links(HTMLParser):
    def __init__(self, document):
        super().__init__()
        self.links = []
        self.feed(document)

    def handle_starttag(self, tag, attrs):
        if tag == 'link':
            self.links.append(dict(attrs))


def forbidden_api(*args, **kwargs):
    pytest.fail('Shortcut assets must not call the API')


def app(auth_enabled):
    dashboard = create_app(
        Settings('http://api:8001', 't' * 48, 'Europe/Berlin'),
        auth_config=AuthSettings(enabled=auth_enabled, secret_key='s' * 48, secure_cookie=False),
        api_client=forbidden_api,
    )
    dashboard.server.testing = True
    return dashboard.server


def test_dash_and_public_documents_share_shortcut_metadata():
    server = app(False)

    @server.get('/test-error')
    def error():
        abort(400)

    client = server.test_client()
    for path, status in [('/', 200), ('/impressum', 200), ('/datenschutz', 200),
                         ('/test-error', 400)]:
        response = client.get(path, buffered=True)
        assert response.status_code == status
        links = Links(response.get_data(as_text=True)).links
        touch = [link for link in links if link.get('rel') == 'apple-touch-icon']
        assert {link['sizes'] for link in touch} == {'180x180', '1024x1024'}
        assert [link['href'] for link in links if link.get('rel') == 'manifest'] == ['/static/site.webmanifest']
        icons = [link for link in links if link.get('rel') == 'icon']
        assert icons == [{'rel': 'icon', 'href': '/static/img/logo-fluxo_io.svg', 'type': 'image/svg+xml'}]
        icon = client.get(icons[0]['href'])
        assert icon.status_code == 200 and icon.mimetype == 'image/svg+xml'
        assert b'fluxo.io' in icon.data
        assert b'/_favicon.ico' not in response.data
        for link in touch:
            response = client.get(link['href'], buffered=True)
            assert response.status_code == 200
            assert response.mimetype == 'image/png'
    login = app(True).test_client().get('/login')
    assert login.status_code == 200
    assert b'rel="apple-touch-icon"' in login.data
    assert b'rel="manifest"' in login.data
    assert "manifest-src 'self'" in login.headers['Content-Security-Policy']


@pytest.mark.parametrize('stale', [False, True])
def test_public_manifest_and_pngs_need_no_session_or_api(monkeypatch, stale):
    monkeypatch.setattr(auth, 'request_api', forbidden_api)
    client = app(True).test_client()
    if stale:
        with client.session_transaction() as session:
            session['_user_id'] = 'expired-user'
            session['authenticated_at'] = 0
    response = client.get('/static/site.webmanifest', buffered=True)
    assert response.status_code == 200
    assert response.mimetype == 'application/manifest+json'
    assert 'Set-Cookie' not in response.headers
    manifest = json.loads(response.data)
    assert set(manifest) == {'name', 'short_name', 'icons'}
    assert {icon['sizes'] for icon in manifest['icons']} == {'192x192', '512x512', '1024x1024'}
    assets = [(180, '/static/icons/fluxo/1.0.0/apple-touch-icon.png')]
    for icon in manifest['icons']:
        assert icon['type'] == 'image/png'
        assert icon['purpose'] == 'any'
        assert not icon['src'].startswith('/')
        assets.append((int(icon['sizes'].split('x')[0]), '/static/' + icon['src']))
    for size, url in assets:
        response = client.get(url, buffered=True)
        assert response.status_code == 200
        assert response.mimetype == 'image/png'
        assert 'Set-Cookie' not in response.headers
        assert response.data[:8] == b'\x89PNG\r\n\x1a\n'
        assert struct.unpack('>II', response.data[16:24]) == (size, size)
        assert client.head(url, buffered=True).status_code == 200
    assert client.get('/static/private.css').status_code == 302
