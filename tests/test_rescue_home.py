import unittest
from randreport.web.app import PublicSite


class TestRescueHome(unittest.TestCase):
    def test_only_approved_shell_on_home(self):
        page = PublicSite()._dispatch({'REQUEST_METHOD':'GET', 'PATH_INFO':'/', 'QUERY_STRING':'period=2025-01'}).text
        self.assertIn('/static/approved-home.css', page)
        self.assertNotIn('class="site-header"', page)
        self.assertEqual(page.count('<nav '), 1)
        self.assertEqual(page.count('class="nav-cut '), 7)
        self.assertNotIn('id="openEditor"', page)
        self.assertNotIn('style=', page)
        self.assertNotIn('счётчики здесь не выдумываются', page)
        self.assertIn('id="eventsCount">40<', page)
