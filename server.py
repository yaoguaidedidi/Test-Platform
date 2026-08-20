#!/usr/bin/env python3
"""
虎测平台 - 本地测试管理 Web 平台
===================================

一句话概括：一个运行在你 Mac 上的小网站，打开浏览器就能用。

架构非常简单：
  浏览器  <--HTTP-->  server.py（你正在看的这个文件）  <--API-->  飞书

核心依赖三个东西：
  1. Python 自带的 http.server —— 不用装任何框架就能跑 Web 服务
  2. lib/feishu.py            —— 负责跟飞书 API 通信（读问卷数据等）
  3. claude -p 命令行          —— 只负责详细 Bug 的内容整理

如果你是第一次读这个文件，建议按这个顺序：
  1. 先看最下面的 main() 函数 —— 了解服务是怎么启动的
  2. 看 do_GET() 和 do_POST() —— 了解浏览器请求怎么被分发到各个功能
  3. 看 _handle_bug_save() —— 了解一条 bug 从提交到飞书的完整流程
  4. 其余功能按兴趣阅读
"""

# ============================================================
# 第一部分：导入依赖
# ============================================================

import json          # 处理 JSON 数据（前端和飞书都用 JSON 通信）
import os            # 文件和目录操作
import re            # 正则匹配（解析日志行时间）
import secrets       # 生成登录会话 ID 和 OAuth state
import sys           # 系统相关（修改模块搜索路径等）
import time          # 时间戳、日期格式化
from datetime import datetime  # 日志时间转时间戳
import urllib.parse  # URL 解析（把 /api/bug/queue/xxx 拆成路径和参数）
import glob          # 文件名匹配（用于搜索本地 xlsx 测试用例文件）
import subprocess    # 启动外部进程（调 claude -p、npx 等命令）
import threading     # 后台冻结异常出现前的循环录屏
import uuid          # 生成不会冲突的附件名
import shutil        # 删除缓存目录
import requests      # 发 HTTP 请求（调飞书 API 验证用户身份）
import openpyxl      # 读写 Excel 文件（搜索本地测试用例时用）
from email.parser import BytesParser  # 解析 multipart/form-data（文件上传）
from email import policy  # HTTP 策略：正确解码中文文件名
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler  # Python 内置的 HTTP 服务器

# ============================================================
# 第二部分：配置
# ============================================================

PORT = 8898  # 服务器监听的端口号。浏览器访问 http://127.0.0.1:8898
             # 127.0.0.1 表示"本机"，所以只有你的电脑能访问

# 飞书 OAuth 登录回调地址（部署到服务器时改成服务器域名 + 环境变量覆盖）
AUTH_REDIRECT_URI = os.environ.get(
    'HUTEST_AUTH_REDIRECT', 'http://127.0.0.1:8898/api/auth/callback'
)
_AUTH_SESSION_COOKIE = 'hutest_session'
_AUTH_SESSION_TTL = 30 * 86400  # 登录会话有效期：30 天
_auth_sessions = {}  # sid -> {'user': {...}, 'expires': 时间戳}
_auth_sessions_lock = threading.Lock()

# 登录时申请的飞书权限：既用于平台身份，也覆盖业务功能所需，
# 授权成功后将令牌写入凭证文件，业务 API（表格/多维表格/wiki/云空间）直接复用。
FEISHU_LOGIN_SCOPES = (
    'auth:user.id:read'          # 登录：获取用户 ID
    ' offline_access'            # 获取 refresh_token（长期有效）
    ' wiki:node:read'            # 打点规范：wiki 链接解析
    ' wiki:node:retrieve'        # 测试用例搜索
    ' sheets:spreadsheet:read'   # 打点规范：读表格
    ' sheets:spreadsheet.meta:read'
    ' drive:drive:readonly'      # 附件下载
    ' bitable:app'               # 问卷记录
    ' search:docs:read'          # 测试用例搜索
)

# 打点规范只从这个页签同步。如果飞书页签改名，可在启动前设置：
# EVENT_SPEC_SHEET_TITLE="新页签名" ./start.sh
EVENT_SPEC_SHEET_TITLE = os.environ.get(
    'EVENT_SPEC_SHEET_TITLE',
    '次留版本test2打点（正式内容）',
).strip()


def find_event_field_type_conflicts(event_fields):
    """找出跨事件同名字段的规范类型冲突。"""
    occurrences = {}
    for event_name, fields in (event_fields or {}).items():
        for field_name, spec in (fields or {}).items():
            field_type = str((spec or {}).get('type', '')).strip().upper()
            occurrences.setdefault(field_name, []).append({
                'event': event_name,
                'type': field_type,
            })
    conflicts = []
    for field_name, definitions in occurrences.items():
        if len(definitions) < 2:
            continue
        types = {item['type'] for item in definitions}
        if len(types) < 2:
            continue
        detail = '，'.join(
            f"{item['event']}={item['type'] or '未填写'}"
            for item in definitions
        )
        conflicts.append({
            'level': 'error',
            'kind': 'field_type_conflict',
            'field': field_name,
            'events': definitions,
            'msg': f'同名字段“{field_name}”类型不一致：{detail}',
        })
    return conflicts


def _load_auth_sessions():
    """启动时从磁盘加载登录会话，保证服务重启后用户不掉线。"""
    global _auth_sessions
    with _auth_sessions_lock:
        _auth_sessions = {}
        if not os.path.exists(_AUTH_SESSIONS_FILE):
            return
        try:
            with open(_AUTH_SESSIONS_FILE, encoding='utf-8') as f:
                data = json.load(f)
            if not isinstance(data, dict):
                return
            now = time.time()
            for sid, session in data.items():
                if isinstance(session, dict) and session.get('expires', 0) > now:
                    _auth_sessions[sid] = session
        except Exception:
            # 会话文件损坏时直接忽略，用户重新登录即可
            _auth_sessions = {}


def _save_auth_sessions():
    """把当前登录会话持久化到磁盘（原子写入）。"""
    with _auth_sessions_lock:
        try:
            tmp_path = _AUTH_SESSIONS_FILE + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(_auth_sessions, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, _AUTH_SESSIONS_FILE)
        except Exception:
            # 写盘失败不影响内存会话，下次登录会重试
            pass

# 三个重要的目录路径
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')           # 前端文件目录：static/index.html
DATA_DIR = os.path.join(BASE_DIR, 'data')               # 数据存储目录：bug 队列、缓存等
_AUTH_SESSIONS_FILE = os.path.join(DATA_DIR, 'auth_sessions.json')


def resolve_static_file(request_path):
    """
    把 /static/xxx 请求安全解析为磁盘文件路径。

    规范化后必须仍位于 static/ 目录内，防止用 ../ 读取 data 等目录外的文件；
    越界时返回 None，调用方返回 403。
    """
    static_real = os.path.realpath(STATIC_DIR)
    rel = request_path.replace('/static/', '')
    fp = os.path.realpath(os.path.join(STATIC_DIR, rel))
    if fp != static_real and not fp.startswith(static_real + os.sep):
        return None
    return fp

# 本地测试用例的存放位置（AI 生成预览时会来这里搜索相关用例）
FUNC_TEST_DIR = os.environ.get(
    'FUNC_TEST_DIR',
    os.path.join(os.path.dirname(BASE_DIR), '功能测试')
)

# 确保必要的数据目录存在（exist_ok=True 表示目录已存在也不报错）
os.makedirs(DATA_DIR, exist_ok=True)
os.makedirs(os.path.join(DATA_DIR, 'bug_queue'), exist_ok=True)    # bug 队列 JSON 文件
os.makedirs(os.path.join(DATA_DIR, 'generated'), exist_ok=True)    # 生成的 xlsx 文件

# 把项目根目录加入 Python 模块搜索路径，这样可以直接 import lib.xxx
sys.path.insert(0, BASE_DIR)
from lib.bug_queue import (
    save_draft, get_bug, list_queue, delete_bug, confirm_bug, update_bug,
    mark_submitted, build_survey_preview, build_simple_preview,
)
from lib.xlsx_template import TestCaseBuilder
from lib.feishu import get_records, download_attachment, _get_token, search_test_cases
from lib.event_tracker import EventTracker
from lib.device_log_capture import DeviceLogCapture
from lib.device_screen_record import DeviceScreenRecorder

# 打点事件追踪器
_event_tracker = EventTracker()
_device_screen_recorder = DeviceScreenRecorder(DATA_DIR)
_problem_recording_lock = threading.Lock()
_problem_recording_busy = 0
_problem_recording_busy_lock = threading.Lock()
_problem_recording_recent = None
_problem_recording_recent_lock = threading.Lock()
_problem_recording_queue = []
_problem_recording_queue_lock = threading.Lock()


def _recording_note(recording_info, label='问题'):
    """生成 Bug 描述里的录屏说明文案。label 表示锚点基准（问题/点击提交）。"""
    if not recording_info:
        return ''
    if recording_info.get('before_seconds') is not None:
        return (
            f'\n{label}前 {recording_info["before_seconds"]:.0f} 秒 + '
            f'{label}后 {recording_info["after_seconds"]:.0f} 秒录屏见附件。'
        )
    return f'\n点击前最近 {recording_info["duration"]:.0f} 秒录屏见附件。'


def _session_bug_recording_worker(bug_id, session_id, click_epoch):
    """后台生成手动提交 Bug 的现场录屏（点击提交前 60 秒 + 后 20 秒）。"""
    try:
        with _problem_recording_lock:
            video_path, recording_info = _device_screen_recorder.snapshot(
                os.path.join(DATA_DIR, 'uploads'), session_id,
                anchor_time=click_epoch,
            )
        bug = get_bug(bug_id)
        if bug is None:
            return
        note = _recording_note(recording_info, '点击提交')
        description = bug.get('preview', {}).get('description', '')
        if note:
            description = re.sub(
                r'录屏生成中[^\n]*', note.lstrip('\n'), description
            )
        attachments = list(bug.get('bug', {}).get('attachments') or [])
        attachments.append(video_path)
        update_bug(bug_id, {
            'recording_status': 'ready',
            'bug': {'attachments': attachments},
            'preview': {'description': description},
        })
        log_activity('log_session_bug_recording_ready', {
            'bug_id': bug_id,
            'recording': os.path.basename(video_path),
        })
    except Exception as exc:
        try:
            update_bug(bug_id, {'recording_status': 'failed'})
        except Exception:
            pass
        log_activity('log_session_bug_recording_failed', {
            'bug_id': bug_id, 'error': str(exc),
        })


