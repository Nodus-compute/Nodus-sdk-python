import json
import unittest
import httpx
import nodus
from nodus.cli import build_parser


class BenchmarkTests(unittest.TestCase):
    def test_submit_forwards_one_budget_and_stable_idempotency(self):
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(202, json={'id': 'bm_one', 'budget_usd': 3,
                'status': 'running', 'spend_usd': 0, 'cells': [
                    {'workload_id': 'wl_one', 'budget_usd': 1.5, 'spend_usd': 0}]})
        with nodus.Client(api_key='test', base_url='https://test.invalid') as client:
            client._http.close()
            client._http = httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handler))
            report = client.benchmark(workload={'source': {'command': ['true']}},
                gpu_families=['A100', 'H100'], batch_sizes=[1], regions=['us'],
                repetitions=1, budget=3, idempotency_key='customer-attempt')
            self.assertEqual(report['cells'][0]['budget_usd'], 1.5)
            self.assertEqual(calls[0].headers['Idempotency-Key'], 'customer-attempt')
            self.assertEqual(json.loads(calls[0].content)['budget_usd'], 3)
            self.assertNotIn('outcome', json.loads(calls[0].content)['workload'])

    def test_no_budget_or_key_is_invented(self):
        with nodus.Client(api_key='test', base_url='https://test.invalid') as client:
            for budget in (-1, float('inf'), True):
                with self.assertRaises(ValueError):
                    client.benchmark(workload={}, gpu_families=['A100'], batch_sizes=[1],
                        regions=['us'], repetitions=1, budget=budget, idempotency_key='key')
            with self.assertRaises(ValueError):
                client.benchmark(workload={}, gpu_families=['A100'], batch_sizes=[1],
                    regions=['us'], repetitions=1, budget=1, idempotency_key='')

    def test_cli_requires_explicit_request_and_idempotency(self):
        args = build_parser().parse_args(['benchmark', 'run', 'benchmark.json', '--idempotency-key', 'same'])
        self.assertEqual(args.benchmark_cmd, 'run')
        self.assertEqual(args.idempotency_key, 'same')

    def test_cli_run_prints_server_report_and_sends_same_key(self):
        import contextlib
        import io
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from nodus.cli import main
        calls = []
        def handler(request):
            calls.append(request)
            return httpx.Response(202, json={'id': 'bm_cli', 'spend_usd': 0,
                'budget_usd': 3, 'cells': []})
        client = nodus.Client(api_key='test', base_url='https://test.invalid')
        client._http.close()
        client._http = httpx.Client(base_url='https://test.invalid', transport=httpx.MockTransport(handler))
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'request.json'
            path.write_text(json.dumps({'workload': {'source': {'command': ['true']}},
                'matrix': {'gpu_families': ['A100'], 'batch_sizes': [1],
                           'regions': ['us'], 'repetitions': 1}, 'budget_usd': 3}))
            output = io.StringIO()
            with patch('nodus.cli.Client', return_value=client), contextlib.redirect_stdout(output):
                self.assertEqual(main(['benchmark', 'run', str(path), '--idempotency-key', 'cli-retry']), 0)
            self.assertEqual(json.loads(output.getvalue())['id'], 'bm_cli')
            self.assertEqual(calls[0].headers['Idempotency-Key'], 'cli-retry')

    def test_async_report_does_not_derive_cost(self):
        import asyncio
        async def exercise():
            async def handler(request):
                return httpx.Response(200, json={'id': 'bm_async', 'spend_usd': 0.4,
                    'cells': [{'workload_id': 'wl_one', 'spend_usd': 0.3}]})
            async with nodus.AsyncClient(api_key='test', base_url='https://test.invalid') as client:
                await client._http.aclose()
                client._http = httpx.AsyncClient(base_url='https://test.invalid', transport=httpx.MockTransport(handler))
                report = await client.get_benchmark('bm_async')
                self.assertEqual(report['spend_usd'], 0.4)
        asyncio.run(exercise())
