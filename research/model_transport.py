"""Single transport attempt; the rotation coordinator selects the next channel."""
from __future__ import annotations
import asyncio
import hashlib
import json
import os
import re
import threading
import time
import uuid
from urllib.parse import urlsplit
import httpx
from foundation import configuration, database as store, security_secrets
from research import protocols, state_store, usage as token_usage

MAX_STREAM_ANSWER_BYTES = 128_000
PERMANENT_STREAM_ERRORS = {'invalid_api_key', 'unauthorized', 'insufficient_quota', 'context_length_exceeded',
                           'invalid_request_error', 'content_policy_violation', 'cyber_policy_blocked'}
TEMPORARY_STREAM_ERRORS = {'rate_limit_exceeded', 'rate_limit_error', 'overloaded_error', 'server_error',
                           'server_overloaded', 'service_unavailable', 'internal_server_error', 'timeout'}


class _CheckpointWriter:
    """Coalesce diagnostic snapshots without putting disk I/O in the SSE reader.

    The request row is saved synchronously before POST. These snapshots contain
    counters and identifiers only, and a late writer cannot replace a terminal
    receipt. At most one write and one newer snapshot exist per request.
    """
    def __init__(self,call_id):
        self.call_id=call_id;self._db=store.db
        self._condition=threading.Condition();self._pending=None;self._closed=False
        self.errors=0
        self._thread=threading.Thread(target=self._run,name='model-checkpoint-'+call_id,daemon=True)
        self._thread.start()

    def submit(self,data):
        value=store.dumps(data)
        with self._condition:
            if self._closed:return
            self._pending=value
            self._condition.notify()

    def close(self):
        # The synchronous terminal write includes the newest in-memory snapshot.
        # Do not drain stale partial snapshots in front of it.
        with self._condition:
            self._closed=True;self._pending=None;self._condition.notify()

    def _run(self):
        while True:
            with self._condition:
                while self._pending is None and not self._closed:self._condition.wait()
                if self._closed:return
                value=self._pending;self._pending=None
            try:
                with self._db() as conn:
                    conn.execute("UPDATE research_calls SET data=%s WHERE id=%s AND status='sent'",
                                 (value,self.call_id))
                    state_store.sync_call(conn,self.call_id)
            except Exception:
                # The final receipt remains mandatory. A diagnostic write error
                # must not discard already-arriving remote terminal frames.
                self.errors+=1


async def _sse_events(response):
    """Dispatch complete SSE events, including multi-line data and an EOF tail."""
    event='';lines=[]
    async for line in response.aiter_lines():
        if not line:
            if lines:yield event,'\n'.join(lines)
            event='';lines=[]
            continue
        if line.startswith(':'):continue
        field,separator,value=line.partition(':')
        if separator and value.startswith(' '):value=value[1:]
        if field=='event':event=value
        elif field=='data':lines.append(value)
    if lines:yield event,'\n'.join(lines)

class ModelFailure(Exception):
    def __init__(self,category,message,*,unknown=False,usage=None,retryable=False):
        super().__init__(message);self.category=category;self.unknown=unknown;self.usage=usage;self.retryable=retryable


def _usage_fields(raw, *, previous=None, complete=False, protocol=None):
    """A missing receipt cannot erase counters already measured for this call."""
    current=token_usage.fields(protocols.numeric_usage(raw),complete=complete,protocol=protocol)
    if any(value is not None for value in current['usage_normalized'].values()):
        return current
    previous=previous or {}
    known=previous.get('usage_normalized') or token_usage.normalize(previous.get('usage'),protocol)
    if not any(value is not None for value in known.values()):
        return current
    return {'usage':previous.get('usage'),'usage_normalized':dict(known),
            'usage_state':'complete' if previous.get('usage_state')=='complete' else 'partial'}