def _problem_bug_recording_worker(bug_id, problem_id, session_id, anchor_epoch):
    """后台确保问题 Bug 附带现场录屏（问题前 60 秒 + 后 20 秒），不阻塞提交。"""
    recording_path = ''
    try:
        # 优先等自动冻结完成（问题出现时已触发，但可能排队），上限 80 秒
        deadline = time.monotonic() + 80
        while time.monotonic() < deadline:
            try:
                latest = _device_log_capture.problem(problem_id, session_id)
            except Exception:
                break
            ready_path = latest.get('recording_path') or ''
            if ready_path and os.path.isfile(ready_path):
                recording_path = ready_path
                break
            if latest.get('recording_status') == 'failed':
                break
            time.sleep(1)
        if not (recording_path and os.path.isfile(recording_path)):
            # 自动冻结不可用，兜底生成（加锁避免与自动冻结并发）
            with _problem_recording_lock:
                video_path, _recording_info = _device_screen_recorder.snapshot(
                    os.path.join(DATA_DIR, 'uploads'), session_id,
                    anchor_time=anchor_epoch,
                )
            recording_path = video_path
        bug = get_bug(bug_id)
        if bug is None:
            return
        attachments = list(bug.get('bug', {}).get('attachments') or [])
        attachments.append(recording_path)
        description = bug.get('preview', {}).get('description', '')
        description = re.sub(
            r'录屏生成中[^\n]*',
            '问题前 60 秒 + 问题后 20 秒录屏见附件。',
            description,
        )
        update_bug(bug_id, {
            'recording_status': 'ready',
            'bug': {'attachments': attachments},
            'preview': {'description': description},
        })
        log_activity('log_problem_bug_recording_ready', {
            'bug_id': bug_id,
            'recording': os.path.basename(recording_path),
        })
    except Exception as exc:
        try:
            update_bug(bug_id, {'recording_status': 'failed'})
        except Exception:
            pass
        log_activity('log_problem_bug_recording_failed', {
            'bug_id': bug_id, 'error': str(exc),
        })


def _log_time_to_epoch(time_str, session):
    """把日志行时间（MM-DD HH:MM:SS.mmm，设备时间）转成 epoch 秒。"""
    if not time_str:
        return None
    try:
        started = datetime.fromisoformat(
            (session or {}).get('started_at', '')
        )
        dt = datetime.strptime(
            f'{started.year}-{time_str}', '%Y-%m-%d %H:%M:%S.%f'
        )
        return dt.timestamp()
    except (KeyError, ValueError, TypeError):
        return None


def _latest_error_time(log_path):
    """扫描合并日志，返回最后一条 ERROR/FATAL 行的设备时间字符串。"""
    latest = None
    try:
        with open(log_path, encoding='utf-8', errors='replace') as handle:
            for line in handle:
                match = re.match(
                    r'^\s*(\d\d-\d\d \d\d:\d\d:\d\d\.\d+)\s+'
                    r'\[(?:ERROR|FATAL)\]',
                    line,
                )
                if match:
                    latest = match.group(1)
    except OSError:
        pass
    return latest


def _problem_anchor(problem):
    """取问题锚点：优先电脑时间戳，兼容旧数据用设备时间换算。"""
    epoch = problem.get('last_epoch')
    if epoch:
        return float(epoch)
    session = (_device_log_capture.status().get('session') or {})
    return _log_time_to_epoch(problem.get('last_at'), session)


# 已录屏过的报错签名（跨会话去重：同一报错只录屏一次）
_RECORDED_SIG_FILE = os.path.join(DATA_DIR, 'problem_recorded_signatures.json')
_recorded_problem_signatures = set()


def _load_recorded_problem_signatures():
    """启动时加载已录屏报错签名（跨会话去重用）。"""
    global _recorded_problem_signatures
    try:
        if os.path.exists(_RECORDED_SIG_FILE):
            with open(_RECORDED_SIG_FILE, encoding='utf-8') as f:
                data = json.load(f)
            _recorded_problem_signatures = (
                set(data) if isinstance(data, list) else set()
            )
    except Exception:
        _recorded_problem_signatures = set()


def _save_recorded_problem_signatures():
    """把已录屏报错签名持久化（原子写入）。"""
    try:
        tmp = _RECORDED_SIG_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(sorted(_recorded_problem_signatures), f,
                      ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, _RECORDED_SIG_FILE)
    except Exception:
        pass


def _schedule_problem_recording(problem):
    """同一录制窗口（报错前 before 秒 + 后 after 秒）内的报错共用一份录屏。

    窗口内：只记录问题、标记 pending，冻结完成后统一关联；
    窗口外：另开新窗口，串行排队执行（同一时间仅一个冻结任务）。
    最近一次窗口（无论是否已完成）覆盖范围内的报错都可复用同一份录屏。
    """
    global _problem_recording_recent
    problem_id = problem.get('id', '')
    session_id = problem.get('session_id', '')
    signature = problem.get('signature', '')
    # 网络错误：不录屏，只记录问题
    if (problem.get('kind') or '').lower() == 'network':
        _device_log_capture.mark_problem_recording(
            problem_id, status='unavailable', session_id=session_id,
        )
        return
    # 同一报错（签名）跨会话只录屏一次
    if signature and signature in _recorded_problem_signatures:
        _device_log_capture.mark_problem_recording(
            problem_id, status='unavailable', session_id=session_id,
        )
        return
    if signature:
        _recorded_problem_signatures.add(signature)
        _save_recorded_problem_signatures()
    anchor = _problem_anchor(problem)
    if anchor is None:
        _device_log_capture.mark_problem_recording(
            problem_id, error='无法确定报错时刻，跳过现场录屏',
            session_id=session_id,
        )
        return
    before = _device_screen_recorder.before_seconds
    after = _device_screen_recorder.after_seconds
    with _problem_recording_recent_lock:
        recent = _problem_recording_recent
        if (
            recent
            and recent.get('session_id') == session_id
            and recent.get('anchor') is not None
            and (float(recent['anchor']) - before
                 <= float(anchor) <= float(recent['anchor']) + after)
        ):
            if recent.get('done'):
                # 已冻结完成：直接复用同一份录屏
                _device_log_capture.mark_problem_recording(
                    problem_id,
                    path=recent.get('result_path', ''),
                    duration=recent.get('result_duration', 0),
                    error=recent.get('result_error', ''),
                    session_id=session_id,
                )
            else:
                # 正在冻结/排队：等待共用
                recent['problem_ids'].append(problem_id)
                _device_log_capture.mark_problem_recording(
                    problem_id, status='pending', session_id=session_id,
                )
            return
        new_window = {
            'session_id': session_id,
            'anchor': anchor,
            'problem_ids': [problem_id],
            'done': False,
            'result_path': '',
            'result_duration': 0,
            'result_error': '',
        }
        _problem_recording_recent = new_window
    with _problem_recording_busy_lock:
        busy = _problem_recording_busy
    if busy >= 1:
        with _problem_recording_queue_lock:
            _problem_recording_queue.append(new_window)
        return
    _start_problem_recording_window(new_window)


def _start_problem_recording_window(window):
    """启动一个窗口的冻结任务（串行，同一时间仅一个）。"""
    global _problem_recording_busy
    with _problem_recording_busy_lock:
        _problem_recording_busy += 1
    name = 'problem-recording'
    if window.get('problem_ids'):
        name = f"problem-recording-{window['problem_ids'][0][:12]}"
    threading.Thread(
        target=_freeze_problem_recording_wrapped,
        args=(window,),
        name=name,
        daemon=True,
    ).start()


def _freeze_problem_recording_wrapped(window):
    """冻结一个窗口的共用录屏，完成后关联窗口内所有报错，并继续处理队列。"""
    path, duration, error = '', 0, ''
    try:
        try:
            with _problem_recording_lock:
                path, info = _device_screen_recorder.snapshot(
                    os.path.join(DATA_DIR, 'uploads'),
                    window.get('session_id', ''),
                    anchor_time=window.get('anchor'),
                )
            duration = info.get('duration', 0)
        except Exception as exc:
            error = str(exc)
        for pid in window.get('problem_ids', []):
            _device_log_capture.mark_problem_recording(
                pid, path=path, duration=duration, error=error,
                session_id=window.get('session_id', ''),
            )
    finally:
        with _problem_recording_recent_lock:
            if _problem_recording_recent is window:
                window['done'] = True
                window['result_path'] = path
                window['result_duration'] = duration
                window['result_error'] = error
        global _problem_recording_busy
        with _problem_recording_busy_lock:
            _problem_recording_busy -= 1
        next_window = None
        with _problem_recording_queue_lock:
            if _problem_recording_queue:
                next_window = _problem_recording_queue.pop(0)
        if next_window:
            _start_problem_recording_window(next_window)


_device_log_capture = DeviceLogCapture(
    DATA_DIR, on_problem=_schedule_problem_recording
)


# ============================================================
# 第三部分：AI 辅助函数（本地智能分析，不依赖 claude）
# ============================================================

# 关键词 → 模块名 的对照表
# 比如用户写了"图鉴红点穿模"，程序扫到"图鉴"和"红点"两个关键词，判定属于"图鉴系统"
MODULE_KEYWORDS = {
    '图鉴系统': ['图鉴', '红点', 'handbook', 'collection'],
    '生成器系统': ['生成器', 'generator', '掉落', '队列', '候补', '问号', '指针', '刮刮卡', '皮纳塔', '水果机'],
    '剧情系统': ['剧情', '引导', 'tutorial', '对话', 'story'],
    '账号系统': ['账号', '登录', '注册'],
    '热更新': ['热更新', 'hotfix', '覆盖安装'],
    '浮木': ['浮木'],
    '游泳圈': ['游泳圈', 'swim'],
    '草雕': ['草雕'],
    '石雕': ['石雕'],
    '蜂蜜': ['蜂蜜', 'honey'],
    '鸟蛋': ['鸟蛋', 'egg'],
    '碎冰机': ['碎冰机'],
    '信箱': ['信箱', 'mailbox'],
    '颜色罐子': ['罐子', '颜色'],
    '关联障碍': ['关联障碍', '障碍'],
    '体力': ['体力', 'stamina'],
    '增益火箭': ['增益火箭', '火箭', 'rocket'],
    '体力漂流瓶': ['漂流瓶', 'bottle'],
    '关联爆炸': ['关联爆炸', '爆炸', 'explosion'],
}


def detect_module(brief):
    """
    根据用户填的"简述"，自动判断属于哪个模块。

    原理：遍历 MODULE_KEYWORDS，看简述里有没有出现关键词。
    比如 detect_module("图鉴红点穿模") → "图鉴系统"

    参数:
        brief: str - 用户填的 bug 简述
    返回:
        str - 模块名，匹配不到返回空字符串 ''
    """
    for module, keywords in MODULE_KEYWORDS.items():
        for kw in keywords:
            if kw.lower() in brief.lower():
                return module
    return ''


def search_test_context(module):
    """
    在飞书测试用例存档中搜索匹配的用例文档。

    流程：
      1. 调用 lib/feishu.search_test_cases 搜索飞书目录
      2. 返回匹配内容摘要

    参数:
        module: str - 模块名，如 "图鉴系统"
    返回:
        str - 匹配到的用例文本，找不到返回空字符串
    """
    if not module:
        return ''
    keywords = [kw.strip() for kw in module.replace('系统', '').split('、') if kw.strip()]
    if not keywords:
        keywords = [module]
    try:
        return search_test_cases(keywords)
    except Exception:
        return ''


