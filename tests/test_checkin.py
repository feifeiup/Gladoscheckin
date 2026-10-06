import json
import os
import unittest
from unittest.mock import patch

import requests

import checkin


COOKIE = 'gld:sess=test-session; gld:sess.sig=test-signature; other=value'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0'


def response(payload):
    result = requests.Response()
    result.status_code = 200
    result._content = json.dumps(payload).encode('utf-8')
    return result


class AccountTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {'GLADOS_USER_AGENT': UA}, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)

    def run_account(self, payloads):
        with patch.object(checkin, 'make_request', side_effect=[
            response(value) if value is not None else None for value in payloads
        ]) as request:
            result = checkin.checkin_and_process(COOKIE, 'plan500')
        return result, request

    def test_old_cookie_stops_before_network(self):
        with patch.object(checkin, 'make_request') as request:
            result = checkin.checkin_and_process('koa:sess=old; koa:sess.sig=old', 'plan500')
        self.assertIn('gld:sess', result[0])
        self.assertIn('失败', result[0])
        request.assert_not_called()

    def test_missing_signature_stops_before_network(self):
        with patch.object(checkin, 'make_request') as request:
            result = checkin.checkin_and_process('gld:sess=new', 'plan500')
        self.assertIn('失败', result[0])
        request.assert_not_called()

    def test_permission_denied_stops_followup_requests(self):
        result, request = self.run_account([{'code': -2, 'message': '没有权限'}])
        self.assertIn('认证失败', result[0])
        self.assertIn('未知', result[3])
        self.assertNotIn('积分不足', result[4])
        self.assertEqual(request.call_count, 1)

    def test_device_mismatch_is_failure_even_with_success_message(self):
        result, request = self.run_account([{
            'code': 4, 'reason': 'device-mismatch', 'message': "Today's observation logged."
        }])
        self.assertIn('GLADOS_USER_AGENT', result[0])
        self.assertIn('失败', result[0])
        self.assertEqual(request.call_count, 1)

    def test_new_observation_and_existing_checkin_messages(self):
        for payload in (
            {'code': 0, 'message': 'Checkin! Got 12 Points', 'points': 12},
            {'code': 1, 'message': "Today's observation logged. Return tomorrow for more points.", 'points': 12},
        ):
            with self.subTest(payload=payload):
                result, request = self.run_account([
                    payload, {'code': 0, 'data': {'leftDays': '23.9'}}, {'points': '120.0'}
                ])
                self.assertIn('签到成功', result[0])
                self.assertEqual(result[1:4], ('12', '23 天', '120 积分'))
                self.assertIn('积分不足', result[4])
                self.assertEqual(request.call_count, 3)

    def test_repeat_is_normal(self):
        result, _ = self.run_account([
            {'code': 1, 'message': 'Checkin Repeats! Please Try Tomorrow'},
            {'data': {'leftDays': 10}}, {'points': 0},
        ])
        self.assertIn('重复', result[0])
        self.assertEqual(result[3], '0 积分')

    def test_unknown_and_non_object_checkin_do_not_continue(self):
        for payload in ([], None, {'code': 4, 'message': 'Checkin! Got 12'}, {'message': 'unknown'}):
            with self.subTest(payload=payload):
                result, request = self.run_account([payload])
                self.assertIn('失败', result[0])
                self.assertEqual(request.call_count, 1)

    def test_unknown_points_never_exchange_or_claim_zero(self):
        for payload in (None, [], {'code': 0}, {'points': 'NaN'}, {'points': {}}, {'points': True}, {'code': 4, 'points': 999}):
            with self.subTest(payload=payload):
                result, request = self.run_account([
                    {'code': 0, 'points': 12}, {'data': {'leftDays': 10}}, payload
                ])
                self.assertIn('获取剩余积分失败', result[3])
                self.assertIn('余额未知', result[4])
                self.assertEqual(request.call_count, 3)

    def test_auth_expires_during_queries(self):
        result, request = self.run_account([
            {'code': 0}, {'code': -2, 'message': '没有权限'}
        ])
        self.assertIn('获取剩余天数失败', result[2])
        self.assertIn('未兑换', result[4])
        self.assertEqual(request.call_count, 2)

    def test_bad_status_prevents_exchange(self):
        for payload in ({'data': None}, {'data': {'leftDays': 'bad'}}, {'code': 4, 'data': {'leftDays': 10}}):
            with self.subTest(payload=payload):
                result, request = self.run_account([{'code': 0}, payload, {'points': 500}])
                self.assertIn('获取剩余天数失败', result[2])
                self.assertIn('状态查询失败', result[4])
                self.assertEqual(request.call_count, 3)

    def test_exchange_runs_once_only_after_valid_balance(self):
        result, request = self.run_account([
            {'code': 0}, {'data': {'leftDays': 10}}, {'points': 500}, {'code': 0}
        ])
        self.assertIn('兑换成功', result[4])
        self.assertEqual(request.call_count, 4)
        self.assertEqual(request.call_args.args[:2], (checkin.EXCHANGE_URL, 'POST'))
        self.assertEqual(request.call_args.args[3], {'planType': 'plan500'})

    def test_headers_and_cookie_prefix(self):
        self.assertEqual(checkin.normalize_cookie(' Cookie: ' + COOKIE), COOKIE)
        headers = checkin.get_request_headers()
        self.assertEqual(headers['user-agent'], UA)
        self.assertIn('141', headers['sec-ch-ua'])
        self.assertEqual(headers['sec-ch-ua-platform'], '"Windows"')

    def test_expired_status_counts_as_failure_in_notification(self):
        title, _ = checkin.format_push_content([{
            'status': '签到成功', 'points': '12', 'days': '获取剩余天数失败（认证失败）',
            'points_total': '未知（认证未通过）', 'exchange': '未兑换（认证未通过）'
        }])
        self.assertIn('失败1', title)