def freeze_plan():
    plans=[p for p in configuration.list_items('plans') if p.get('enabled')]
    if not plans:raise ModelFailure('configuration','没有启用的文本方案，请到 AI 设置选择方案')
    plan=plans[0]
    models={m['id']:m for m in configuration.list_items('models')}
    chain=[models[i] for i in plan.get('model_ids',[]) if i in models and models[i].get('enabled')]
    if not chain:raise ModelFailure('configuration','方案中没有启用的模型')
    chosen=chain[0]
    request_timeout=plan.get('request_timeout_seconds')
    def freeze(m):return {'model':{k:v for k,v in m.items() if k not in ('has_api_key','configured','api_key_env')},'config_hash':hashlib.sha256(store.dumps(m).encode()).hexdigest()}
    roles=[{'id':a['id'],'name':a['name'],'role':a.get('role',''),'responsibilities':a.get('responsibilities','')[:600]}
           for a in configuration.list_items('agents') if a.get('enabled') and plan['id'] in a.get('plan_ids',[])][:12]
    return {'plan_id':plan['id'],'plan_name':plan['name'],'saved_order':plan['model_ids'],
      'request_timeout_seconds':request_timeout,
      **freeze(chosen),'chain':[freeze(m) for m in chain],'research_roles':roles,
      'budget':{'max_requests':None,'max_completion_tokens':chosen.get('max_output_tokens'),'timeout_seconds':request_timeout or chosen.get('timeout_seconds') or 500},
      'rotation_policy':{'mode':'until_success_or_stop','cycles_per_visit':3,'cooldown_start_seconds':30,'cooldown_max_seconds':300},
      'retry_policy':'明确失败后按保存顺序切换，临时失败可参与下轮。各通道遵守原签发截止并预留校验时间。缺少远端终态时保留请求编号和用量。'}

def select_model(frozen,index):
    value=frozen['chain'][index];model=value['model']
    return {**frozen,**value,'budget':{**frozen.get('budget',{}),'max_completion_tokens':model.get('max_output_tokens'),
                                     'timeout_seconds':frozen.get('request_timeout_seconds') or model.get('timeout_seconds') or 500}}

def _secret(model_id,expected_hash):
    with store.db(readonly=True) as c:
        row=c.execute('SELECT * FROM models WHERE id=%s',(model_id,)).fetchone()
        if not row:raise ModelFailure('configuration','模型已删除')
        public=configuration.item(row,'models')
        if hashlib.sha256(store.dumps(public).encode()).hexdigest()!=expected_hash:raise ModelFailure('configuration','模型配置已改变，请重新冻结本轮研究')
        config=json.loads(row['data'])
        if row['secret']:
            return security_secrets._backend(store.database_path()).decrypt(row['secret'],security_secrets._context(store.database_path())).decode()
        return os.environ.get(config.get('api_key_env',''),'')