def generate_smart_preview(brief, assignee):
    """
    智能生成 bug 预览 —— 这是"简单模式"的前置 AI，不调 claude，纯规则匹配。

    根据简述里的关键词（红点、穿模、剧情等），套用预设模板生成标题和描述。
    虽然不够智能，但胜在快速（不需要等 claude -p 返回）。

    参数:
        brief: str    - bug 简述
        assignee: str - 经办人姓名
    返回:
        dict - 包含 title, module, severity, priority, description 等字段
    """
    module = detect_module(brief)
    test_ctx = search_test_context(module)

    # 标题格式：【模块名】简述内容
    title = f'【{module}】{brief}' if module else brief

    # 根据关键词套用不同的描述模板
    desc_lines = [f'【{module}】{brief}', '', '操作步骤']

    if '红点' in brief:
        desc_lines.append('1. 进入二合章节')
        if 'new_item_reward' in test_ctx:
            desc_lines.append('2. 设定new_item_reward触发未领奖状态')
        else:
            desc_lines.append('2. 首次合成/掉落出对应元素触发图鉴红点')
        if '剧情' in brief:
            desc_lines.append('3. 触发剧情，进入剧情界面')
            desc_lines.append('4. 查看图鉴入口红点位置')
        else:
            desc_lines.append('3. 查看图鉴入口红点')
    else:
        desc_lines.append('1. ')
        desc_lines.append('2. ')
        desc_lines.append('3. 观察表现')

    desc_lines.append('')
    desc_lines.append('实际结果')
    if '穿模' in brief:
        desc_lines.append('红点位置超出按钮范围，出现穿模现象')
    else:
        desc_lines.append('')

    desc_lines.append('')
    desc_lines.append('预期结果')
    if '红点' in brief and '剧情' in brief:
        desc_lines.append('红点应正常显示在按钮范围内，不超出边界')
    elif '红点' in brief:
        desc_lines.append('红点应正常显示在图鉴入口按钮上方，领取后消失')
    else:
        desc_lines.append('')

    # 把所有行用换行符拼成一段完整描述
    description = '\n'.join(desc_lines)

    # 返回组装好的预览数据
    return {
        "title": title,
        "module": module,
        "severity": "2",           # 严重程度：2=重要（默认值，用户可改）
        "severity_label": "重要",
        "priority": "1",           # 优先级：1=P1（默认值）
        "priority_label": "P1",
        "issue_stage": "stage_first",
        "business": "699e5baff696cf2f934946f8",
        "platform": "fpxnk0d7p",
        "device": "y44_srr3a",
        "description": description,
    }


# ============================================================
# 第四部分：活动日志
# ============================================================

def log_activity(action, detail=None):
    """
    记录用户操作到日志文件（data/activity.log）。

    每行一条 JSON，方便后续排查问题。
    比如查 "刚才那个 bug 提交到哪一步了"，直接看这个文件。

    参数:
        action: str  - 操作名称，如 'bug_draft_saved', 'bug_confirmed'
        detail: dict - 附加信息，如 {'bug_id': 'xxx', 'brief': '...'}
    """
    log_path = os.path.join(DATA_DIR, 'activity.log')
    entry = {
        'ts': time.strftime('%Y-%m-%dT%H:%M:%S'),
        'action': action,
        'detail': detail or {}
    }
    with open(log_path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(entry, ensure_ascii=False) + '\n')


# ============================================================
# 第五部分：HTTP 请求处理器 —— 整个服务器的核心
# ============================================================
# Python 的 BaseHTTPRequestHandler 是一个"模板类"。
# 你只需要继承它，然后实现 do_GET()（处理浏览器 GET 请求）
# 和 do_POST()（处理浏览器 POST 请求），框架会自动调用。
#
# 每当浏览器发来一个请求，Python 就会：
#   1. 创建一个 HuTestHandler 实例
#   2. 根据请求方法调用 do_GET() 或 do_POST()
#   3. 你写的代码处理请求、返回响应
#   4. 实例销毁，等待下一个请求
#
# 注意：使用 ThreadingHTTPServer，每个请求一个线程。耗时操作（调 claude、
# 飞书 API、合并录屏）仍尽量放后台子进程/线程，避免长时间占用请求线程；
# 共享状态（bug JSON、设备会话、事件列表）由各自的锁保护。
# ============================================================

