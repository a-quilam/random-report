"""The wire format uses major units, including numeric subtotals and balances."""
import unittest
from collections import Counter
from randreport.connectors.zenmoney import ZenmoneyClient, ZenmoneyProtocolError
from randreport.connectors.transport import Response
from tests.fixtures_ingest import RecordingTransport


class WireMoney(unittest.TestCase):
    def read(self, amounts):
        rows = [f'{{"id":"x{i}","date":"2025-11-01","incomeAccount":"a","outcomeAccount":"a","income":{n},"outcome":0,"tag":["{tag}"]}}' for i,(tag,n) in enumerate(amounts)]
        body = ('{"serverTimestamp":123,"account":[{"id":"a","balance":-2.04}],"transaction":[' + ','.join(rows) + ']}').encode()
        return ZenmoneyClient(RecordingTransport(lambda req: Response(200,body))).fetch_diff()

    def test_subtotals_from_wire(self):
        diff = self.read([('a','10'),('a','0.09'),('b','12.34')]); sums = Counter()
        for m in diff.movements:
            sums[m.category_id] += m.amount_kopecks
        self.assertEqual(dict(sums), {'a':1009,'b':1234})
        self.assertEqual(diff.account_rows[0].balance_kopecks,-204)

    def test_subcent_never_silently_rounded(self):
        with self.assertRaises(ZenmoneyProtocolError):
            self.read([('a','1.00000000000000000000000000001')])
