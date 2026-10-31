"""HIRA public-data adapter; registered information is not live availability."""

from __future__ import annotations

import socket
from dataclasses import dataclass
from types import TracebackType
from typing import Final, Literal
from urllib.parse import unquote
from xml.etree import ElementTree

import anyio
import httpx2
from pydantic import BaseModel, ConfigDict

LIST_URL: Final = 'https://apis.data.go.kr/B551182/hospInfoServicev2/getHospBasisList'
DETAIL_URL: Final = 'https://apis.data.go.kr/B551182/MadmDtlInfoService2.8/'
MAX_RESPONSE_BYTES: Final = 5_000_000
MAX_PAGES: Final = 5
PAGE_SIZE: Final = 100


class HiraError(Exception):
    """A HIRA response is unavailable, invalid, or incomplete."""


class HiraTooBroad(HiraError):
    """Search radius contains more results than the bounded reader can retrieve."""


def _valid_hhmm(value: str, *, end: bool = False) -> bool:
    if len(value) != 4 or not value.isascii() or not value.isdigit():
        return False
    hour, minute = int(value[:2]), int(value[2:])
    return (0 <= hour <= 23 and 0 <= minute <= 59) or (end and value == '2400')


class HiraHospital(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    name: str
    address: str
    phone: str
    latitude: float
    longitude: float


class HiraDetails(BaseModel):
    model_config = ConfigDict(frozen=True)
    specialties: tuple[str, ...] | None = None
    specialists: dict[str, int] | None = None
    hours: dict[str, tuple[str, str]] | None = None
    open_status: Literal['unknown'] = 'unknown'
    updated_at: str | None = None
    unavailable: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _Reply:
    items: tuple[ElementTree.Element, ...]
    total: int


class HiraClient:
    """Read-only bounded client. Service keys and locations are never logged."""

    def __init__(self, service_key: str, *, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self._key = unquote(service_key.strip())
        if not self._key:
            raise HiraError('Public-data service key is not configured')
        self._transport = transport
        self._client: httpx2.AsyncClient | None = None

    def _new_client(self) -> httpx2.AsyncClient:
        transport = self._transport or httpx2.AsyncHTTPTransport(
            http2=True, retries=3,
            limits=httpx2.Limits(max_connections=200, max_keepalive_connections=40, keepalive_expiry=30),
            socket_options=[(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)],
        )
        timeout = httpx2.Timeout(connect=5, read=30, write=10, pool=10)
        return httpx2.AsyncClient(transport=transport, timeout=timeout, follow_redirects=False, trust_env=False)

    async def __aenter__(self) -> HiraClient:
        if self._client is not None:
            raise HiraError('Public-data client is already open')
        client = self._new_client()
        await client.__aenter__()
        self._client = client
        return self

    async def __aexit__(self, exc_type: type[BaseException] | None, exc: BaseException | None,
                        traceback: TracebackType | None) -> None:
        client = self._client
        self._client = None
        if client is not None:
            await client.__aexit__(exc_type, exc, traceback)

    async def _get(self, url: str, params: dict[str, str | int | float]) -> _Reply:
        if self._client is not None:
            return await self._request(self._client, url, params)
        async with self._new_client() as client:
            return await self._request(client, url, params)

    async def _request(self, client: httpx2.AsyncClient, url: str,
                       params: dict[str, str | int | float]) -> _Reply:
        try:
            async with client.stream('GET', url, params=params) as response:
                if response.status_code != 200:
                    raise HiraError('Public-data HTTP request failed')
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > MAX_RESPONSE_BYTES:
                        raise HiraError('Public-data response exceeded the size limit')
            root = ElementTree.fromstring(content)
            if root.tag != 'response' or root.findtext('./header/resultCode') not in ('00', '0'):
                raise HiraError('Public-data service rejected the request or returned an invalid response')
            total_text = root.findtext('./body/totalCount')
            if total_text is None or not total_text.isdigit():
                raise HiraError('Public-data response lacks a valid count')
            return _Reply(tuple(root.findall('./body/items/item')), int(total_text))
        except (httpx2.HTTPError, ElementTree.ParseError):
            raise HiraError('Public-data service could not be reached or decoded') from None

    async def list_nearby(self, latitude: float, longitude: float, radius_m: int, *,
                          specialty_code: str | None = None) -> list[HiraHospital]:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180 and 0 < radius_m <= 100_000):
            raise HiraError('Invalid coordinates or search radius')
        output: list[HiraHospital] = []
        for page in range(1, MAX_PAGES + 1):
            params: dict[str, str | int | float] = {'ServiceKey': self._key, 'xPos': longitude, 'yPos': latitude,
                                                    'radius': radius_m, 'pageNo': page, 'numOfRows': PAGE_SIZE}
            if specialty_code is not None:
                params['dgsbjtCd'] = specialty_code
            reply = await self._get(LIST_URL, params)
            if reply.total > MAX_PAGES * PAGE_SIZE:
                raise HiraTooBroad('Search is too broad; reduce radius to avoid incomplete results')
            for item in reply.items:
                identity = item.findtext('ykiho') or ''
                name = item.findtext('yadmNm') or ''
                try:
                    x, y = float(item.findtext('XPos') or ''), float(item.findtext('YPos') or '')
                except ValueError:
                    continue
                if identity and name and -90 <= y <= 90 and -180 <= x <= 180:
                    output.append(HiraHospital(id=identity, name=name, address=item.findtext('addr') or '',
                                               phone=item.findtext('telno') or '', latitude=y, longitude=x))
            if page * PAGE_SIZE >= reply.total:
                return output
            if not reply.items:
                raise HiraError('Public-data pagination was incomplete')
        raise HiraError('Public-data pagination exceeded the safe limit')

    async def details(self, ykiho: str) -> HiraDetails:
        if not ykiho or len(ykiho) > 300:
            raise HiraError('Invalid hospital identifier')
        replies: dict[str, _Reply | None] = {'specialties': None, 'specialists': None, 'hours': None}
        async def fetch(name: str, operation: str) -> None:
            try:
                replies[name] = await self._get(DETAIL_URL + operation, {'serviceKey': self._key, 'ykiho': ykiho,
                                                                       'pageNo': 1, 'numOfRows': PAGE_SIZE})
            except HiraError:
                replies[name] = None
        async with anyio.create_task_group() as tasks:
            tasks.start_soon(fetch, 'specialties', 'getDgsbjtInfo2.8')
            tasks.start_soon(fetch, 'specialists', 'getSpcSbjtSdrInfo2.8')
            tasks.start_soon(fetch, 'hours', 'getDtlInfo2.8')
        for reply in replies.values():
            if reply is not None and reply.total > PAGE_SIZE:
                raise HiraError('Hospital details were truncated')
        subjects = replies['specialties']
        specialists = replies['specialists']
        schedule = replies['hours']
        names = tuple(sorted({text for item in subjects.items if (text := item.findtext('dgsbjtCdNm'))})) if subjects else ()
        counts: dict[str, int] = {}
        for item in specialists.items if specialists else ():
            name, count = item.findtext('dgsbjtCdNm'), item.findtext('dtlSdrCnt')
            if name and count and count.isdigit():
                counts[name] = max(counts.get(name, 0), int(count))
        hours: dict[str, tuple[str, str]] = {}
        if schedule and len(schedule.items) == 1:
            item = schedule.items[0]
            for day in ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'):
                start, end = item.findtext(f'trmt{day}Start'), item.findtext(f'trmt{day}End')
                if start and end and start != end and _valid_hhmm(start) and _valid_hhmm(end, end=True):
                    hours[day.lower()] = (start, end)
        return HiraDetails(specialties=names or None, specialists=counts or None, hours=hours or None,
                           unavailable=tuple(name for name, reply in replies.items() if reply is None))
