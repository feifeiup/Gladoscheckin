"""One hosted-browser observation through the official GLaDOS check-in page."""

import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from playwright.sync_api import Error, TimeoutError, sync_playwright


HOST = 'glados.space'
ALTERNATE_HOST = 'glados.cloud'
CONSOLE_URL = f'https://{HOST}/console/checkin'
SESSION_COOKIES = ('gld:sess', 'gld:sess.sig')
POINTS = re.compile(r'(?im)^[ \t]*points[ \t]*\n[ \t]*(\d+(?:\.\d+)?)[ \t]*$')
DAYS_LEFT = re.compile(r'(?mi)^[ \t]*days left[ \t]*\n[ \t]*\d+')
REWARD = re.compile(r'✓\s*\+(\d+(?:\.\d+)?)')
CLOUDFLARE_HOST = 'challenges.cloudflare.com'
CHALLENGE_FRAME = f'iframe[src^="https://{CLOUDFLARE_HOST}/"]'


class TrialStopped(Exception):
    """A fixed, credential-free message describing why the trial stopped."""


def request_category(request):
    url = urlsplit(request.url)
    if url.scheme != 'https':
        return None
    if url.netloc == CLOUDFLARE_HOST:
        return 'cloudflare'
    if url.netloc not in (HOST, ALTERNATE_HOST):
        return None
    if request.method == 'POST' and url.path == '/api/user/checkin':
        return 'checkin-post' if url.netloc == HOST else 'alternate-checkin-post'
    if request.method == 'GET' and url.path == '/api/user/checkin/challenge':
        return 'site-challenge'
    if request.method == 'POST':
        return 'other-site-post'
    return None


class BrowserDiagnostics:
    """Observe only event categories. Never emit URLs, headers, bodies or raw errors."""

    def __init__(self, page):
        self.phase = 'page-load'
        self.events = {phase: Counter() for phase in ('page-load', 'click-window', 'post-check')}
        self.click_returned = False
        page.context.on('request', lambda request: self.record(request, 'sent'))
        page.context.on('response', lambda response: self.record(response.request, 'http', response.status))
        page.context.on('requestfinished', lambda request: self.record(request, 'finished'))
        page.context.on('requestfailed', lambda request: self.record(request, 'failed'))
        page.on('console', self.record_console)
        page.on('pageerror', lambda _: self.events[self.phase].update(['page-js-error']))

    def record(self, request, event, status=None):
        category = request_category(request)
        if category is not None:
            label = f'{category}.{event}'
            if type(status) is int and 100 <= status <= 599:
                label += f'.{status}'
            self.events[self.phase][label] += 1

    def record_console(self, message):
        if message.type in ('error', 'warning') and message.text.lower().startswith('[cloudflare turnstile]'):
            self.events[self.phase]['turnstile-console-warning-or-error'] += 1

    def timeout_reason(self, verification_prompt):
        events = self.events['click-window']
        if events['checkin-post.sent']:
            if events['checkin-post.failed']:
                return 'The browser issued the check-in POST and observed a transport failure.'
            if any(key.startswith('checkin-post.http.') for key in events):
                return 'The observer saw check-in response headers, but the response wait did not resolve.'
            return 'The browser issued the check-in POST but observed no response headers.'
        if events['alternate-checkin-post.sent']:
            return 'The browser issued a check-in POST to the alternate GLaDOS host outside the primary response filter.'
        if verification_prompt or events['turnstile-console-warning-or-error']:
            return 'No known check-in POST was observed. Verification UI or a Turnstile warning/error was observed.'
        if events['cloudflare.sent']:
            return 'No known check-in POST was observed. Cloudflare network activity was seen; its outcome is unknown.'
        return 'No known check-in POST was observed and no post-click Cloudflare signal was seen; cause unknown.'

    def report(self):
        print(f'Browser diagnostics: click returned={self.click_returned}.', flush=True)
        for phase, events in self.events.items():
            counts = ', '.join(f'{key}={count}' for key, count in sorted(events.items()))
            print(f'Browser diagnostics [{phase}]: {counts or "no matching events"}.', flush=True)


def local_day():
    return datetime.now(timezone(timedelta(hours=8))).date()


def session_cookies():
    value = os.environ.get('GLADOS_COOKIES', '').strip().removeprefix('Cookie:').strip()
    if not value or '&' in value:
        raise TrialStopped('Expected one configured GLaDOS account.')
    header = SimpleCookie()
    header.load(value)
    if any(name not in header or not header[name].value for name in SESSION_COOKIES):
        raise TrialStopped('The existing session does not contain both required cookies.')
    return [
        {'name': name, 'value': header[name].value, 'url': f'https://{HOST}/',
         'httpOnly': True, 'secure': True}
        for name in SESSION_COOKIES
    ]


def visible_verification(page):
    return page.locator(f'{CHALLENGE_FRAME}:visible').count() > 0


