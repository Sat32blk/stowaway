"""Just enough of FastAPI's conveniences, on plain Starlette.

FastAPI and pydantic cost about 13 MB of memory at runtime, which is a lot for
a tool whose job is saving resources. Stowaway only uses a small part of them:
decorator routes, a JSON body parsed into a typed model, path and query
parameters, and HTTPException -> {"detail": ...}. This module provides those.
"""
import inspect
import json
import types
import typing

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute

__all__ = ["App", "Router", "Model", "HTTPException", "Request"]


class ValidationError(Exception):
    pass


def _coerce(tp, value, field: str):
    """Loose conversion like pydantic's default mode, for the simple types used here."""
    origin = typing.get_origin(tp)
    if origin in (typing.Union, types.UnionType):
        args = typing.get_args(tp)
        if value is None and type(None) in args:
            return None
        errors = []
        # exact type match first (so 5 stays an int in `int | str`)
        for a in args:
            if a is not type(None) and isinstance(a, type) and type(value) is a:
                return value
        for a in args:
            if a is type(None):
                continue
            try:
                return _coerce(a, value, field)
            except ValidationError as e:
                errors.append(e)
        raise errors[0] if errors else ValidationError(f"{field} is required")
    if tp is typing.Any:
        return value
    if origin is list:
        if not isinstance(value, list):
            raise ValidationError(f"{field} must be a list")
        (item,) = typing.get_args(tp) or (typing.Any,)
        return [_coerce(item, v, field) for v in value]
    if origin is dict or tp is dict:
        if not isinstance(value, dict):
            raise ValidationError(f"{field} must be an object")
        return value
    if tp is bool:
        if isinstance(value, bool):
            return value
        if value in (0, 1):
            return bool(value)
        if isinstance(value, str) and value.lower() in ("true", "false", "1", "0", "yes", "no", "on", "off"):
            return value.lower() in ("true", "1", "yes", "on")
        raise ValidationError(f"{field} must be true or false")
    if tp is int:
        if isinstance(value, bool):
            raise ValidationError(f"{field} must be a whole number")
        if isinstance(value, int):
            return value
        if isinstance(value, float) and value.is_integer():
            return int(value)
        if isinstance(value, str):
            try:
                return int(value.strip())
            except ValueError:
                pass
        raise ValidationError(f"{field} must be a whole number")
    if tp is float:
        if isinstance(value, bool):
            raise ValidationError(f"{field} must be a number")
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value.strip())
            except ValueError:
                pass
        raise ValidationError(f"{field} must be a number")
    if tp is str:
        if isinstance(value, str):
            return value
        raise ValidationError(f"{field} must be text")
    return value


class Model:
    """A request body: class attributes with type hints and defaults."""

    def __init__(self, data):
        if not isinstance(data, dict):
            raise ValidationError("the request body must be a JSON object")
        hints = typing.get_type_hints(type(self))
        self._set = set()
        for name, tp in hints.items():
            if name.startswith("_"):
                continue
            if name in data:
                setattr(self, name, _coerce(tp, data[name], name))
                self._set.add(name)
            elif hasattr(type(self), name):
                default = getattr(type(self), name)
                setattr(self, name, list(default) if isinstance(default, list) else default)
            else:
                raise ValidationError(f"{name} is required")

    def model_dump(self, exclude_none=False, exclude_unset=False):
        out = {}
        for name in typing.get_type_hints(type(self)):
            if name.startswith("_") or (exclude_unset and name not in self._set):
                continue
            v = getattr(self, name)
            if exclude_none and v is None:
                continue
            out[name] = v
        return out


def _endpoint(fn, status_code: int, dependencies):
    sig = inspect.signature(fn)
    hints = typing.get_type_hints(fn)
    params = [(name, hints.get(name, str), p.default) for name, p in sig.parameters.items()]

    async def endpoint(request: Request):
        for dep in dependencies:
            dep(request)
        kwargs = {}
        try:
            for name, tp, default in params:
                if tp is Request:
                    kwargs[name] = request
                elif isinstance(tp, type) and issubclass(tp, Model):
                    raw = await request.body()
                    try:
                        body = json.loads(raw) if raw.strip() else {}     # an empty body means "defaults"
                    except ValueError:
                        raise ValidationError("the request body isn't valid JSON")
                    kwargs[name] = tp(body)
                elif name in request.path_params:
                    kwargs[name] = _coerce(tp, request.path_params[name], name)
                elif name in request.query_params:
                    kwargs[name] = _coerce(tp, request.query_params[name], name)
                elif default is not inspect.Parameter.empty:
                    kwargs[name] = default
                else:
                    raise ValidationError(f"{name} is required")
        except ValidationError as e:
            return JSONResponse({"detail": str(e)}, 422)
        result = fn(**kwargs)
        if inspect.isawaitable(result):
            result = await result
        if isinstance(result, Response):
            return result
        if result is None and status_code == 204:
            return Response(status_code=204)
        return JSONResponse(result, status_code)
    return endpoint


class Router:
    def __init__(self, prefix: str = "", dependencies=()):
        self.prefix = prefix
        self.dependencies = list(dependencies)
        self.routes = []

    def api_route(self, path: str, methods, status_code: int = 200):
        def deco(fn):
            self.routes.append(Route(self.prefix + path, _endpoint(fn, status_code, self.dependencies),
                                     methods=list(methods)))
            return fn
        return deco

    def get(self, path, status_code=200):
        return self.api_route(path, ["GET"], status_code)

    def post(self, path, status_code=200):
        return self.api_route(path, ["POST"], status_code)

    def put(self, path, status_code=200):
        return self.api_route(path, ["PUT"], status_code)

    def patch(self, path, status_code=200):
        return self.api_route(path, ["PATCH"], status_code)

    def delete(self, path, status_code=200):
        return self.api_route(path, ["DELETE"], status_code)


async def _http_error(request, exc: HTTPException):
    return JSONResponse({"detail": exc.detail}, exc.status_code, headers=getattr(exc, "headers", None))


class App(Starlette):
    """Starlette with the decorators above. Routes match in the order they're added."""

    def __init__(self, lifespan=None):
        super().__init__(lifespan=lifespan, exception_handlers={HTTPException: _http_error})
        self._r = Router()

    def api_route(self, path, methods, status_code=200):
        def deco(fn):
            self._r.api_route(path, methods, status_code)(fn)
            self.router.routes.append(self._r.routes.pop())
            return fn
        return deco

    def get(self, path, status_code=200):
        return self.api_route(path, ["GET"], status_code)

    def post(self, path, status_code=200):
        return self.api_route(path, ["POST"], status_code)

    def include_router(self, router: Router):
        self.router.routes.extend(router.routes)

    def websocket(self, path):
        def deco(fn):
            names = [n for n in inspect.signature(fn).parameters][1:]

            async def endpoint(ws):
                await fn(ws, **{n: ws.path_params.get(n, "") for n in names})
            self.router.routes.append(WebSocketRoute(path, endpoint))
            return fn
        return deco