def complete(run_id,frozen,system,payload,*,probe_key=None):
    model=frozen['model'];protocol=model.get('protocol','openai')
    if protocol=='codex':return _native_complete(run_id,frozen,system,payload)
    if protocol not in ('openai','responses','anthropic','gemini','xai'):
        raise ModelFailure('unsupported_protocol','当前通道的协议尚未接通，未发出模型请求')
    key=probe_key if run_id is None else _secret(model['id'],frozen['config_hash'])
    if not key:raise ModelFailure('configuration','当前通道没有可用密钥')
    user=store.dumps(payload)
    if len(user.encode())>220000:raise ModelFailure('input_budget','研究输入超过220KB上限，未发起请求')
    try:
        base=protocols.base_url(model['base_url'],protocol)
        url,headers,body=protocols.request(model,key,system,user,frozen['budget']['max_completion_tokens'])
    except ValueError:
        raise ModelFailure('configuration','接口地址或原生协议配置无效，未发起请求') from None
    if protocol=='anthropic':
        frozen={**frozen,'budget':{**frozen['budget'],'max_completion_tokens':body['max_tokens']}}
    call_id=uuid.uuid4().hex[:16]
    request_hash=hashlib.sha256(store.dumps([system,payload,frozen['config_hash']]).encode()).hexdigest()
    receipt = {'protocol':protocol, 'input_sha256':request_hash, 'config_hash':frozen['config_hash'], 'cost':None, 'budget':frozen['budget'],
               **frozen.get('attempt_context',{})}
    with store.db() as c:
        c.execute('INSERT INTO research_calls(id,run_id,model_id,started,status,data) VALUES(%s,%s,%s,%s,%s,%s)',
          (call_id,run_id or call_id,model['id'],store.now(),'sent',store.dumps({**receipt,'usage':None})))
        state_store.sync_call(c,call_id)
    def provider_reference(value):
        value=str(value or '')
        return value[:160] if key not in value else ''
    def safe_usage(value,depth=0):
        return protocols.numeric_usage(value,depth)
    content=bytearray();answer=bytearray();status=None;request_id=None;provider_id=None;usage=None;stream_model=None
    chat_stream=False;oversized=False;finish=None;done=False;stream_error=None;usage_complete=True
    progress={'received_events':0,'content_bytes':0,'reasoning_bytes':0,'data_bytes':0,
              'first_event_at':None,'last_event_at':None,'first_event_ms':None,'last_event_ms':None,
              'last_content_ms':None,'finish_reason':None,'done_seen':False}
    checkpoint_writer=None;last_checkpoint=0

    def remote_metadata():
        return {'provider_request_id':provider_reference(request_id),
                'completion_id':provider_reference(provider_id),
                'stream_progress':{**progress,'checkpoint_errors_observed':checkpoint_writer.errors if checkpoint_writer else 0}}

    def checkpoint(*,force=False):
        nonlocal last_checkpoint
        moment=time.monotonic()
        if not force and moment-last_checkpoint<2:return
        last_checkpoint=moment
        checkpoint_writer.submit({**receipt,**remote_metadata(),
                                  **token_usage.fields(usage,complete=False,protocol=protocol)})

    async def exchange():
        nonlocal status, request_id, provider_id, usage, chat_stream, oversized, finish, done, stream_error, stream_model
        seconds = frozen['budget']['timeout_seconds']
        timeout = httpx.Timeout(seconds, connect=min(15,seconds))
        try:
            async with asyncio.timeout(seconds), httpx.AsyncClient(timeout=timeout,follow_redirects=False,trust_env=False) as client:
                async with client.stream('POST',url,headers=headers,json=body) as response:
                    status=response.status_code
                    request_id=response.headers.get('x-request-id') or response.headers.get('request-id')
                    if request_id:checkpoint(force=True)
                    chat_stream = protocol in ('openai','xai') and status==200 and 'text/event-stream' in response.headers.get('content-type','')
                    if chat_stream:
                        async for event,value in _sse_events(response):
                            elapsed=round((time.monotonic()-request_started)*1000)
                            progress.update(received_events=progress['received_events']+1,
                                            data_bytes=progress['data_bytes']+len(value.encode()),
                                            last_event_at=store.now(),last_event_ms=elapsed)
                            if progress['first_event_at'] is None:
                                progress.update(first_event_at=progress['last_event_at'],first_event_ms=elapsed)
                            if value.strip()=='[DONE]':
                                done=True;progress['done_seen']=True;break
                            try:chunk=json.loads(value)
                            except ValueError:raise ModelFailure('invalid_stream','模型流式内容无法解析，未签发报告',unknown=True,usage=usage) from None
                            if not isinstance(chunk,dict):
                                raise ModelFailure('invalid_stream','模型流式结构无法解析，保留未知',unknown=True,usage=usage)
                            first_id = not provider_id and bool(chunk.get('id'))
                            incoming_id=chunk.get('id')
                            if isinstance(incoming_id,str) and incoming_id:
                                if provider_id and incoming_id!=provider_id:
                                    raise ModelFailure('stream_identity','模型流的完成编号发生变化，保留未知，不混合不同响应',unknown=True,usage=usage)
                                provider_id=provider_id or incoming_id
                            if isinstance(chunk.get('model'),str):stream_model=chunk['model']
                            if chunk.get('usage'):
                                usage=safe_usage(chunk['usage'])
                            if first_id:checkpoint(force=True)
                            if chunk.get('error') or event=='error':
                                err=chunk.get('error',chunk)
                                codes={str(err.get(field,'')).lower() for field in ('code','type')} if isinstance(err,dict) else set()
                                permanent = bool(codes & PERMANENT_STREAM_ERRORS)
                                temporary = bool(codes & TEMPORARY_STREAM_ERRORS) and not permanent
                                retryable = temporary or not permanent and bool(codes & {'api_error','upstream_error'})
                                stream_error = ModelFailure('stream_error','服务商返回流式失败',usage=usage,retryable=retryable)
                                if event=='error' or permanent or temporary:
                                    raise stream_error
                                # Keep reading for the terminal frame and usage after an untyped error chunk.
                                continue
                            for choice in chunk.get('choices',[]):
                                delta_object=choice.get('delta',{})
                                delta=delta_object.get('content')
                                for field in ('reasoning_content','reasoning'):
                                    if isinstance(delta_object.get(field),str):
                                        progress['reasoning_bytes']+=len(delta_object[field].encode())
                                if isinstance(delta,str) and not oversized:
                                    encoded=delta.encode()
                                    progress['content_bytes']+=len(encoded);progress['last_content_ms']=elapsed
                                    if len(answer)+len(encoded)>MAX_STREAM_ANSWER_BYTES:
                                        oversized=True
                                        answer.clear()
                                    else:
                                        answer.extend(encoded)
                                if choice.get('finish_reason'):
                                    finish=choice['finish_reason'];progress['finish_reason']=finish
                            checkpoint()
                    else:
                        async for chunk in response.aiter_bytes():
                            content.extend(chunk)
                            if len(content)>1_000_000:
                                raise ModelFailure('response_size','模型响应超过可解析大小，远端终态未知',unknown=True)
        finally:
            checkpoint_writer.close()

    request_started=time.monotonic()
    checkpoint_writer=_CheckpointWriter(call_id)
    try:
        try:
            asyncio.run(exchange())
        except (TimeoutError,httpx.TransportError):
            if not finish and not done and not stream_error and status in (None,200):
                raise
            usage_complete=False
        if status!=200:
            try:rejected=json.loads(content)
            except ValueError:rejected={}
            usage=safe_usage(rejected.get('usage')) if isinstance(rejected,dict) else None
            error_code=str((rejected.get('error') or {}).get('code','')) if isinstance(rejected,dict) and isinstance(rejected.get('error'),dict) else ''
            transient=status in (408,409,429,500,502,503,504,529) and error_code not in ('insufficient_quota','usage_limit_reached')
            raise ModelFailure('http_'+str(status),'模型通道返回 HTTP '+str(status),usage=usage,retryable=transient)
        if chat_stream:
            if stream_error:
                stream_error.usage = usage
                raise stream_error
            if not done and not finish:
                raise ModelFailure('stream_interrupted','模型流在完成前中断，远端终态未知',unknown=True,usage=usage)
            if oversized:
                raise ModelFailure('answer_size','模型已结束，研究正文超过签发大小上限',usage=usage)
            result={'id':provider_id,'model':stream_model,'usage':usage,'choices':[{'message':{'content':answer.decode()},'finish_reason':finish or 'stop'}]}
        else:
            try:result=json.loads(content)
            except ValueError:raise ModelFailure('invalid_response','模型响应不是有效 JSON，未签发报告',unknown=True) from None
            # Some compatible gateways ignore stream=true and return a complete
            # Chat JSON object. Its completion identity is still independent of
            # the HTTP request header; do not lose it in the non-SSE path.
            if protocol in ('openai','xai') and isinstance(result,dict) and isinstance(result.get('id'),str):
                provider_id=result['id']
        if protocol=='responses' and model.get('recoverable_responses'):
            result=_poll_response(result,base,headers,frozen,call_id,receipt,key,request_started)
        final_usage_fields=_usage_fields(result.get('usageMetadata') if protocol=='gemini' else result.get('usage'),
                                        previous=receipt,complete=usage_complete,protocol=protocol)
        usage=final_usage_fields['usage']
        if protocol in ('openai','xai'):
            choice=(result.get('choices') or [{}])[0]
            text=choice.get('message',{}).get('content')
            finished=choice.get('finish_reason')
            if finished not in ('stop',None):raise ModelFailure('incomplete','模型输出被截断或未完成，未签发报告',usage=usage)
        elif protocol=='responses':
            text=''.join(part.get('text','') for item in result.get('output',[]) if item.get('type')=='message' for part in item.get('content',[]) if part.get('type')=='output_text')
            if result.get('status')!='completed':raise ModelFailure('incomplete','模型响应尚未完成，未签发报告',usage=usage)
        elif protocol=='gemini':
            try:text=protocols.gemini_result(result)
            except ValueError:raise ModelFailure('incomplete','Gemini 未返回完整的研究正文，未签发报告',usage=usage) from None
        else:
            text=''.join(part.get('text','') for part in result.get('content',[]) if part.get('type')=='text')
            if result.get('stop_reason') not in ('end_turn','stop_sequence'):
                raise ModelFailure('incomplete','模型输出被截断或未完成，未签发报告',usage=usage)
        if not isinstance(text,str) or not text.strip():raise ModelFailure('empty_output','模型没有返回可读研究结果，未签发报告',usage=usage)
        diagnostic={**receipt,**remote_metadata(),
                    **final_usage_fields,'output_sha256':hashlib.sha256(text.encode()).hexdigest()}
        if protocol=='responses' and model.get('recoverable_responses'):
            diagnostic.update(response_id=_response_id(result.get('id'),key),
                              recoverable=bool(_response_id(result.get('id'),key)),recovery_base=base)
        actual_model=result.get('modelVersion') if protocol=='gemini' else result.get('model')
        if isinstance(actual_model,str) and key not in actual_model:
            diagnostic['actual_model']=actual_model[:160]
        with store.db() as c:
            c.execute('UPDATE research_calls SET status=%s,finished=%s,data=%s WHERE id=%s',('completed',store.now(),store.dumps(diagnostic),call_id))
            state_store.sync_call(c,call_id)
        return text,call_id
    except (TimeoutError,httpx.TransportError) as exc:
        if isinstance(exc,(httpx.ConnectError,httpx.ConnectTimeout,httpx.PoolTimeout)):
            failure=ModelFailure('connect_failed','未能连接服务商，请检查接口地址和网络',
                                 usage={'input_tokens':0,'output_tokens':0,'total_tokens':0},retryable=True)
        elif isinstance(exc,TimeoutError):
            failure=ModelFailure('stream_deadline' if chat_stream else 'response_deadline',
                                 '模型调用达到技术超时，远端终态未知',unknown=True,usage=usage)
        else:
            failure=ModelFailure('transport_unknown','连接中断或超时，远端终态未知；已停止本轮派发',unknown=True,usage=usage)
        failure.diagnostic=type(exc).__name__
    except ModelFailure as exc:failure=exc
    except Exception:
        failure=ModelFailure('response_error','模型返回结构无法使用，未签发报告',unknown=True,usage=usage)
    recovery=getattr(failure,'recovery',{})
    with store.db() as c:
        c.execute('UPDATE research_calls SET status=%s,finished=%s,data=%s WHERE id=%s',
      ('unknown' if failure.unknown else 'failed',store.now(),store.dumps({**receipt,**remote_metadata(),**recovery,'category':failure.category,'retryable':failure.retryable,'diagnostic':getattr(failure,'diagnostic',None),'message':str(failure),**_usage_fields(failure.usage,previous=receipt,complete=not failure.unknown and getattr(failure,'usage_complete',usage_complete),protocol=protocol)}),call_id))
        state_store.sync_call(c,call_id)
    failure.call_id=call_id
    raise failure


