import requests
import os
import logging
import datetime
import math
import re
from typing import Dict, List, Optional, Tuple
from pypushdeer import PushDeer


def beijing_time_converter(timestamp):
    utc_dt = datetime.datetime.fromtimestamp(timestamp, tz=datetime.timezone.utc)
    beijing_tz = datetime.timezone(datetime.timedelta(hours=8))
    beijing_dt = utc_dt.astimezone(beijing_tz)
    return beijing_dt.timetuple()

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

root_logger = logging.getLogger()
for handler in root_logger.handlers:
    if hasattr(handler, 'formatter') and handler.formatter is not None:
        handler.formatter.converter = beijing_time_converter

logger = logging.getLogger(__name__)


# ENVIRONMENT
ENV_PUSH_KEY = "PUSHDEER_SENDKEY"
ENV_COOKIES = "GLADOS_COOKIES"
ENV_EXCHANGE_PLAN = "GLADOS_EXCHANGE_PLAN"
ENV_USER_AGENT = "GLADOS_USER_AGENT"
REQUEST_TIMEOUT = 20

# API URLs
CHECKIN_URL = "https://glados.cloud/api/user/checkin"
STATUS_URL = "https://glados.cloud/api/user/status"
POINTS_URL = "https://glados.cloud/api/user/points"
EXCHANGE_URL = "https://glados.cloud/api/user/exchange"

# POST DATA
CHECKIN_DATA = {"token": "glados.cloud"}

# Request Headers
HEADERS_TEMPLATE = {
    'referer': 'https://glados.cloud/console/checkin',
    'origin': "https://glados.cloud",
    'user-agent': "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/102.0.0.0 Safari/537.36",
    'content-type': 'application/json;charset=UTF-8'
}

# Exchange Plan Points
EXCHANGE_POINTS = {"plan100": 100, "plan200": 200, "plan500": 500}

def load_config() -> Tuple[str, List[str], str]:
    push_key_env = os.environ.get(ENV_PUSH_KEY)
    raw_cookies_env = os.environ.get(ENV_COOKIES)
    exchange_plan_env = os.environ.get(ENV_EXCHANGE_PLAN)

    if not push_key_env:
        logger.warning(f"环境变量 '{ENV_PUSH_KEY}' 未设置。")
        push_key = ''
    else:
        push_key = push_key_env

    if not raw_cookies_env:
        logger.warning(f"环境变量 '{ENV_COOKIES}' 未设置。")
        cookies_list = []
    else:
        cookies_list = [normalize_cookie(cookie) for cookie in raw_cookies_env.split('&') if cookie.strip()]
        if not cookies_list:
            raise ValueError(f"环境变量 '{ENV_COOKIES}' 已设置，但未包含任何有效的 Cookie。")

    if not exchange_plan_env:
        logger.warning(f"环境变量 '{ENV_EXCHANGE_PLAN}' 未设置，将使用默认兑换计划 'plan500'。")
        exchange_plan = "plan500"
    else:
        if exchange_plan_env in EXCHANGE_POINTS:
             exchange_plan = exchange_plan_env
             logger.info(f"使用指定的兑换计划: {exchange_plan}")
        else:
            logger.warning(f"环境变量 '{ENV_EXCHANGE_PLAN}' 的值 '{exchange_plan_env}' 无效，将使用默认兑换计划 'plan500'。")
            exchange_plan = "plan500"


    logger.info(f"共加载了 {len(cookies_list)} 个 Cookie 用于签到。")
    logger.info(f"当前 {ENV_PUSH_KEY} {'已设置' if push_key_env else '未设置'}。")
    logger.info(f"当前 {ENV_EXCHANGE_PLAN}: {exchange_plan}。")

    return push_key, cookies_list, exchange_plan


def normalize_cookie(cookie: str) -> str:
    cookie = cookie.strip()
    if cookie.lower().startswith('cookie:'):
        cookie = cookie.split(':', 1)[1].strip()
    return cookie


def get_request_headers() -> Dict[str, str]:
    headers = HEADERS_TEMPLATE.copy()
    user_agent = os.environ.get(ENV_USER_AGENT, '').strip()
    if user_agent:
        headers['user-agent'] = user_agent
    chrome = re.search(r'(?:Chrome|Chromium)/(\d+)', headers['user-agent'])
    if chrome:
        major = chrome.group(1)
        ua = headers['user-agent']
        platform = next((name for marker, name in (
            ('Android', 'Android'), ('Windows', 'Windows'),
            ('Macintosh', 'macOS'), ('Linux', 'Linux')
        ) if marker in ua), 'Unknown')
        headers.update({
            'sec-ch-ua': f'"Chromium";v="{major}", "Not_A Brand";v="99"',
            'sec-ch-ua-mobile': '?1' if 'Mobile' in ua else '?0',
            'sec-ch-ua-platform': f'"{platform}"',
        })
    return headers


