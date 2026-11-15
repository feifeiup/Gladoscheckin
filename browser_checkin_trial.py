"""One hosted-browser observation through the official GLaDOS check-in page."""

import os
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from http.cookies import SimpleCookie
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError, sync_playwright


HOST = 'glados.cloud'
CONSOLE_URL = f'https://{HOST}/console/checkin'
SESSION_COOKIES = ('gld:sess', 'gld:sess.sig')
POINTS = re.compile(r'(?im)^[ \t]*points[ \t]*\n[ \t]*(\d+(?:\.\d+)?)[ \t]*$')
DAYS_LEFT = re.compile(r'(?mi)^[ \t]*days left[ \t]*\n[ \t]*\d+')
REWARD = re.compile(r'✓\s*\+(\d+(?:\.\d+)?)')
CHALLENGE_FRAME = 'iframe[src^="https://challenges.cloudflare.com/"]'


class TrialStopped(Exception):
    """A fixed, credential-free message describing why the trial stopped."""


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
    return page.locator(CHALLENGE_FRAME).first.is_visible()


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
    url = urlsplit(response.url)
    return (
        url.scheme == 'https' and url.netloc == HOST and url.path == '/api/user/checkin'
        and response.request.method == 'POST'
    )


def checkin_once(page, day):
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
    try:
        with page.expect_response(is_checkin_response, timeout=20000) as pending:
            button.click(timeout=5000)
        response = pending.value
    except TimeoutError:
        if visible_verification(page):
            raise TrialStopped('Cloudflare verification appeared after the check-in click. No interaction with it.')
        raise TrialStopped('No completed check-in response after the single click. No retry.')

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
                checkin_once(context.new_page(), f'{today.month}/{today.day}')
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
