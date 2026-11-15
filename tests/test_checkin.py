import json
import os
import unittest
from unittest.mock import patch

import requests

import checkin


COOKIE = 'gld:sess=test-session; gld:sess.sig=test-signature; other=value'
UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0'
CHALLENGE = {'code': 0, 'nonce': 'test-challenge', 'ttl': 300_000}


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
        self.sleep = self.enterContext(patch.object(checkin.time, 'sleep'))

    def run_account(self, payloads, *, challenge=CHALLENGE):
        with patch.object(checkin, 'make_request', side_effect=[
            response(value) if value is not None else None for value in [challenge, *payloads]
        ]) as request:
            result = checkin.checkin_and_process(COOKIE, 'plan500')
        return result, request

    def test_old_cookie_stops_before_network(self):
        with patch.object(checkin, 'make_request') as request:
            result = checkin.checkin_and_process('koa:sess=old; koa:sess.sig=old', 'plan500')
        self.assertIn('gld:sess', result[0])
        self.assertIn('失败', result[0])
        request.assert_not_called()

    def test_valid_challenge_is_used_for_one_checkin_post(self):
        result, request = self.run_account([
            {'code': 0, 'points': 12}, {'data': {'leftDays': 10}}, {'points': 200},
        ])
        self.assertEqual(result[1:4], ('12', '10 天', '200 积分'))
        self.sleep.assert_called_once_with(2.7)
        calls = request.call_args_list
        self.assertEqual([call.args[:2] for call in calls], [
            (checkin.CHECKIN_CHALLENGE_URL, 'GET'), (checkin.CHECKIN_URL, 'POST'),
            (checkin.STATUS_URL, 'GET'), (checkin.POINTS_URL, 'GET'),
        ])
        self.assertEqual(calls[1].args[3], {
            'token': 'glados.cloud', 'nonce': 'test-challenge',
            'proof': 'b96970356b316a7b8cbf726351f3dfb7362d9faae20ad1ec47035ef36ddb5c87',
        })
        self.assertEqual(calls[0].args[2], calls[1].args[2])
        self.assertEqual(calls[0].kwargs['cookies'], calls[1].kwargs['cookies'])

    def test_challenge_auth_rejection_stops_before_checkin(self):
        result, request = self.run_account([], challenge={'code': -2, 'message': '没有权限'})
        self.assertIn('认证失败', result[0])
        self.assertEqual(request.call_args.args[:2], (checkin.CHECKIN_CHALLENGE_URL, 'GET'))
        request.assert_called_once()
        self.sleep.assert_not_called()

    def test_invalid_challenge_stops_before_checkin(self):
        for challenge in (None, [], {'code': 0}, {**CHALLENGE, 'nonce': '\ud800'},
                          {**CHALLENGE, 'ttl': 2700}, {**CHALLENGE, 'ttl': '300000'}):
            with self.subTest(challenge=challenge):
                result, request = self.run_account([], challenge=challenge)
                self.assertIn('签到失败', result[0])
                self.assertEqual(request.call_args.args[:2], (checkin.CHECKIN_CHALLENGE_URL, 'GET'))
                request.assert_called_once()
        self.sleep.assert_not_called()

    def test_challenge_expiring_while_waiting_is_not_submitted(self):
        with patch.object(checkin.time, 'monotonic', side_effect=[10, 13]):
            result, request = self.run_account([], challenge={**CHALLENGE, 'ttl': 3000})
        self.assertIn('签到校验已过期', result[0])
        request.assert_called_once()
        self.sleep.assert_called_once_with(2.7)

    def test_failed_checkin_post_is_never_retried(self):
        result, request = self.run_account([None])
        self.assertIn('签到失败', result[0])
        self.assertEqual([call.args[:2] for call in request.call_args_list], [
            (checkin.CHECKIN_CHALLENGE_URL, 'GET'), (checkin.CHECKIN_URL, 'POST'),
        ])

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
        self.assertEqual(request.call_count, 2)

    def test_device_mismatch_is_failure_even_with_success_message(self):
        result, request = self.run_account([{
            'code': 4, 'reason': 'device-mismatch', 'message': "Today's observation logged."
        }])
        self.assertIn('GLADOS_USER_AGENT', result[0])
        self.assertIn('失败', result[0])
        self.assertEqual(request.call_count, 2)

    def test_new_checkin_reports_numeric_rewards(self):
        for payload, expected in (
            ({'code': 0, 'message': 'Checkin! Got 12 Points', 'points': 12}, '12'),
            ({'code': 1, 'message': 'Checkin! Got 12 Points', 'points': 12}, '12'),
            ({'code': 0, 'points': '0.5'}, '0.5'),
        ):
            with self.subTest(payload=payload):
                result, request = self.run_account([
                    payload, {'code': 0, 'data': {'leftDays': '23.9'}}, {'points': '120.0'}
                ])
                self.assertIn('签到成功', result[0])
                self.assertEqual(result[1:4], (expected, '23 天', '120 积分'))
                self.assertIn('积分不足', result[4])
                self.assertEqual(request.call_count, 4)

    def test_repeat_is_normal(self):
        for payload in (
            {'code': 1, 'message': 'Checkin Repeats! Please Try Tomorrow'},
            {'code': 1, 'message': "Today's observation logged. Return tomorrow for more points.", 'points': 0},
        ):
            with self.subTest(payload=payload):
                result, request = self.run_account([payload, {'data': {'leftDays': 10}}, {'points': 226}])
                self.assertEqual(result[:4], ('今日已签到（重复提交）', '0', '10 天', '226 积分'))
                self.assertEqual(request.call_count, 4)
                title, _ = checkin.format_push_content([dict(zip(
                    ('status', 'points', 'days', 'points_total', 'exchange'), result
                ))])
                self.assertEqual(title, 'GLaDOS 签到, 成功0, 失败0, 重复1')

    def test_ambiguous_acceptance_does_not_count_as_success_or_repeat(self):
        for payload, expected in (
            ({'code': 0, 'points': 0, 'message': 'private-message'}, '0'),
            ({'code': 0}, '未知'),
            ({'code': 1, 'message': "Today's observation logged.", 'points': True}, '未知'),
            ({'code': 1, 'message': "Today's observation logged.", 'points': 12}, '未知'),
        ):
            with self.subTest(payload=payload):
                with self.assertLogs(checkin.logger, level='INFO') as logs:
                    result, request = self.run_account([payload, {'data': {'leftDays': 10}}, {'points': 226}])
                self.assertEqual(result[:4], ('签到待确认，请查看官网签到记录', expected, '10 天', '226 积分'))
                self.assertEqual(request.call_count, 4)
                self.assertIn(f"业务码={payload['code']}，签到待确认", '\n'.join(logs.output))
                self.assertNotIn('private-message', '\n'.join(logs.output))
                title, _ = checkin.format_push_content([dict(zip(
                    ('status', 'points', 'days', 'points_total', 'exchange'), result
                ))])
                self.assertEqual(title, 'GLaDOS 签到, 成功0, 失败0, 重复0, 待确认1')

    def test_unknown_and_non_object_checkin_do_not_continue(self):
        for payload in ([], None, {'code': 4, 'message': 'Checkin! Got 12'}, {'message': 'unknown'}):
            with self.subTest(payload=payload):
                result, request = self.run_account([payload])
                self.assertIn('失败', result[0])
                self.assertEqual(request.call_count, 2)

    def test_unknown_checkin_logs_only_a_bounded_integer_code(self):
        for code, expected in ((5, '5'), (1000, '未知'), ('private-code', '未知')):
            with self.subTest(code=code):
                with self.assertLogs(checkin.logger, level='WARNING') as logs:
                    result, request = self.run_account([{'code': code, 'message': 'private-message'}])
                self.assertEqual(logs.records[-1].getMessage(), f'签到接口返回未识别的结果，业务码: {expected}')
                self.assertIn('签到失败', result[0])
                self.assertEqual(request.call_count, 2)

    def test_explicit_cloudflare_failure_stops_followup_requests(self):
        with self.assertLogs(checkin.logger, level='WARNING') as logs:
            result, request = self.run_account([{
                'code': 5, 'message': 'Cloudflare verification failed. Nothing was recorded. private-data'
            }])
        self.assertIn('签到失败: Cloudflare 验证失败', result[0])
        self.assertNotIn('private-data', '\n'.join(logs.output))
        self.assertEqual(request.call_count, 2)

    def test_unknown_points_never_exchange_or_claim_zero(self):
        for payload in (None, [], {'code': 0}, {'points': 'NaN'}, {'points': {}}, {'points': True}, {'code': 4, 'points': 999}):
            with self.subTest(payload=payload):
                result, request = self.run_account([
                    {'code': 0, 'points': 12}, {'data': {'leftDays': 10}}, payload
                ])
                self.assertIn('获取剩余积分失败', result[3])
                self.assertIn('余额未知', result[4])
                self.assertEqual(request.call_count, 4)

    def test_auth_expires_during_queries(self):
        result, request = self.run_account([
            {'code': 0}, {'code': -2, 'message': '没有权限'}
        ])
        self.assertIn('获取剩余天数失败', result[2])
        self.assertIn('未兑换', result[4])
        self.assertEqual(request.call_count, 3)

    def test_bad_status_prevents_exchange(self):
        for payload in ({'data': None}, {'data': {'leftDays': 'bad'}}, {'code': 4, 'data': {'leftDays': 10}}):
            with self.subTest(payload=payload):
                result, request = self.run_account([{'code': 0}, payload, {'points': 500}])
                self.assertIn('获取剩余天数失败', result[2])
                self.assertIn('状态查询失败', result[4])
                self.assertEqual(request.call_count, 4)

    def test_exchange_runs_once_only_after_valid_balance(self):
        result, request = self.run_account([
            {'code': 0}, {'data': {'leftDays': 10}}, {'points': 500}, {'code': 0}
        ])
        self.assertIn('兑换成功', result[4])
        self.assertEqual(request.call_count, 5)
        self.assertEqual(request.call_args.args[:2], (checkin.EXCHANGE_URL, 'POST'))
        self.assertEqual(request.call_args.args[3], {'planType': 'plan500'})

    def test_headers_and_cookie_prefix(self):
        self.assertEqual(checkin.normalize_cookie(' Cookie: ' + COOKIE), COOKIE)
        headers = checkin.get_request_headers()
        self.assertEqual(headers['user-agent'], UA)
        self.assertIn('141', headers['sec-ch-ua'])
        self.assertEqual(headers['sec-ch-ua-platform'], '"Windows"')

    def test_expired_status_counts_as_failure_in_notification(self):
        title, content = checkin.format_push_content([{
            'status': '签到成功', 'points': '12', 'days': '获取剩余天数失败（认证失败）',
            'points_total': '未知（认证未通过）', 'exchange': '未兑换（认证未通过）'
        }])
        self.assertEqual(title, 'GLaDOS 签到, 成功1, 失败1, 重复0')
        self.assertNotIn('https://glados.space/console/checkin', content)

    def test_unrecognized_result_is_not_counted_as_success_in_notification(self):
        title, _ = checkin.format_push_content([{
            'status': '签到失败: 接口未返回可识别的成功结果', 'points': '0', 'days': '未知',
            'points_total': '未知', 'exchange': '未兑换（签到失败）'
        }])
        self.assertEqual(title, 'GLaDOS 签到, 成功0, 失败1, 重复0')