def authentication_error(payload: Dict) -> Optional[str]:
    message = str(payload.get('message', '')).lower()
    if payload.get('reason') == 'device-mismatch' or 'automated check-in detected' in message:
        return '登录设备不匹配，请重新登录并更新完整 Cookie 和 GLADOS_USER_AGENT'
    if payload.get('code') == -2 or any(marker in message for marker in (
        '没有权限', 'unauthorized', 'permission', '未登录', '登录失效'
    )):
        return '认证失败，请重新登录 glados.cloud，更新含 gld:sess 和 gld:sess.sig 的 GLADOS_COOKIES'
    return None


def read_payload(response: Optional[requests.Response], label: str) -> Optional[Dict]:
    if response is None:
        return None
    try:
        payload = response.json()
    except ValueError:
        logger.error('%s响应不是有效 JSON。', label)
        return None
    if not isinstance(payload, dict):
        logger.error('%s响应结构异常：应为 JSON 对象。', label)
        return None
    return payload


def numeric_value(value) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return int(number) if math.isfinite(number) and number >= 0 else None
    except (ValueError, TypeError, OverflowError):
        return None


def make_request(url: str, method: str, headers: Dict[str, str], data: Optional[Dict] = None, cookies: str = "") -> Optional[requests.Response]:

    session_headers = headers.copy()
    session_headers['cookie'] = cookies

    try:
        if method.upper() == 'POST':
            response = requests.post(url, headers=session_headers, json=data, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        elif method.upper() == 'GET':
            response = requests.get(url, headers=session_headers, timeout=REQUEST_TIMEOUT, allow_redirects=False)
        else:
            logger.error(f"不支持的 HTTP 方法: {method}")
            return None

        if not 200 <= response.status_code < 300:
            logger.warning(f"向 {url} 发起的请求失败，状态码 {response.status_code}。")
            if response.status_code in (401, 403):
                logger.error('认证被拒绝，请更新 GLADOS_COOKIES 和 GLADOS_USER_AGENT。')
            return None
        return response
    except requests.exceptions.RequestException as e:
        logger.error('向 %s 发起请求时发生网络错误 (%s)。', url, type(e).__name__)
        return None


def checkin_and_process(cookie: str, exchange_plan: str) -> Tuple[str, str, str, str, str]:
    cookie = normalize_cookie(cookie)
    cookie_values = dict(part.strip().split('=', 1) for part in cookie.split(';') if '=' in part)
    if not all(cookie_values.get(name) for name in ('gld:sess', 'gld:sess.sig')):
        return ('签到失败: Cookie 缺少 gld:sess 或 gld:sess.sig，请重新登录并更新 GLADOS_COOKIES',
                '0', '未知（认证未通过）', '未知（认证未通过）', '未兑换（认证未通过）')

    headers = get_request_headers()
    if not os.environ.get(ENV_USER_AGENT, '').strip():
        logger.warning('GLADOS_USER_AGENT 未设置；设备校验失败时请复制登录浏览器的 User-Agent。')
    checkin_data = read_payload(make_request(
        CHECKIN_URL, 'POST', headers, CHECKIN_DATA, cookies=cookie
    ), '签到')
    if checkin_data is None:
        return '签到失败: 请求或响应解析失败', '0', '未知', '未知', '未兑换（签到失败）'
    auth_error = authentication_error(checkin_data)
    if auth_error:
        return f'签到失败: {auth_error}', '0', '未知（认证未通过）', '未知（认证未通过）', '未兑换（认证未通过）'

    message = str(checkin_data.get('message', '')).lower()
    code = checkin_data.get('code')
    points_gained = str(numeric_value(checkin_data.get('points')) or 0)
    if 'checkin repeats!' in message and code in (None, 0, 1):
        status_msg, points_gained = '重复签到，明天再来', '0'
    elif code == 0 or (code in (None, 1) and any(marker in message for marker in (
        'checkin! got', "today's observation logged"
    ))):
        status_msg = f'签到成功，获得 {points_gained} 积分'
    else:
        return '签到失败: 接口未返回可识别的成功结果', '0', '未知', '未知', '未兑换（签到失败）'

    status_data = read_payload(make_request(STATUS_URL, 'GET', headers, cookies=cookie), '状态')
    remaining_days = '获取剩余天数失败（请求或响应异常）'
    if status_data is not None:
        auth_error = authentication_error(status_data)
        if auth_error:
            return status_msg, points_gained, f'获取剩余天数失败（{auth_error}）', '未知（认证未通过）', '未兑换（认证未通过）'
        account_data = status_data.get('data')
        left_days = numeric_value(account_data.get('leftDays')) if isinstance(account_data, dict) else None
        if status_data.get('code') in (None, 0) and left_days is not None:
            remaining_days = f'{left_days} 天'

    points_data = read_payload(make_request(POINTS_URL, 'GET', headers, cookies=cookie), '积分')
    current_points = None
    remaining_points = '获取剩余积分失败（请求或响应异常）'
    if points_data is not None:
        auth_error = authentication_error(points_data)
        if auth_error:
            return status_msg, points_gained, remaining_days, f'获取剩余积分失败（{auth_error}）', '未兑换（认证未通过）'
        if points_data.get('code') in (None, 0):
            current_points = numeric_value(points_data.get('points'))
        if current_points is not None:
            remaining_points = f'{current_points} 积分'

    required_points = EXCHANGE_POINTS.get(exchange_plan, 500)
    if current_points is None:
        exchange_msg = '未兑换（积分查询失败，余额未知）'
    elif remaining_days.startswith('获取'):
        exchange_msg = '未兑换（状态查询失败）'
    elif current_points < required_points:
        logger.info('积分不足以兑换 %s。所需: %s, 当前: %s', exchange_plan, required_points, current_points)
        exchange_msg = f'积分不足，未兑换: {exchange_plan}'
    else:
        logger.info('开始兑换 %s 计划 (需要 %s 积分)', exchange_plan, required_points)
        exchange_data = read_payload(make_request(
            EXCHANGE_URL, 'POST', headers, {'planType': exchange_plan}, cookies=cookie
        ), '兑换')
        if exchange_data is None:
            exchange_msg = f'兑换失败: 请求或响应解析失败，{exchange_plan}'
        elif authentication_error(exchange_data):
            exchange_msg = f'兑换失败: {authentication_error(exchange_data)}'
        elif exchange_data.get('code') == 0:
            exchange_msg = f'兑换成功：{exchange_plan}'
        else:
            exchange_msg = f'兑换失败: 接口未返回成功结果，{exchange_plan}'
    return status_msg, points_gained, remaining_days, remaining_points, exchange_msg


def account_failed(result: Dict[str, str]) -> bool:
    return (
        '失败' in result['status'] or '失败' in result['exchange']
        or result['days'].startswith('获取') or result['points_total'].startswith('获取')
    )


def format_push_content(results: List[Dict[str, str]]) -> Tuple[str, str]:

    success_count = sum(1 for r in results if "成功" in r['status'])
    fail_count = sum(1 for r in results if account_failed(r))
    repeat_count = sum(1 for r in results if "重复" in r['status'])

    title = f'GLaDOS 签到, 成功{success_count}, 失败{fail_count}, 重复{repeat_count}'

    content_lines = []
    for i, res in enumerate(results, 1):
        line_parts = [
            f"账号{i}:",
            f"P:{res['points']}",
            f"剩余天数:{res['days']}",
            f"总积分:{res['points_total']}",
            f"| {res['status']}",
            f"; {res['exchange']}"
        ]
        line = " ".join(line_parts)
        content_lines.append(line)

    content = "\n".join(content_lines)
    return title, content


def main():
    push_key = ''
    exit_code = 0
    try:
        push_key, cookies_list, exchange_plan = load_config()

        if not cookies_list:
            logger.error("未找到有效的 Cookie，退出程序。")
            title, content = "# 未找到 cookies!", ""
            exit_code = 1
        else:
            results = []
            for idx, cookie in enumerate(cookies_list, 1):
                logger.info(f"正在处理第 {idx} 个账户...")
                status, points, days, points_total, exchange = checkin_and_process(cookie, exchange_plan)
                results.append({
                    'status': status,
                    'points': points,
                    'days': days,
                    'points_total': points_total,
                    'exchange': exchange
                })

            title, content = format_push_content(results)
            exit_code = int(any(account_failed(r) for r in results))
            logger.info(f"推送标题: {title}")
            logger.info(f"推送内容:\n{content}")

    except Exception as e:
        logger.error('主程序执行过程中发生未预期的错误 (%s)。', type(e).__name__)
        title, content = "# 脚本执行出错", "请检查配置与脚本日志。"
        exit_code = 1

    if not push_key:
        logger.info(f"未设置 '{ENV_PUSH_KEY}'，跳过推送通知。")
    else:
        try:
            pushdeer = PushDeer(pushkey=push_key)
            pushdeer.send_text(title, desp=content)
            logger.info("推送通知发送成功。")
        except Exception as e:
            logger.error('发送推送通知失败 (%s)。', type(e).__name__)
            exit_code = 1
    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