def verification_prompt(page):
    try:
        if visible_verification(page) or 'just a moment' in page.title().lower():
            return True
        text = page.locator('body').inner_text(timeout=1500).lower()
        return any(phrase in text for phrase in (
            'verify you are human', 'cloudflare verification failed', '请验证您是真人'
        ))
    except Error:
        return None


def read_day(page, day):
    for _ in range(40):
        origin = urlsplit(page.url)
        if origin.scheme != 'https' or origin.netloc != HOST or origin.path != '/console/checkin':
            raise TrialStopped('The browser left the expected check-in page. No further interaction.')
        if visible_verification(page):
            raise TrialStopped('The page displays a Cloudflare verification frame. Trial stopped.')
        body = page.locator('body').inner_text(timeout=2500)
        if 'just a moment' in page.title().lower() or 'verify you are human' in body.lower():
            raise TrialStopped('The page requires human verification. Trial stopped.')
        balance = POINTS.search(body)
        marker = page.locator(f'[title="{day}"], [title^="{day} "]')
        if balance and DAYS_LEFT.search(body) and marker.count() == 1:
            return Decimal(balance[1]), marker.get_attribute('title', timeout=2500)
        page.wait_for_timeout(250)
    raise TrialStopped('The browser could not confirm the signed-in account balance and today’s calendar entry.')


def is_checkin_response(response):
    return request_category(response.request) == 'checkin-post'


def checkin_once(page, day, diagnostics):
    page.goto(CONSOLE_URL, wait_until='domcontentloaded', timeout=25000)
    before, entry = read_day(page, day)
    if entry != day:
        raise TrialStopped('Today already has a calendar result. No check-in button clicked.')
    button = page.get_by_role('button', name='Checkin', exact=True)
    if not button.is_visible() or not button.is_enabled():
        raise TrialStopped('The ordinary Checkin button is unavailable.')
    if local_day().isoformat() != os.environ.get('GLADOS_TRIAL_DAY'):
        raise TrialStopped('The allowed trial date changed. No check-in button clicked.')
    print(f'Pre-check: calendar has no check-in; balance={before}.', flush=True)
    diagnostics.phase = 'click-window'
    try:
        with page.expect_response(is_checkin_response, timeout=20000) as pending:
            try:
                button.click(timeout=5000)
            except TimeoutError:
                raise TrialStopped('The browser could not confirm the check-in button click. No retry.')
            diagnostics.click_returned = True
        response = pending.value
    except TimeoutError:
        prompt = verification_prompt(page)
        print(f'Browser diagnostics: visible verification signal at timeout={prompt}.', flush=True)
        raise TrialStopped(f'{diagnostics.timeout_reason(prompt)} No retry.')

    print(f'Check-in response HTTP {response.status}.', flush=True)
    if not response.ok:
        raise TrialStopped('The page’s check-in request did not return HTTP success.')
    try:
        data = response.json()
    except ValueError:
        raise TrialStopped('The check-in response could not be classified.')
    if not isinstance(data, dict):
        raise TrialStopped('The check-in response could not be classified.')
    code = data.get('code')
    safe_code = code if type(code) is int and -999 <= code <= 999 else 'unknown'
    print(f'Check-in business code: {safe_code}.', flush=True)
    message = data.get('message')
    if data.get('reason') == 'turnstile' or (
        isinstance(message, str) and 'cloudflare verification failed' in message.lower()
    ):
        raise TrialStopped('The site rejected check-in at Cloudflare verification.')
    if isinstance(message, str) and 'checkin repeats' in message.lower():
        raise TrialStopped('The service reported this was a repeat check-in.')
    if safe_code not in (0, 1, 'unknown'):
        raise TrialStopped('The service did not accept the check-in.')

    diagnostics.phase = 'post-check'
    page.reload(wait_until='domcontentloaded', timeout=25000)
    after, entry = read_day(page, day)
    reward = REWARD.search(entry)
    change = after - before
    print(f'Post-check: balance={after}; change={change}.', flush=True)
    if not reward or change <= 0 or change != Decimal(reward[1]):
        raise TrialStopped('A new reward attributable to this test could not be confirmed.')
    print(f'Confirmed on the reloaded official calendar: +{change} points.', flush=True)


def main():
    today = local_day()
    try:
        if today.isoformat() != os.environ.get('GLADOS_TRIAL_DAY'):
            raise TrialStopped('This one-time run is restricted to the specified date.')
        cookies = session_cookies()
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel='chrome')
            try:
                context = browser.new_context()
                context.add_cookies(cookies)
                page = context.new_page()
                diagnostics = BrowserDiagnostics(page)
                try:
                    checkin_once(page, f'{today.month}/{today.day}', diagnostics)
                finally:
                    diagnostics.report()
            finally:
                browser.close()
    except TrialStopped as error:
        print(error, flush=True)
        return 1
    except Exception as error:
        # Browser errors can contain page contents. Emit only a fixed error type.
        print(f'Browser trial error: {type(error).__name__}.', flush=True)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
