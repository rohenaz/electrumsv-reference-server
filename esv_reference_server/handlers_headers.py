import logging
import uuid

import aiohttp
import bitcoinx
import typing
from aiohttp import web

from esv_reference_server.types import HeadersWSClient

if typing.TYPE_CHECKING:
    from esv_reference_server.server import ApplicationState

logger = logging.getLogger('handlers-headers')


async def get_header(request: web.Request) -> web.Response:
    client_session: aiohttp.ClientSession = request.app['client_session']
    app_state: ApplicationState = request.app['app_state']

    accept_type = request.headers.get('Accept')
    blockhash = request.match_info.get('hash')
    if not blockhash:
        return web.HTTPBadRequest(reason="'hash' path parameter not supplied")

    try:
        url_to_fetch = f"{app_state.header_sv_url}/api/v1/chain/header/{blockhash}"
        if accept_type == 'application/octet-stream':
            request_headers = {'Content-Type': 'application/octet-stream'}  # Should be 'Accept'
            async with client_session.get(url_to_fetch, headers=request_headers) as response:
                result = await response.read()
            response_headers = {'Content-Type': 'application/octet-stream', 'User-Agent': 'ESV-Ref-Server'}
            return web.Response(body=result, status=200, reason='OK', headers=response_headers)

        # else: application/json
        request_headers = {'Content-Type': 'application/json'}  # Should be 'Accept'
        async with client_session.get(url_to_fetch, headers=request_headers) as response:
            if response.status != 200:
                return web.Response(reason=response.reason, status=response.status)

            result = await response.json()
        response_headers = {'User-Agent': 'ESV-Ref-Server'}
        return web.json_response(result, status=200, reason='OK', headers=response_headers)
    except aiohttp.ClientConnectorError as e:
        logger.error(f"HeaderSV service is unavailable on {app_state.header_sv_url}")
        return web.HTTPServiceUnavailable()


async def get_headers_by_height(request: web.Request) -> web.Response:
    client_session: aiohttp.ClientSession = request.app['client_session']
    app_state: ApplicationState = request.app['app_state']

    accept_type = request.headers.get('Accept')
    params = request.rel_url.query
    height = params.get('height', '0')
    count = params.get('count', '1')

    try:
        url_to_fetch = f"{app_state.header_sv_url}/api/v1/chain/header/byHeight?height={height}&count={count}"
        if accept_type == 'application/octet-stream':
            request_headers = {'Accept': 'application/octet-stream'}
            async with client_session.get(url_to_fetch, headers=request_headers) as response:
                if response.status != 200:
                    return web.Response(reason=response.reason, status=response.status)

                result = await response.read()
            response_headers = {'Content-Type': 'application/octet-stream', 'User-Agent': 'ESV-Ref-Server'}
            return web.Response(body=result, status=200, reason='OK', headers=response_headers)

        # else: application/json
        request_headers = {'Accept': 'application/json'}
        async with client_session.get(url_to_fetch, headers=request_headers) as response:
            result = await response.json()
        response_headers = {'User-Agent': 'ESV-Ref-Server'}
        return web.json_response(result, status=200, reason='OK', headers=response_headers)
    except aiohttp.ClientConnectorError as e:
        logger.error(f"HeaderSV service is unavailable on {app_state.header_sv_url}")
        return web.HTTPServiceUnavailable()


async def get_chain_tips(request: web.Request) -> web.Response:
    client_session: aiohttp.ClientSession = request.app['client_session']
    app_state: ApplicationState = request.app['app_state']

    url_to_fetch = f"{app_state.header_sv_url}/api/v1/chain/tips"
    request_headers = {'Accept': 'application/json'}
    async with client_session.get(url_to_fetch, headers=request_headers) as response:
        result = await response.json()
    response_headers = {'User-Agent': 'ESV-Ref-Server'}
    return web.json_response(result, status=200, reason='OK', headers=response_headers)


async def get_peers(request: web.Request) -> web.Response:
    client_session: aiohttp.ClientSession = request.app['client_session']
    app_state: ApplicationState = request.app['app_state']

    url_to_fetch = f"{app_state.header_sv_url}/api/v1/network/peers"
    request_headers = {'Accept': 'application/json'}
    async with client_session.get(url_to_fetch, headers=request_headers) as response:
        result = await response.json()
    response_headers = {'User-Agent': 'ESV-Ref-Server'}
    return web.json_response(result, status=200, reason='OK', headers=response_headers)


class HeadersWebSocket(web.View):

    logger = logging.getLogger("headers-websocket")

    async def get(self):
        """The communication for this is one-way - for header notifications only.
        Client messages will be ignored"""
        ws = web.WebSocketResponse()
        await ws.prepare(self.request)
        ws_id = str(uuid.uuid4())

        try:
            client = HeadersWSClient(ws_id=ws_id, websocket=ws)
            self.request.app['app_state'].add_ws_client(client)
            self.logger.debug('%s connected. host=%s.', client.ws_id, self.request.host)
            await self._handle_new_connection(client)
            return ws
        finally:
            await ws.close()
            self.logger.debug("removing headers websocket id: %s", ws_id)
            del self.request.app['headers_ws_clients'][ws_id]

    async def _send_chain_tip(self, client: HeadersWSClient):
        """Called once on initial connection"""
        client_session: aiohttp.ClientSession = self.request.app['client_session']
        app_state = self.request.app['app_state']
        try:
            request_headers = {'Accept': 'application/json'}
            url_to_fetch = f"{app_state.header_sv_url}/api/v1/chain/tips"
            async with client_session.get(url_to_fetch, headers=request_headers) as response:
                result = await response.json()
                for tip in result:
                    if tip['state'] == "LONGEST_CHAIN":
                        longest_chain_tip = tip
                        current_best_hash = longest_chain_tip['header']['hash']
                        current_best_height = longest_chain_tip['height']

            # Todo: this should actually be 'Accept' but HeaderSV uses 'Content-Type'
            request_headers = {'Content-Type': 'application/octet-stream'}
            url_to_fetch = f"{app_state.header_sv_url}/api/v1/chain/header/{current_best_hash}"
            async with client_session.get(url_to_fetch, headers=request_headers) as response:
                result = await response.read()
                self.logger.debug(f"Sending tip to new websocket connection, ws_id: {client.ws_id}")
                response = bytearray()
                response += result  # 80 byte header
                response += bitcoinx.pack_be_uint32(current_best_height)
                await client.websocket.send_bytes(response)
        except aiohttp.ClientConnectorError as e:
            # When HeaderSV comes back online there will be a compensating chain tip notification
            self.logger.error(f"HeaderSV service is unavailable on {app_state.header_sv_url}")
            pass

    async def _handle_new_connection(self, client: HeadersWSClient):
        self.ws_clients = self.request.app['headers_ws_clients']
        await self._send_chain_tip(client)

        async for msg in client.websocket:
            # Ignore all messages from client
            if msg.type == aiohttp.WSMsgType.text:
                self.logger.debug('%s new headers websocket client sent: %s', client.ws_id, msg.data)
                # request_json = json.loads(msg.data)
                # response_json = json.dumps(request_json)
                # await client.websocket.send_str(response_json)

            elif msg.type == aiohttp.WSMsgType.error:
                # 'client.websocket.exception()' merely returns ClientWebSocketResponse._exception
                # without a traceback. see aiohttp.ws_client.py:receive for details.
                self.logger.error('ws connection closed with exception %s',
                    client.websocket.exception())
