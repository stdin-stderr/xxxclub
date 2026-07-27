"""Details page vs soft-404 vs challenge. Offline; no network, no DB.

The site never returns a real 404 for a missing ID — it serves HTTP 200 with its
normal chrome plus an errordiv. Misreading that as a block is what escalated the old
circuit breaker to its cap and stalled the crawler.
"""

import os
import unittest

from scraper import looks_like_challenge, looks_like_details_page, looks_like_soft_404

# Trimmed from a live response for /torrents/details/460000 (an ID far above the
# frontier): HTTP 200, full site nav, errordiv, no detailsdiv.
SOFT_404 = """
<html><head><title></title></head><body>
<div class="header"><a href="/">Home</a> <a href="/torrents/browse/all/">Browse Torrents</a></div>
<div class="errordiv"> <h1>Error :</h1> <div class="belowheading">
<p>404 : Not Found</p> <br> <p> Meanwhile you can return to our <a href="/">Homepage</a>.</p>
</div></div>
<script>window.__CF$cv$params={r:'a20b',t:'MTc4NA=='};var a=document.createElement('script');
a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>
</body></html>
"""

DETAILS = """
<html><body><div class="detailsdiv"><h1>Some Release</h1>
<div class="detailsdescr"><ul><li><span>Category</span><span>:</span><span>720p/HD</span></li></ul></div>
</div>
<script>a.src='/cdn-cgi/challenge-platform/scripts/jsd/main.js';</script>
</body></html>
"""

CF_INTERSTITIAL = """
<html><head><title>Just a moment...</title></head><body>
<div class="cf-browser-verification cf-im-under-attack"></div>
<form id="challenge-form" action="/cdn-cgi/l/chk_jschl"></form>
<script>window._cf_chl_opt={cvId:'2'};</script>
</body></html>
"""


class TestClassification(unittest.TestCase):
    def test_details_page(self):
        self.assertTrue(looks_like_details_page(DETAILS))
        self.assertFalse(looks_like_soft_404(DETAILS))
        self.assertFalse(looks_like_challenge(DETAILS))

    def test_soft_404(self):
        self.assertFalse(looks_like_details_page(SOFT_404))
        self.assertTrue(looks_like_soft_404(SOFT_404))
        self.assertFalse(looks_like_challenge(SOFT_404))

    def test_cloudflare_interstitial(self):
        self.assertFalse(looks_like_details_page(CF_INTERSTITIAL))
        self.assertFalse(looks_like_soft_404(CF_INTERSTITIAL))
        self.assertTrue(looks_like_challenge(CF_INTERSTITIAL))

    def test_beacon_script_alone_is_not_a_challenge(self):
        """/cdn-cgi/challenge-platform/ is on every page, valid ones included. Matching
        the bare word 'challenge' would mark the whole site as blocked."""
        for html in (DETAILS, SOFT_404):
            with self.subTest():
                self.assertIn("challenge-platform", html)
                self.assertFalse(looks_like_challenge(html))

    @unittest.skipUnless(os.path.exists("450462.html"), "fixture not present")
    def test_real_fixture_is_a_details_page(self):
        html = open("450462.html").read()
        self.assertTrue(looks_like_details_page(html))
        self.assertFalse(looks_like_soft_404(html))
        self.assertFalse(looks_like_challenge(html))


if __name__ == "__main__":
    unittest.main()
