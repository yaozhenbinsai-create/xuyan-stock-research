"""Fresh, environment-disabled official account turns through the installed CLI.

No retired bridge imports, login copies, private-key extraction, or global config edits.
"""
from __future__ import annotations
import json,os,queue,shutil,subprocess,tempfile,threading,time
from contextlib import contextmanager
from pathlib import Path
from foundation import database as store


def official_environment():
    # Isolate this install's explicit sign-in from user-wide keys, proxies and CLI profiles.
    basics = {"PATH", "PATHEXT", "SYSTEMROOT", "WINDIR", "COMSPEC", "TEMP", "TMP",
              "USERPROFILE", "USERNAME", "USER", "HOMEDRIVE", "HOMEPATH", "HOME",
              "LOCALAPPDATA", "APPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)",
              "SYSTEMDRIVE", "PROCESSOR_ARCHITECTURE", "LANG", "LC_ALL", "TERM"}
    child = {key: value for key, value in os.environ.items() if key.upper() in basics}
    directory = store.data_root() / "official-codex"
    directory.mkdir(parents=True, exist_ok=True)
    child["CODEX_HOME"] = str(directory)
    return child


def official_command(executable, *arguments):
    return [executable, "-c", 'cli_auth_credentials_store="file"', "-c", 'forced_login_method="chatgpt"',
            "-c", 'model_provider="openai"', *arguments]

class NativeFailure(Exception):
    def __init__(self,message,*,kind='permanent',usage=None,remote=None,dispatched=False):
        super().__init__(message);self.kind=kind;self.usage=usage;self.remote=remote or {};self.dispatched=dispatched

def list_models(timeout):
    return run(None,None,None,timeout,catalog=True)


