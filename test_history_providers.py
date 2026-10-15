import json
import io
from pathlib import Path
import subprocess
import tempfile
import sys
import time
import threading
import unittest
from unittest.mock import Mock, MagicMock, patch

from history_providers import (CircuitBreaker, HistoryNotCurrent,
                               ProviderUnavailable, ProviderBusy, SourceRouter,
                               normalize_baostock, normalize_tushare,
                               _bao_query, _baostock_worker, _baostock_stream, _acquire_worker_lease)


def raw(day='20261014', close=10):
    return dict(trade_date=day, open=close, high=close+1, low=close-1,
                close=close, pre_close=close-.5, vol=123, amount=45)


def combined(day='2026-10-14'):
    return normalize_tushare([raw(day)], [dict(trade_date=day, adj_factor=2)])


class ProviderTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('history_providers.MarketRequestBudget', return_value=MagicMock())
        self.budget_class = patcher.start()
        self.budget_class.return_value.status.return_value = {}
        self.budget_class.return_value.initialize.return_value = {}
        self.addCleanup(patcher.stop)

    def router(self, root=None):
        if root is None:
            tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);root=tmp.name
        with patch.dict('os.environ', {}, clear=True), patch('history_providers.importlib.util.find_spec', return_value=None):
            return SourceRouter(base_dir=root)

    def test_tushare_factor_raw_prices_and_units(self):
        bars = normalize_tushare([raw('20261013'),raw()], [dict(trade_date='20261013',adj_factor=1),dict(trade_date='20261014',adj_factor=2)])
        self.assertEqual(bars[0]['close'],5)
        self.assertEqual(bars[0]['raw_close'],10)
        self.assertEqual(bars[0]['adjustment_factor'],.5)
        self.assertEqual(bars[1]['volume'],123)
        self.assertEqual(bars[1]['amount'],45000)
        self.assertEqual(bars[1]['raw_pre_close'],9.5)

    def test_missing_factor_is_not_silently_unadjusted(self):
        with self.assertRaises(ProviderUnavailable):
            normalize_tushare([raw()],[])
        self.assertEqual(normalize_tushare([raw()],[], 'none')[0]['close'],10)

    def test_baostock_units_and_original_preclose(self):
        r=dict(date='2026-10-14',open=10,close=10,high=11,low=9,volume=12300,amount=45678,preclose=9.8,tradestatus='1')
        a=dict(r,open=5,close=5,high=5.5,low=4.5)
        b=normalize_baostock([r],[a])[0]
        self.assertEqual((b['volume'],b['amount'],b['close'],b['raw_close'],b['raw_pre_close']),(123,45678,5,10,9.8))

    def test_suspension_zero_volume_preserved_without_synthetic_bar(self):
        r=dict(date='2026-10-14',open=10,close=10,high=10,low=10,volume=0,amount=0,tradestatus='0')
        self.assertEqual(normalize_baostock([r],[r])[0]['volume'],0)
        r['open']=''
        self.assertEqual(normalize_baostock([r],[r]),[])

    def test_baostock_dated_st_flag_known_or_unknown(self):
        row=dict(date='2026-10-14',open=10,close=10,high=11,low=9,volume=12300,amount=45678,tradestatus='1')
        for flag,expected in [('0',False),('1',True),('',None),(None,None),('unexpected',None)]:
            with self.subTest(flag=flag):
                r=dict(row,isST=flag)
                bar=normalize_baostock([r],[r])[0]
                if expected is None:
                    self.assertNotIn('is_st',bar)
                else:
                    self.assertIs(bar['is_st'],expected)
        self.assertNotIn('is_st',normalize_baostock([row],[row])[0])

    def test_baostock_st_query_field_only_for_mainboard(self):
        sdk=Mock(); result=Mock();result.error_code='0';result.next.return_value=False
        sdk.query_history_k_data_plus.return_value=result
        base='date,open,high,low,close,preclose,volume,amount,tradestatus'
        for code in ('sh.600036','sh.603001','sz.000001','sz.002001','sh.000300','sh.000001','sz.399001','sz.300750','sh.688001'):
            _bao_query(sdk,code,'2026-10-14','2026-10-14','3')
            expected=base+',isST' if code in ('sh.600036','sh.603001','sz.000001','sz.002001') else base
            with self.subTest(code=code):
                self.assertEqual(sdk.query_history_k_data_plus.call_args.args[1],expected)

    def test_mismatched_raw_dates_rejected(self):
        with self.assertRaises(ProviderUnavailable):
            normalize_baostock([dict(date='2026-10-14')],[])

    def test_invalid_prices_and_activity_rejected(self):
        for field,value in [('open',float('nan')),('low',20),('vol',-1),('amount',-1)]:
            row=raw();row[field]=value
            with self.subTest(field=field), self.assertRaises(ProviderUnavailable):
                normalize_tushare([row],[dict(trade_date='20261014',adj_factor=1)])

    def test_credentials_config_private_and_not_status(self):
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'data/private';path.mkdir(parents=True)
            (path/'market_data.json').write_text(json.dumps({'tushare_token':'a-private-token'}))
            router=self.router(directory)
            self.assertTrue(router.status()['tushare']['configured'])
            self.assertNotIn('a-private-token',json.dumps(router.status()))
            with patch.dict('os.environ',{'TUSHARE_TOKEN':'env-private-token'}), patch('history_providers.importlib.util.find_spec',return_value=None):
                other=SourceRouter(directory)
                self.assertEqual(other._token,'env-private-token')

    def test_optional_uninstalled_legacy_compatibility(self):
        router=self.router(); legacy=Mock(return_value=('name',combined(),'legacy',['legacy_warning']))
        self.assertEqual(router.download('sh600036','2026-10-13','2026-10-14',legacy_download=legacy),legacy.return_value)
        legacy.assert_called_once_with('sh600036','2026-10-13','2026-10-14','qfq')

    def test_source_failover_and_no_secret_in_warning(self):
        router=self.router();router._token='secret';router._bao_available=True
        router._tushare=Mock(side_effect=RuntimeError('https://api?token=secret'))
        router._baostock=Mock(return_value=combined())
        result=router.download('sh600036','2026-10-13','2026-10-14')
        self.assertEqual(result[2],'baostock')
        self.assertNotIn('secret',json.dumps(result[3]))
        self.assertEqual(result[3],['tushare_RuntimeError'])

    def test_stale_provider_fails_over_and_keeps_legacy_partial(self):
        router=self.router();router._token='secret';router._tushare=Mock(return_value=combined('2026-10-13'))
        legacy=Mock(return_value=('name',combined('2026-10-13'),'legacy',[]))
        result=router.download('sh600036','2026-10-13','2026-10-14',legacy_download=legacy)
        self.assertEqual(result[2],'legacy')
        self.assertIn('stale_history',result[3][0])
        self.assertEqual(router.circuit.status()['tushare']['failures'],0)

    def test_future_data_rejected(self):
        router=self.router();router._token='secret';router._tushare=Mock(return_value=combined('2026-10-15'))
        self.assertEqual(router.download('sh600036','2026-10-13','2026-10-14')[1],[])

    def test_unadjusted_index_uses_index_daily_and_no_factors(self):
        router=self.router(); router._tushare_api=Mock(return_value=[raw()])
        bars=router._tushare('sh000300','2026-10-14','2026-10-14','none')
        self.assertEqual(bars[0]['close'],bars[0]['raw_close'])
        self.assertEqual(router._tushare_api.call_args.args[0],'index_daily')
        self.assertEqual(router._tushare_api.call_count,1)

    def test_tushare_long_window_is_chunked(self):
        router=self.router();router._tushare_api=Mock(return_value=[])
        self.assertEqual(router._tushare('sh600036','2010-01-01','2026-10-14','qfq'),[])
        self.assertGreater(router._tushare_api.call_count,2)
        for call in router._tushare_api.call_args_list:
            self.assertLessEqual(int(call.args[1]['end_date'][:4])-int(call.args[1]['start_date'][:4]),5)

    def test_tushare_http_no_proxy_and_timeout(self):
        router=self.router();router._token='private-token'
        response=Mock();response.json.return_value={'code':0,'data':{'fields':['close'],'items':[[10]]}}
        router.session=Mock();router.session.post.return_value=response
        self.assertEqual(router._tushare_api('daily',{},'close'),[{'close':10}])
        self.assertEqual(router.session.post.call_args.kwargs['timeout'],6)
        self.assertEqual(router.session.post.call_args.args,('https://api.tushare.pro',))
        router.session.post.return_value.json.return_value={'code':-1,'msg':'private-token denied'}
        with self.assertRaisesRegex(ProviderUnavailable,'^api_rejected$'):
            router._tushare_api('daily',{},'close')

    def test_baostock_worker_timeout_releases_lock(self):
        router=self.router();router._bao_stream=False
        with patch('history_providers.subprocess.run',side_effect=subprocess.TimeoutExpired('worker',6)) as run:
            for _ in range(2):
                with self.assertRaises(subprocess.TimeoutExpired):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
            self.assertEqual(run.call_count,2)
            self.assertEqual(run.call_args.kwargs['timeout'],30)

    def test_baostock_worker_no_stdout_protocol_noise(self):
        router=self.router();router._bao_stream=False
        with patch('history_providers.subprocess.run',return_value=Mock(returncode=0,stdout=json.dumps(combined()))):
            self.assertEqual(router._baostock('sh600036','2026-10-13','2026-10-14','qfq'),combined())
        with patch('history_providers.subprocess.run',return_value=Mock(returncode=1,stdout='server detail private')):
            with self.assertRaisesRegex(ProviderUnavailable,'^worker_failed$'):
                router._baostock('sh600036','2026-10-13','2026-10-14','qfq')

    def test_bao_iteration_error_checked(self):
        sdk=Mock(); result=Mock();result.error_code='0';result.fields=['date'];result.next.side_effect=[True,False];result.get_row_data.return_value=['2026-10-14'];sdk.query_history_k_data_plus.return_value=result
        self.assertEqual(_bao_query(sdk,'sh.600036','2026-10-14','2026-10-14','3'),[{'date':'2026-10-14'}])
        result.error_code='-1'
        with self.assertRaises(ProviderUnavailable):
            _bao_query(sdk,'sh.600036','2026-10-14','2026-10-14','3')

    def test_baostock_login_logout_and_flags(self):
        sdk=Mock();sdk.login.return_value.error_code='0'
        with patch('history_providers._install_bao_budget'), patch.dict('sys.modules',{'baostock':sdk}), patch('history_providers._bao_query',side_effect=[[],[]]) as query:
            self.assertEqual(_baostock_worker('sh600036','2026-10-13','2026-10-14','qfq'),[])
            self.assertEqual([c.args[-1] for c in query.call_args_list],['3','2'])
            sdk.logout.assert_called_once()
        sdk.reset_mock()
        with patch('history_providers._install_bao_budget'), patch.dict('sys.modules',{'baostock':sdk}), patch('history_providers._bao_query',side_effect=ValueError):
            with self.assertRaises(ValueError):
                _baostock_worker('sh600036','2026-10-13','2026-10-14','qfq')
            sdk.logout.assert_called_once()

    def test_three_bad_assets_do_not_disable_healthy_source(self):
        router=self.router();router._bao_available=True
        router._baostock=Mock(side_effect=[HistoryNotCurrent('invalid_number')]*3+[combined()])
        for symbol in ('sh600001','sh600002','sh600003'):
            self.assertEqual(router.download(symbol,'2026-10-13','2026-10-14')[1],[])
        self.assertEqual(router.circuit.status()['baostock']['failures'],0)
        self.assertEqual(router.download('sh600036','2026-10-13','2026-10-14')[2],'baostock')
        diag=router.status()['failure_diagnostics']['baostock']
        self.assertEqual(diag['last_error_code'],'invalid_number')
        self.assertEqual(diag['last_failed_symbol'],'sh600003')
        self.assertEqual(diag['counts_by_code'],{'invalid_number':3})

    def test_optional_invalid_preclose_does_not_discard_verified_ohlc(self):
        for value in ('',0,'0',None,float('nan'),'unknown'):
            with self.subTest(value=value):
                row=raw();row['pre_close']=value
                b=normalize_tushare([row],[dict(trade_date='20261014',adj_factor=1)])[0]
                self.assertEqual(b['raw_close'],10)
                self.assertNotIn('raw_pre_close',b)
        row=dict(date='2026-10-14',open=10,close=10,high=11,low=9,volume=12300,amount=45678,preclose='0')
        self.assertNotIn('raw_pre_close',normalize_baostock([row],[row])[0])

    def test_safe_failure_code_and_symbol_retained_without_secrets(self):
        router=self.router();router._bao_available=True
        router._baostock=Mock(side_effect=ProviderUnavailable('sdk_error_10001001'))
        router.download('sh600036','2026-10-13','2026-10-14')
        self.assertEqual(router.status()['circuits']['baostock']['last_error_code'],'sdk_error_10001001')
        router._baostock.side_effect=ProviderUnavailable('token=private-super-secret')
        router.download('sh600036','2026-10-13','2026-10-14')
        self.assertNotIn('private-super-secret',json.dumps(router.status()))
        self.assertEqual(router.status()['failure_diagnostics']['baostock']['last_error_code'],'provider_error')

    def test_stream_login_refusal_emits_sanitized_numeric_code(self):
        sdk=Mock();sdk.login.return_value.error_code='10001011'
        sdk.login.return_value.error_msg='private response must not escape'
        out=io.StringIO()
        with patch('history_providers._install_bao_budget'),patch.dict('sys.modules',{'baostock':sdk}),patch('sys.stdout',out),patch('sys.stdin',io.StringIO('')):
            self.assertEqual(_baostock_stream(),1)
        message=json.loads(out.getvalue())
        self.assertEqual(message,{'id':None,'phase':'login','error':'sdk_error_10001011','asset_error':False})
        self.assertNotIn('private',out.getvalue())
        sdk.logout.assert_not_called()
        sdk.query_history_k_data_plus.assert_not_called()

    def test_blacklist_persists_six_hours_and_cannot_be_reset_by_success(self):
        router=self.router()
        with patch('history_providers.time.time',return_value=100000):
            router._persist_bao_restriction()
            data=json.loads(router._provider_state_path.read_text())
            self.assertEqual(data['baostock']['blocked_until'],121600)
            self.assertEqual(router._provider_state_path.stat().st_mode & 0o777,0o600)
            router.circuit.call('baostock',lambda:42)
            another=self.router(str(router.base_dir))
            self.assertEqual(another.status()['baostock']['retry_after_seconds'],21600)
            self.assertEqual(another.status()['circuits']['baostock']['state'],'open')
            self.assertEqual(another.status()['circuits']['baostock']['retry_after_seconds'],21600)
            with patch('history_providers.subprocess.Popen') as spawn:
                with self.assertRaisesRegex(ProviderUnavailable,'provider_restricted'):
                    another._baostock('sh600036','2026-10-13','2026-10-14','qfq')
                spawn.assert_not_called()
        with patch('history_providers.time.time',return_value=121601):
            self.assertEqual(router.status()['baostock']['retry_after_seconds'],0)

    def test_cross_process_lease_and_idle_worker_logout(self):
        with tempfile.TemporaryDirectory() as directory:
            leasepath=str(Path(directory)/'baostock.lock')
            with patch.dict('os.environ',{'QUANT_BAO_LEASE_PATH':leasepath}):
                handle=_acquire_worker_lease()
                try:
                    code="from history_providers import _acquire_worker_lease,ProviderBusy\ntry:\n _acquire_worker_lease();print('unexpected')\nexcept ProviderBusy:\n print('busy')"
                    child=subprocess.run([sys.executable,'-c',code],capture_output=True,text=True,timeout=3)
                    self.assertEqual(child.stdout.strip(),'busy')
                finally:handle.close()
                sdk=Mock();sdk.login.return_value.error_code='0'
                def idle(*args):
                    with self.assertRaises(ProviderBusy):_acquire_worker_lease()
                    return ([],[],[])
                with patch('history_providers._install_bao_budget'),patch.dict('sys.modules',{'baostock':sdk}),patch('sys.stdout',io.StringIO()),patch('history_providers.select.select',side_effect=idle):
                    self.assertEqual(_baostock_stream(),0)
                sdk.logout.assert_called_once()
                after=_acquire_worker_lease();after.close()

    def test_one_shot_blacklist_code_persisted_without_generic_worker_error(self):
        router=self.router();router._bao_stream=False
        result=Mock(returncode=1,stdout=json.dumps({'error':'sdk_error_10001011'}))
        with patch('history_providers.subprocess.run',return_value=result):
            with self.assertRaisesRegex(ProviderUnavailable,'sdk_error_10001011'):
                router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
        self.assertGreaterEqual(router.status()['baostock']['retry_after_seconds'],21599)

    def test_provider_state_wrong_types_fail_closed_without_process(self):
        router=self.router();router._provider_state_path.parent.mkdir(parents=True)
        for state in ([],{'baostock':[]},{'baostock':{'blocked_until':'bad'}},{'baostock':{'blocked_until':float('nan')}}):
            with self.subTest(state=state):
                router._provider_state_path.write_text(json.dumps(state))
                with patch('history_providers.subprocess.Popen') as spawn:
                    with self.assertRaisesRegex(ProviderUnavailable,'provider_state_unavailable'):
                        router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
                    spawn.assert_not_called()

    def test_invalid_symbol_never_starts_worker(self):
        with self.assertRaises(ProviderUnavailable):
            self.router().download('../private','2026-10-13','2026-10-14')


