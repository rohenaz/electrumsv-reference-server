import asyncio
import base64
import json
from http import HTTPStatus
import logging
import os
from pathlib import Path
import shutil
import struct
import sys
import threading
from typing import Any, cast, Generator, Optional

import aiohttp
from aiohttp import WSServerHandshakeError
from bitcoinx import PublicKey, PrivateKey
from electrumsv_database.sqlite import DatabaseContext, replace_db_context_with_connection
import pytest
import requests

try:
    # Linux expects the latest package version of 3.35.4 (as of pysqlite-binary 0.4.6)
    import pysqlite3 as sqlite3
except ModuleNotFoundError:
    # MacOS has latest brew version of 3.35.5 (as of 2021-06-20).
    # Windows builds use the official Python 3.10.0 builds and bundled version of 3.35.5.
    import sqlite3

from esv_reference_server.application_state import ApplicationState
from esv_reference_server.constants import DEFAULT_DATABASE_NAME
from esv_reference_server.errors import WebsocketUnauthorizedException
from esv_reference_server.sqlite_db import delete_all_tables

from server import main as application_main


logger = logging.getLogger("unittest")


TEST_EXTERNAL_HOST = "127.0.0.1"
TEST_EXTERNAL_PORT = 55666
TEST_HREF_HOST = "127.0.0.1"
TEST_HREF_PORT = 55666
TEST_INTERNAL_HOST = "127.0.0.1"
TEST_INTERNAL_PORT = 55668

WS_URL_GENERAL = f"ws://{TEST_EXTERNAL_HOST}:{TEST_EXTERNAL_PORT}/api/v1/web-socket"
WS_URL_HEADERS = f"ws://{TEST_EXTERNAL_HOST}:{TEST_EXTERNAL_PORT}/api/v1/headers/tips/websocket"
WS_URL_TEMPLATE_MSG_BOX = "ws://"+ TEST_EXTERNAL_HOST +":"+ str(TEST_EXTERNAL_PORT) + \
    "/api/v1/channel/{channelid}/notify"

REGTEST_GENESIS_BLOCK_HASH = "0f9188f13cb7b2c71f2a335e3a4fc328bf5beb436012afca590b1a11466e2206"
PRIVATE_KEY_1 = PrivateKey.from_hex(
    "720f1987db69efa562b3dabd78e51f19bd8da76c70ad839b72b939f4071b144b")
PUBLIC_KEY_1: PublicKey = PRIVATE_KEY_1.public_key

REF_TYPE_OUTPUT = 0
REF_TYPE_INPUT = 1
STREAM_TERMINATION_BYTE = b"\x00"

MODULE_DIR = Path(os.path.dirname(os.path.abspath(__file__)))

CHANNEL_ID: str = ""
CHANNEL_BEARER_TOKEN: str = ""
CHANNEL_BEARER_TOKEN_ID: int = 0
CHANNEL_READ_ONLY_TOKEN: str = ""
CHANNEL_READ_ONLY_TOKEN_ID: int = 0



def electrumsv_reference_server_thread() -> None:
    """Launches the ESV-Reference-Server to run in the background but with a test database"""
    try:
        asyncio.run(application_main())
        sys.exit(0)
    except KeyboardInterrupt:
        logger.debug("ElectrumSV Reference Server stopped")
    except Exception:
        logger.exception("unexpected exception in __main__")
    finally:
        logger.info("ElectrumSV Reference Server stopped")


def _no_auth(url: str, method: str) -> None:
    assert method.lower() in {'get', 'post', 'head', 'delete', 'put'}
    request_call = getattr(requests, method.lower())
    result = request_call(url)
    assert result.status_code == HTTPStatus.BAD_REQUEST, result.reason
    assert result.reason is not None  # {"authorization": "is required"}


def _wrong_auth_type(url: str, method: str) -> None:
    assert method.lower() in {'get', 'post', 'head', 'delete', 'put'}
    request_call = getattr(requests, method.lower())
    # No auth -> 400 {"authorization": "is required"}
    headers = {}
    headers["Authorization"] = "Basic xyz"
    result = request_call(url, headers=headers)
    assert result.status_code == HTTPStatus.BAD_REQUEST, result.reason
    assert result.reason is not None


def _bad_token(url: str, method: str, headers: Optional[dict[str, str]] = None) -> None:
    assert method.lower() in {'get', 'post', 'head', 'delete', 'put'}
    request_call = getattr(requests, method.lower())
    if not headers:
        headers = {}
    headers["Authorization"] = "Bearer bad bearer token"
    result = request_call(url, headers=headers)
    assert result.status_code == 401, result.reason
    assert result.reason is not None


def _successful_call(url: str, method: str, headers: Optional[dict[str, str]] = None,
                     request_body: Optional[dict[str, Any]] = None,
                     good_bearer_token: Optional[str] = None) -> requests.Response:
    assert method.lower() in {'get', 'post', 'head', 'delete', 'put'}
    request_call = getattr(requests, method.lower())
    if not headers:
        headers = {}
    if good_bearer_token:
        headers["Authorization"] = f"Bearer {good_bearer_token}"
    return cast(requests.Response,
        request_call(url, data=json.dumps(request_body), headers=headers))


def _assert_tip_notification_structure(tip_notification: bytes) -> bool:
    assert len(tip_notification) == 84
    height = struct.unpack('<I', tip_notification[80:84])[0]
    assert isinstance(height, int)
    return True