def _response_id(value, key=''):
    return value if (isinstance(value,str) and re.fullmatch(r'[A-Za-z0-9_-]{1,160}',value)
                     and (not key or key not in value)) else None


def _response_get(client, base, response_id, headers):
    with client.stream('GET',base+'/responses/'+response_id,headers=headers) as response:
        if response.status_code != 200:
            raise ModelFailure('reconcile_http','原请求暂不可查询，保留未知；不会重新生成',unknown=True)
        raw=bytearray()
        for part in response.iter_bytes():
            raw.extend(part)
            if len(raw)>1_000_000:
                raise ModelFailure('reconcile_size','原响应超出可核对大小，保留未知',unknown=True)
    result=json.loads(raw)
    if not isinstance(result,dict) or result.get('id')!=response_id:
        raise ModelFailure('reconcile_identity','查询回执身份不符，保留原请求',unknown=True)
    return result


def _response_text(result):
    output=result.get('output')
    if not isinstance(output,list):return ''
    parts=[]
    for item in output:
        if not isinstance(item,dict) or item.get('type')!='message':continue
        content=item.get('content')
        if not isinstance(content,list):continue
        parts.extend(part['text'] for part in content if isinstance(part,dict)
                     and part.get('type')=='output_text' and isinstance(part.get('text'),str))
    return ''.join(parts)


