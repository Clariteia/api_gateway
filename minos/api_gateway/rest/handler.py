import logging
from typing import (
    Any,
    Optional,
)

from aiohttp import (
    ClientConnectorError,
    ClientResponse,
    ClientSession,
    web,
)
from yarl import (
    URL,
)

from .exceptions import (
    InvalidAuthenticationException,
    NoTokenException,
)

logger = logging.getLogger(__name__)


async def orchestrate(request: web.Request) -> web.Response:
    """ Orchestrate discovery and microservice call """
    discovery_host = request.app["config"].discovery.host
    discovery_port = request.app["config"].discovery.port

    verb = request.method
    url = f"/{request.match_info['endpoint']}"

    discovery_data = await discover(discovery_host, int(discovery_port), "/microservices", verb, url)

    user = await get_user(request)

    microservice_response = await call(**discovery_data, original_req=request, user=user)
    return microservice_response


async def get_user(request: web.Request) -> Optional[str]:
    """Get The user identifier if it is available.

    :param request: The external request.
    :return: An string value containing the user identifier or ``None`` if no user information is available.
    """
    auth = request.app["config"].rest.auth
    if auth is None:
        return None

    try:
        await get_token(request)
    except NoTokenException:
        return None

    try:
        original_headers = dict(request.headers.copy())
        return await authenticate(auth.host, auth.port, auth.method, auth.path, original_headers)
    except InvalidAuthenticationException:
        return None


async def discover(host: str, port: int, path: str, verb: str, endpoint: str) -> dict[str, Any]:
    """Call discovery service and get microservice connection data.

    :param host: Discovery host name.
    :param port: Discovery port.
    :param path: Discovery path.
    :param verb: Endpoint Verb.
    :param endpoint: Endpoint url.
    :return: The response of the discovery.
    """

    url = URL.build(scheme="http", host=host, port=port, path=path, query={"verb": verb, "path": endpoint})
    try:
        async with ClientSession() as session:
            async with session.get(url=url) as response:
                if not response.ok:
                    raise web.HTTPBadGateway(text="The discovery response is not okay.")
                data = await response.json()
    except ClientConnectorError:
        raise web.HTTPGatewayTimeout(text="The discovery is not available.")

    data["port"] = int(data["port"])

    return data


# noinspection PyUnusedLocal
async def call(address: str, port: int, original_req: web.Request, user: Optional[str], **kwargs) -> web.Response:
    """Call microservice (redirect the original call)

    :param address: The ip of the microservices.
    :param port: The port of the microservice.
    :param original_req: The original request.
    :param kwargs: Additional named arguments.
    :param user: User that makes the request
    :return: The web response to be retrieved to the client.
    """

    headers = original_req.headers.copy()
    if user is not None:
        headers["User"] = user
    else:  # Enforce that the 'User' entry is only generated by the auth system.
        # noinspection PyTypeChecker
        headers.pop("User", None)

    url = original_req.url.with_scheme("http").with_host(address).with_port(port)
    method = original_req.method
    content = await original_req.text()

    logger.info(f"Redirecting {method!r} request to {url!r}...")

    try:
        async with ClientSession() as session:
            async with session.request(headers=headers, method=method, url=url, data=content) as response:
                return await _clone_response(response)
    except ClientConnectorError:
        raise web.HTTPServiceUnavailable(text="The requested endpoint is not available.")


# noinspection PyMethodMayBeStatic
async def _clone_response(response: ClientResponse) -> web.Response:
    return web.Response(
        body=await response.read(), status=response.status, reason=response.reason, headers=response.headers,
    )


async def authenticate(host: str, port: str, method: str, path: str, authorization_headers: dict[str, str]) -> str:
    authentication_url = URL(f"http://{host}:{port}{path}")
    authentication_method = method
    logger.info("Authenticating request...")

    try:
        async with ClientSession(headers=authorization_headers) as session:
            async with session.request(method=authentication_method, url=authentication_url) as response:
                if response.ok:
                    jwt_payload = await response.json()
                    return jwt_payload["sub"]
                else:
                    raise InvalidAuthenticationException
    except (ClientConnectorError, web.HTTPError):
        raise InvalidAuthenticationException


async def get_token(request: web.Request) -> str:
    headers = request.headers
    if "Authorization" in headers and "Bearer" in headers["Authorization"]:
        parts = headers["Authorization"].split()
        if len(parts) == 2:
            return parts[1]

    raise NoTokenException