class CircuitTests(unittest.TestCase):
    def test_three_failures_skip_then_single_halfopen_probe(self):
        clock=[0];breaker=CircuitBreaker(clock=lambda:clock[0]);bad=Mock(side_effect=RuntimeError('secret'))
        for _ in range(3):
            with self.assertRaises(ProviderUnavailable):breaker.call('one',bad)
        with self.assertRaisesRegex(ProviderUnavailable,'circuit_open'):breaker.call('one',bad)
        self.assertEqual(bad.call_count,3)
        self.assertEqual(breaker.status()['one']['state'],'open')
        clock[0]=301
        entered,release=threading.Event(),threading.Event()
        def callback():entered.set();release.wait(2);return 'okay'
        thread=threading.Thread(target=lambda:breaker.call('one',callback));thread.start();self.assertTrue(entered.wait(1))
        with self.assertRaisesRegex(ProviderUnavailable,'circuit_open'):breaker.call('one',bad)
        release.set();thread.join(2)
        self.assertEqual(breaker.status()['one']['state'],'closed')
        self.assertEqual(breaker.status()['one']['failures'],0)
        self.assertNotIn('secret',json.dumps(breaker.status()))

    def test_asset_specific_absence_does_not_disable_other_stocks(self):
        breaker=CircuitBreaker()
        for _ in range(10):
            with self.assertRaises(HistoryNotCurrent):
                breaker.call('one',lambda: (_ for _ in ()).throw(HistoryNotCurrent('stale_history')))
        self.assertEqual(breaker.status()['one']['failures'],0)
        self.assertEqual(breaker.call('one',lambda:42),42)

    def test_local_contention_does_not_trip_remote_circuit(self):
        breaker=CircuitBreaker()
        for _ in range(10):
            with self.assertRaises(ProviderBusy):
                breaker.call('one',lambda: (_ for _ in ()).throw(ProviderBusy('busy')))
        self.assertEqual(breaker.status()['one']['failures'],0)


