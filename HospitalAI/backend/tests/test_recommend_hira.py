"""Contract checks for the public-data hospital adapter."""

import anyio
import httpx2
import pytest

from app.recommend_hira import HiraClient, HiraError


def xml(body: str, total: int = 1) -> bytes:
    return f"<response><header><resultCode>00</resultCode></header><body><items>{body}</items><totalCount>{total}</totalCount></body></response>".encode()


def test_list_preserves_real_coordinates_and_encoded_key_once() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params['ServiceKey'] == 'abc+/='
        assert request.url.params['xPos'] == '127.0'
        assert request.url.params['yPos'] == '37.0'
        assert request.url.params['radius'] == '3000'
        return httpx2.Response(200, content=xml('<item><ykiho>id1</ykiho><yadmNm>가상병원</yadmNm><addr>가상 주소</addr><telno>02-000-0000</telno><XPos>127.1</XPos><YPos>37.1</YPos></item>'))

    async def run() -> None:
        client = HiraClient('abc%2B%2F%3D', transport=httpx2.MockTransport(handler))
        hospitals = await client.list_nearby(37.0, 127.0, 3000)
        assert len(hospitals) == 1
        assert hospitals[0].id == 'id1'
        assert hospitals[0].longitude == 127.1
        assert hospitals[0].latitude == 37.1

    anyio.run(run)


def test_list_sends_requested_department_code_to_the_provider() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params['dgsbjtCd'] == '05'
        return httpx2.Response(200, content=xml('', total=0))

    async def run() -> None:
        client = HiraClient('key', transport=httpx2.MockTransport(handler))
        assert await client.list_nearby(37.0, 127.0, 3000, specialty_code='05') == []

    anyio.run(run)


def test_general_hospital_search_requires_the_registered_classification() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        assert request.url.params['clCd'] == '11'
        return httpx2.Response(200, content=xml(
            '<item><ykiho>general</ykiho><yadmNm>한빛병원</yadmNm><clCd>11</clCd><XPos>127</XPos><YPos>37</YPos></item>'
            '<item><ykiho>clinic</ykiho><yadmNm>내과의원</yadmNm><clCd>31</clCd><XPos>127</XPos><YPos>37</YPos></item>', total=2))

    async def run() -> None:
        client = HiraClient('key', transport=httpx2.MockTransport(handler))
        result = await client.list_nearby(37, 127, 3000, classification_code='11')
        assert [hospital.id for hospital in result] == ['general']
        assert result[0].classification_code == '11'

    anyio.run(run)


def test_details_distinguishes_specialists_from_all_physicians() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        path = request.url.path
        if path.endswith('getSpcSbjtSdrInfo2.8'):
            content = xml('<item><dgsbjtCdNm>내과</dgsbjtCdNm><dtlSdrCnt>2</dtlSdrCnt></item>')
        elif path.endswith('getDgsbjtInfo2.8'):
            content = xml('<item><dgsbjtCdNm>내과</dgsbjtCdNm><dgsbjtPrSdrCnt>8</dgsbjtPrSdrCnt></item>')
        else:
            content = xml('<item><trmtMonStart>0900</trmtMonStart><trmtMonEnd>1800</trmtMonEnd></item>')
        return httpx2.Response(200, content=content)

    async def run() -> None:
        client = HiraClient('fake-key', transport=httpx2.MockTransport(handler))
        details = await client.details('id1')
        assert details.specialties == ('내과',)
        assert details.specialists == {'내과': 2}
        assert details.hours == {'mon': ('0900', '1800')}
        assert details.open_status == 'unknown'

    anyio.run(run)


def test_provider_error_is_not_a_successful_empty_result() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=b'<OpenAPI_ServiceResponse><cmmMsgHeader><returnReasonCode>30</returnReasonCode></cmmMsgHeader></OpenAPI_ServiceResponse>')

    async def run() -> None:
        client = HiraClient('fake-secret', transport=httpx2.MockTransport(handler))
        with pytest.raises(HiraError) as error:
            await client.list_nearby(37.0, 127.0, 3000)
        assert 'fake-secret' not in str(error.value)

    anyio.run(run)


def test_broad_incomplete_search_fails_instead_of_fabricating_nearest() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, content=xml('', total=501))

    async def run() -> None:
        client = HiraClient('key', transport=httpx2.MockTransport(handler))
        with pytest.raises(HiraError):
            await client.list_nearby(37.0, 127.0, 3000)

    anyio.run(run)


def test_failed_detail_service_is_unknown_not_zero() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('getSpcSbjtSdrInfo2.8'):
            return httpx2.Response(503)
        return httpx2.Response(200, content=xml('', total=0))

    async def run() -> None:
        client = HiraClient('key', transport=httpx2.MockTransport(handler))
        details = await client.details('id1')
        assert details.specialists is None
        assert details.open_status == 'unknown'
        assert details.unavailable == ('specialists',)

    anyio.run(run)


def test_context_reuses_transport_until_all_operations_finish() -> None:
    class CountingTransport(httpx2.AsyncBaseTransport):
        def __init__(self) -> None:
            self.calls = 0
            self.closed = 0

        async def handle_async_request(self, request: httpx2.Request) -> httpx2.Response:
            self.calls += 1
            if request.url.path.endswith('getHospBasisList'):
                return httpx2.Response(200, content=xml('<item><ykiho>id1</ykiho><yadmNm>sample</yadmNm><XPos>127.0</XPos><YPos>37.0</YPos></item>'))
            return httpx2.Response(200, content=xml('', total=0))

        async def aclose(self) -> None:
            self.closed += 1

    async def run() -> None:
        transport = CountingTransport()
        async with HiraClient('key', transport=transport) as client:
            await client.list_nearby(37.0, 127.0, 3000)
            await client.details('id1')
            assert transport.calls == 4
            assert transport.closed == 0
        assert transport.closed == 1

    anyio.run(run)


def test_malformed_or_ambiguous_hours_are_not_displayed_as_a_schedule() -> None:
    def handler(request: httpx2.Request) -> httpx2.Response:
        if request.url.path.endswith('getDtlInfo2.8'):
            return httpx2.Response(200, content=xml('<item><trmtMonStart>9999</trmtMonStart><trmtMonEnd>1800</trmtMonEnd><trmtTueStart>0900</trmtTueStart><trmtTueEnd>2460</trmtTueEnd><trmtWedStart>2400</trmtWedStart><trmtWedEnd>0200</trmtWedEnd><trmtThuStart>0900</trmtThuStart><trmtThuEnd>2400</trmtThuEnd><trmtFriStart>0000</trmtFriStart><trmtFriEnd>0000</trmtFriEnd><trmtSatStart>2200</trmtSatStart><trmtSatEnd>0300</trmtSatEnd></item>'))
        return httpx2.Response(200, content=xml('', total=0))

    async def run() -> None:
        details = await HiraClient('key', transport=httpx2.MockTransport(handler)).details('id1')
        assert details.hours == {'thu': ('0900', '2400'), 'sat': ('2200', '0300')}

    anyio.run(run)
