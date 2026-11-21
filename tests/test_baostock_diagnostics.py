"""Safe BaoStock diagnostics use SDK/process doubles, with no network or database."""
import contextlib
import io
import json
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from foundation import baostock_worker as worker, sources


PRIVATE = 'PRIVATE_REQUEST_LOGIN_RESPONSE_SENTINEL'
REQUEST = {'operation':'basics','params':{'start':'2026-11-16','end':'2026-11-16'}}


class BaoStockDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.no_db = patch.object(sources.store,'db',side_effect=AssertionError('No DB allowed'))
        self.no_db.start();self.addCleanup(self.no_db.stop)
        self.sdk = ModuleType('baostock')
        self.result = SimpleNamespace(error_code='0',error_msg=PRIVATE,fields=['code'],
                                      next=Mock(return_value=False),get_row_data=Mock(return_value=['sh.600000']))
        self.sdk.login = Mock(return_value=SimpleNamespace(error_code='0'))
        self.sdk.logout = Mock(return_value=None)
        for name in ('query_stock_basic','query_profit_data','query_balance_data','query_cash_flow_data','query_growth_data'):
            setattr(self.sdk,name,Mock(return_value=self.result))
        sectors = ModuleType('baostock.security.sectorinfo')
        sectors.query_stock_concept = Mock();sectors.query_stock_area = Mock()
        modules={'baostock':self.sdk,'baostock.security':ModuleType('baostock.security'),
                 'baostock.security.sectorinfo':sectors}
        self.modules = patch.dict(sys.modules,modules)
        self.modules.start();self.addCleanup(self.modules.stop)
        self.socket_timeout = patch.object(worker.socket,'setdefaulttimeout')
        self.timeout = self.socket_timeout.start();self.addCleanup(self.socket_timeout.stop)
        self.eof_guard = patch.object(worker,'_sdk_eof_guard',side_effect=contextlib.nullcontext)
        self.eof_guard.start();self.addCleanup(self.eof_guard.stop)

    def execute(self):
        stdout=io.StringIO()
        with contextlib.redirect_stdout(stdout):
            response=worker.execute(REQUEST)
        self.assertEqual(stdout.getvalue(),'')
        self.assertNotIn(PRIVATE,json.dumps(response))
        self.timeout.assert_called_with(20)
        return response

    def source_error(self,response):
        process=SimpleNamespace(stdout=json.dumps(response),stderr=PRIVATE)
        with patch.object(sources.subprocess,'run',return_value=process) as run:
            with self.assertRaises(sources.SourceError) as captured:
                sources.bs_call('basics',REQUEST['params'],300)
        self.assertEqual(run.call_args.kwargs['timeout'],300)
        self.assertNotIn(PRIVATE,str(captured.exception))
        return captured.exception

    def test_query_timeout_is_a_safe_sdk_report_not_an_exception_claim(self):
        def query(**kwargs):
            print(PRIVATE);print('timed out')
            self.result.error_code='10002007'
            return self.result
        self.sdk.query_stock_basic.side_effect=query
        response=self.execute()
        stages=response['diagnostics']['stages']
        self.assertEqual(response['diagnostics']['failed_phase'],'query')
        self.assertEqual([item['phase'] for item in stages],['login','query','logout'])
        self.assertEqual(stages[1]['sdk_signals'],['sdk_timeout_reported'])
        self.assertTrue(all(type(item['elapsed_ms']) is int and item['elapsed_ms']>=0 for item in stages))
        error=self.source_error(response)
        self.assertEqual(error.kind,'network')
        self.assertIn('10002007',str(error));self.assertIn('查询阶段',str(error));self.assertIn('SDK报告超时',str(error))
        self.assertIn('阶段耗时',str(error))
        self.assertNotIn('TimeoutError',json.dumps(response))

    def test_login_failure_has_only_its_own_stage(self):
        self.sdk.login.return_value=SimpleNamespace(error_code='10002007')
        response=self.execute()
        self.assertEqual(response['diagnostics']['failed_phase'],'login')
        self.assertEqual(len(response['diagnostics']['stages']),1)
        self.sdk.logout.assert_not_called()
        self.assertIn('登录阶段',str(self.source_error(response)))

    def test_pagination_failure_keeps_partial_rows_unpublished(self):
        def next_page():
            print('timed out');print(PRIVATE)
            self.result.error_code='10002007'
            return False
        self.result.next.side_effect=next_page
        response=self.execute()
        self.assertNotIn('rows',response)
        self.assertEqual(response['diagnostics']['failed_phase'],'pagination')
        self.assertIn('分页阶段',str(self.source_error(response)))

    def test_logout_failure_does_not_replace_the_query_failure(self):
        self.result.error_code='10002007'
        def logout():
            print('timed out');print('接收数据异常，请稍后再试。');print(PRIVATE)
            raise RuntimeError(PRIVATE)
        self.sdk.logout.side_effect=logout
        response=self.execute()
        self.assertEqual(response['diagnostics']['failed_phase'],'query')
        self.assertIn('10002007',response['error'])
        self.assertEqual(response['diagnostics']['stages'][-1]['status'],'failed')
        error=self.source_error(response)
        self.assertEqual(error.kind,'network')
        self.assertIn('查询阶段',str(error));self.assertNotIn('SDK报告超时',str(error))
        self.result.error_code='0'
        logout_only=self.execute()
        self.assertEqual(logout_only['diagnostics']['failed_phase'],'logout')
        self.assertEqual(logout_only['error'],'BaoStock 本地调用未完成')

    def test_success_keeps_rows_and_does_not_export_sdk_output(self):
        self.result.next.side_effect=[True,False]
        def logout():
            print('timed out');print('接收数据异常，请稍后再试。');print(PRIVATE)
        self.sdk.logout.side_effect=logout
        self.assertEqual(self.execute(),{'rows':[{'code':'sh.600000'}]})

    def test_only_exact_whitelist_lines_become_signals_and_raw_errors_are_hidden(self):
        def query(**kwargs):
            for line in (' timed out ','timed out '+PRIVATE,'TimeoutError','timeout',PRIVATE):
                print(line)
            raise ValueError(PRIVATE)
        self.sdk.query_stock_basic.side_effect=query
        response=self.execute()
        self.assertEqual(response['diagnostics']['stages'][1]['sdk_signals'],[])
        self.assertEqual(response['error'],'BaoStock 本地调用未完成')

    def test_parent_accepts_only_safe_phase_enum_signal_and_bounded_elapsed(self):
        response={'error':'BaoStock 查询未完成 (10002006)','diagnostics':{
            'failed_phase':'query','stages':[{'phase':'query','status':'failed','elapsed_ms':10**30,
                                            'sdk_signals':[PRIVATE],'raw_stdout':PRIVATE}]}}
        error=self.source_error(response)
        self.assertEqual(error.kind,'network');self.assertIn('10002006',str(error))
        self.assertIn('查询阶段',str(error));self.assertNotIn('SDK报告超时',str(error));self.assertNotIn('耗时',str(error))
        response['diagnostics']['failed_phase']=PRIVATE
        self.assertNotIn('阶段',str(self.source_error(response)))

    def test_login_and_query_session_errors_keep_the_existing_retry_meanings(self):
        self.sdk.login.return_value=SimpleNamespace(error_code='10001001')
        self.assertEqual(self.source_error(self.execute()).kind,'upstream')
        self.sdk.login.return_value=SimpleNamespace(error_code='0')
        self.result.error_code='10001001'
        self.assertEqual(self.source_error(self.execute()).kind,'network')


