"""Exercise the three built images using only temporary data and an internal network.

Run after building api/web/collector using the CI image tags,
or set ANKER_TEST_API_IMAGE, ANKER_TEST_WEB_IMAGE, ANKER_TEST_COLLECTOR_IMAGE.
No real Anker credentials or cloud requests are used.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid


def docker(*args, source=None):
    result = subprocess.run(
        ["docker", *args], input=source, text=True, capture_output=True, timeout=90
    )
    if result.returncode:
        raise RuntimeError(result.stderr or result.stdout)
    return result.stdout.strip()


def python(container, source):
    return docker("exec", "-i", container, "python", "-", source=source)


def healthy(container, port):
    for _ in range(100):
        try:
            python(
                container,
                "import urllib.request; "
                f"urllib.request.urlopen('http://127.0.0.1:{port}/healthz', timeout=2)",
            )
            return
        except RuntimeError:
            time.sleep(0.2)
    raise RuntimeError("Test service did not become ready")


API_CHECK = """
from datetime import datetime, UTC, timedelta
from solarbank_dashboard.config import Settings
from solarbank_dashboard.repository import request_api, ApiError
import os, importlib.util
from pathlib import Path
assert importlib.util.find_spec("solarbank_api") is None
for module in ("energy", "freshness"):
    assert importlib.util.find_spec("solarbank_dashboard."+module) is None
assert not Path("/data/app.sqlite3").exists()
cfg=Settings.from_environment()
rows=request_api(cfg, '/v1/measurements/latest')['rows']
assert len(rows)==1 and rows[0]['device_sn']=='test-device'
assert rows[0]['site_id']=='test-site'
now=datetime.now(UTC)
data=request_api(cfg, '/v1/dashboard',params={
    'start':(now-timedelta(hours=1)).isoformat(),
    'end':(now+timedelta(hours=1)).isoformat()})
assert data['kpis']['pv_total_w']==123
assert data['energy_status']=='available'
assert data['energy'][0]['pv_kwh'] > 0
assert data['unknown_source_age'] is True
assert data['collector']['status']=='fresh'
assert data['rows'][0]['pv_total_w']==123
assert len(data['aggregated'])==1
assert 'COLLECTOR_API_TOKEN' not in os.environ
try:
    request_api(cfg, '/v1/measurements', payload={'rows':[]})
except ApiError as error:
    assert error.status==403
else:
    raise AssertionError('Web token received collector write authority')
print('Web reads API-computed energy/KPI/freshness; '
      'no calculation/store modules or write authority')