class StreamWorkerTests(unittest.TestCase):
    setUp = ProviderTests.setUp
    def router(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        with patch.dict('os.environ', {}, clear=True), patch('history_providers.importlib.util.find_spec', return_value=None):
            return SourceRouter(base_dir=tmp.name)

    def child(self, code):
        # Real local pipes exercise framing/timeouts; no provider/network access.
        return subprocess.Popen([sys.executable,'-u','-c',code],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,bufsize=0)

    def test_two_requests_reuse_one_process_with_chunked_response(self):
        child=self.child("import sys,json,time\nfor line in sys.stdin:\n r=json.loads(line);s=json.dumps({'id':r['id'],'bars':[{'date':r['args'][2]}]})+'\\n';sys.stdout.write(s[:5]);sys.stdout.flush();time.sleep(.005);sys.stdout.write(s[5:]);sys.stdout.flush()")
        router=self.router()
        try:
            with patch('history_providers.subprocess.Popen',return_value=child) as spawn:
                a=router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
                b=router._baostock('sh600519','2026-10-13','2026-10-14','qfq')
                self.assertEqual(a,b)
                self.assertEqual(spawn.call_count,1)
                self.assertEqual(router._bao_process.pid,child.pid)
        finally:
            router.close()
        self.assertIsNotNone(child.poll())
        self.assertIsNone(router._bao_process)

    def test_timeout_kills_reaps_and_next_request_starts_fresh_worker(self):
        hung=self.child("import time;time.sleep(30)")
        ready=self.child("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'bars':[]} ),flush=True)")
        router=self.router();router.timeout=.1
        try:
            with patch('history_providers.subprocess.Popen',side_effect=[hung,ready]) as spawn:
                started=time.monotonic()
                with self.assertRaisesRegex(ProviderUnavailable,'worker_timeout'):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
                self.assertLess(time.monotonic()-started,1.5)
                self.assertIsNotNone(hung.poll())
                self.assertIsNone(router._bao_process)
                self.assertEqual(router._baostock('sh600519','2026-10-13','2026-10-14','qfq'),[])
                self.assertEqual(spawn.call_count,2)
        finally:
            router.close()
            for child in (hung,ready):
                if child.poll() is None:child.kill();child.wait()

    def test_truncated_response_eof_is_not_reused(self):
        child=self.child("import sys;sys.stdin.readline();sys.stdout.write('{');sys.stdout.flush()")
        router=self.router()
        try:
            with patch('history_providers.subprocess.Popen',return_value=child):
                with self.assertRaisesRegex(ProviderUnavailable,'worker_closed'):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
            self.assertIsNone(router._bao_process)
            self.assertIsNotNone(child.poll())
        finally:router.close()

    def test_wrong_request_id_is_rejected_and_worker_reaped(self):
        child=self.child("import sys,json;sys.stdin.readline();print(json.dumps({'id':999,'bars':[]}),flush=True);sys.stdin.read()")
        router=self.router()
        try:
            with patch('history_providers.subprocess.Popen',return_value=child):
                with self.assertRaisesRegex(ProviderUnavailable,'invalid_worker_result'):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
            self.assertIsNone(router._bao_process)
            self.assertIsNotNone(child.poll())
        finally:router.close()

    def test_worker_known_bad_asset_response_is_not_transport_failure(self):
        child=self.child("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'error':'invalid_number','asset_error':True}),flush=True)")
        router=self.router()
        try:
            with patch('history_providers.subprocess.Popen',return_value=child):
                with self.assertRaisesRegex(HistoryNotCurrent,'invalid_number'):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
            self.assertIsNone(router._bao_process)
            self.assertIsNotNone(child.poll())
        finally:router.close()

    def test_untrusted_worker_error_text_not_exposed(self):
        child=self.child("import sys,json\nfor line in sys.stdin:\n r=json.loads(line);print(json.dumps({'id':r['id'],'error':'private-secret-token','asset_error':True}),flush=True)")
        router=self.router()
        try:
            with patch('history_providers.subprocess.Popen',return_value=child):
                with self.assertRaisesRegex(ProviderUnavailable,'^invalid_worker_result$'):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
        finally:router.close()

    def test_login_handshake_failure_preserves_numeric_sdk_code(self):
        child=self.child("import json;print(json.dumps({'id':None,'phase':'login','error':'sdk_error_10001011','asset_error':False}),flush=True)")
        router=self.router()
        try:
            with patch('history_providers.subprocess.Popen',return_value=child):
                with self.assertRaisesRegex(ProviderUnavailable,'^sdk_error_10001011$'):
                    router._baostock('sh600036','2026-10-13','2026-10-14','qfq')
            self.assertIsNone(router._bao_process)
        finally:router.close()

class BulkAndAdmissionTests(unittest.TestCase):
    setUp = ProviderTests.setUp
    router = ProviderTests.router
    def fixture(self, day='20261014', code='600036.SH', factor=2):
        return dict(raw(day), ts_code=code), dict(ts_code=code, trade_date=day, adj_factor=factor)

    def test_daily_batches_paginate_to_empty_and_reuse_across_routers(self):
        with tempfile.TemporaryDirectory() as root:
            router=self.router(root); router._token='test'
            r,f=self.fixture()
            router._tushare_api=Mock(side_effect=[[r],[],[f],[]])
            result=router.prepare_trade_days(['2026-10-14'])
            self.assertEqual(result['downloaded'],['2026-10-14'])
            self.assertEqual([c.args[1]['offset'] for c in router._tushare_api.call_args_list],[0,1,0,1])
            other=self.router(root); other._token='test';other._tushare_api=Mock()
            self.assertEqual(other.prepare_trade_days(['2026-10-14'])['ready'],['2026-10-14'])
            self.assertEqual(other._tushare('sh600036','2026-10-14','2026-10-14','qfq')[0]['raw_close'],10)
            other._tushare_api.assert_not_called()
            # Complete batch proves absence; never query that asset again.
            self.assertEqual(other._tushare('sh600999','2026-10-14','2026-10-14','qfq'),[])
            other._tushare_api.assert_not_called()

    def test_partial_batch_not_published_and_failure_cooldown_survives_restart(self):
        with tempfile.TemporaryDirectory() as root:
            router=self.router(root);router._token='test';r,_=self.fixture()
            router._tushare_api=Mock(side_effect=[[r],[],[]])
            self.assertEqual(router.prepare_trade_days(['2026-10-14'])['ready'],[])
            self.assertFalse(router._daily_cache_path('2026-10-14').exists())
            other=self.router(root);other._token='test';other._tushare_api=Mock()
            self.assertEqual(other.prepare_trade_days(['2026-10-14'])['errors'],{'2026-10-14':'rate_wait'})
            other._tushare_api.assert_not_called()

    def test_ignored_offset_is_rejected(self):
        router=self.router();r,_=self.fixture();router._tushare_api=Mock(return_value=[r])
        with self.assertRaisesRegex(ProviderUnavailable,'query_incomplete'):
            router._tushare_day_pages('daily','2026-10-14','ts_code,trade_date')
        self.assertEqual(router._tushare_api.call_count,2)

    def test_wrong_day_duplicate_or_bad_factor_rejects_entire_batch(self):
        router=self.router();r,f=self.fixture()
        for rows,factors in (([r,r],[f]),([r],[dict(f,trade_date='20261013')]),([r],[dict(f,adj_factor=0)])):
            with self.subTest(rows=rows,factors=factors),self.assertRaises((ProviderUnavailable,ValueError)):
                router._validate_daily_batch(dict(version=1,complete=True,date='2026-10-14',daily=rows,adj_factor=factors),'2026-10-14')

    def test_batch_slice_rebases_qfq_at_last_available_date(self):
        router=self.router();router._token='test'
        fixtures={d:self.fixture(d.replace('-',''),factor=f) for d,f in [('2026-10-13',1),('2026-10-14',2)]}
        router._tushare_api=Mock(side_effect=[x for d in fixtures for x in ([fixtures[d][0]],[],[fixtures[d][1]],[])])
        self.assertEqual(len(router.prepare_trade_days(fixtures)['ready']),2)
        bars=router._tushare('sh600036','2026-10-13','2026-10-14','qfq')
        self.assertEqual([b['close'] for b in bars],[5,10])
        self.assertEqual([b['raw_close'] for b in bars],[10,10])

    def test_unconfigured_holidays_and_future_dates_never_request(self):
        router=self.router();router._tushare_api=Mock()
        self.assertEqual(router.prepare_trade_days(['2026-10-14'])['ready'],[])
        router._token='test'
        router.prepare_trade_days(['2026-10-01','2099-01-01'])
        router._tushare_api.assert_not_called()

    def test_http_admission_denial_does_not_send_or_trip_source(self):
        from market_request_budget import RequestBudgetExceeded
        router=self.router();router._token='test';router.session=Mock()
        router._budget.request.side_effect=RequestBudgetExceeded('daily_limit','tushare',100)
        router.download('sh600036','2026-10-14','2026-10-14')
        router.session.post.assert_not_called()
        self.assertEqual(router.circuit.status()['tushare']['failures'],0)
        self.assertEqual(router.status()['failure_diagnostics']['tushare']['last_error_code'],'daily_limit')

    def test_sdk_packet_wrapper_counts_login_query_page_and_logout_boundary(self):
        import types
        from history_providers import _install_bao_budget
        package=types.ModuleType('baostock');package.__path__=[]
        util=types.ModuleType('baostock.util');util.__path__=[]
        transport=types.ModuleType('baostock.util.socketutil');transport.send_msg=Mock(return_value='ok')
        wire=transport.send_msg
        with tempfile.TemporaryDirectory() as root,patch.dict('os.environ',{'QUANT_MARKET_BASE_DIR':root}),patch.dict('sys.modules',{'baostock':package,'baostock.util':util,'baostock.util.socketutil':transport}):
            _install_bao_budget()
            for packet in ('login','query','page','logout'):
                self.assertEqual(transport.send_msg(packet),'ok')
            self.assertEqual(self.budget_class.return_value.request.call_count,4)
            self.assertEqual(wire.call_count,4)
            # Installing twice must not double-count requests.
            _install_bao_budget();transport.send_msg('query')
            self.assertEqual(self.budget_class.return_value.request.call_count,5)

    def test_sdk_unknown_daily_usage_denies_before_login_socket(self):
        import types
        package=types.ModuleType('baostock');package.__path__=[];package.login=Mock()
        util=types.ModuleType('baostock.util');util.__path__=[]
        transport=types.ModuleType('baostock.util.socketutil');transport.send_msg=Mock()
        self.budget_class.return_value.initialize.return_value={'reason':'untracked_prior_usage'}
        with patch.dict('sys.modules',{'baostock':package,'baostock.util':util,'baostock.util.socketutil':transport}),self.assertRaisesRegex(ProviderBusy,'untracked_prior_usage'):
            _baostock_worker('sh600036','2026-10-14','2026-10-14','qfq')
        package.login.assert_not_called();transport.send_msg.assert_not_called()
