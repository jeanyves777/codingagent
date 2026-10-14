"""Bounded Ollama streaming; activity measures output without exposing generated contents."""
import asyncio
import json
import time
import httpx
from . import accounting


async def chat(client, url, body, request, first_timeout, idle_timeout, total_timeout):
    started = last_output = last_report = time.monotonic()
    chunks = characters = 0
    first_output = None
    content, thinking, calls = [], [], []
    payload = None
    body = {**body, 'stream': True}
    try:
        async with asyncio.timeout(total_timeout):
            async with client.stream('POST', url, json=body,
                    timeout=httpx.Timeout(connect=10, read=max(first_timeout, idle_timeout), write=30, pool=10)) as response:
                response.raise_for_status()
                lines = response.aiter_lines().__aiter__()
                while True:
                    budget = (first_timeout if chunks == 0 else idle_timeout) - (time.monotonic() - last_output)
                    if budget <= 0:
                        raise TimeoutError('Model output deadline exceeded')
                    try:
                        line = await asyncio.wait_for(anext(lines), budget)
                    except StopAsyncIteration:
                        raise httpx.ReadError('Ollama closed its stream without a completed response')
                    if not line:
                        continue
                    part = json.loads(line)
                    if part.get('error'):
                        raise httpx.ReadError('Ollama generation failed: ' + str(part['error'])[:300])
                    message = part.get('message') or {}
                    text, reasoning = message.get('content') or '', message.get('thinking') or ''
                    tool_calls = message.get('tool_calls') or []
                    content.append(text); thinking.append(reasoning); calls.extend(tool_calls)
                    if text or reasoning or tool_calls:
                        chunks += 1
                        characters += len(text) + len(reasoning) + (len(json.dumps(tool_calls)) if tool_calls else 0)
                        last_output = time.monotonic()
                        if first_output is None:
                            first_output = last_output
                        request.observed = {'chunks': chunks, 'characters': characters, 'last_output': last_output}
                        if characters > 200000:
                            raise ValueError('Model response exceeded the character budget; use smaller edits')
                        if chunks == 1 or last_output - last_report >= 2:
                            last_report = last_output
                            accounting.emit('model_progress', status='RUNNING', agent=request.agent,
                                provider='ollama', model=body['model'], duration=last_output-started,
                                summary=f'Received {chunks} output chunks, {characters} characters' +
                                        (f'; {characters / (last_output-first_output):.1f} characters/s' if last_output-first_output >= 1 else ''),
                                data={'chunks': chunks, 'characters': characters})
                    if part.get('done'):
                        payload = part
                        break
        if payload.get('done_reason') == 'length':
            raise ValueError('Model output limit reached. Use bounded edits instead of full-file replacements.')
        payload['message'] = {'role': 'assistant', 'content': ''.join(content), 'tool_calls': calls}
        if thinking:
            payload['message']['thinking'] = ''.join(thinking)
        accounting.emit('model_progress', status='COMPLETED', agent=request.agent, provider='ollama',
            model=body['model'], summary=f"Model response completed: {payload.get('eval_count', 'unknown')} generated tokens",
            data={key: payload[key] for key in ('eval_count', 'prompt_eval_count', 'load_duration', 'eval_duration') if key in payload})
        return httpx.Response(200, json=payload, request=httpx.Request('POST', url))
    except (TimeoutError, httpx.ReadTimeout) as error:
        quiet = time.monotonic() - last_output
        if time.monotonic() - started >= total_timeout:
            raise httpx.ReadTimeout(f'Ollama total request limit ({total_timeout}s) reached after '
                f'{chunks} output chunks. Reduce the workload or use smaller edits.') from error
        raise httpx.ReadTimeout(f'Ollama {body["model"]}: no output for {quiet:.0f}s '
            f'({chunks} chunks, {characters} characters received). Request stopped; inspect model readiness or reduce the workload.') from error