def _poll_response(result,base,headers,frozen,call_id,receipt,key,started):
    response_id=_response_id(result.get('id'),key)
    if result.get('status')=='completed':return result
    recovery={'recoverable':bool(response_id),'response_id':response_id,'recovery_base':base}
    measured=protocols.numeric_usage(result.get('usage'))
    receipt.update(_usage_fields(measured,previous=receipt,complete=False))
    if result.get('status') in ('failed','cancelled','incomplete'):
        failure=ModelFailure('response_terminal','服务商确认原请求已结束但未完成',
                             usage=measured,retryable=result['status']!='incomplete')
        failure.usage_complete=bool(measured)
        raise failure
    def pending(category,message):
        failure=ModelFailure(category,message,unknown=True,usage=measured)
        failure.recovery=recovery
        return failure
    if not response_id:
        raise pending('response_handle_missing','服务商未返回可查询的原响应编号，保留未知')
    with store.db() as c:
        c.execute('UPDATE research_calls SET data=%s WHERE id=%s',
                  (store.dumps({**receipt,**recovery,**token_usage.fields(measured,complete=False)}),call_id))
        state_store.sync_call(c,call_id)
    deadline=started+frozen['budget']['timeout_seconds']
    try:
        while True:
            state=result.get('status')
            if state=='completed':return result
            if state in ('failed','cancelled','incomplete'):
                failure=ModelFailure('response_terminal','服务商确认原请求未完成，允许按原方案切换',
                                     usage=measured,retryable=state!='incomplete')
                failure.recovery=recovery
                failure.usage_complete=bool(protocols.numeric_usage(result.get('usage')))
                raise failure
            if state not in ('queued','in_progress'):
                raise pending('response_status','原响应状态无法核对，保留未知')
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise pending('response_pending','原响应仍在处理，已保存编号，可继续查询原请求')
            time.sleep(min(1,remaining))
            remaining=deadline-time.monotonic()
            if remaining<=0:
                raise pending('response_pending','原响应仍在处理，已保存编号，可继续查询原请求')
            with httpx.Client(timeout=min(15,remaining),follow_redirects=False,trust_env=False) as client:
                result=_response_get(client,base,response_id,headers)
            measured=protocols.numeric_usage(result.get('usage')) or measured
            receipt.update(_usage_fields(protocols.numeric_usage(result.get('usage')),previous=receipt,complete=False))
            with store.db() as c:
                c.execute('UPDATE research_calls SET data=%s WHERE id=%s',
                          (store.dumps({**receipt,**recovery,**token_usage.fields(measured,complete=False)}),call_id))
                state_store.sync_call(c,call_id)
    except ModelFailure as failure:
        failure.recovery=recovery
        raise
    except (httpx.HTTPError,ValueError,OSError):
        raise pending('reconcile_unavailable','原响应查询中断，保留编号与实际用量，不重新生成') from None