class TransportTests(unittest.TestCase):
    def test_post_uses_json_and_preserves_full_cookie(self):
        with patch.object(checkin.requests, 'post', return_value=response({'code': 0})) as post:
            checkin.make_request(checkin.CHECKIN_URL, 'POST', {}, checkin.CHECKIN_DATA, cookies=COOKIE)
        self.assertEqual(post.call_args.kwargs['json'], {'token': 'glados.cloud'})
        self.assertEqual(post.call_args.kwargs['headers']['cookie'], COOKIE)
        self.assertEqual(post.call_args.kwargs['timeout'], checkin.REQUEST_TIMEOUT)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])

    def test_timeout_does_not_leak_exception_message(self):
        with patch.object(checkin.requests, 'get', side_effect=requests.Timeout('private-data')):
            with self.assertLogs(checkin.logger, level='ERROR') as logs:
                self.assertIsNone(checkin.make_request(checkin.STATUS_URL, 'GET', {}))
        self.assertNotIn('private-data', '\n'.join(logs.output))

    def test_timeout_redirect_policy_and_http_error_no_body_leak(self):
        denied = requests.Response()
        denied.status_code = 403
        denied._content = b'private-cookie-and-account-data'
        with patch.object(checkin.requests, 'get', return_value=denied) as get:
            with self.assertLogs(checkin.logger, level='WARNING') as logs:
                result = checkin.make_request(checkin.STATUS_URL, 'GET', {}, cookies=COOKIE)
        self.assertIsNone(result)
        self.assertNotIn('private-cookie', '\n'.join(logs.output))
        self.assertEqual(get.call_args.kwargs['timeout'], checkin.REQUEST_TIMEOUT)
        self.assertFalse(get.call_args.kwargs['allow_redirects'])

    def test_invalid_json_does_not_leak_response(self):
        bad = requests.Response()
        bad.status_code = 200
        bad._content = b'private-account-data'
        with self.assertLogs(checkin.logger, level='ERROR') as logs:
            self.assertIsNone(checkin.read_payload(bad, '签到'))
        self.assertNotIn('private-account', '\n'.join(logs.output))


class MainTests(unittest.TestCase):
    def test_failure_not_hidden_by_successful_push_and_other_account_continues(self):
        results = [
            ('签到失败: 认证失败', '0', '未知', '未知', '未兑换（认证未通过）'),
            ('重复签到，明天再来', '0', '10 天', '10 积分', '积分不足，未兑换'),
        ]
        with patch.object(checkin, 'load_config', return_value=('test-key', [COOKIE, COOKIE], 'plan500')):
            with patch.object(checkin, 'checkin_and_process', side_effect=results) as account:
                with patch.object(checkin, 'PushDeer') as push:
                    self.assertEqual(checkin.main(), 1)
        self.assertEqual(account.call_count, 2)
        push.return_value.send_text.assert_called_once()

    def test_success_and_repeat_exit_zero(self):
        for status in ('签到成功，获得 12 积分', '重复签到，明天再来'):
            with self.subTest(status=status):
                with patch.object(checkin, 'load_config', return_value=('', [COOKIE], 'plan500')):
                    with patch.object(checkin, 'checkin_and_process', return_value=(
                        status, '0', '10 天', '10 积分', '积分不足，未兑换'
                    )):
                        self.assertEqual(checkin.main(), 0)

    def test_config_exception_does_not_reference_uninitialized_push_key(self):
        with patch.object(checkin, 'load_config', side_effect=ValueError('private-data')):
            with self.assertLogs(checkin.logger, level='ERROR') as logs:
                self.assertEqual(checkin.main(), 1)
        self.assertNotIn('private-data', '\n'.join(logs.output))

    def test_query_failure_is_nonzero(self):
        with patch.object(checkin, 'load_config', return_value=('', [COOKIE], 'plan500')):
            with patch.object(checkin, 'checkin_and_process', return_value=(
                '签到成功，获得 12 积分', '12', '10 天', '获取剩余积分失败', '未兑换（积分查询失败，余额未知）'
            )):
                self.assertEqual(checkin.main(), 1)


if __name__ == '__main__':
    unittest.main()
