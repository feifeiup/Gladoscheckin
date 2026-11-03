"""Synthetic responses only: no real accounts, HTTP requests or notifications."""
import json
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

import requests
from urllib3._collections import HTTPHeaderDict

import checkin


COOKIE = 'gld:sess=TEST_OLD_SESSION; gld:sess.sig=TEST_OLD_SIGNATURE'


def response(headers=(), status=200, payload=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload or {'code': 0}).encode()
    raw_headers = HTTPHeaderDict()
    for header in headers:
        raw_headers.add('Set-Cookie', header)
    result.raw = SimpleNamespace(headers=raw_headers)
    return result


class CookieDiagnosticTests(unittest.TestCase):
    def inspect(self, result):
        with self.assertLogs(checkin.logger, level='INFO') as captured:
            checkin.log_session_cookie_diagnostics(result, COOKIE, '签到')
        logs = '\n'.join(captured.output)
        for secret in ('TEST_OLD_SESSION', 'TEST_OLD_SIGNATURE', 'TEST_NEW_SESSION',
                       'TEST_NEW_SIGNATURE', 'TEST_PRIVATE_ATTRIBUTE', 'TEST_PRIVATE_NAME'):
            self.assertNotIn(secret, logs)
        return logs

    def test_absent_headers_report_both_session_names(self):
        logs = self.inspect(response())
        self.assertIn('Set-Cookie=无', logs)
        self.assertIn('gld:sess 未观察到下发', logs)
        self.assertIn('gld:sess.sig 未观察到下发', logs)

    def test_unrelated_cookie_names_and_values_are_not_logged(self):
        logs = self.inspect(response(['TEST_PRIVATE_NAME=TEST_NEW_SESSION; Path=/']))
        self.assertIn('Set-Cookie=有', logs)
        self.assertIn('gld:sess 未观察到下发', logs)

    def test_rotated_pair_and_normalized_expiry_without_values(self):
        result = response([
            'gld:sess=TEST_NEW_SESSION; Max-Age=604800; HttpOnly; Secure',
            'gld:sess.sig=TEST_NEW_SIGNATURE; Expires=Thu, 01 Jan 2099 03:00:00 GMT',
        ])
        logs = self.inspect(result)
        self.assertEqual(logs.count('动作=值变化'), 2)
        self.assertIn('Max-Age秒=604800', logs)
        self.assertIn('ExpiresUTC=2099-01-01T03:00:00Z', logs)
        self.assertEqual(result.cookies.get_dict(), {})

    def test_unchanged_and_new_cookie_are_distinguished(self):
        logs = self.inspect(response(['gld:sess=TEST_OLD_SESSION']))
        self.assertIn('动作=值未变化', logs)
        with self.assertLogs(checkin.logger, level='INFO') as captured:
            checkin.log_session_cookie_diagnostics(response(['gld:sess=new']), '', '状态')
        self.assertIn('动作=新增', '\n'.join(captured.output))

    def test_deletion_header_is_observed_even_when_cookiejar_is_empty(self):
        logs = self.inspect(response([
            'gld:sess=; Max-Age=0; Expires=Thu, 01 Jan 1970 00:00:00 GMT',
            'gld:sess.sig=TEST_NEW_SIGNATURE; Expires=Thu, 01 Jan 1970 00:00:00 GMT',
        ], status=403))
        self.assertEqual(logs.count('动作=清除或空值'), 2)
        self.assertIn('HTTP 403', logs)

    def test_valid_max_age_takes_precedence_over_past_expires(self):
        logs = self.inspect(response([
            'gld:sess=TEST_NEW_SESSION; Max-Age=600; Expires=Thu, 01 Jan 1970 00:00:00 GMT'
        ]))
        self.assertIn('动作=值变化', logs)
        self.assertNotIn('动作=清除', logs)

    def test_invalid_expiry_attributes_and_parser_errors_do_not_leak(self):
        logs = self.inspect(response([
            'gld:sess=TEST_NEW_SESSION; Max-Age=TEST_PRIVATE_ATTRIBUTE; Expires=TEST_PRIVATE_ATTRIBUTE',
            'invalid[TEST_PRIVATE_NAME]=TEST_NEW_SIGNATURE',
        ]))
        self.assertIn('Max-Age秒=无效', logs)
        self.assertIn('ExpiresUTC=无效', logs)
        self.assertIn('解析=不完整', logs)

    def test_silently_unparseable_header_is_not_reported_as_complete(self):
        logs = self.inspect(response(['not a valid cookie TEST_PRIVATE_ATTRIBUTE']))
        self.assertIn('解析=不完整', logs)

    def test_combined_header_without_raw_headers_reports_unknown(self):
        result = response()
        result.raw = None
        result.headers['Set-Cookie'] = 'gld:sess=TEST_NEW_SESSION, gld:sess.sig=TEST_NEW_SIGNATURE'
        logs = self.inspect(result)
        self.assertIn('换发情况未知', logs)
        self.assertNotIn('未观察到下发', logs)

    def test_diagnostic_failure_preserves_success_response_and_request_count(self):
        result = response()
        def broken_headers(_):
            raise RuntimeError('TEST_PRIVATE_ATTRIBUTE')
        result.raw.headers = SimpleNamespace(getlist=broken_headers)
        with patch.object(checkin.requests, 'get', return_value=result) as get:
            with self.assertLogs(checkin.logger, level='WARNING') as captured:
                actual = checkin.make_request(checkin.STATUS_URL, 'GET', {}, cookies=COOKIE)
        self.assertIs(actual, result)
        get.assert_called_once()
        self.assertEqual(get.call_args.kwargs['headers']['cookie'], COOKIE)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])
        self.assertNotIn('TEST_PRIVATE_ATTRIBUTE', '\n'.join(captured.output))

    def test_auth_http_failure_keeps_original_return_value(self):
        result = response(['gld:sess=; Max-Age=0'], status=403)
        with patch.object(checkin.requests, 'get', return_value=result) as get:
            with self.assertLogs(checkin.logger, level='INFO') as captured:
                actual = checkin.make_request(checkin.STATUS_URL, 'GET', {}, cookies=COOKIE)
        self.assertIsNone(actual)
        get.assert_called_once()
        self.assertIn('动作=清除或空值', '\n'.join(captured.output))

    def test_account_uses_same_three_requests_without_applying_new_values(self):
        post_result = response(['gld:sess=TEST_NEW_SESSION; Max-Age=604800'])
        get_results = [response(payload={'data': {'leftDays': 10}}),
                       response(payload={'points': 100})]
        with patch.dict(os.environ, {'GLADOS_USER_AGENT': 'Test browser'}, clear=True):
            with patch.object(checkin.requests, 'post', return_value=post_result) as post:
                with patch.object(checkin.requests, 'get', side_effect=get_results) as get:
                    with self.assertLogs(checkin.logger, level='INFO'):
                        result = checkin.checkin_and_process(COOKIE, 'plan500')
        self.assertIn('签到成功', result[0])
        post.assert_called_once()
        self.assertEqual(post.call_args.args, (checkin.CHECKIN_URL,))
        self.assertEqual(get.call_count, 2)
        self.assertEqual([call.args[0] for call in get.call_args_list],
                         [checkin.STATUS_URL, checkin.POINTS_URL])
        for call in [post.call_args] + get.call_args_list:
            self.assertEqual(call.kwargs['headers']['cookie'], COOKIE)
            self.assertEqual(call.kwargs['timeout'], checkin.REQUEST_TIMEOUT)
            self.assertFalse(call.kwargs['allow_redirects'])


if __name__ == '__main__':
    unittest.main()
