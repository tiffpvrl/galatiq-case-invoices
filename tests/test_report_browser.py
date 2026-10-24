"""Drive the report's interactions in a real headless browser.

Skipped when Chrome or Chromium is not installed. The page's script is exercised
end to end: selection, stepping, filtering, search, groups, the fraud banner and
the theme control.
"""

import html as htmllib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from invoice_ap.config import INVOICE_DIR, SETTINGS
from invoice_ap.html_report import build_report
from invoice_ap.mock_llm import MockProvider
from invoice_ap.pipeline import process_file

CANDIDATES = [
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
    shutil.which("google-chrome"), shutil.which("chromium"), shutil.which("chromium-browser"),
]
CHROME = next((c for c in CANDIDATES if c and Path(c).exists()), None)
pytestmark = pytest.mark.skipif(CHROME is None, reason="Chrome/Chromium not installed")

HARNESS = r"""
<script>
(function(){
  var out = {}, $ = function(s, r){ return (r||document).querySelector(s); },
      $$ = function(s, r){ return [].slice.call((r||document).querySelectorAll(s)); };
  function vis(el){ return !!el && !el.hidden && el.offsetParent !== null; }
  function detail(){ return { vendor: ($('.ap-dvendor')||{}).textContent, pos: ($('[data-pos]')||{}).textContent,
      prevDisabled: ($('[data-prev]')||{}).disabled, nextDisabled: ($('[data-next]')||{}).disabled }; }
  try {
    out.initial = detail();
    var rows = $$('#ap-review-rows .ap-row');
    rows[2].click(); out.third = detail();
    $('[data-next]').click(); out.next = detail();
    $('[data-prev]').click();
    $$('.ap-chip').filter(function(c){return c.getAttribute('data-owner')==='Treasury';})[0].click();
    out.treasury = $$('#ap-review-rows .ap-row-item').filter(function(li){return !li.classList.contains('is-filtered');}).length;
    $$('.ap-chip')[0].click();
    $('[data-mode-btn="count"]').click();
    out.count = vis($('[data-mode="count"]')) && !vis($('[data-mode="dollars"]'));
    $('[data-fraud-btn]').click();
    out.fraud = { expanded: $('.ap-group-btn[aria-controls="ap-grp-fraud"]').getAttribute('aria-expanded'), detail: detail() };
    var q = $('#ap-q'); q.value = 'treasury'; q.dispatchEvent(new Event('input'));
    out.search = { shown: vis($('[data-results]')), count: $('[data-count]').textContent,
                   queuesHidden: $$('.ap-list > [data-queue]').every(function(s){return s.hidden;}) };
    q.value = 'zzzz'; q.dispatchEvent(new Event('input')); out.noMatch = vis($('[data-none]'));
    q.value = ''; q.dispatchEvent(new Event('input'));
    out.restored = $$('.ap-list > [data-queue]').every(function(s){return !s.hidden;});
    var dup = $('.ap-group-btn[aria-controls="ap-grp-dup"]'); dup.click();
    out.dupOpen = dup.getAttribute('aria-expanded') === 'true' && !$('#ap-grp-dup').classList.contains('is-collapsed');
    out.paidCollapsed = $('#ap-paid-rows').classList.contains('is-collapsed');
    $('[data-theme-btn]').click(); out.theme = $('.ap').getAttribute('data-theme');
    $('[data-close]').click(); out.closed = !!$('.ap-dempty');
  } catch (err) { out.error = String(err && err.stack || err); }
  var pre = document.createElement('pre'); pre.id = 'RESULT'; pre.textContent = JSON.stringify(out); document.body.appendChild(pre);
})();
</script>
"""


@pytest.fixture(scope="module")
def outcome(tmp_path_factory):
    from invoice_ap.db import init_db

    conn = init_db(":memory:")
    names = ["invoice_1001.txt", "invoice_1002.txt", "invoice_1003.txt", "invoice_1004.json",
             "invoice_1004_revised.json", "invoice_1008.txt", "invoice_1010.txt", "invoice_1011.pdf",
             "invoice_1011.txt", "invoice_1012.pdf", "invoice_1014.xml"]
    results = [process_file(INVOICE_DIR / n, conn, MockProvider(), run_id="b") for n in names]
    page = build_report(results, "b", "mock", True, SETTINGS.as_of_date).replace("</body>", HARNESS + "</body>")
    path = tmp_path_factory.mktemp("browser") / "report.html"
    path.write_text(page)
    dom = subprocess.run(
        [CHROME, "--headless=new", "--disable-gpu", "--window-size=1200,1800",
         "--virtual-time-budget=4000", "--dump-dom", path.as_uri()],
        capture_output=True, text=True, timeout=90).stdout
    match = re.search(r'<pre id="RESULT">(.*?)</pre>', dom, re.S)
    assert match, "the page did not finish running its script"
    return json.loads(htmllib.unescape(match.group(1)))


def test_the_script_runs_without_errors(outcome):
    assert "error" not in outcome, outcome.get("error")


def test_the_first_item_needing_a_person_is_open_on_a_wide_screen(outcome):
    assert outcome["initial"]["pos"].startswith("1 of 4") and outcome["initial"]["prevDisabled"]


def test_selecting_and_stepping_through_the_queue(outcome):
    assert outcome["third"]["pos"] == "3 of 4 · Needs review"
    assert outcome["next"]["pos"] == "4 of 4 · Needs review" and outcome["next"]["nextDisabled"]


def test_the_owner_filter_narrows_the_review_list(outcome):
    assert outcome["treasury"] == 1


def test_the_outcome_toggle_switches_between_dollars_and_count(outcome):
    assert outcome["count"] is True


def test_the_fraud_banner_opens_the_group_and_the_first_blocked_invoice(outcome):
    assert outcome["fraud"]["expanded"] == "true"
    assert outcome["fraud"]["detail"]["vendor"] == "Fraudster LLC"
    assert "Blocked: possible fraud" in outcome["fraud"]["detail"]["pos"]


def test_search_replaces_the_queues_and_restores_them(outcome):
    assert outcome["search"] == {"shown": True, "count": "1 match", "queuesHidden": True}
    assert outcome["noMatch"] is True and outcome["restored"] is True


def test_groups_expand_and_clean_invoices_start_collapsed(outcome):
    assert outcome["dupOpen"] is True and outcome["paidCollapsed"] is True


def test_the_theme_cycles_and_the_detail_can_be_closed(outcome):
    assert outcome["theme"] == "light" and outcome["closed"] is True