def reconcile_call(call_id):
    """GET the original Responses object only. Never create a replacement request."""
    with store.db(readonly=True) as c:
        row=c.execute('SELECT * FROM research_calls WHERE id=%s',(call_id,)).fetchone()
    if not row:return {'state':'missing'}
    receipt=json.loads(row['data'])
    if row['status'] not in ('unknown','completed'):return {'state':row['status']}
    if receipt.get('protocol')!='responses' or not receipt.get('recoverable'):
        return {'state':'unqueryable','reason':'该原请求没有已保存的可查询句柄'}
    key=_secret(row['model_id'],receipt.get('config_hash'))
    response_id=_response_id(receipt.get('response_id'),key)
    if not response_id:return {'state':'unknown'}
    try:base=protocols.base_url(receipt.get('recovery_base',''),'responses')
    except (TypeError,ValueError):return {'state':'unqueryable','reason':'原请求缺少有效的已冻结查询地址'}
    try:
        with httpx.Client(timeout=15,follow_redirects=False,trust_env=False) as client:
            result=_response_get(client,base,response_id,{'Authorization':'Bearer '+key})
    except (httpx.HTTPError,ModelFailure,ValueError):return {'state':'unknown'}
    state=result.get('status')
    if state in ('queued','in_progress'):
        measured=protocols.numeric_usage(result.get('usage'))
        if measured:
            receipt.update(token_usage.fields(measured,complete=False),reconciled_at=store.now(),remote_status=state)
            with store.db() as c:
                c.execute('UPDATE research_calls SET data=%s WHERE id=%s AND status=%s',
                          (store.dumps(receipt),call_id,'unknown'))
                state_store.sync_call(c,call_id)
        return {'state':'pending'}
    if state not in ('completed','failed','cancelled','incomplete'):return {'state':'unknown'}
    text=_response_text(result)
    if state=='completed' and (not text.strip() or len(text.encode())>MAX_STREAM_ANSWER_BYTES):
        state='incomplete'
    receipt.update(_usage_fields(result.get('usage'),previous=receipt,complete=True),
                   reconciled_at=store.now(),remote_status=state)
    status='completed' if state=='completed' else 'failed'
    with store.db() as c:
        c.execute('UPDATE research_calls SET status=%s,finished=%s,data=%s WHERE id=%s AND status=%s',
                  (status,store.now(),store.dumps(receipt),call_id,'unknown'))
        state_store.sync_call(c,call_id)
    return {'state':status,'text':text if status=='completed' else None,'call_id':call_id}