"""
BROWSER_CHECK = """
import http.cookiejar, json, re, urllib.request, urllib.parse
opener=urllib.request.build_opener(
    urllib.request.ProxyHandler({}),
    urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
def get(path):
    with opener.open('http://127.0.0.1:8000'+path,timeout=5) as response:
        return response.read().decode()
page=get('/login')
token=re.search(r'name="csrf_token"[^>]*value="([^"]+)"',page)[1]
for url in re.findall(r'(?:src|href)="(/static/dist/[^\"]+)"',page):
    assert get(url)
fields=dict(username='admin',password='synthetic-integration-password',csrf_token=token)
with opener.open('http://127.0.0.1:8000/login',
                 urllib.parse.urlencode(fields).encode(), timeout=5) as response:
    assert response.geturl().endswith('/')
assert 'dash-renderer' in get('/')
dependency=next(item for item in json.loads(get('/_dash-dependencies'))
                if 'collector-status.children' in item['output'])
outputs=[dict(zip(('id','property'),item.rsplit('.',1)))
         for item in dependency['output'].strip('.').split('...')]
values={'period-filter':'24 Stunden','custom-range':None,'site-filter':[],
        'device-filter':[], 'valid-filter':['valid'],'reload-button':1,
        'auto-refresh-interval':0}
payload={'output':dependency['output'],'outputs':outputs,
         'inputs':[{**item,'value':values[item['id']]}
                   for item in dependency['inputs']],
         'state':[],'changedPropIds':['reload-button.n_clicks']}
request=urllib.request.Request(
    'http://127.0.0.1:8000/_dash-update-component',
    data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'})
with opener.open(request,timeout=10) as response:
    result=json.load(response)['response']
assert result['kpi-pv']['children']=='123 W'
assert result['notification-data']['data']['level']=='success'
assert len(result['energy-chart']['figure']['data']) > 0
print('Built web image authenticates via API and serves hashed assets')
"""
COLLECT = """
import asyncio, os
from dataclasses import fields
from datetime import datetime, UTC
from solarbank_collector.config import Settings
from solarbank_collector.models import Measurement
from solarbank_collector.repository import MeasurementRepository
async def run():
    assert 'API_TOKEN' not in os.environ
    cfg=Settings.from_environment()
    now=datetime.now(UTC)
    values={f.name:None for f in fields(Measurement)}
    values.update(bucket_start=now,collected_at=now,site_id='test-site',device_sn='test-device',data_valid=True,pv_reported_total_w=123,raw_data={'synthetic':True})
    async with MeasurementRepository(cfg) as repo:
        await repo.mark_attempt(now)
        await repo.save([Measurement(**values)])
asyncio.run(run())
"""


def main():
    identity = uuid.uuid4().hex[:12]
    network = "anker-check-" + identity
    volume = "anker-check-data-" + identity
    names = {
        role: "anker-check-" + role + "-" + identity
        for role in ("api", "web", "collector")
    }
    images = {
        role: os.getenv(
            "ANKER_TEST_" + role.upper() + "_IMAGE",
            "anker-solix-dashboard-"
            + ("worker" if role == "collector" else role)
            + ":check",
        )
        for role in names
    }
    token = uuid.uuid4().hex * 2
    collector_token = uuid.uuid4().hex * 2
    secret = uuid.uuid4().hex * 2
    created = []
    runtime = [
        "--read-only",
        "--tmpfs",
        "/tmp:rw,nosuid,size=64m",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges:true",
        "--network",
        network,
        "--user",
        "1000:1000",
    ]
    try:
        docker("network", "create", "--internal", network)
        docker("volume", "create", volume)
        common = ["-e", "API_TOKEN=" + token, "-e", "SECRET_KEY=" + secret]
        for role in ("api", "web"):
            args = ["run", "-d", "--name", names[role], *runtime, *common]
            if role == "api":
                args += [
                    "-e",
                    "COLLECTOR_API_TOKEN=" + collector_token,
                    "--network-alias",
                    "api",
                    "--mount",
                    f"type=volume,src={volume},dst=/data",
                    "--user",
                    "0:0",
                    "--cap-add",
                    "CHOWN",
                    "--cap-add",
                    "DAC_OVERRIDE",
                    "--cap-add",
                    "SETUID",
                    "--cap-add",
                    "SETGID",
                    "-e",
                    "INIT_ADMIN_USERNAME=admin",
                    "-e",
                    "INIT_ADMIN_PASSWORD=synthetic-integration-password",
                ]
            else:
                args += [
                    "-e",
                    "API_URL=http://api:8001",
                    "-e",
                    "CANONICAL_ORIGIN=http://localhost:8000",
                ]
            created.append(names[role])
            docker(*args, images[role])
            healthy(names[role], 8001 if role == "api" else 8000)
        # The actual collector repository publishes through HTTP, without cloud calls.
        docker(
            "run",
            "--rm",
            "-i",
            *runtime,
            "-e",
            "COLLECTOR_API_TOKEN=" + collector_token,
            "-e",
            "API_URL=http://api:8001",
            "-e",
            "ANKER_USERNAME=synthetic@example.invalid",
            "-e",
            "ANKER_PASSWORD=synthetic-unused-password",
            "--entrypoint",
            "python",
            images["collector"],
            "-",
            source=COLLECT,
        )
        print(python(names["web"], API_CHECK))
        print(python(names["web"], BROWSER_CHECK))
        web = json.loads(docker("inspect", names["web"]))[0]
        assert not web["Mounts"] and not web["HostConfig"]["PortBindings"]
        docker("stop", names["api"])
        outage = python(
            names["web"],
            "import urllib.request, urllib.error\n"
            "try: urllib.request.urlopen('http://127.0.0.1:8000/healthz',timeout=5)\n"
            "except urllib.error.HTTPError as error:\n"
            "    assert error.code==503; print(error.read().decode())\n"
            "else: raise AssertionError('API loss hidden')\n"
            "from solarbank_dashboard.repository import fetch_dashboard,ApiError\n"
            "from solarbank_dashboard.config import Settings\n"
            "from datetime import datetime,UTC,timedelta\n"
            "now=datetime.now(UTC)\n"
            "try: fetch_dashboard(Settings.from_environment(),"
            "start=now-timedelta(hours=1),end=now)\n"
            "except ApiError: pass\n"
            "else: raise AssertionError('Dashboard computed a local fallback')",
        )
        assert json.loads(outage) == {"status": "unavailable"}
        docker("start", names["api"])
        healthy(names["api"], 8001)
        print(python(names["web"], API_CHECK))
        print(
            "Three-image integration passed: data survives API restart; "
            "web has no data mount or host port; outage returns minimal 503."
        )
    except Exception:
        for name in created:
            print(
                subprocess.run(
                    ["docker", "logs", "--tail", "20", name],
                    capture_output=True,
                    text=True,
                ).stderr,
                flush=True,
            )
        raise
    finally:
        for name in reversed(created):
            subprocess.run(
                ["docker", "rm", "-f", name], capture_output=True, check=False
            )
        subprocess.run(
            ["docker", "network", "rm", network], capture_output=True, check=False
        )
        subprocess.run(
            ["docker", "volume", "rm", volume], capture_output=True, check=False
        )


if __name__ == "__main__":
    main()