class TransportTests(unittest.TestCase):
    def test_post_uses_json_and_preserves_full_cookie(self):
        payload = {'token': 'glados.cloud', 'nonce': 'test-challenge', 'proof': 'test-proof'}
        with patch.object(checkin.requests, 'post', return_value=response({'code': 0})) as post:
            checkin.make_request(checkin.CHECKIN_URL, 'POST', {}, payload, cookies=COOKIE)
        self.assertEqual(post.call_args.kwargs['json'], payload)
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
    def setUp(self):
        environment = patch.dict(os.environ, {'GLADOS_PUSH_PROVIDER': 'pushdeer'}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_failure_not_hidden_by_successful_push_and_other_account_continues(self):
        results = [
            ('签到失败: 认证失败', '0', '未知', '未知', '未兑换（认证未通过）'),
            ('今日已签到（重复提交）', '0', '10 天', '10 积分', '积分不足，未兑换'),
        ]
        with patch.object(checkin, 'load_config', return_value=('test-key', [COOKIE, COOKIE], 'plan500')):
            with patch.object(checkin, 'checkin_and_process', side_effect=results) as account:
                with patch.object(checkin, 'PushDeer') as push:
                    push.return_value.send_text.return_value = True
                    self.assertEqual(checkin.main(), 1)
        self.assertEqual(account.call_count, 2)
        push.return_value.send_text.assert_called_once()
        content = push.return_value.send_text.call_args.kwargs['desp']
        self.assertIn('签到失败: 认证失败', content)
        self.assertIn('\n\n[打开 GLaDOS 官网签到](https://glados.space/console/checkin)', content)

    def test_success_and_repeat_exit_zero(self):
        for status, points in (('签到成功，获得 12 积分', '12'), ('今日已签到（重复提交）', '0')):
            with self.subTest(status=status):
                with (
                    patch.object(checkin, 'load_config', return_value=('test-key', [COOKIE], 'plan500')),
                    patch.object(checkin, 'checkin_and_process', return_value=(
                        status, points, '10 天', '200 积分', '积分不足，未兑换'
                    )),
                    patch.object(checkin, 'send_notification', return_value=True) as push,
                ):
                    self.assertEqual(checkin.main(), 0)
                push.assert_called_once()
                content = push.call_args.args[2]
                self.assertIn(status, content)
                self.assertIn(f'P:{points}', content)
                self.assertIn('总积分:200 积分', content)
                self.assertNotIn('https://glados.space/console/checkin', content)

    def test_missing_cookies_sends_manual_checkin_link_without_attempt(self):
        for cookies in ('', ' & '):
            with self.subTest(cookies=cookies):
                with (
                    patch.dict(os.environ, {'PUSHDEER_SENDKEY': 'test-key', 'GLADOS_COOKIES': cookies}),
                    patch.object(checkin, 'checkin_and_process') as account,
                    patch.object(checkin, 'send_notification', return_value=True) as push,
                ):
                    self.assertEqual(checkin.main(), 1)
                account.assert_not_called()
                push.assert_called_once()
                self.assertIn('https://glados.space/console/checkin', push.call_args.args[2])

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

    def test_rejected_push_fails_even_when_checkin_succeeded(self):
        with patch.object(checkin, 'load_config', return_value=('fake-key', [COOKIE], 'plan500')):
            with patch.object(checkin, 'checkin_and_process', return_value=(
                '签到成功', '6', '388 天', '439 积分', '积分不足，未兑换'
            )):
                with patch.object(checkin, 'PushDeer') as push:
                    push.return_value.send_text.return_value = False
                    with self.assertLogs(checkin.logger, level='ERROR') as logs:
                        self.assertEqual(checkin.main(), 1)
        self.assertNotIn('发送成功', '\n'.join(logs.output))


class NotificationTests(unittest.TestCase):
    KEY = 'sctp123tFakeTestToken'

    def setUp(self):
        environment = patch.dict(os.environ, {}, clear=True)
        environment.start()
        self.addCleanup(environment.stop)

    def test_configuration_uses_only_serverchan_key_by_default(self):
        with patch.dict(os.environ, {
            'SERVERCHAN_SENDKEY': self.KEY, 'SENDKEY': 'old-token',
            'PUSHDEER_SENDKEY': 'old-pushdeer', 'GLADOS_COOKIES': COOKIE
        }):
            key, cookies, plan = checkin.load_config()
        self.assertEqual(key, self.KEY)
        self.assertEqual(cookies, [COOKIE])
        self.assertEqual(plan, 'plan500')

    def test_missing_serverchan_key_does_not_fallback_to_other_service(self):
        with patch.dict(os.environ, {'PUSHDEER_SENDKEY': 'old-token', 'GLADOS_COOKIES': COOKIE}):
            key, _, _ = checkin.load_config()
        self.assertEqual(key, '')

    def test_serverchan_acceptance_url_body_and_timeout(self):
        with patch.object(checkin.requests, 'post', return_value=response({'code': 0})) as post:
            with patch.object(checkin, 'PushDeer') as old:
                self.assertTrue(checkin.send_notification(self.KEY, '签到', '测试摘要'))
        self.assertEqual(post.call_args.args[0], 'https://123.push.ft07.com/send/' + self.KEY + '.send')
        self.assertEqual(post.call_args.kwargs['json'], {'title': '签到', 'desp': '测试摘要'})
        self.assertEqual(post.call_args.kwargs['timeout'], checkin.REQUEST_TIMEOUT)
        self.assertFalse(post.call_args.kwargs['allow_redirects'])
        old.assert_not_called()

    def test_api_rejections_do_not_claim_success_or_leak_body(self):
        for payload in ({'code': 1, 'message': self.KEY}, {'code': False}, {'code': '0'}, {}, [], None):
            with self.subTest(payload=payload):
                with patch.object(checkin.requests, 'post', return_value=response(payload)):
                    with self.assertLogs(checkin.logger, level='ERROR') as logs:
                        self.assertFalse(checkin.send_notification(self.KEY, '标题', '内容'))
                self.assertNotIn(self.KEY, '\n'.join(logs.output))
                self.assertNotIn('已接受', '\n'.join(logs.output))

    def test_http_error_json_error_and_timeout_are_bounded_failures(self):
        bad_http = response({'code': 0, 'private': self.KEY})
        bad_http.status_code = 403
        bad_json = response({'code': 0})
        bad_json._content = self.KEY.encode('utf-8')
        for item in (bad_http, bad_json, requests.Timeout(self.KEY)):
            kwargs = {'side_effect': item} if isinstance(item, Exception) else {'return_value': item}
            with self.subTest(item=type(item).__name__):
                with patch.object(checkin.requests, 'post', **kwargs) as post:
                    with self.assertLogs(checkin.logger, level='ERROR') as logs:
                        self.assertFalse(checkin.send_notification(self.KEY, '标题', '内容'))
                self.assertEqual(post.call_count, 1)
                self.assertNotIn(self.KEY, '\n'.join(logs.output))

    def test_invalid_key_never_makes_network_request(self):
        for key in ('SCToldkey', 'sctp123', 'sctp123tbad/route', 'sctp123tbad?query', ''):
            with self.subTest(key=key):
                with patch.object(checkin.requests, 'post') as post:
                    self.assertFalse(checkin.send_notification(key, '标题', '内容'))
                post.assert_not_called()

    def test_legacy_pushdeer_false_is_failure(self):
        with patch.dict(os.environ, {'GLADOS_PUSH_PROVIDER': 'pushdeer'}):
            with patch.object(checkin, 'PushDeer') as push:
                push.return_value.send_text.return_value = False
                with self.assertLogs(checkin.logger, level='ERROR') as logs:
                    self.assertFalse(checkin.send_notification('fake-key', '标题', '内容'))
        self.assertNotIn('已接受', '\n'.join(logs.output))


if __name__ == '__main__':
    unittest.main()