def _native_complete(run_id,frozen,system,payload):
    from research import codex_native
    model=frozen['model'];serialized=store.dumps(payload)
    if len(serialized.encode())>220000:raise ModelFailure('input_budget','研究输入超过220KB上限')
    # Verify the frozen public config without opening any shared login file.
    if run_id is not None:_secret(model['id'],frozen['config_hash'])
    call_id=uuid.uuid4().hex[:16];stamp=store.now()
    data={'protocol':'codex','input_sha256':hashlib.sha256(store.dumps([system,payload,frozen['config_hash']]).encode()).hexdigest(),'cost':None,'budget':frozen['budget'],'dispatched':False,**frozen.get('attempt_context',{}),**token_usage.fields(None,complete=False)}
    with store.db() as c:
        c.execute('INSERT INTO research_calls(id,run_id,model_id,started,status,data) VALUES(%s,%s,%s,%s,%s,%s)',(call_id,run_id or call_id,model['id'],stamp,'prepared',store.dumps(data)))
        state_store.sync_call(c,call_id)
    def update(**changes):
        complete=changes.pop('complete',False);raw=changes.pop('usage',None)
        data.update(changes)
        if raw is not None:data.update(token_usage.fields(raw,complete=complete))
        with store.db() as c:
            c.execute('UPDATE research_calls SET status=%s,data=%s WHERE id=%s',('sent' if data.get('dispatched') else 'prepared',store.dumps(data),call_id))
            state_store.sync_call(c,call_id)
    def stopped():
        if run_id is None:return False
        with store.db(readonly=True) as c:r=c.execute('SELECT data FROM research_runs WHERE id=%s',(run_id,)).fetchone()
        return not r or json.loads(r[0]).get('stop_requested',False)
    try:
        text,raw,remote=codex_native.run(model,system,serialized,frozen['budget']['timeout_seconds'],on_update=update,stopped=stopped)
        data.update(token_usage.fields(raw,complete=True),remote=remote,dispatched=True,output_sha256=hashlib.sha256(text.encode()).hexdigest())
        with store.db() as c:
            c.execute("UPDATE research_calls SET status='completed',finished=%s,data=%s WHERE id=%s",(store.now(),store.dumps(data),call_id))
            state_store.sync_call(c,call_id)
        return text,call_id
    except codex_native.NativeFailure as exc:
        data.update(token_usage.fields(exc.usage,complete=False),remote=exc.remote,dispatched=exc.dispatched,category='codex_'+exc.kind,message=str(exc))
        if not exc.dispatched:data.update(token_usage.fields({'inputTokens':0,'outputTokens':0,'totalTokens':0},complete=True))
        failure=ModelFailure('codex_'+exc.kind,str(exc),unknown=exc.kind=='unknown',usage=exc.usage,retryable=exc.kind=='transient')
    except OSError:
        data.update(category='codex_start',message='官方账号客户端未能启动')
        failure=ModelFailure('codex_start','官方账号客户端未能启动')
    except Exception:
        data.update(category='codex_protocol',message='官方账号协议处理未完成')
        failure=ModelFailure('codex_protocol','官方账号协议处理未完成',unknown=bool(data.get('dispatched')),usage=data.get('usage'))
    with store.db() as c:
        c.execute('UPDATE research_calls SET status=%s,finished=%s,data=%s WHERE id=%s',('unknown' if failure.unknown else 'failed',store.now(),store.dumps(data),call_id))
        state_store.sync_call(c,call_id)
    failure.call_id=call_id
    raise failure
