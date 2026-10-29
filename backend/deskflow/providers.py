import asyncio
import json
import math
from urllib.parse import urlparse
import httpx
from .security import decrypt, redact


class ModelError(Exception):
    pass


def validate_profile(profile, kind='chat'):
    url = urlparse(profile.get('base_url', ''))
    if url.scheme not in {'http', 'https'} or not url.hostname or url.username or url.password or url.query or url.fragment:
        raise ValueError('Base URL 必须是无凭据、无查询参数的 HTTP(S) 地址')
    if not profile.get('model', '').strip():
        raise ValueError('请填写模型 ID')
    if not profile.get('encrypted_key'):
        raise ValueError('请填写并保存独立 API Key')


def headers(profile):
    return {'Authorization': f"Bearer {decrypt(profile['encrypted_key'])}", 'Content-Type': 'application/json'}


def http_error(response):
    labels = {400: '请求参数或模型不兼容', 401: '认证失败', 403: '模型访问被拒绝', 404: '接口或模型不存在', 429: '请求限流'}
    return ModelError(f"{labels.get(response.status_code, '模型请求失败')} HTTP {response.status_code}: {redact(response.text)}")


async def chat(profile, messages, tools=None, on_delta=None):
    try:
        async with asyncio.timeout(min(180, float(profile.get('timeout', 45)) + 15)):
            return await _chat(profile, messages, tools, on_delta)
    except TimeoutError:
        raise ModelError('模型请求达到总时限，请检查接口后继续')


async def _chat(profile, messages, tools=None, on_delta=None):
    validate_profile(profile)
    body = {'model': profile['model'], 'messages': messages, 'stream': True, 'stream_options': {'include_usage': True},
            'max_tokens': int(profile.get('max_tokens', 1500))}
    if tools:
        body['tools'] = tools
        body['tool_choice'] = 'auto'
    text_parts, calls, usage = [], {}, {}
    timeout = httpx.Timeout(float(profile.get('timeout', 45)), connect=10)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream('POST', profile['base_url'].rstrip('/') + '/chat/completions', headers=headers(profile), json=body) as response:
                if response.is_error:
                    await response.aread()
                    raise http_error(response)
                async for line in response.aiter_lines():
                    if not line.startswith('data:'):
                        continue
                    raw = line[5:].strip()
                    if raw == '[DONE]':
                        break
                    try:
                        item = json.loads(raw)
                    except ValueError:
                        raise ModelError('模型流式响应不是有效 JSON')
                    if item.get('error'):
                        raise ModelError(redact(item['error']))
                    if item.get('usage'):
                        usage = item['usage']
                    for choice in item.get('choices', []):
                        delta = choice.get('delta', {})
                        if delta.get('content'):
                            text_parts.append(delta['content'])
                            if on_delta:
                                await on_delta(delta['content'])
                        for tc in delta.get('tool_calls', []):
                            current = calls.setdefault(tc.get('index', 0), {'id': '', 'type': 'function', 'function': {'name': '', 'arguments': ''}})
                            if tc.get('id'):
                                current['id'] = tc['id']
                            for field in ('name', 'arguments'):
                                current['function'][field] += tc.get('function', {}).get(field, '')
    except httpx.TimeoutException:
        raise ModelError('模型请求超时，可检查接口后继续运行')
    except httpx.HTTPError:
        raise ModelError('无法连接模型服务，请检查地址和网络')
    if not text_parts and not calls:
        raise ModelError('模型返回空结果；请检查流式输出与 Tool Calling 支持')
    return {'role': 'assistant', 'content': ''.join(text_parts), **({'tool_calls': list(calls.values())} if calls else {})}, usage


async def embed(profile, texts):
    validate_profile(profile, 'embedding')
    if not 1 <= len(texts) <= 10:
        raise ValueError('每批向量输入必须为 1–10 条')
    try:
        async with httpx.AsyncClient(timeout=float(profile.get('timeout', 45))) as client:
            response = await client.post(profile['base_url'].rstrip('/') + '/embeddings', headers=headers(profile),
                                         json={'model': profile['model'], 'input': texts, 'dimensions': 1024, 'encoding_format': 'float'})
        if response.is_error:
            raise http_error(response)
        items = sorted(response.json()['data'], key=lambda i: i['index'])
        vectors = [item['embedding'] for item in items]
        if len(vectors) != len(texts) or [x['index'] for x in items] != list(range(len(texts))):
            raise ValueError('返回向量数量或索引不匹配')
        if any(len(v) != 1024 or any(type(n) not in (int, float) or not math.isfinite(n) for n in v) or not any(v) for v in vectors):
            raise ValueError('向量必须为 1024 维有限数值且不能是零向量')
        return vectors
    except httpx.TimeoutException:
        raise ModelError('向量接口超时')
    except httpx.HTTPError:
        raise ModelError('无法连接向量接口')
    except (ValueError, KeyError, TypeError) as exc:
        raise ModelError(f'向量响应格式错误：{redact(exc)}')


def json_result(text):
    value = text.strip()
    if value.startswith('```'):
        value = value.split('\n', 1)[-1].rsplit('```', 1)[0].strip()
    try:
        result = json.loads(value)
        if not isinstance(result, dict):
            raise ValueError('not object')
        return result
    except (ValueError, TypeError):
        raise ModelError('模型未返回约定的结构化方案，请调整模型或重试；未执行任何新方案')
