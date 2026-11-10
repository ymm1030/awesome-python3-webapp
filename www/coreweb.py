import functools
import asyncio
import os
import inspect
import logging
import json
from urllib import parse
from aiohttp import web
from apis import APIError


def get(path):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kw):
            return func(*args, **kw)
        wrapper.__method__ = 'GET'
        wrapper.__route__ = path
        return wrapper
    return decorator


def post(path):
    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args, **kw):
            return func(*args, **kw)
        wrapper.__method__ = 'POST'
        wrapper.__route__ = path
        return wrapper
    return decorator


def get_required_kw_args(fn):
    args = []
    params = inspect.signature(fn).parameters
    for name, param in params.items():
        if param.kind == inspect.Parameter.KEYWORD_ONLY and param.default == inspect.Parameter.empty:
            args.append(name)
    return tuple(args)


def get_named_kw_args(fn):
    args = []
    params = inspect.signature(fn).parameters
    for name, param in params.items():
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            args.append(name)
    return tuple(args)


def has_named_kw_args(fn):
    params = inspect.signature(fn).parameters
    for name, param in params.items():
        if param.kind == inspect.Parameter.KEYWORD_ONLY:
            return True


def has_var_kw_arg(fn):
    params = inspect.signature(fn).parameters
    for name, param in params.items():
        if param.kind == inspect.Parameter.VAR_KEYWORD:
            return True


def has_request_arg(fn):
    sig = inspect.signature(fn)
    params = sig.parameters
    found = False
    for name, param in params.items():
        if name == 'request':
            found = True
            continue
        if found and (param.kind != inspect.Parameter.VAR_POSITIONAL and param.kind != inspect.Parameter.KEYWORD_ONLY and param.kind != inspect.Parameter.VAR_KEYWORD):
            raise ValueError('request parameter must be the last named parameter in function: %s%s' % (fn.__name__, str(sig)))
    return found


def _default(o):
    """json.dumps 的 default，把模型对象转成 dict"""
    if hasattr(o, '__dict__'):
        return o.__dict__
    return str(o)


def _dumps(obj):
    return json.dumps(obj, ensure_ascii=False, default=_default)


def _build_response(app, r, request=None):
    """把 handler 的返回值统一转换成 web.Response"""
    # 已经是 Response 就直接返回
    if isinstance(r, web.StreamResponse):
        return r

    # bytes
    if isinstance(r, bytes):
        return web.Response(body=r, content_type='application/octet-stream')

    # 字符串
    if isinstance(r, str):
        if r.startswith('redirect:'):
            return web.HTTPFound(r[9:])
        return web.Response(text=r, content_type='text/html')

    # dict：区分模板渲染和 JSON 响应
    if isinstance(r, dict):
        template = r.get('__template__')
        if template is None:
            return web.json_response(r, dumps=_dumps)
        env = app['__templating__']
        ctx = dict(r)
        if request is not None:
            ctx['__user__'] = getattr(request, '__user__', None)
        body = env.get_template(template).render(**ctx)
        return web.Response(text=body, content_type='text/html')

    # 状态码
    if isinstance(r, int) and 100 <= r < 600:
        return web.Response(status=r)

    # (status, text) 元组
    if isinstance(r, tuple) and len(r) == 2:
        t, m = r
        if isinstance(t, int) and 100 <= t < 600:
            return web.Response(status=t, text=str(m))

    # 模型对象（User / Blog / Comment 等）
    if hasattr(r, '__dict__'):
        return web.json_response(r.__dict__, dumps=_dumps)

    return web.Response(text=str(r))


class RequestHandler(object):
    def __init__(self, app, fn):
        self._app = app
        self._func = fn
        self._has_request_arg = has_request_arg(fn)
        self._has_var_kw_arg = has_var_kw_arg(fn)
        self._has_named_kw_args = has_named_kw_args(fn)
        self._named_kw_args = get_named_kw_args(fn)
        self._require_kw_args = get_required_kw_args(fn)

    async def __call__(self, request):
        kw = None
        if self._has_var_kw_arg or self._has_named_kw_args or self._require_kw_args:
            if request.method == 'POST':
                if not request.content_type:
                    return web.HTTPBadRequest(text='Missing Content-Type.')
                ct = request.content_type.lower()
                if ct.startswith('application/json'):
                    params = await request.json()
                    if not isinstance(params, dict):
                        return web.HTTPBadRequest(text='JSON body must be object.')
                    kw = params
                elif ct.startswith('application/x-www-form-urlencoded') or ct.startswith('multipart/form-data'):
                    params = await request.post()
                    kw = dict(**params)
                else:
                    return web.HTTPBadRequest(text='Unsupported Content-Type:%s' % request.content_type)
            if request.method == 'GET':
                qs = request.query_string
                if qs:
                    kw = dict()
                    for k, v in parse.parse_qs(qs, True).items():
                        kw[k] = v[0]
        if kw is None:
            kw = dict(**request.match_info)
        else:
            if not self._has_var_kw_arg and self._named_kw_args:
                copy = dict()
                for name in self._named_kw_args:
                    if name in kw:
                        copy[name] = kw[name]
                kw = copy
            for k, v in request.match_info.items():
                if k in kw:
                    logging.warning('Duplicate arg name in named arg and kw args: %s' % k)
                kw[k] = v

        if self._has_request_arg:
            kw['request'] = request

        if self._require_kw_args:
            for name in self._require_kw_args:
                if name not in kw:
                    return web.HTTPBadRequest(text='Missing argument: %s' % name)
        logging.info('call with args: %s' % str(kw))
        try:
            r = await self._func(**kw)
            return _build_response(self._app, r, request)          # 传 request，注入 __user__
        except APIError as e:
            return _build_response(self._app, dict(error=e.error, data=e.data, message=e.message), request)


def add_static(app):
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static')
    app.router.add_static('/static', path)
    logging.info('add static %s ==> %s' % ('/static/', path))


def add_route(app, fn):
    method = getattr(fn, '__method__', None)
    path = getattr(fn, '__route__', None)
    if path is None or method is None:
        raise ValueError('@get or @post not defined in %s.' % str(fn))
    # 统一包装成协程（兼容普通函数、生成器函数、async def）
    if not asyncio.iscoroutinefunction(fn):
        fn = asyncio.coroutine(fn)
    logging.info('add route %s %s ==> %s(%s)' % (method, path, fn.__name__, ','.join(inspect.signature(fn).parameters.keys())))
    app.router.add_route(method, path, RequestHandler(app, fn))


def add_routes(app, module_name):
    n = module_name.rfind('.')
    if n == (-1):
        mod = __import__(module_name, globals(), locals())
    else:
        name = module_name[n+1:]
        mod = getattr(__import__(module_name[:n], globals(), locals(), [name]), name)
    for attr in dir(mod):
        if attr.startswith('_'):
            continue
        fn = getattr(mod, attr)
        if callable(fn):
            method = getattr(fn, '__method__', None)
            path = getattr(fn, '__route__', None)
            if method and path:
                add_route(app, fn)