def run(model,system,payload,timeout,*,on_update=lambda **kw:None,stopped=lambda:False,process_factory=subprocess.Popen,interrupt_grace=3,catalog=False):
    executable=shutil.which('codex.cmd') or shutil.which('codex')
    if not executable:raise NativeFailure('未找到本机官方 Codex CLI')
    events=queue.Queue(maxsize=256);process=None;counter=0;usage=None;remote={};dispatched=False;terminal=None
    deadline=time.monotonic()+timeout
    texts={};last_phase={}
    def send(v):
        try:process.stdin.write(json.dumps(v,ensure_ascii=False)+'\n');process.stdin.flush()
        except (OSError,ValueError):raise NativeFailure('官方账号连接已断开',kind='unknown' if dispatched else 'transient',usage=usage,remote=remote,dispatched=dispatched) from None
    def receive(wait):
        nonlocal usage,terminal
        try:message=events.get(timeout=max(.01,min(wait,1)))
        except queue.Empty:
            if process.poll() is not None:raise NativeFailure('官方账号连接进程已退出',kind='unknown' if dispatched else 'transient',usage=usage,remote=remote,dispatched=dispatched)
            return None
        method=message.get('method');p=message.get('params') or {}
        if method=='thread/tokenUsage/updated' and p.get('threadId')==remote.get('thread_id'):
            token=p.get('tokenUsage') or {};usage=token.get('total') or token.get('last') or usage
            on_update(usage=usage,remote=remote,complete=False)
        if method=='turn/completed' and p.get('threadId')==remote.get('thread_id'):
            terminal=p.get('turn') or {}
        if method=='error' and p.get('threadId')==remote.get('thread_id') and p.get('turnId') and p.get('willRetry') is False:
            terminal={'id':p['turnId'],'status':'failed','error':p.get('error') or {}}
        if method=='item/completed' and p.get('threadId')==remote.get('thread_id'):
            item=p.get('item') or {}
            if item.get('type')=='agentMessage' and isinstance(item.get('text'),str):
                key=item.get('id',str(len(texts)));texts[key]=item['text'];last_phase[key]=item.get('phase')
        if 'method' in message and 'id' in message:
            # Environment access is disabled. Decline any unexpected interaction.
            send({'id':message['id'],'error':{'code':-32601,'message':'Research text transport does not allow tool or approval requests'}})
        return message
    def request(method,params,seconds=20):
        nonlocal counter
        counter+=1;rid=counter;send({'id':rid,'method':method,'params':params});end=min(deadline,time.monotonic()+seconds)
        while time.monotonic()<end:
            msg=receive(end-time.monotonic())
            if msg and msg.get('id')==rid:
                if 'error' in msg:raise NativeFailure('官方账号接口拒绝请求',kind='permanent' if not dispatched else 'unknown',usage=usage,remote=remote,dispatched=dispatched)
                return msg.get('result') or {}
        raise NativeFailure('官方账号接口等待超时',kind='unknown' if dispatched else 'transient',usage=usage,remote=remote,dispatched=dispatched)
    def ended_failure(turn):
        code=(turn.get('error') or {}).get('codexErrorInfo') or ''
        encoded=json.dumps(code).lower()
        kind='permanent' if any(x in encoded for x in ('unauthorized','usagelimitexceeded','badrequest','contextwindowexceeded')) else 'transient'
        raise NativeFailure('官方账号请求已明确结束但未完成',kind=kind,usage=usage,remote=remote,dispatched=True)
    def completed_text():
        nonlocal usage
        usage=terminal.get('usage') or usage
        if terminal.get('status')!='completed':ended_failure(terminal)
        for item in terminal.get('items') or []:
            if item.get('type')=='agentMessage' and item.get('text'):
                key=item.get('id',str(len(texts)));texts[key]=item['text'];last_phase[key]=item.get('phase')
        final=[t for key,t in texts.items() if last_phase.get(key)=='final_answer'] or list(texts.values())[-1:]
        if not final:raise NativeFailure('官方账号已结束但无最终文本',kind='permanent',usage=usage,remote=remote,dispatched=True)
        if len(final[-1].encode())>128000:raise NativeFailure('官方最终文本超过128KB上限',kind='permanent',usage=usage,remote=remote,dispatched=True)
        on_update(remote=remote,usage=usage,complete=True)
        return final[-1],usage,remote
    def cleanup():
        if process:
            if process.poll() is None:
                process.terminate()
                try:process.wait(timeout=3)
                except subprocess.TimeoutExpired:process.kill();process.wait(timeout=3)
            if process.stdin and not process.stdin.closed:process.stdin.close()
            if process.stdout and not process.stdout.closed:process.stdout.close()
    @contextmanager
    def workspace():
        with tempfile.TemporaryDirectory(prefix='stock-native-text-') as cwd:
            try:yield cwd
            finally:cleanup()
    try:
        with workspace() as cwd:
            process=process_factory(official_command(executable,'app-server','--stdio'),cwd=cwd,env=official_environment(),stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,
              text=True,encoding='utf-8',bufsize=1,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            def reader():
                try:
                    while line:=process.stdout.readline(1_000_001):
                        if len(line)>1_000_000:break
                        try:value=json.loads(line)
                        except ValueError:continue
                        events.put(value,timeout=2)
                except (OSError,ValueError,queue.Full):pass
            thread=threading.Thread(target=reader,daemon=True);thread.start()
            request('initialize',{'clientInfo':{'name':'stock_research_native','version':'1.0.0'},'capabilities':{'experimentalApi':True}})
            send({'method':'initialized','params':{}})
            if catalog:
                return request('model/list',{'limit':100,'includeHidden':False})
            config={'model_provider':'stock-official','model_providers.stock-official':{'name':'Stock official account','wire_api':'responses','requires_openai_auth':True,'request_max_retries':0,'stream_max_retries':0}}
            thread_result=request('thread/start',{'cwd':cwd,'runtimeWorkspaceRoots':[cwd],'model':model['model_identifier'],'allowProviderModelFallback':False,
              'approvalPolicy':'never','sandbox':'read-only','environments':[],'ephemeral':True,'baseInstructions':system,'config':config,'serviceName':'stock-research'})
            remote={'thread_id':thread_result['thread']['id'],'turn_id':None};on_update(remote=remote,usage=usage,complete=False)
            if stopped():raise NativeFailure('研究已停止，官方通道尚未发送模型请求',remote=remote)
            dispatched=True;on_update(remote=remote,dispatched=True,usage=usage,complete=False)
            params={'threadId':remote['thread_id'],'model':model['model_identifier'],'input':[{'type':'text','text':payload}],
              'environments':[]}
            if model.get('reasoning_effort'):params['effort']=model['reasoning_effort']
            started=request('turn/start',params)
            remote['turn_id']=started['turn']['id'];on_update(remote=remote,usage=usage,complete=False)
            while time.monotonic()<deadline and not stopped():
                if terminal:return completed_text()
                receive(deadline-time.monotonic())
            # An interrupt ACK is not a terminal event. Observe turn/completed.
            try:request('turn/interrupt',{'threadId':remote['thread_id'],'turnId':remote['turn_id']},seconds=3)
            except NativeFailure:pass
            end=time.monotonic()+interrupt_grace
            while not terminal and time.monotonic()<end:receive(end-time.monotonic())
            if terminal:return completed_text()
            raise NativeFailure('官方请求已中止等待，远端终态仍未知',kind='unknown',usage=usage,remote=remote,dispatched=True)
    finally:
        cleanup()
