from __future__ import annotations

import io
import json
import subprocess
import sys
import time
import unittest
from unittest import mock

from dashboard.app import Dashboard
from dashboard.omcp import OMCPError
from dashboard import system


class SystemTests(unittest.TestCase):
    def request(self, app, path='/api/system', method='GET'):
        responses = []
        body = b''.join(app({'PATH_INFO': path, 'REQUEST_METHOD': method, 'wsgi.input': io.BytesIO()}, lambda status, headers: responses.append((status, dict(headers)))))
        return responses[0], body.decode()

    def test_exact_allowlist(self):
        for argv in [('/bin/sh', '-c', 'id'), ('/usr/bin/systemctl', 'restart', 'docker'),
                     system.COMMANDS['docker'] + ('--no-trunc',), ('/usr/bin/curl', 'https://other.test'),
                     ('/usr/bin/sudo', '-n', '/usr/bin/id'),
                     system.COMMANDS['docker'][2:],  # no unprivileged fallback
                     tuple(arg for arg in system.COMMANDS['docker'] if arg != '-n'),
                     tuple('stop' if arg == 'ls' else arg for arg in system.COMMANDS['docker']),
                     tuple('{{json .}}' if arg == '{{.State}}\t{{.Image}}' else arg for arg in system.COMMANDS['docker']),
                     tuple('HEAD' if arg == 'GET' else arg for arg in system.COMMANDS['public']),
                     system.COMMANDS['public'][:-1] + ('https://mcp.delnavazan.com/',),
                     system.COMMANDS['public'] + ('--location',)]:
            with self.subTest(argv=argv), mock.patch.object(subprocess, 'Popen') as popen:
                with self.assertRaises(ValueError):
                    system.run_command(argv)
                popen.assert_not_called()
        self.assertEqual(len(system.ALLOWED_COMMANDS), 4)
        self.assertIn('-q', system.COMMANDS['public'])
        self.assertNotIn('--location', system.COMMANDS['public'])

    def test_fixed_probe_argv(self):
        expected = {
            'docker': ('/usr/bin/sudo', '-n', '/usr/bin/docker', 'container', 'ls', '--all', '--format', '{{.State}}\t{{.Image}}'),
            'public': ('/usr/bin/curl', '-q', '--silent', '--request', 'GET', '--output', '/dev/null', '--write-out', '%{http_code}', '--max-time', '2', '--connect-timeout', '1', '--noproxy', '*', '--proto', '=https', 'https://mcp.delnavazan.com/healthz'),
        }
        for name, argv in expected.items():
            with self.subTest(probe=name):
                self.assertEqual(system.COMMANDS[name], argv)
                self.assertEqual(self.helper("print('200', end='')", argv=argv), '200')
        self.assertEqual([argv for argv in system.ALLOWED_COMMANDS if argv[0] == '/usr/bin/sudo'], [expected['docker']])

    def helper(self, code, timeout=0.5, argv=None):
        original = subprocess.Popen
        spawned = []
        calls = []
        def launch(argv, **kwargs):
            calls.append((argv, kwargs))
            child = original((sys.executable, '-c', code), **kwargs)
            spawned.append(child)
            return child
        with mock.patch.object(system.subprocess, 'Popen', side_effect=launch), mock.patch.object(system, 'TIMEOUT', timeout):
            result = system.run_command(argv or system.COMMANDS['docker'])
        self.assertIsNotNone(spawned[0].poll())
        self.assertEqual(calls[0][0], argv or system.COMMANDS['docker'])
        self.assertFalse(calls[0][1]['shell'])
        self.assertEqual(calls[0][1]['env'], {'PATH': '/usr/bin:/bin', 'LANG': 'C', 'LC_ALL': 'C', 'HOME': '/nonexistent'})
        self.assertEqual(calls[0][1]['stderr'], subprocess.DEVNULL)
        return result

    def test_success_and_nonzero_discard_errors(self):
        self.assertEqual(self.helper("print('running\\tnextcloud:latest')"), 'running\tnextcloud:latest\n')
        self.assertIsNone(self.helper("import sys; print('TOKEN=secret'); sys.exit(1)"))

    def test_timeout_reaps_process(self):
        start = time.monotonic()
        self.assertIsNone(self.helper('import time; time.sleep(10)', timeout=0.08))
        self.assertLess(time.monotonic() - start, 2)

    def test_output_limit_during_read(self):
        self.assertIsNone(self.helper("import sys; sys.stdout.write('X' * 2000000); sys.stdout.flush()"))
        self.assertEqual(len(self.helper("import sys; sys.stdout.write('X' * 65536)")), system.MAX_OUTPUT)

    def test_missing_and_denied(self):
        for error in [FileNotFoundError('secret'), PermissionError('secret')]:
            with mock.patch.object(system.subprocess, 'Popen', side_effect=error):
                self.assertIsNone(system.run_command(system.COMMANDS['docker']))

    def test_projection_redacts_secrets(self):
        secret = 'CANARY_SECRET_123'
        services = system.service_states('Id=docker.service\nLoadState=loaded\nActiveState=active\nEnvironment=TOKEN=' + secret)
        self.assertEqual(services['docker'], 'active')
        self.assertEqual(system.service_states('Id=docker.service\nLoadState=not-found\nActiveState=inactive')['docker'], 'unavailable')
        tailscale = system.tailscale_summary(json.dumps({'BackendState': 'Running', 'TailscaleIPs': ['100.64.0.1', 'fd7a:115c:a1e0::1'], 'Peer': {secret: secret}, 'AuthURL': secret}))
        docker = system.docker_summary('running\tnextcloud:latest\nexited\tprivate/' + secret + ':latest\n')
        self.assertEqual(docker['nextcloud_running'], 1)
        self.assertNotIn(secret, json.dumps([services, tailscale, docker, system.public_summary(secret)]))
        self.assertEqual(system.tailscale_summary('{broken')['state'], 'unavailable')
        self.assertEqual(system.tailscale_summary(json.dumps({'BackendState': secret, 'TailscaleIPs': [secret]}))['addresses'], [])
        self.assertEqual(system.docker_summary('TOKEN=' + secret)['state'], 'unavailable')
        self.assertEqual(system.public_summary('403')['state'], 'reachable')
        self.assertEqual(system.public_summary('503')['state'], 'unhealthy')
        self.assertEqual(system.public_summary('200')['state'], 'healthy')

    def test_missing_host_metrics_and_file_bounds(self):
        with mock.patch.object(system.os, 'getloadavg', side_effect=OSError), mock.patch.object(system, 'read_metric', side_effect=OSError), mock.patch.object(system.shutil, 'disk_usage', side_effect=OSError):
            self.assertTrue(all(value is None for value in system.host_metrics().values()))
        with mock.patch('builtins.open', mock.mock_open(read_data=b'X' * 8193)) as source:
            with self.assertRaises(ValueError):
                system.read_metric('/proc/uptime')
            source().read.assert_called_once_with(8193)

    def test_host_metric_projection(self):
        files = {
            "/proc/meminfo": "MemTotal: 2048 kB\nMemAvailable: 1024 kB\nSecret: NEVER_DISPLAY\n",
            "/proc/uptime": "3600.12 99",
            "/sys/class/thermal/thermal_zone0/type": "x86_pkg_temp\n",
            "/sys/class/thermal/thermal_zone0/temp": "42500\n",
        }
        with mock.patch.object(system, 'read_metric', side_effect=lambda path: files[path]), mock.patch.object(system.os, 'getloadavg', return_value=(1, 2, 3)), mock.patch.object(system.os, 'cpu_count', return_value=4), mock.patch.object(system.shutil, 'disk_usage', return_value=mock.Mock(total=4096, used=1024)):
            metrics = system.host_metrics()
        self.assertEqual(metrics['memory'], {'total_bytes': 2097152, 'used_bytes': 1048576})
        self.assertEqual(metrics['temperature_c'], 42.5)
        self.assertEqual(metrics['uptime_seconds'], 3600.12)
        self.assertEqual(metrics['cpu_count'], 4)
        self.assertNotIn('NEVER_DISPLAY', json.dumps(metrics))

    def test_cache_and_unavailable_sections(self):
        collector = system.SystemCollector()
        with mock.patch.object(system, 'run_command', return_value=None) as run, mock.patch.object(system, 'host_metrics', return_value={}):
            one = collector.snapshot()
            one['services']['docker'] = 'tampered'
            two = collector.snapshot()
            self.assertEqual(run.call_count, 4)
            self.assertEqual(two['services']['docker'], 'unavailable')
            self.assertEqual(two['docker']['state'], 'unavailable')
            collector._collected -= system.CACHE_SECONDS
            collector.snapshot()
            self.assertEqual(run.call_count, 8)

    def test_endpoint_security_and_methods(self):
        collector = mock.Mock()
        collector.snapshot.return_value = {'services': {'docker': 'unavailable'}}
        client = mock.Mock()
        app = Dashboard(client, system_collector=collector)
        (status, headers), body = self.request(app)
        self.assertEqual(status, '200 OK')
        self.assertEqual(json.loads(body), collector.snapshot.return_value)
        self.assertEqual(headers['Cache-Control'], 'no-store')
        self.assertEqual(headers['Content-Security-Policy'], "default-src 'none'")
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')
        self.assertEqual(headers['X-Frame-Options'], 'DENY')
        self.assertEqual(headers['Referrer-Policy'], 'no-referrer')
        for method in ['POST', 'PUT', 'DELETE', 'HEAD']:
            (status, headers), _ = self.request(app, method=method)
            self.assertEqual(status, '405 Method Not Allowed')
            self.assertEqual(headers['Allow'], 'GET')
        collector.snapshot.assert_called_once()
        client.call.assert_not_called()

    def test_rendering_survives_omcp_offline(self):
        client = mock.Mock()
        client.call.side_effect = OMCPError('TOKEN=secret')
        app = Dashboard(client)
        (status, _), page = self.request(app, '/')
        self.assertEqual(status, '503 Service Unavailable')
        self.assertIn('system-overview', page)
        self.assertIn('PixelBox', page)
        self.assertIn('Orchestra is offline', page)
        self.assertNotIn('TOKEN=secret', page)
        self.assertIn('system-load', page)
        self.assertNotIn('restart', page.lower())
        from dashboard.app import esc
        self.assertEqual(esc('<script>"'), '&lt;script&gt;&quot;')


if __name__ == '__main__':
    unittest.main()