class BaoStockEofTests(unittest.TestCase):
    def setUp(self):
        from baostock.common import context,contants
        from baostock.data import messageheader
        from baostock.util import socketutil
        self.context=context;self.constants=contants;self.headers=messageheader;self.sdk_sockets=socketutil
        for replacement in (
            patch.object(worker.socket,'socket',side_effect=AssertionError('No network allowed')),
            patch.object(worker.socket,'setdefaulttimeout'),
            patch.object(sources.store,'db',side_effect=AssertionError('No DB allowed')),
            patch.object(context,'apiKey','0',create=True),
            patch.object(context,'user_id','test-user',create=True),
        ):
            replacement.start();self.addCleanup(replacement.stop)

    @contextlib.contextmanager
    def fake_connection(self,chunks):
        class FakeSocket:
            def __init__(self,values):
                self.values=list(values);self.recv_calls=0;self.closed=False
            def send(self,value):
                return len(value)
            def recv(self,size):
                self.recv_calls+=1
                if not self.values:
                    raise AssertionError('Unexpected extra read after EOF or a complete frame')
                value=self.values.pop(0)
                if isinstance(value,Exception):raise value
                return value
            def close(self):
                self.closed=True
            def fileno(self):
                return 42
        connection=FakeSocket(chunks);previous=FakeSocket([])
        def connect(instance,api_key):
            self.context.default_socket=connection
            return 'connected'
        with patch.object(self.context,'default_socket',previous,create=True):
            with patch.object(self.sdk_sockets.SocketUtil,'connect',connect):
                try:
                    yield connection
                finally:
                    self.assertIs(self.sdk_sockets.SocketUtil.connect,connect)
                    self.assertIs(self.context.default_socket,previous)
                    self.assertFalse(previous.closed)
                    self.assertTrue(connection.closed)

    def test_real_sdk_login_eof_returns_retryable_error_without_an_extra_read(self):
        with self.fake_connection([b'']) as connection:
            stdout=io.StringIO()
            with contextlib.redirect_stdout(stdout):
                response=worker.execute(REQUEST)
            self.assertEqual(stdout.getvalue(),'')
            self.assertEqual(connection.recv_calls,1)
            self.assertNotIn('rows',response)
            self.assertIn('10002007',response['error'])
            self.assertEqual(response['diagnostics']['failed_phase'],'login')
            self.assertEqual(set(response['diagnostics']['stages'][0]['sdk_signals']),{'sdk_peer_closed','sdk_receive_error_reported'})
            self.assertNotIn(PRIVATE,json.dumps(response))
            process=SimpleNamespace(stdout=json.dumps(response))
            with patch.object(sources.subprocess,'run',return_value=process):
                with self.assertRaises(sources.SourceError) as captured:
                    sources.bs_call('basics',REQUEST['params'],300)
            self.assertEqual(captured.exception.kind,'network')
            self.assertIn('10002007',str(captured.exception))
            self.assertIn('报文未完连接关闭',str(captured.exception))

    def test_real_sdk_partial_frame_eof_is_not_accepted_as_data(self):
        diagnostics=[]
        with self.fake_connection([PRIVATE.encode(),b'']) as connection:
            with worker._sdk_eof_guard():
                self.assertEqual(self.sdk_sockets.SocketUtil().connect('0'),'connected')
                self.assertEqual(self.context.default_socket.fileno(),42)
                with self.assertRaises(worker._SDKError):
                    with worker._stage(diagnostics,'query'):
                        result=self.sdk_sockets.send_msg(PRIVATE)
                        self.assertIsNone(result)
                        raise worker._SDKError('query','10002007')
            self.assertEqual(connection.recv_calls,2)
        self.assertEqual(set(diagnostics[0]['sdk_signals']),{'sdk_peer_closed','sdk_receive_error_reported'})
        self.assertEqual(diagnostics[0]['status'],'failed')
        self.assertNotIn(PRIVATE,json.dumps(diagnostics))

    def test_complete_split_plain_and_compressed_frames_are_unchanged(self):
        import zlib
        body='safe response 测试'
        for compressed in (False,True):
            with self.subTest(compressed=compressed):
                message_type=self.constants.MESSAGE_TYPE_GETKDATAPLUS_RESPONSE if compressed else self.constants.MESSAGE_TYPE_LOGIN_RESPONSE
                payload=zlib.compress(body.encode()) if compressed else body.encode()
                header=self.headers.to_message_header(message_type,len(payload))
                frame=header.encode()+payload+b'<![CDATA[]]>\n'
                expected=header+body if compressed else frame.decode()
                diagnostics=[]
                with self.fake_connection([frame[:4],frame[4:-7],frame[-7:]]) as connection:
                    constructor=worker.socket.socket
                    with worker._sdk_eof_guard():
                        self.assertIs(worker.socket.socket,constructor)
                        self.sdk_sockets.SocketUtil().connect('0')
                        with worker._stage(diagnostics,'query'):
                            actual=self.sdk_sockets.send_msg('safe request')
                    self.assertEqual(actual,expected)
                    self.assertEqual(connection.recv_calls,3)
                self.assertEqual(diagnostics[0]['status'],'ok')
                self.assertEqual(diagnostics[0]['sdk_signals'],[])

    def test_guard_restores_connector_and_context_when_body_raises(self):
        with self.fake_connection([]):
            with self.assertRaisesRegex(RuntimeError,PRIVATE):
                with worker._sdk_eof_guard():
                    self.sdk_sockets.SocketUtil().connect('0')
                    raise RuntimeError(PRIVATE)

    def test_existing_sdk_timeout_is_not_mislabeled_as_peer_eof(self):
        diagnostics=[]
        with self.fake_connection([TimeoutError('timed out')]) as connection:
            with worker._sdk_eof_guard():
                self.sdk_sockets.SocketUtil().connect('0')
                with self.assertRaises(worker._SDKError):
                    with worker._stage(diagnostics,'query'):
                        self.assertIsNone(self.sdk_sockets.send_msg('safe request'))
            self.assertEqual(connection.recv_calls,1)
        self.assertEqual(set(diagnostics[0]['sdk_signals']),{'sdk_timeout_reported','sdk_receive_error_reported'})

    def test_real_resultdata_partial_rows_are_rejected_on_next_page_eof_or_timeout(self):
        import baostock as bs
        from baostock.data.resultset import ResultData
        for failure,signal in ((b'','sdk_peer_closed'),(TimeoutError('timed out'),'sdk_timeout_reported'),
                               (ConnectionResetError(PRIVATE),'sdk_receive_error_reported')):
            with self.subTest(signal=signal):
                result=ResultData();result.error_code='0';result.fields=['code'];result.cur_page_num='1'
                result.data=[['sh.600000'] for _ in range(self.constants.BAOSTOCK_PER_PAGE_COUNT)]
                result.msg_type=self.constants.MESSAGE_TYPE_QUERYSTOCKBASIC_REQUEST
                result.msg_body=self.constants.MESSAGE_SPLIT.join(('query_stock_basic','test-user','1',
                                                                  str(self.constants.BAOSTOCK_PER_PAGE_COUNT),'',''))
                def login():
                    self.sdk_sockets.SocketUtil().connect('0')
                    return SimpleNamespace(error_code='0')
                with self.fake_connection([failure]) as connection:
                    with patch.object(bs,'login',side_effect=login),patch.object(bs,'logout'),\
                         patch.object(bs,'query_stock_basic',return_value=result):
                        response=worker.execute(REQUEST)
                    self.assertEqual(connection.recv_calls,1)
                    self.assertEqual(result.error_code,'0')  # The real SDK fails to update this.
                    self.assertNotIn('rows',response)
                    self.assertIn('10002007',response['error'])
                    self.assertEqual(response['diagnostics']['failed_phase'],'pagination')
                    stage=next(item for item in response['diagnostics']['stages'] if item['phase']=='pagination')
                    self.assertIn(signal,stage['sdk_signals'])
                    self.assertEqual(stage['status'],'failed')
                    self.assertNotIn(PRIVATE,json.dumps(response))
                    with patch.object(sources.subprocess,'run',return_value=SimpleNamespace(stdout=json.dumps(response))):
                        with self.assertRaises(sources.SourceError) as captured:
                            sources.bs_call('basics',REQUEST['params'],300)
                    self.assertEqual(captured.exception.kind,'network')

    def test_transport_marker_keeps_an_existing_sdk_error_code(self):
        diagnostics=[]
        with self.assertRaises(worker._SDKError) as captured:
            with worker._stage(diagnostics,'query'):
                print('timed out')
                raise worker._SDKError('query','10002006')
        self.assertEqual(captured.exception.code,'10002006')
        for phase in ('login','query','pagination'):
            with self.subTest(phase=phase):
                entries=[]
                with self.assertRaises(worker._SDKError) as captured:
                    with worker._stage(entries,phase):
                        print('接收数据异常，请稍后再试。')
                self.assertEqual(captured.exception.code,'10002007')
                self.assertEqual(entries[0]['status'],'failed')
                self.assertEqual(entries[0]['phase'],phase)


if __name__=='__main__':
    unittest.main()
