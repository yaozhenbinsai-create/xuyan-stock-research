"""Explicit text protocols; never guess a different endpoint after dispatch."""
from urllib.parse import urlsplit, urlunsplit, quote


def base_url(value, protocol):
    parts = urlsplit(value.rstrip('/'))
    if (parts.scheme not in ('http', 'https') or not parts.hostname
            or parts.username or parts.password or parts.query or parts.fragment):
        raise ValueError('接口地址无效，不能包含密钥或查询参数')
    path = parts.path.rstrip('/')
    terminal=False
    for suffix in ('/chat/completions', '/responses', '/messages', '/models'):
        if path.endswith(suffix):
            path = path[:-len(suffix)]
            terminal=True
            break
    if not path:
        path = '/v1beta' if protocol == 'gemini' else '/v1'
    elif protocol == 'anthropic' and not path.endswith('/v1') and not terminal:
        # Preserve the existing Anthropic gateway contract: a configured gateway
        # prefix is followed by /v1. Explicit /messages endpoints stay explicit.
        path += '/v1'
    return urlunsplit((parts.scheme, parts.netloc, path, '', ''))


def request(model, key, system, user, cap):
    protocol = model.get('protocol', 'openai')
    base = base_url(model['base_url'], protocol)
    headers = {'Authorization': 'Bearer ' + key, 'Content-Type': 'application/json'}
    if protocol in ('openai', 'xai'):
        body = {'model': model['model_identifier'], 'messages': [
            {'role': 'system', 'content': system}, {'role': 'user', 'content': user}],
            'response_format': {'type': 'json_object'}, 'stream': True,
            'stream_options': {'include_usage': True}}
        if cap is not None:
            body['max_completion_tokens'] = cap
        if model.get('reasoning_effort'):
            body['reasoning_effort'] = model['reasoning_effort']
        return base + '/chat/completions', headers, body
    if protocol == 'responses':
        body = {'model': model['model_identifier'], 'instructions': system, 'input': user,
                'store': False, 'text': {'format': {'type': 'json_object'}}}
        if model.get('recoverable_responses'):
            body.update(background=True, store=True)
        if cap is not None:
            body['max_output_tokens'] = cap
        if model.get('reasoning_effort'):
            body['reasoning'] = {'effort': model['reasoning_effort']}
        return base + '/responses', headers, body
    if protocol == 'gemini':
        model_id = model['model_identifier'].removeprefix('models/')
        if not model_id or '/' in model_id:
            raise ValueError('Gemini Model ID 无效')
        body = {'systemInstruction': {'parts': [{'text': system}]},
                'contents': [{'role': 'user', 'parts': [{'text': user}]}],
                'generationConfig': {'responseMimeType': 'application/json'}}
        if cap is not None:
            body['generationConfig']['maxOutputTokens'] = cap
        return (base + '/models/' + quote(model_id, safe='-._') + ':generateContent',
                {'x-goog-api-key': key, 'Content-Type': 'application/json'}, body)
    if protocol == 'anthropic':
        return base + '/messages', {'x-api-key': key, 'anthropic-version': '2023-06-01',
                                   'Content-Type': 'application/json'}, {
            'model': model['model_identifier'], 'system': system,
            'messages': [{'role': 'user', 'content': user}], 'max_tokens': cap or 8192}
    raise ValueError('不支持的文本接口协议')


def numeric_usage(value, depth=0):
    """Only measured counters can enter public receipts, never provider-echoed text."""
    if not isinstance(value, dict) or depth > 3:
        return None
    allowed={'input_tokens','output_tokens','total_tokens','prompt_tokens','completion_tokens',
             'inputTokens','outputTokens','totalTokens','cachedInputTokens','reasoningOutputTokens',
             'promptTokenCount','candidatesTokenCount','totalTokenCount','cachedContentTokenCount','thoughtsTokenCount',
             'cache_read_input_tokens','cache_creation_input_tokens','prompt_tokens_details',
             'input_tokens_details','completion_tokens_details','output_tokens_details',
             'cached_tokens','reasoning_tokens'}
    return {k: v if type(v) is int and v >= 0 else numeric_usage(v, depth+1)
            for k, v in value.items() if k in allowed
            and (type(v) is int and v >= 0 or isinstance(v, dict))}


def gemini_result(result):
    candidates = result.get('candidates') or []
    candidate = candidates[0] if candidates else {}
    if candidate.get('finishReason') != 'STOP':
        raise ValueError('Gemini 未返回完整文本')
    text = ''.join(p.get('text', '') for p in candidate.get('content', {}).get('parts', [])
                   if isinstance(p.get('text'), str) and not p.get('thought'))
    if not text.strip():
        raise ValueError('Gemini 没有返回可读正文')
    return text