def _assert_header_structure(header: dict[str, str]) -> bool:
    assert isinstance(header['hash'], str)
    assert len(header['hash']) == 64
    assert isinstance(header['version'], int)
    assert isinstance(header['prevBlockHash'], str)
    assert len(header['prevBlockHash']) == 64
    assert isinstance(header['merkleRoot'], str)
    assert len(header['merkleRoot']) == 64
    assert isinstance(header['creationTimestamp'], int)
    assert isinstance(header['difficultyTarget'], int)
    assert isinstance(header['nonce'], int)
    assert isinstance(header['transactionCount'], int)
    assert isinstance(header['work'], int)
    assert isinstance(header['work'], int)
    return True


def _assert_tip_structure_correct(tip: dict[str, Any]) -> bool:
    assert isinstance(tip, dict)
    assert tip['header'] is not None
    header = tip['header']
    _assert_header_structure(header)
    assert tip['state'] == 'LONGEST_CHAIN'
    assert isinstance(tip['chainWork'], int)
    assert isinstance(tip['height'], int)
    assert isinstance(tip['confirmations'], int)
    return True


def _assert_binary_tip_structure_correct(tip: bytes) -> bool:
    assert isinstance(tip, bytes)
    assert len(tip) == 84
    return True


async def _subscribe_to_general_notifications_peer_channels(url: str, api_token: str,
        expected_count: int, completion_event: asyncio.Event) -> None:
    """Todo - Tests to assert that a different account_id does NOT receive messages it should not"""
    logger = logging.getLogger("test-general-notifications")

    count = 0
    async with aiohttp.ClientSession() as session:
        try:
            async with session.ws_connect(url + f"?token={api_token}",
                                          timeout=5.0) as ws:

                logger.info('Connected to %s', url)
                async for msg in ws:
                    if msg.type == aiohttp.WSMsgType.TEXT:
                        content = json.loads(msg.data)
                        logger.info('New message: %s', content)
                        assert content['message_type'] == 'bsvapi.channels.notification'
                        assert isinstance(content['result'], dict)
                        assert isinstance(content['result']['channel_id'], str)
                        channel_id_bytes = base64.urlsafe_b64decode(content['result']['channel_id'])
                        assert len(channel_id_bytes) == 64
                        assert content['result']['sequence'] == count + 1
                        assert content['result']['content_type'] == "application/json"
                        assert isinstance(content['result']['received'], str)
                        assert len(content['result']['received']) > 1 and \
                            content['result']['received'][-1] == "Z"

                        count += 1
                        if expected_count == count:
                            logger.debug("Received all %d messages", expected_count)
                            await session.close()
                            completion_event.set()
                            return

                    elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR,
                                    aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING):
                        logger.info("CLOSED")
                        break
                    else:
                        assert False, f"Unexpected message type {msg.type}"
        except WSServerHandshakeError as e:
            if e.status == 401:
                raise WebsocketUnauthorizedException()


def _is_server_running(url: str) -> bool:
    try:
        result = requests.get(url)
        if result.status_code == 200:
            return True
        else:
            return False
    except requests.ConnectionError:
        return False


@pytest.fixture(scope="session", autouse=True)
def run_server() -> Generator[ApplicationState, None, None]:
    data_path = MODULE_DIR / "localdata"
    if data_path.exists():
        shutil.rmtree(data_path)
    data_path.mkdir()

    os.environ['EXTERNAL_HOST'] = TEST_EXTERNAL_HOST
    os.environ['EXTERNAL_PORT'] = str(TEST_EXTERNAL_PORT)
    os.environ['INTERNAL_HOST'] = TEST_INTERNAL_HOST
    os.environ['INTERNAL_PORT'] = str(TEST_INTERNAL_PORT)
    os.environ['HREF_HOST'] = TEST_HREF_HOST
    os.environ['HREF_PORT'] = str(TEST_HREF_PORT)
    os.environ['NETWORK'] = "regtest"
    os.environ['EXPOSE_HEADER_SV_APIS'] = '1'
    os.environ['HEADER_SV_URL'] = 'http://127.0.0.1:33444'
    os.environ['EXPOSE_INDEXER_APIS'] = '0'
    os.environ['INDEXER_URL'] = 'http://127.0.0.1:49241'
    os.environ['ENABLE_OUTBOUND_DATA_DELIVERY'] = '0'
    os.environ['SKIP_DOTENV_FILE'] = '1'
    os.environ['REFERENCE_SERVER_RESET'] = '0'
    os.environ['REFERENCE_SERVER_DATA_PATH'] = str(data_path)
    os.environ['NOTIFICATION_TEXT_NEW_MESSAGE'] = 'New message arrived'
    os.environ['MAX_MESSAGE_CONTENT_LENGTH'] = '65536'
    os.environ['CHUNKED_BUFFER_SIZE'] = '1024'

    # Reset db before use
    database_context = DatabaseContext(str(data_path / DEFAULT_DATABASE_NAME))
    @replace_db_context_with_connection
    def execute_with_context(db: sqlite3.Connection) -> None:
        delete_all_tables(db)
    execute_with_context(database_context)
    database_context.close()

    thread = threading.Thread(target=electrumsv_reference_server_thread, args=(), daemon=True)
    thread.start()

    if not ApplicationState.singleton_event.wait(10.0):
        raise Exception("Application startup timed out")

    assert ApplicationState.singleton_reference is not None
    application_state = ApplicationState.singleton_reference()
    assert application_state is not None
    yield application_state