class HuTestHandler(BaseHTTPRequestHandler):
    """
    虎测平台的核心类：接收浏览器请求 → 调用对应功能 → 返回结果。
    """

    # ── 工具方法（带 _ 前缀表示是内部方法，不给外部用）──

    def _read_session_user(self):
        """从请求 Cookie 解析当前登录用户（飞书登录会话）。"""
        cookie = self.headers.get('Cookie', '')
        sid = None
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith(_AUTH_SESSION_COOKIE + '='):
                sid = part[len(_AUTH_SESSION_COOKIE) + 1:]
                break
        if not sid:
            return None
        session = _auth_sessions.get(sid)
        if not session:
            return None
        if session.get('expires', 0) < time.time():
            _auth_sessions.pop(sid, None)
            return None
        return session.get('user')

    def _require_login(self, path):
        """除登录/回调/健康检查外的 /api/* 请求要求登录，未登录返回 401。"""
        if not path.startswith('/api/'):
            return True
        if path.startswith('/api/auth/'):
            return True
        if path == '/api/health':
            return True
        if self._read_session_user():
            return True
        self._error('未登录，请先使用飞书登录', 401)
        return False

    def _origin_allowed(self):
        origin = self.headers.get('Origin')
        if not origin:
            return True
        try:
            host = urllib.parse.urlparse(origin).hostname
            return host in ('127.0.0.1', 'localhost', '::1')
        except ValueError:
            return False

    def _add_cors_header(self):
        origin = self.headers.get('Origin')
        if origin and self._origin_allowed():
            self.send_header('Access-Control-Allow-Origin', origin)

    def _json(self, data, code=200):
        """
        返回 JSON 格式的响应 —— 这是最常用的响应方式。
        前端用 fetch() 拿到的就是这个。

        参数:
            data: dict - 要返回的数据，如 {'ok': True, 'records': [...]}
            code: int  - HTTP 状态码，200=成功，400=参数错误，500=服务器错误
        """
        payload = json.dumps(data, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        self._add_cors_header()
        self.send_header('Cache-Control', 'no-store')          # 不让浏览器缓存，确保每次都拿到最新数据
        self.end_headers()
        # 把 Python 字典转成 JSON 字符串，再转成字节流发出去
        self.wfile.write(payload)

    def _html(self, path=None):
        """
        返回 HTML 页面 —— 浏览器访问 http://127.0.0.1:8898 时就看这个。

        参数:
            path: str - HTML 文件路径，默认用 static/index.html
        """
        if path is None:
            path = os.path.join(STATIC_DIR, 'index.html')
        with open(path, 'rb') as f:
            payload = f.read()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(payload)))
        # no-store + no-cache：强效禁用缓存，确保代码更新后浏览器立即加载新版本
        self.send_header('Cache-Control', 'no-store, no-cache, must-revalidate, max-age=0')
        self.end_headers()
        self.wfile.write(payload)

    def _error(self, msg, code=400):
        """
        返回错误响应的快捷方法。
        前端收到 ok=False 就知道出错了，error 字段里是具体原因。
        """
        self._json({'ok': False, 'error': msg}, code)

    def _serve_file(self, filepath, content_type='application/octet-stream'):
        """
        返回一个文件给浏览器下载。
        用于：生成 xlsx 后下载、查看日志等。

        参数:
            filepath: str      - 文件路径
            content_type: str  - MIME 类型（告诉浏览器这是什么格式）
        """
        if not os.path.exists(filepath):
            self._error('文件不存在', 404)
            return
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        # Content-Disposition: attachment 告诉浏览器"下载"而不是"打开"
        self.send_header('Content-Disposition', f'attachment; filename="{os.path.basename(filepath)}"')
        self.send_header('Content-Length', os.path.getsize(filepath))
        self._add_cors_header()
        self.send_header('Cache-Control', 'no-store')
        self.end_headers()
        with open(filepath, 'rb') as f:
            while True:
                chunk = f.read(1024 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)

    def _read_body(self):
        """
        读取浏览器 POST 请求的 Body 内容。
        Body 就是附加在请求里的数据，比如用户填的表单、上传的文件。

        返回:
            dict - 解析后的数据。
                   JSON 格式 → 直接解析成 dict
                   文件上传   → 解析 multipart，提取文件名和内容
                   其他       → 返回空 dict
        """
        # Content-Length 头告诉我们要读多少字节
        length = int(self.headers.get('Content-Length', 0))
        if length == 0:
            return {}

        raw = self.rfile.read(length)
        ct = self.headers.get('Content-Type', '')

        # 前端用 fetch({body: JSON.stringify(...)}) 发的请求
        if 'application/json' in ct:
            return json.loads(raw.decode('utf-8'))

        # 前端用 FormData 发的请求（包含文件）
        if 'multipart/form-data' in ct:
            return self._parse_multipart(raw, ct)

        return {}

    def _parse_multipart(self, raw, content_type):
        """
        解析 multipart/form-data 格式的请求体。
        当前端用 <input type="file"> 上传文件时，浏览器就用这种格式发送。

        这是一种特殊的编码方式，把数据和文件打包在同一个请求里。
        Python 标准库的 email.parser 刚好能解析这种格式（它跟邮件附件格式一样）。

        参数:
            raw: bytes           - 原始请求体
            content_type: str    - Content-Type 头（包含 boundary 分隔符）
        返回:
            dict - {字段名: 值}，文件字段的值包含 filename, filepath, size
        """
        result = {}
        # 用 email 库解析 multipart（multipart 格式和邮件 MIME 格式一样）
        # 必须用 HTTP 策略解析：默认策略会把中文文件名解码成乱码
        # （UTF-8 字节被替换成 U+FFFD），导致飞书附件名乱码。
        msg = BytesParser(policy=policy.HTTP).parsebytes(
            b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + raw
        )
        if not msg.is_multipart():
            return result

        upload_dir = os.path.join(DATA_DIR, 'uploads')
        os.makedirs(upload_dir, exist_ok=True)

        # 遍历每个 part（每个 part 是一个表单字段或一个文件）
        for part in msg.get_payload():
            disp = str(part.get('Content-Disposition', ''))
            if 'form-data' not in disp:
                continue

            # 提取字段名和文件名
            name = None
            filename = None
            for item in disp.split(';'):
                item = item.strip()
                if item.startswith('name='):
                    name = item[5:].strip('"')
                elif item.startswith('filename='):
                    filename = item[9:].strip('"')

            if name is None:
                continue

            payload = part.get_payload(decode=True) or b''

            if filename:
                # 文件字段 → 保存到 data/uploads/
                original_filename = os.path.basename(filename.replace('\\', '/'))
                if not original_filename:
                    continue
                stored_filename = f'{int(time.time())}_{uuid.uuid4().hex[:8]}_{original_filename}'
                filepath = os.path.join(upload_dir, stored_filename)
                with open(filepath, 'wb') as f:
                    f.write(payload)
                file_info = {
                    'filename': original_filename,
                    'filepath': filepath,
                    'size': os.path.getsize(filepath)
                }
                # 支持同名字段多文件：已有值则转为列表追加
                if name in result:
                    existing = result[name]
                    if isinstance(existing, list):
                        existing.append(file_info)
                    else:
                        result[name] = [existing, file_info]
                else:
                    result[name] = file_info
            else:
                # 这是普通文本字段
                result[name] = payload.decode('utf-8', errors='replace')

        return result

    # ── HTTP 方法处理 ──

    def do_OPTIONS(self):
        """
        处理浏览器预检请求（CORS preflight）。
        浏览器在发跨域请求前会先发 OPTIONS 问"服务器允不允许"。
        这里直接告诉浏览器：GET、POST、DELETE 都可以。
        """
        if not self._origin_allowed():
            self._error('Forbidden origin', 403)
            return
        self.send_response(200)
        self._add_cors_header()
        self.send_header('Access-Control-Allow-Methods', 'GET, POST, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Content-Type')
        self.end_headers()

    def do_GET(self):
        """
        处理浏览器 GET 请求（打开页面、查询数据、下载文件等）。

        路由规则：
          /                  → 返回首页 HTML
          /api/health        → 健康检查
          /api/survey/records → 获取问卷列表
          /api/bug/queue/xxx  → 获取某个 bug 详情
          /api/testcase/download/xxx → 下载生成的 xlsx
          /static/xxx         → 静态文件（CSS、JS 等）
        """
        # 用 urllib 把 URL 拆成 路径 和 查询参数
        # 例如 /api/survey/records?force=1 → path=/api/survey/records, qs={force:['1']}
        if not self._origin_allowed():
            self._error('Forbidden origin', 403)
            return

        p = urllib.parse.urlparse(self.path)
        path = p.path
        qs = urllib.parse.parse_qs(p.query)
        if not self._require_login(path):
            return

        if path in ('/', '/index.html'):
            self._html()
        elif path == '/api/health':
            # 健康检查端点：前端用这个判断服务器是否活着
            self._json({'ok': True, 'status': 'running'})
        elif path == '/api/dashboard/stats':
            self._handle_dashboard_stats()
        elif path == '/api/dashboard/activity':
            try:
                limit = int(qs.get('limit', ['20'])[0])
            except (TypeError, ValueError):
                self._error('limit 必须是整数', 400)
                return
            self._handle_activity(limit)
        elif path == '/api/testcase/list':
            self._handle_list_generated()
        elif path.startswith('/api/testcase/download/'):
            # URL 中提取文件名 → 返回文件下载
            fname = os.path.basename(path.replace('/api/testcase/download/', ''))
            self._serve_file(
                os.path.join(DATA_DIR, 'generated', fname),
                'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
            )
        elif path == '/api/survey/records':
            # 获取飞书问卷数据
            self._handle_survey_records()
        elif path == '/api/bug/queue':
            # 获取 bug 队列列表
            self._handle_bug_queue_list()
        elif path.startswith('/api/bug/queue/'):
            # 获取单个 bug 详情（ID 从 URL 中提取）
            bug_id = urllib.parse.unquote(path.replace('/api/bug/queue/', ''))
            self._handle_bug_get(bug_id)
        elif path == '/api/auth/status':
            self._handle_auth_status()
        elif path == '/api/auth/feishu-login':
            self._handle_auth_feishu_login()
        elif path == '/api/auth/callback':
            self._handle_auth_callback(qs)
        elif path == '/api/auth/login':
            self._handle_auth_login()
        elif path == '/api/bug/export-claude':
            self._handle_export_claude(urllib.parse.unquote(qs.get('id', [''])[0]))
        elif path == '/api/events':
            self._handle_events(qs)
        elif path == '/api/events/spec':
            self._handle_events_spec()
        elif path == '/api/log-capture/environment':
            self._json(_device_log_capture.environment())
        elif path == '/api/log-capture/devices':
            self._json(_device_log_capture.devices())
        elif path == '/api/log-capture/apps':
            try:
                self._json(_device_log_capture.apps(
                    qs.get('platform', [''])[0], qs.get('device_id', [''])[0]
                ))
            except Exception as e:
                self._error(str(e), 400)
        elif path == '/api/log-capture/status':
            self._json(_device_log_capture.status())
        elif path == '/api/log-capture/current/save':
            self._handle_log_current_save()
        elif path == '/api/log-capture/current/download':
            try:
                snapshot_path, _session = _device_log_capture.session_snapshot(
                    os.path.join(DATA_DIR, 'uploads')
                )
                self._serve_file(snapshot_path, 'text/plain; charset=utf-8')
            except FileNotFoundError as e:
                self._error(str(e), 404)
            except Exception as e:
                self._error(str(e), 400)
        elif path.startswith('/api/uploads/'):
            # 下载上传目录里的文件（仅限文件名，防止路径穿越）
            fname = os.path.basename(
                path.replace('/api/uploads/', '', 1)
            )
            if not fname:
                self._error('缺少文件名', 400)
                return
            self._serve_file(
                os.path.join(DATA_DIR, 'uploads', fname),
                'application/octet-stream',
            )
        elif path == '/api/screen-record/environment':
            self._json(_device_screen_recorder.environment())
        elif path == '/api/screen-record/status':
            self._json(_device_screen_recorder.status())
        elif path == '/api/log-capture/lines':
            self._json(_device_log_capture.lines_after(
                qs.get('after', ['0'])[0], qs.get('limit', ['1000'])[0]
            ))
        elif path == '/api/log-capture/problems':
            try:
                self._json(_device_log_capture.problems(
                    qs.get('session_id', [''])[0]
                ))
            except Exception as e:
                self._error(str(e), 400)
        elif path.startswith('/api/log-capture/problems/'):
            problem_id = urllib.parse.unquote(path.split('/')[-1])
            try:
                self._json({
                    'ok': True,
                    'problem': _device_log_capture.problem(
                        problem_id, qs.get('session_id', [''])[0]
                    ),
                })
            except FileNotFoundError as e:
                self._error(str(e), 404)
            except Exception as e:
                self._error(str(e), 400)
        elif path == '/api/log-capture/sessions':
            self._json(_device_log_capture.sessions())
        elif path.startswith('/api/log-capture/sessions/') and path.endswith('/download'):
            session_id = path.split('/')[-2]
            try:
                self._serve_file(
                    _device_log_capture.session_zip(session_id), 'application/zip'
                )
            except FileNotFoundError as e:
                self._error(str(e), 404)
            except Exception as e:
                self._error(str(e), 400)
        elif path.startswith('/static/'):
            # 静态文件：CSS、JS、图片等（resolve_static_file 防路径穿越）
            fp = resolve_static_file(path)
            if fp is None:
                self._error('Forbidden', 403)
            elif os.path.isfile(fp):
                ct = {
                    'html': 'text/html', 'css': 'text/css',
                    'js': 'application/javascript',
                    'png': 'image/png', 'jpg': 'image/jpeg', 'jpeg': 'image/jpeg'
                }.get(fp.rsplit('.', 1)[-1], 'application/octet-stream')
                self._serve_file(fp, ct)
            else:
                self._error('Not found', 404)
        else:
            self._error('Not found', 404)

    def do_POST(self):
        """
        处理浏览器 POST 请求（提交数据、执行操作等）。

        POST 和 GET 的区别：
          GET  —— 从服务器"拿"数据（查询），参数在 URL 里
          POST —— 向服务器"送"数据（提交表单、上传文件），参数在 Body 里

        路由规则：
          /api/bug/save       → 保存 bug 草稿；详细模式按需触发 AI 内容整理
          /api/bug/confirm/xxx → 确认 bug，触发自动提交
          /api/testcase/generate → 生成测试用例 xlsx
          /api/tools/reauth   → 飞书重新授权
        """
        p = urllib.parse.urlparse(self.path)
        path = p.path
        if not self._origin_allowed():
            self._error('Forbidden origin', 403)
            return
        if not self._require_login(path):
            return
        try:
            body = self._read_body()  # POST 的数据在 Body 里，先读出来
        except ValueError as e:
            # 非法 JSON / 编码错误 → 返回 400，而不是断开连接
            self._error(f'请求体解析失败: {e}', 400)
            return

        if path == '/api/testcase/generate':
            self._handle_generate(body)
        elif path == '/api/bug/save':
            self._handle_bug_save(body)
        elif path == '/api/bug/trigger':
            self._json({'ok': True, 'msg': 'signaled'})
        elif path.startswith('/api/bug/confirm/'):
            # URL 中包含 bug ID：/api/bug/confirm/20260706_xxx
            bug_id = urllib.parse.unquote(path.replace('/api/bug/confirm/', ''))
            self._handle_bug_confirm(bug_id, body)
        elif path.startswith('/api/bug/retry/'):
            bug_id = urllib.parse.unquote(path.replace('/api/bug/retry/', ''))
            self._handle_bug_retry(bug_id)
        elif path.startswith('/api/bug/submit/'):
            bug_id = urllib.parse.unquote(path.replace('/api/bug/submit/', ''))
            self._handle_bug_submit(bug_id, body)
        elif path == '/api/bug/queue/delete':
            self._handle_bug_delete(body)
        elif path == '/api/auth/status':
            self._handle_auth_status()
        elif path == '/api/auth/login':
            self._handle_auth_login()
        elif path == '/api/auth/logout':
            self._handle_auth_logout()
        elif path == '/api/events/start':
            self._handle_events_start()
        elif path == '/api/events/spec/update':
            self._handle_events_spec_update(body)
        elif path == '/api/events/stop':
            self._handle_events_stop()
        elif path == '/api/events/clear':
            self._handle_events_clear()
        elif path == '/api/log-capture/start':
            try:
                session = _device_log_capture.start(
                    body.get('platform', ''), body.get('device_id', ''),
                    body.get('app_id', ''), body.get('app_name', ''),
                    body.get('executable', '')
                )
                self._json({'ok': True, 'session': session})
            except Exception as e:
                self._error(str(e), 400)
        elif path == '/api/log-capture/stop':
            recording = _device_screen_recorder.status().get('session') or {}
            current = _device_log_capture.status().get('session') or {}
            if (
                recording.get('active')
                and recording.get('id') == current.get('id')
            ):
                _device_screen_recorder.stop()
            self._json(_device_log_capture.stop())
        elif path == '/api/log-capture/current/save':
            self._handle_log_current_save()
        elif path == '/api/screen-record/start':
            try:
                current = _device_log_capture.status().get('session') or {}
                if not current or current.get('id') != body.get('session_id'):
                    raise RuntimeError('请先开始当前 App 的日志抓取')
                if current.get('device_id') != body.get('device_id'):
                    raise RuntimeError('录屏设备与日志设备不一致')
                result = _device_screen_recorder.start(
                    body.get('device_id', ''), body.get('session_id', ''),
                    current.get('app_name', ''),
                )
                self._json(result)
            except Exception as e:
                self._error(str(e), 400)
        elif path == '/api/screen-record/stop':
            self._json(_device_screen_recorder.stop())
        elif path == '/api/screen-record/save':
            self._handle_screen_record_save(body)
        elif path == '/api/log-capture/bug':
            self._handle_log_session_bug(body)
        elif path.startswith('/api/log-capture/problems/') and path.endswith('/bug'):
            problem_id = urllib.parse.unquote(path.split('/')[-2])
            self._handle_log_problem_bug(problem_id, body)
        elif path == '/api/tools/reauth':
            self._handle_reauth(body)
        elif path == '/api/tools/clear-testcase-cache':
            self._handle_clear_testcase_cache()
        elif path == '/api/tools/clear-uploads':
            self._handle_clear_uploads(body)
        elif path == '/api/tools/open-uploads':
            self._handle_open_uploads()
        else:
            self._error('Not found', 404)

    def do_DELETE(self):
        """
        处理浏览器 DELETE 请求（删除某个 bug）。
        URL 格式：/api/bug/queue/<bug_id>
        """
        path = urllib.parse.urlparse(self.path).path
        if not self._origin_allowed():
            self._error('Forbidden origin', 403)
            return
        if path.startswith('/api/bug/queue/'):
            bug_id = urllib.parse.unquote(path.replace('/api/bug/queue/', ''))
            self._handle_bug_delete_by_id(bug_id)
        elif path == '/api/log-capture/sessions':
            try:
                result = _device_log_capture.clear_sessions()
                for session_id in result.get('deleted_session_ids', []):
                    _device_screen_recorder.delete_session(session_id)
                self._json(result)
            except Exception as e:
                self._error(str(e), 400)
        elif path.startswith('/api/log-capture/sessions/'):
            session_id = urllib.parse.unquote(
                path.replace('/api/log-capture/sessions/', '')
            )
            try:
                result = _device_log_capture.delete_session(session_id)
                _device_screen_recorder.delete_session(session_id)
                self._json(result)
            except FileNotFoundError as e:
                self._error(str(e), 404)
            except Exception as e:
                self._error(str(e), 400)
        else:
            self._error('Not found', 404)

    # ── 业务处理函数 ──

    def _handle_screen_record_save(self, data=None):
        """保存当前循环录屏的视频并下载。

        mode=standard（默认）：点击前 60 秒 + 点击后 20 秒（需等待续录）；
        mode=realtime：点击前最近 60 秒，立即导出。
        """
        try:
            status = _device_screen_recorder.status()
            session = status.get('session') or {}
            session_id = session.get('id', '')
            if not session_id or not _device_screen_recorder.has_session(
                session_id
            ):
                raise RuntimeError('请先开始循环录屏')
            mode = 'standard'
            if isinstance(data, dict):
                mode = data.get('mode', 'standard')
            if mode == 'realtime':
                anchor_time = None
                before_seconds, after_seconds = 60, 0
            else:
                anchor_time = time.time()
                before_seconds, after_seconds = None, None
            with _problem_recording_lock:
                video_path, _recording_info = _device_screen_recorder.snapshot(
                    os.path.join(DATA_DIR, 'uploads'), session_id,
                    anchor_time=anchor_time,
                    before_seconds=before_seconds,
                    after_seconds=after_seconds,
                )
            self._json({
                'ok': True,
                'file': os.path.basename(video_path),
                'path': video_path,
                'size': os.path.getsize(video_path),
                'mode': mode,
                'download': (
                    '/api/uploads/' + urllib.parse.quote(
                        os.path.basename(video_path)
                    )
                ),
            })
        except RuntimeError as e:
            self._error(str(e), 409)
        except Exception as e:
            self._error(f'保存录屏失败: {str(e)}', 500)

    def _handle_log_current_save(self):
        """保存当前抓取的日志快照到固定目录，返回文件信息（不直接下载）。"""
        try:
            snapshot_path, _session = _device_log_capture.session_snapshot(
                os.path.join(DATA_DIR, 'uploads')
            )
            self._json({
                'ok': True,
                'file': os.path.basename(snapshot_path),
                'path': snapshot_path,
                'size': os.path.getsize(snapshot_path),
                'download': (
                    '/api/uploads/' + urllib.parse.quote(
                        os.path.basename(snapshot_path)
                    )
                ),
            })
        except FileNotFoundError as e:
            self._error(str(e), 404)
        except Exception as e:
            self._error(str(e), 400)

    def _handle_open_uploads(self):
        """在系统文件管理器中打开上传目录（仅本机使用）。"""
        upload_dir = os.path.join(DATA_DIR, 'uploads')
        if not os.path.isdir(upload_dir):
            self._error('上传目录不存在', 404)
            return
        try:
            opener = 'open' if sys.platform == 'darwin' else 'xdg-open'
            subprocess.Popen([opener, upload_dir])
            self._json({'ok': True, 'path': upload_dir})
        except Exception as e:
            self._error(f'打开目录失败: {str(e)}', 500)

    def _handle_log_session_bug(self, data):
        """无需自动识别问题，直接用当前完整日志生成可编辑 Bug。

        录屏改为后台生成：同步只写日志快照并保存草稿，界面先出；
        录屏（点击提交前 60 秒 + 后 20 秒）完成后自动附加到附件列表。
        """
        try:
            snapshot_path, session = _device_log_capture.session_snapshot(
                os.path.join(DATA_DIR, 'uploads'),
                str(data.get('session_id', '') or ''),
            )
            attachments = [snapshot_path]
            has_recorder = _device_screen_recorder.has_session(
                session.get('id', '')
            )
            click_epoch = time.time()
            app_name = session.get('app_name') or session.get('app_id') or 'Android App'
            title = f'【日志】{app_name} · 请补充问题标题'
            description = '\n'.join([
                '【日志摘要】',
                f'App：{app_name}',
                f'设备：{session.get("device_id", "—")}',
                f'日志开始时间：{session.get("started_at", "—")}',
                f'快照日志行数：{session.get("line_count", 0)}',
                '',
                '请在这里补充问题表现、复现步骤和预期结果。',
                '',
                '完整带类型日志见附件，可下载查看。'
                + (
                    '\n录屏生成中，稍后自动附加（点击提交前 60 秒 + '
                    '点击提交后 20 秒）。'
                    if has_recorder else ''
                ),
            ])
            preview = {
                'title': title, 'description': description, 'module': '',
                'severity': '2', 'priority': '1',
            }
            bug_id, _ = save_draft(
                brief_description=title,
                assignee=str(data.get('assignee', '') or '萌虎'),
                attachments=attachments,
                mode='scripted',
                preview=preview,
                recording_status='pending' if has_recorder else 'none',
            )
            if has_recorder:
                threading.Thread(
                    target=_session_bug_recording_worker,
                    args=(bug_id, session.get('id', ''), click_epoch),
                    name='session-bug-recording',
                    daemon=True,
                ).start()
            log_activity('log_session_bug_saved', {
                'bug_id': bug_id, 'session_id': session.get('id', ''),
                'snapshot': os.path.basename(snapshot_path),
                'recording': '',
            })
            self._json({
                'ok': True, 'id': bug_id, 'status': 'preview_ready',
                'recording_status': 'pending' if has_recorder else 'none',
            })
        except FileNotFoundError as e:
            self._error(str(e), 404)
        except RuntimeError as e:
            self._error(str(e), 409)
        except Exception as e:
            self._error(f'生成日志 Bug 失败: {str(e)}', 500)

    def _handle_log_problem_bug(self, problem_id, data):
        """把日志问题和点击时的完整日志快照生成可编辑 Bug 预览。"""
        try:
            session_id = str(data.get('session_id', '') or '')
            problem = _device_log_capture.problem(problem_id, session_id)
            existing_id = problem.get('bug_id')
            existing = get_bug(existing_id) if existing_id else None
            if existing:
                self._json({
                    'ok': True, 'id': existing_id,
                    'status': existing.get('status', 'preview_ready'),
                    'existing': True,
                })
                return

            snapshot_path, problem = _device_log_capture.problem_snapshot(
                problem_id, os.path.join(DATA_DIR, 'uploads'), session_id
            )
            attachments = [snapshot_path]
            recording_path = problem.get('recording_path', '')
            problem_session_id = problem.get('session_id', '')
            need_recording = _device_screen_recorder.has_session(
                problem_session_id
            )
            recording_status = 'none'
            if recording_path and os.path.isfile(recording_path):
                attachments.append(recording_path)
                recording_status = 'ready'
            elif need_recording:
                # 录屏后台确保（等自动冻结完成或兜底生成），先返回预览不阻塞
                recording_status = 'pending'
            session = (_device_log_capture.status().get('session') or {})
            app_name = session.get('app_name') or session.get('app_id') or 'Android App'
            kind_labels = {
                'crash': '崩溃', 'exception': '异常',
                'network': '网络错误', 'error': '错误',
            }
            kind_label = kind_labels.get(problem.get('kind'), '日志异常')
            core = (problem.get('title') or '检测到日志异常').strip()
            title = f'【日志{kind_label}】{app_name} · {core}'[:180]
            lines = [
                '【问题摘要】',
                f'App：{app_name}',
                f'类型：{kind_label}',
                f'来源：{problem.get("source") or "—"}',
                f'出现次数：{problem.get("count") or 1}',
                '',
                f'摘要：{problem.get("message") or core}',
                '',
                '完整带类型日志见附件，可下载查看。',
            ]
            if recording_status == 'ready':
                lines.append('问题前 60 秒 + 问题后 20 秒录屏见附件。')
            elif recording_status == 'pending':
                lines.append(
                    '录屏生成中，稍后自动附加'
                    '（问题前 60 秒 + 问题后 20 秒）。'
                )
            preview = {
                'title': title,
                'description': '\n'.join(lines),
                'module': '',
                'severity': '1' if problem.get('severity') == 'critical' else '2',
                'priority': '1',
            }
            bug_id, _ = save_draft(
                brief_description=title,
                assignee=str(data.get('assignee', '') or '萌虎'),
                attachments=attachments,
                mode='scripted',
                preview=preview,
                recording_status=recording_status,
            )
            if recording_status == 'pending':
                threading.Thread(
                    target=_problem_bug_recording_worker,
                    args=(
                        bug_id, problem_id, problem_session_id,
                        _problem_anchor(problem),
                    ),
                    name='problem-bug-recording',
                    daemon=True,
                ).start()
            _device_log_capture.mark_problem_bug(problem_id, bug_id, session_id)
            log_activity('log_problem_bug_saved', {
                'bug_id': bug_id, 'problem_id': problem_id,
                'session_id': problem.get('session_id', ''),
                'snapshot': os.path.basename(snapshot_path),
                'recording': (
                    os.path.basename(attachments[-1])
                    if recording_status == 'ready' else ''
                ),
            })
            self._json({
                'ok': True, 'id': bug_id, 'status': 'preview_ready',
                'recording_status': recording_status,
            })
        except FileNotFoundError as e:
            self._error(str(e), 404)
        except Exception as e:
            self._error(f'生成日志 Bug 失败: {str(e)}', 500)

    # === 仪表盘 ===

    def _handle_dashboard_stats(self):
        """返回仪表盘统计数据：生成了多少用例、待提交多少 bug 等。"""
        generated = len(os.listdir(os.path.join(DATA_DIR, 'generated')))
        bugs = list_queue()
        pending = sum(1 for b in bugs if b['status'] in (
            'draft', 'preview_ready', 'analysis_failed', 'confirmed',
            'submission_failed', 'pending_submit'
        ))
        submitted = sum(1 for b in bugs if b['status'] == 'submitted')
        self._json({
            'generated_files': generated,
            'bugs_pending': pending,
            'bugs_submitted': submitted,
            'bugs_total': len(bugs)
        })

    def _handle_activity(self, limit=20):
        """返回最近 N 条操作日志。"""
        log_path = os.path.join(DATA_DIR, 'activity.log')
        if not os.path.exists(log_path):
            self._json([])
            return
        lines = []
        with open(log_path, 'r', encoding='utf-8') as f:
            for line in f:
                try:
                    lines.append(json.loads(line.strip()))
                except json.JSONDecodeError:
                    continue
        lines.reverse()  # 最新的在前面
        self._json(lines[:limit])

    # === 用例生成 ===

    def _handle_generate(self, data):
        """
        生成测试用例 xlsx 文件。

        前端会传来：
          - title: 文件标题
          - cases: [{'b': '模块', 'c': '子模块', 'g': '操作步骤', 'h': '期望结果'}, ...]

        生成的文件保存在 data/generated/ 目录。
        """
        try:
            title = data.get('title', '未命名')
            builder = TestCaseBuilder(title=title)
            cases = data.get('cases', [])
            if not isinstance(cases, list):
                self._error('cases 必须是数组', 400)
                return
            if not all(isinstance(case, dict) for case in cases):
                self._error('cases 每项必须是对象', 400)
                return
            for case in cases:
                builder.add_case(
                    module1=case.get('b', ''), module2=case.get('c', ''),
                    module3=case.get('d', ''), module4=case.get('e', ''),
                    module5=case.get('f', ''),
                    steps=case.get('g', ''), results=case.get('h', ''),
                    priority=case.get('k', 1), remark=case.get('l', '')
                )
            # 文件名：标题_时间戳.xlsx（时间戳避免重名）
            safe = title.replace('/', '_').replace(' ', '_')
            fname = f"{safe}_{int(time.time())}.xlsx"
            out = os.path.join(DATA_DIR, 'generated', fname)
            builder.build_xlsx(out)

            log_activity('testcase_generated', {
                'title': title,
                'count': len(data.get('cases', [])),
                'filename': fname
            })
            self._json({'ok': True, 'filename': fname, 'count': len(data.get('cases', []))})
        except Exception as e:
            self._error(f'生成失败: {str(e)}', 500)

    def _handle_list_generated(self):
        """列出所有已生成的 xlsx 文件。"""
        gd = os.path.join(DATA_DIR, 'generated')
        files = []
        for f in sorted(os.listdir(gd), reverse=True):
            if f.endswith('.xlsx'):
                fp = os.path.join(gd, f)
                files.append({
                    'filename': f,
                    'size': os.path.getsize(fp),
                    'mtime': time.strftime('%Y-%m-%d %H:%M:%S',
                                           time.localtime(os.path.getmtime(fp)))
                })
        self._json(files)

    # === 问卷（飞书多维表格数据） ===

    def _handle_survey_records(self):
        """
        获取飞书问卷数据。

        数据流：
          前端请求 → server.py → lib/feishu.py → 飞书 Open API → 多维表格
                                    ↑
                              优先用本地缓存（5分钟内有效）
                              API 失败时降级返回旧缓存
        """
        try:
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            force = qs.get('force', ['0'])[0] == '1'  # force=1 强制刷新，跳过缓存

            result = get_records(force_refresh=force, _track_fallback=True)

            # get_records 可能返回 (records, is_fallback) 元组，也可能直接返回 list
            if isinstance(result, tuple):
                records, is_fallback = result
            else:
                records, is_fallback = result, False

            resp = {'ok': True, 'records': records, 'count': len(records)}
            if is_fallback:
                resp['_fallback'] = True  # 告诉前端"这是缓存数据"
            self._json(resp)
        except Exception as e:
            import traceback
            self._error(f'获取问卷失败: {str(e)}\n{traceback.format_exc()}', 500)

    # === Bug 提交 —— 这是整个平台最核心的功能 ===
    # Bug 有三个状态流转：
    #   draft → preview_ready → confirmed → submitted
    #   草稿     预览已生成     用户确认    已提交飞书

    def _handle_bug_save(self, data):
        """
        保存 bug 草稿。只有详细模式在没有预览时启动 AI 内容整理。

        这是 bug 提交流程的第一步。
        用户填了简述+附件后点击"执行"，就会走到这里。

        流程：
          1. 解析前端传来的数据
          2. 问卷、简单提交和脚本生成的 Bug 直接生成预览
          3. 只有详细 Bug fork auto_process.py → AI 生成预览
          4. 立即返回 bug_id
        """
        try:

            # 解析请求数据
            brief_value = data.get('brief', data.get('brief_description', ''))
            brief = brief_value.strip() if isinstance(brief_value, str) else ''
            if len(brief) < 2:
                self._error('简述至少需要 2 个字符', 400)
                return
            assignee = data.get('assignee', '萌虎')
            mode = data.get('mode', 'detail')  # detail=详细, simple=简单, survey=问卷, scripted=脚本
            submitter = data.get('submitter', '')
            attachments = data.get('attachments', [])
            # 前端 FormData 字段名是 "files"，兼容两种来源
            files_from_form = data.get('files', None)
            if files_from_form:
                if isinstance(files_from_form, list):
                    attachments = attachments + files_from_form
                elif isinstance(files_from_form, dict):
                    attachments = attachments + [files_from_form]
            survey_desc = data.get('survey_desc', '')
            survey_env = data.get('survey_env', '')
            survey_attachments = data.get('survey_attachments', [])
            preview = data.get('preview')

            # 统一格式：确保附件是列表
            if isinstance(preview, str):
                try:
                    preview = json.loads(preview)
                except Exception:
                    preview = None
            if isinstance(survey_attachments, str):
                try:
                    survey_attachments = json.loads(survey_attachments)
                except Exception:
                    survey_attachments = []
            if isinstance(attachments, dict):
                attachments = [attachments]
            if mode == 'survey' and not isinstance(preview, dict):
                preview = build_survey_preview(brief, survey_desc, survey_env)
            elif mode == 'simple' and not isinstance(preview, dict):
                preview = build_simple_preview(brief, detect_module(brief))

            # 保存 bug 草稿到 JSON 文件
            bug_id, _ = save_draft(
                brief_description=brief, assignee=assignee,
                attachments=[a.get('filepath', a) if isinstance(a, dict) else a for a in attachments],
                mode=mode, submitter=submitter,
                survey_attachments=survey_attachments,
                survey_desc=survey_desc, survey_env=survey_env,
                preview=preview if isinstance(preview, dict) else None,
            )

            log_activity('bug_draft_saved', {
                'bug_id': bug_id, 'brief': brief,
                'survey_desc': survey_desc[:50] if survey_desc else '',
                'survey_env': survey_env[:50] if survey_env else ''
            })

            # 只有没有固定预览的详细 Bug 才需要 AI 整理内容。
            if mode == 'detail' and not isinstance(preview, dict):
                log_path = os.path.join(DATA_DIR, 'auto_process.log')
                with open(log_path, 'a') as log:
                    log.write(f'\n--- {bug_id} ---\n')
                    subprocess.Popen(
                        [sys.executable, os.path.join(BASE_DIR, 'claude_tasks', 'auto_process.py'), bug_id],
                        stdout=log, stderr=log
                    )

            self._json({
                'ok': True, 'id': bug_id,
                'status': 'preview_ready' if isinstance(preview, dict) else 'draft',
            })
        except Exception as e:
            self._error(f'保存失败: {str(e)}', 500)

    def _handle_bug_queue_list(self):
        """返回 bug 队列全部记录。"""
        self._json(list_queue())

    def _handle_bug_get(self, bug_id):
        """返回单个 bug 的详细信息。前端用这个获取预览和提交状态。"""
        data = get_bug(bug_id)
        if data is None:
            self._error('Bug not found', 404)
        else:
            attachment_details = []
            for item in (data.get('bug', {}).get('attachments') or []):
                path = item.get('filepath', '') if isinstance(item, dict) else str(item)
                name = (
                    item.get('name') if isinstance(item, dict) else ''
                ) or os.path.basename(path) or '本地附件'
                attachment_details.append({
                    'name': name,
                    'size': os.path.getsize(path) if path and os.path.isfile(path) else None,
                    'source': 'local',
                })
            for item in (data.get('survey_attachments') or []):
                if isinstance(item, dict):
                    name = (
                        item.get('name') or item.get('filename')
                        or item.get('file_name') or item.get('file_token')
                    )
                    size = item.get('size')
                else:
                    name, size = str(item), None
                attachment_details.append({
                    'name': name or '问卷附件', 'size': size, 'source': 'survey',
                })
            data['attachment_details'] = attachment_details
            self._json(data)

    def _handle_bug_confirm(self, bug_id, body):
        """
        用户确认 bug 预览，提交到飞书。

        这是 bug 提交流程的第二步。
        用户在预览界面编辑满意后点击"确认提交"，走到这里。

        流程：
          1. 把 bug 状态改为 confirmed
          2. fork 子进程 auto_submit.py → Meegle CLI 创建 issue 并校验附件
          3. 立即返回（不等待飞书返回）
        """
        preview = body.get('preview') if isinstance(body, dict) else None
        assignee = body.get('assignee') if isinstance(body, dict) else None
        current = get_bug(bug_id)
        if current is None:
            self._error('Bug not found', 404)
            return
        if current.get('recording_status') == 'pending':
            self._error('现场录屏仍在生成中，请稍候再确认', 409)
            return
        result = confirm_bug(bug_id, preview=preview, assignee=assignee)
        if result is None:
            self._error('Bug not found', 404)
            return
        data, changed = result
        if not changed:
            # 已在确认/提交中（可能被另一个请求抢先），拒绝重复提交
            self._error(
                f'当前状态 {data.get("status")} 不允许确认（可能已在提交中）',
                409,
            )
            return
        log_activity('bug_confirmed', {'bug_id': bug_id})

        # 启动后台提交进程
        # 日志写入 data/auto_submit.log，出问题可以排查
        log_path = os.path.join(DATA_DIR, 'auto_submit.log')
        with open(log_path, 'a') as log:
            log.write(f'\n--- confirmed {bug_id} ---\n')
            env = os.environ.copy()
            env['HUTEST_SUPPRESS_LOG_PRINT'] = '1'
            subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, 'claude_tasks', 'auto_submit.py'), bug_id],
                stdout=log, stderr=log, env=env
            )
        self._json({'ok': True, 'status': 'confirmed'})

    def _handle_bug_retry(self, bug_id):
        """重新执行失败的内容整理或飞书脚本提交。"""
        bug = get_bug(bug_id)
        if bug is None:
            self._error('Bug not found', 404)
            return
        status = bug.get('status')
        if status == 'analysis_failed':
            mode = bug.get('mode', 'detail')
            if mode in ('survey', 'simple', 'scripted'):
                brief = bug.get('bug', {}).get('brief_description', '')
                if mode == 'survey':
                    preview = build_survey_preview(
                        brief, bug.get('survey_desc', ''), bug.get('survey_env', ''),
                    )
                elif isinstance(bug.get('preview'), dict):
                    preview = bug['preview']
                else:
                    preview = build_simple_preview(
                        brief, bug.get('bug', {}).get('module', ''),
                    )
                update_bug(bug_id, {
                    'status': 'preview_ready', 'error': None,
                    'preview': preview,
                    'bug': {
                        'title': preview['title'], 'module': preview['module'],
                        'severity': preview['severity'],
                        'priority': preview['priority'],
                        'description': preview['description'],
                    },
                })
                self._json({'ok': True, 'status': 'preview_ready'})
                return
            update_bug(bug_id, {'status': 'draft', 'error': None})
            script = 'auto_process.py'
            log_name = 'auto_process.log'
        elif status in ('submission_failed', 'pending_submit'):
            update_bug(bug_id, {'status': 'confirmed', 'error': None})
            script = 'auto_submit.py'
            log_name = 'auto_submit.log'
        else:
            self._error(f'当前状态 {status} 不支持重试', 409)
            return
        log_path = os.path.join(DATA_DIR, log_name)
        with open(log_path, 'a') as log:
            env = os.environ.copy()
            if script == 'auto_submit.py':
                env['HUTEST_SUPPRESS_LOG_PRINT'] = '1'
            subprocess.Popen(
                [sys.executable, os.path.join(BASE_DIR, 'claude_tasks', script), bug_id],
                stdout=log, stderr=log, env=env
            )
        self._json({'ok': True, 'status': 'retrying'})

    def _handle_bug_submit(self, bug_id, body):
        """手动标记 bug 为已提交（保留给兼容工具使用）。"""
        r = mark_submitted(bug_id, body.get('work_item_id'))
        if r is None:
            self._error('Bug not found', 404)
        else:
            if 'preview' in body:
                update_bug(bug_id, {
                    'preview': body['preview'],
                    'bug': body.get('bug', r['bug'])
                })
            log_activity('bug_submitted', {
                'bug_id': bug_id,
                'work_item_id': body.get('work_item_id')
            })
            self._json({'ok': True, 'status': 'submitted'})

    def _handle_bug_delete(self, body):
        """删除 bug（通过 POST body 传 ID）。"""
        delete_bug(body.get('id', ''))
        self._json({'ok': True})

    # === 打点测试 ===

    def _handle_events(self, qs):
        """返回打点事件列表。优先使用 ?after=<cursor> 增量查询。"""
        after = qs.get('after', [''])[0]
        since = qs.get('since', [''])[0]
        all_events = _event_tracker.events
        if after:
            try:
                events = _event_tracker.events_after(int(after))
            except ValueError:
                self._error('after 必须是整数', 400)
                return
        elif since:
            events = _event_tracker.events_since(since)
        else:
            events = all_events
        from lib.event_tracker import analyze_events
        self._json({
            'ok': True,
            'events': events,
            'total': len(all_events),
            'cursor': _event_tracker.latest_cursor,
            'running': _event_tracker.is_running,
            'issues': analyze_events(all_events),
        })

    def _handle_events_start(self):
        """启动 adb logcat 抓取"""
        try:
            _event_tracker.start()
            self._json({'ok': True, 'running': True})
        except Exception as e:
            self._error(f'启动失败: {e} (请确认模拟器已连接)', 500)

    def _handle_events_spec(self):
        """返回打点规范"""
        import json as _json
        import os as _os
        path = _os.path.join(DATA_DIR, 'event_spec.json')
        if _os.path.exists(path):
            with open(path) as f:
                self._json(_json.load(f))
        else:
            self._error('spec not found', 404)

    def _handle_events_spec_update(self, body):
        """接收飞书链接，更新打点规范"""
        url = body.get('url', '') if isinstance(body, dict) else ''
        if not url:
            self._error('请提供飞书表格链接', 400)
            return
        try:
            import re as _re
            sheet_match = _re.search(r'/sheets/([A-Za-z0-9_-]+)(?:[/?#]|$)', url)
            wiki_match = _re.search(r'/wiki/([A-Za-z0-9_-]+)(?:[/?#]|$)', url)
            if not sheet_match and not wiki_match:
                self._error('无法从链接中提取表格ID', 400)
                return
            from lib.feishu import _api as _fapi
            if wiki_match:
                wiki_token = wiki_match.group(1)
                wiki_data = _fapi(
                    'GET',
                    '/wiki/v2/spaces/get_node',
                    params={'token': wiki_token},
                )
                node = wiki_data.get('node', {})
                if node.get('obj_type') != 'sheet' or not node.get('obj_token'):
                    self._error('该 Wiki 链接指向的不是飞书电子表格', 400)
                    return
                token = node['obj_token']
            else:
                token = sheet_match.group(1)
            meta = _fapi('GET', '/sheets/v2/spreadsheets/' + token + '/metainfo')
            sheets = meta.get('sheets', [])
            # 只读取配置的规范页签，故意忽略链接里的 ?sheet=xxx。
            target = next(
                (
                    s for s in sheets
                    if s.get('title', '').strip() == EVENT_SPEC_SHEET_TITLE
                ),
                None,
            )
            if not target:
                self._error(f'未找到规范页签“{EVENT_SPEC_SHEET_TITLE}”', 400)
                return
            data = _fapi(
                'GET',
                '/sheets/v2/spreadsheets/' + token + '/values/' + target['sheetId'],
            )
            rows = data.get('valueRange', {}).get('values', [])
            fields = {}
            events = {}
            event_fields = {}
            current_event = None
            for row in rows[1:]:
                if len(row) >= 2:
                    event_name = str(row[0]).strip() if row[0] else ''
                    event_desc = str(row[1]).strip() if row[1] else ''
                    if event_name and event_name != '事件名':
                        events[event_name] = event_desc
                        current_event = event_name
                        event_fields.setdefault(current_event, {})
                if len(row) >= 4:
                    name = str(row[2]).strip() if row[2] else ''
                    desc = str(row[3]).strip() if row[3] else ''
                    if name and name not in ('字段名', '事件名'):
                        field_type = (
                            str(row[5]).strip().upper()
                            if len(row) >= 6 and row[5]
                            else ''
                        )
                        value_desc = (
                            str(row[4]).strip()
                            if len(row) >= 5 and row[4]
                            else ''
                        )
                        field = {
                            'desc': desc,
                            'type': field_type,
                            'value_desc': value_desc,
                        }
                        fields[name] = field
                        if current_event:
                            event_fields.setdefault(current_event, {})[name] = field
            if not fields:
                self._error('页签中没有读取到有效字段，未修改本地规范', 400)
                return

            spec_path = os.path.join(DATA_DIR, 'event_spec.json')
            existing = {'events': {}, 'fields': {}}
            if os.path.exists(spec_path):
                with open(spec_path, encoding='utf-8') as f:
                    existing = json.load(f)
            old_fields = existing.get('fields', {})
            old_event_fields = existing.get('event_fields', {})
            updated_fields = {}
            for name, incoming in fields.items():
                old = old_fields.get(name, {})
                updated_fields[name] = {
                    'type': incoming['type'] or (
                        old.get('type', '') if isinstance(old, dict) else ''
                    ),
                    'desc': incoming['desc'],
                    'value_desc': incoming['value_desc'],
                }
            updated_event_fields = {}
            for event_name, incoming_fields in event_fields.items():
                updated_event_fields[event_name] = {}
                old_for_event = old_event_fields.get(event_name, {})
                for name, incoming in incoming_fields.items():
                    old = old_for_event.get(name, old_fields.get(name, {}))
                    updated_event_fields[event_name][name] = {
                        'type': incoming['type'] or (
                            old.get('type', '') if isinstance(old, dict) else ''
                        ),
                        'desc': incoming['desc'],
                        'value_desc': incoming['value_desc'],
                    }
            spec = {
                'events': events or existing.get('events', {}),
                'fields': updated_fields,
                'event_fields': (
                    updated_event_fields
                    if events
                    else existing.get('event_fields', {})
                ),
            }
            spec['consistency_issues'] = find_event_field_type_conflicts(
                spec['event_fields']
            )

            tmp_path = spec_path + '.tmp'
            with open(tmp_path, 'w', encoding='utf-8') as f:
                json.dump(spec, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, spec_path)
            self._json({
                'ok': True,
                'count': len(fields),
                'event_count': len(spec['events']),
                'sheet': target['title'],
                'events_preserved': len(spec['events']),
                'conflict_count': len(spec['consistency_issues']),
                'consistency_issues': spec['consistency_issues'],
            })
        except Exception as e:
            message = str(e)
            if 'refresh token' in message.lower() or '20026' in message:
                # 409：飞书业务凭证过期，与平台登录 401 区分开，
                # 避免前端把"凭证过期"误判成"未登录"而弹出登录遮罩。
                self._error('飞书授权已过期，请先在工具页完成重新授权', 409)
            else:
                self._error(f'更新失败: {message}', 500)

    def _handle_events_stop(self):
        """停止抓取"""
        _event_tracker.stop()
        self._json({'ok': True, 'running': False})

    def _handle_events_clear(self):
        """清空事件列表"""
        cursor = _event_tracker.clear()
        self._json({'ok': True, 'total': 0, 'cursor': cursor})

    # === 飞书登陆 ===

    def _handle_auth_status(self):
        """返回当前飞书登录会话的用户信息。"""
        user = self._read_session_user()
        if not user:
            self._json({'ok': True, 'logged_in': False, 'reason': '未登录'})
            return
        self._json({'ok': True, 'logged_in': True, 'user': user})

    def _handle_auth_feishu_login(self):
        """跳转飞书授权页开始登录。"""
        try:
            from lib.feishu import _load_creds
            creds = _load_creds()
            app_id = creds.get('LARK_APP_ID', '')
            if not app_id:
                self._error('未配置飞书应用，请先在工具页完成飞书授权', 500)
                return
            redirect = urllib.parse.quote(AUTH_REDIRECT_URI, safe='')
            scope = urllib.parse.quote(FEISHU_LOGIN_SCOPES, safe='')
            url = (
                'https://accounts.feishu.cn/open-apis/authen/v1/authorize'
                f'?client_id={app_id}&response_type=code&redirect_uri={redirect}'
                f'&scope={scope}'
                f'&state={secrets.token_urlsafe(16)}'
            )
            self.send_response(302)
            self.send_header('Location', url)
            self.end_headers()
        except Exception as e:
            self._error(f'启动飞书登录失败: {str(e)}', 500)

    def _handle_auth_callback(self, qs):
        """飞书 OAuth 回调：换 token → 查用户 → 建会话 → 回首页。"""
        code = qs.get('code', [''])[0]
        if not code:
            self._error('缺少授权 code', 400)
            return
        try:
            from lib.feishu import _load_creds
            creds = _load_creds()
            app_id = creds.get('LARK_APP_ID', '')
            app_secret = creds.get('LARK_APP_SECRET', '')
            token_resp = requests.post(
                'https://accounts.feishu.cn/oauth/v3/token',
                json={
                    'grant_type': 'authorization_code', 'code': code,
                    'client_id': app_id, 'client_secret': app_secret,
                    'redirect_uri': AUTH_REDIRECT_URI,
                },
                timeout=10,
            )
            data = token_resp.json()
            if data.get('code') != 0:
                self._error(f'换取飞书 token 失败: {data.get("msg")}', 502)
                return
            access_token = data.get('access_token', '')
            if not access_token:
                self._error(
                    f'换取飞书 token 失败: {data.get("error_description", data.get("msg", ""))}',
                    502,
                )
                return
            # 登录成功即把业务令牌写入凭证文件，
            # 之后读表格/问卷/附件等直接复用，无需再去工具页单独授权。
            try:
                from lib.feishu import _save_refreshed_creds
                refresh_token = data.get('refresh_token', '')
                expires_in = int(data.get('expires_in') or 7200)
                if refresh_token:
                    _save_refreshed_creds(
                        access_token, refresh_token, int(time.time()) + expires_in
                    )
            except Exception as e:
                log_activity('user_login_creds_save_failed', {
                    'error': str(e),
                })
            user_resp = requests.get(
                'https://open.feishu.cn/open-apis/authen/v1/user_info',
                headers={'Authorization': f'Bearer {access_token}'},
                timeout=10,
            )
            user_data = user_resp.json()
            if user_data.get('code') != 0:
                self._error(f'获取飞书用户信息失败: {user_data.get("msg")}', 502)
                return
            user = user_data['data']
            sid = secrets.token_urlsafe(32)
            _auth_sessions[sid] = {
                'user': {
                    'name': user.get('name', ''),
                    'email': user.get('email', ''),
                    'avatar_url': user.get('avatar_url', ''),
                    'user_id': user.get('user_id', ''),
                    'open_id': user.get('open_id', ''),
                },
                'expires': time.time() + _AUTH_SESSION_TTL,
            }
            _save_auth_sessions()
            log_activity('user_login', {
                'name': user.get('name', ''),
                'email': user.get('email', ''),
            })
            self.send_response(302)
            self.send_header('Location', '/')
            self.send_header(
                'Set-Cookie',
                f'{_AUTH_SESSION_COOKIE}={sid}; HttpOnly; Path=/; '
                f'SameSite=Lax; Max-Age={_AUTH_SESSION_TTL}',
            )
            self.end_headers()
        except Exception as e:
            self._error(f'飞书登录失败: {str(e)}', 500)

    def _handle_auth_logout(self):
        """登出：删除会话并清 cookie。"""
        cookie = self.headers.get('Cookie', '')
        for part in cookie.split(';'):
            part = part.strip()
            if part.startswith(_AUTH_SESSION_COOKIE + '='):
                _auth_sessions.pop(part[len(_AUTH_SESSION_COOKIE) + 1:], None)
        _save_auth_sessions()
        self.send_response(302)
        self.send_header('Location', '/')
        self.send_header(
            'Set-Cookie',
            f'{_AUTH_SESSION_COOKIE}=; HttpOnly; Path=/; Max-Age=0',
        )
        self.end_headers()

    def _handle_auth_login(self):
        """
        触发飞书 OAuth 授权流程（复用 feishu-user-plugin）。
        后台启动 npx feishu-user-plugin oauth，会弹出浏览器完成授权。
        """
        try:
            subprocess.Popen(
                ['npx', 'feishu-user-plugin', 'oauth'],
                cwd=BASE_DIR,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
            log_activity('feishu_auth_login', {'status': 'started'})
            self._json({
                'ok': True,
                'msg': '已在浏览器中打开飞书授权页面，完成后刷新本页面即可'
            })
        except Exception as e:
            self._error(f'启动授权失败: {str(e)}', 500)

    # === 飞书重新授权 ===

    def _handle_reauth(self, body):
        """
        后台启动飞书 OAuth 授权流程。

        当飞书 token 过期导致问卷加载失败时，用户点击"重新授权"按钮，
        后台跑 npx feishu-user-plugin oauth，会弹出浏览器完成授权。
        """
        log_path = os.path.join(DATA_DIR, 'reauth.log')
        try:
            with open(log_path, 'w') as log:
                subprocess.Popen(
                    ['npx', 'feishu-user-plugin', 'oauth'],
                    cwd=BASE_DIR,
                    stdout=log, stderr=log
                )
            log_activity('feishu_reauth', {'status': 'started'})
            self._json({
                'ok': True,
                'output': '授权流程已在后台启动\n\n请在弹出的浏览器窗口中完成飞书授权。\n授权完成后，返回问卷页面点击刷新即可。',
                'msg': '授权流程已启动'
            })
        except FileNotFoundError:
            self._error('未找到 npx 命令，请确保 Node.js 已安装', 500)
        except Exception as e:
            self._error(f'启动失败: {str(e)}', 500)

    def _handle_clear_testcase_cache(self):
        """清理飞书测试用例搜索/文件缓存。"""
        cache_dir = os.path.join(DATA_DIR, 'testcase_cache')
        try:
            if os.path.exists(cache_dir):
                shutil.rmtree(cache_dir)
            os.makedirs(cache_dir, exist_ok=True)
            log_activity('testcase_cache_cleared', {'path': cache_dir})
            self._json({'ok': True, 'msg': '测试用例缓存已清理'})
        except Exception as e:
            self._error(f'清理失败: {str(e)}', 500)

    def _handle_clear_uploads(self, data=None):
        """清理上传目录里未被任何 Bug 引用的附件。

        days 默认 7（保留 7 天内的文件）；传 0/'all' 表示不限时间全部清理。
        无论哪种情况，被任何 Bug 引用的文件都会保留，避免破坏草稿。
        """
        days = 7
        if isinstance(data, dict):
            raw = data.get('days', 7)
            if raw in ('all', '0', 0, None):
                days = 0
            else:
                try:
                    days = max(1, min(int(raw), 90))
                except (TypeError, ValueError):
                    days = 7
        upload_dir = os.path.join(DATA_DIR, 'uploads')
        queue_dir = os.path.join(DATA_DIR, 'bug_queue')

        # 收集所有 bug 引用的附件，被引用的文件一律保留（避免破坏草稿）
        referenced = set()
        for f in glob.glob(os.path.join(queue_dir, '*.json')):
            try:
                with open(f, encoding='utf-8') as handle:
                    bug = json.load(handle)
            except (OSError, ValueError):
                continue
            for att in (bug.get('bug', {}).get('attachments') or []):
                path = att.get('filepath', att) if isinstance(att, dict) else att
                if isinstance(path, str) and path:
                    referenced.add(os.path.realpath(path))

        cutoff = time.time() - days * 86400 if days else 0
        deleted, freed = 0, 0
        try:
            for name in os.listdir(upload_dir):
                path = os.path.join(upload_dir, name)
                if not os.path.isfile(path):
                    continue
                if days and os.path.getmtime(path) >= cutoff:
                    continue
                if os.path.realpath(path) in referenced:
                    continue
                try:
                    freed += os.path.getsize(path)
                    os.remove(path)
                    deleted += 1
                except OSError:
                    pass
        except OSError as e:
            self._error(f'清理失败: {str(e)}', 500)
            return
        remaining = sum(
            1 for name in os.listdir(upload_dir)
            if os.path.isfile(os.path.join(upload_dir, name))
        )
        log_activity('uploads_cleaned', {
            'days': days, 'deleted': deleted,
            'freed_mb': round(freed / 1024 / 1024, 1),
            'remaining': remaining,
        })
        self._json({
            'ok': True, 'deleted': deleted,
            'freed_mb': round(freed / 1024 / 1024, 1),
            'remaining': remaining,
        })

    def _handle_bug_delete_by_id(self, bug_id):
        """删除 bug（通过 DELETE URL 中的 ID）。"""
        delete_bug(bug_id)
        log_activity('bug_deleted', {'bug_id': bug_id})
        self._json({'ok': True})

    def _handle_export_claude(self, bug_id):
        """导出 Claude Code 提示文本（调试用）。"""
        data = get_bug(bug_id)
        if data is None:
            self._error('Bug not found', 404)
            return

        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        action = qs.get('action', ['preview'])[0]
        b = data['bug']
        assignee = b.get('assignee', '萌虎')

        if action == 'submit':
            prompt = (
                "v1.5 已禁用 AI/人工提示词方式写入飞书项目。\n"
                "标准字段、经办人、附件上传、工作项创建与结果校验统一由 "
                "claude_tasks/auto_submit.py 的确定性脚本负责；请在平台中点击重试。\n"
            )
        else:
            prompt = (
                "分析这个 bug 并生成完整的结构化描述。\n\n"
                f"Bug ID: {data['id']}\n"
                f"文件: {os.path.join(DATA_DIR, 'bug_queue', data['id'] + '.json')}\n\n"
                "步骤：\n"
                "1. 读取 JSON\n"
                "2. 查看附件，分析问题，推断操作步骤和实际结果\n"
                "3. 去 /Users/hh/work/功能测试/ 查找相关测试用例，推断预期结果\n"
                "4. 根据简述判断：模块名、severity、priority\n"
                "5. 生成 preview\n"
                "6. 用 Python 写回 JSON\n\n"
                f"已知信息：\n"
                f"- 简述: {b.get('brief_description', '')}\n"
                f"- 经办人: {assignee}\n"
                f"- 附件: {', '.join(b.get('attachments', [])) or '无'}\n"
            )

        self.send_response(200)
        self.send_header('Content-Type', 'text/plain; charset=utf-8')
        self.send_header('Access-Control-Allow-Origin', '*')
        self.end_headers()
        self.wfile.write(prompt.encode('utf-8'))

    # 禁用默认的请求日志（太吵了）
    def log_message(self, *args):
        pass


# ============================================================
# 第六部分：程序入口
# ============================================================

def main():
    """
    启动 HTTP 服务器。

    ThreadingHTTPServer(('127.0.0.1', PORT), HuTestHandler) 做了三件事：
      1. 绑定 127.0.0.1:8898（只监听本机，外部无法访问——安全）
      2. 每当有请求进来，在线程池中创建一个 HuTestHandler 实例处理
      3. 处理完销毁，等待下一个请求

    serve_forever() 进入无限循环，直到按 Ctrl+C。
    """
    _load_auth_sessions()
    _load_recorded_problem_signatures()
    server = ThreadingHTTPServer(('127.0.0.1', PORT), HuTestHandler)
    print(f'🐯 萌虎测试平台 v1.5: http://127.0.0.1:{PORT}')
    print(f'   Ctrl+C 停止')
    import signal

    def handle_sigterm(_signum, _frame):
        raise KeyboardInterrupt

    previous_sigterm = signal.signal(signal.SIGTERM, handle_sigterm)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\n👋 虎测平台已停止')
    finally:
        _event_tracker.stop()
        _device_screen_recorder.stop()
        _device_log_capture.stop()
        server.server_close()
        signal.signal(signal.SIGTERM, previous_sigterm)


# Python 的特殊变量：当直接运行 python3 server.py 时，__name__ 等于 '__main__'
# 当被 import 时（比如别的文件 import server），__name__ 不等于 '__main__'，不执行 main()
if __name__ == '__main__':
    main()
