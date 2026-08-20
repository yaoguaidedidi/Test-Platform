"""
虎测平台 - Bug 队列管理
========================

这个文件管理 bug 的整个生命周期。
存储方式：每个 bug 存为一个独立的 JSON 文件，放在 data/bug_queue/ 目录。

为什么用 JSON 文件而不是数据库？
  1. 简单 —— 不需要装 MySQL，直接读写文件
  2. 透明 —— 出问题了可以直接打开 JSON 看内容
  3. 够用 —— 每天几十个 bug，JSON 文件完全 hold 住

Bug 的状态机：
  draft ──→ preview_ready ──→ confirmed ──→ submitted
  草稿      预览已生成       用户点了确认   已提交到飞书

Bug JSON 文件的结构（以 data/bug_queue/xxx.json 为例）：
  {
    "id": "20260706_171235_简述",
    "status": "draft",          ← 当前状态
    "created_at": "2026-07-06T17:12:35",
    "mode": "detail",           ← detail=详细, simple=简单, survey=问卷, scripted=脚本
    "submitter": "晓夜",
    "survey_attachments": [],   ← 问卷中的远程附件（file_token）
    "survey_desc": "",
    "survey_env": "",
    "bug": {
      "brief_description": "",  ← 用户填的简述
      "title": "",
      "assignee": "萌虎",
      "attachments": [],        ← 本地附件路径
      "module": "",
      "severity": "2",
      "priority": "1"
    },
    "preview": {                ← 可编辑预览（status=preview_ready 时）
      "title": "", "module": "",
      "severity": "2", "severity_label": "重要",
      "priority": "1", "priority_label": "P1",
      "description": ""
    },
    "submitted_at": null,       ← 提交时间
    "feishu_work_item_id": null ← 飞书项目 ID
  }
"""

import json
import os
import time
import glob
import re
import uuid
import fcntl
from contextlib import contextmanager


# bug 队列存放目录（硬编码路径，因为这个工具只在本地用）
QUEUE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    'data', 'bug_queue'
)

# 合法 bug id 的白名单字符（同时用于防路径穿越）
_SAFE_ID_PATTERN = re.compile(r'[\w\u4e00-\u9fff.,，。【】（）()：:+-]+')


def _validate_bug_id(bug_id):
    return bool(_SAFE_ID_PATTERN.fullmatch(bug_id or ''))


@contextmanager
def lock_bug(bug_id, queue_dir=None):
    """
    跨线程/跨进程互斥锁：串行化同一条 bug 的“读-改-写”。

    server.py 的多请求线程、auto_process.py、auto_submit.py 都可能并发修改
    同一条 bug 的 JSON。这个锁保证状态检查与写回是原子的；进程退出时
    fcntl 自动释放，不会留下死锁。

    参数:
        bug_id: str - 要锁定的 bug id
        queue_dir: str - 队列目录（默认用本模块 QUEUE_DIR；子进程脚本可传
                         自己那份可能被测试替换的 QUEUE_DIR）
    """
    if not _validate_bug_id(bug_id):
        raise ValueError('非法 bug id')
    base = queue_dir or QUEUE_DIR
    lock_dir = os.path.join(base, '.locks')
    os.makedirs(lock_dir, exist_ok=True)
    handle = open(os.path.join(lock_dir, bug_id + '.lock'), 'a+')
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()


def _ensure_dir():
    """确保队列目录存在（不存在就创建）。"""
    os.makedirs(QUEUE_DIR, exist_ok=True)


def _write_bug_json(data):
    """原子写回 bug JSON（先写临时文件再替换，避免并发读看到半截文件）。"""
    filepath = os.path.join(QUEUE_DIR, f"{data['id']}.json")
    tmp_path = filepath + '.tmp'
    with open(tmp_path, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, filepath)


def generate_id(module, brief):
    """
    生成唯一的 bug ID。

    格式：时间戳_简述前20字
    例如：20260706_171235_图鉴红点穿模

    参数:
        module: str - 模块名（当前未实际使用，保留兼容）
        brief: str  - bug 简述
    返回:
        str - bug ID
    """
    ts = time.strftime('%Y%m%d_%H%M%S')
    safe = re.sub(r'[^\w\u4e00-\u9fff.-]+', '_', brief[:20]).strip('._') or '未命名'
    return f"{ts}_{uuid.uuid4().hex[:8]}_{safe}"


def build_survey_preview(brief, survey_desc="", survey_env=""):
    """用问卷原文生成可编辑预览，不调用 AI。"""
    description_parts = []
    if survey_desc:
        description_parts.append(survey_desc)
    if survey_env:
        description_parts.append(f"环境信息：{survey_env}")
    description_parts.append("（如截图录屏log所示）")
    return {
        "title": brief or "",
        "module": "",
        "severity": "2",
        "severity_label": "重要",
        "priority": "1",
        "priority_label": "P1",
        "description": "\n\n".join(description_parts),
    }


def build_simple_preview(brief, module=""):
    """用固定规则生成简单提交预览，不调用 AI。"""
    title = f"【{module}】{brief}" if module else brief
    return {
        "title": title,
        "module": module,
        "severity": "2",
        "severity_label": "重要",
        "priority": "1",
        "priority_label": "P1",
        "description": f"{title}\n\n{brief}（如截图录屏log所示）",
    }


def save_draft(brief_description, assignee="萌虎", attachments=None,
               mode="detail", submitter="", survey_attachments=None,
               survey_desc="", survey_env="", preview=None,
               recording_status="none"):
    """
    创建新的 bug 草稿 —— bug 生命周期的起点。

    用户在页面上填了简述，点击"执行"，前端调 /api/bug/save，最终走到这里。

    参数:
        brief_description: str   - 用户填的简述
        assignee: str            - 经办人（默认萌虎）
        attachments: list        - 本地附件路径列表
        mode: str                - detail=详细, simple=简单, survey=问卷
        submitter: str           - 提交人（问卷模式用）
        survey_attachments: list - 问卷附件（远程 file_token）
        survey_desc: str         - 问卷中的原始描述
        survey_env: str          - 问卷中的环境信息（build 号等）
        preview: dict             - 脚本或人工预览；提供时直接进入待确认状态
        recording_status: str     - 现场录屏状态：pending=生成中 / ready=就绪 /
                                    failed=失败 / none=本次无录屏

    返回:
        (bug_id, filepath) - bug ID 和 JSON 文件路径
    """
    _ensure_dir()
    bug_id = generate_id("", brief_description)

    initial_preview = preview if isinstance(preview, dict) else None
    data = {
        "id": bug_id,
        "status": "preview_ready" if initial_preview else "draft",
        "recording_status": (
            recording_status if recording_status in (
                'pending', 'ready', 'failed', 'none'
            ) else 'none'
        ),
        "error": None,
        "created_at": time.strftime('%Y-%m-%dT%H:%M:%S'),
        "mode": mode,
        "submitter": submitter,
        "survey_attachments": survey_attachments or [],
        "survey_desc": survey_desc,
        "survey_env": survey_env,
        "bug": {
            "brief_description": brief_description,
            "title": (initial_preview or {}).get("title", brief_description),
            "assignee": assignee,
            "attachments": attachments or [],
            "module": (initial_preview or {}).get("module", ""),
            "severity": (initial_preview or {}).get("severity", "2"),
            "priority": (initial_preview or {}).get("priority", "1"),
            "issue_stage": "stage_first",
            "business": "699e5baff696cf2f934946f8",
            "platform": "fpxnk0d7p",
            "device": "y44_srr3a",
            "description": (initial_preview or {}).get("description", ""),
            "steps": "",
            "actual_result": "",
            "expected_result": "",
        },
        "preview": initial_preview,
        "submitted_at": None,        # 提交时间（status=submitted 时才有值）
        "feishu_work_item_id": None, # 飞书项目 ID
    }

    filepath = os.path.join(QUEUE_DIR, f"{bug_id}.json")
    _write_bug_json(data)

    return bug_id, filepath


def get_bug(bug_id):
    """
    读取单个 bug 的完整 JSON 数据。

    参数:
        bug_id: str
    返回:
        dict 或 None（文件不存在）
    """
    if not _validate_bug_id(bug_id):
        return None
    filepath = os.path.join(QUEUE_DIR, f"{bug_id}.json")
    if not os.path.exists(filepath):
        return None
    with open(filepath, 'r', encoding='utf-8') as f:
        return json.load(f)


def update_bug(bug_id, updates):
    """
    更新 bug 的部分字段（深度合并）。

    参数:
        bug_id: str
        updates: dict - 要更新的字段，如 {'preview': {...}} 或 {'status': 'submitted'}

    返回:
        dict 或 None

    示例:
        update_bug('20260706_xxx', {'status': 'submitted', 'feishu_work_item_id': '7039522154'})
    """
    if not _validate_bug_id(bug_id):
        return None
    with lock_bug(bug_id):
        data = get_bug(bug_id)
        if data is None:
            return None

        # 深度合并：updates 中的嵌套 dict 不会覆盖整个 bug dict，而是合并
        _deep_update(data, updates)
        _write_bug_json(data)
    return data


def _deep_update(d, u):
    """
    深度合并两个字典。

    和 dict.update() 的区别：
      dict.update() 是浅合并 —— {'a': {'x': 1}}.update({'a': {'y': 2}}) → {'a': {'y': 2}}（x 丢了！）
      _deep_update() 是深合并 —— 同上操作 → {'a': {'x': 1, 'y': 2}}（x 保留）

    这个函数确保 update_bug({'bug': {'module': '图鉴系统'}}) 只会更新 bug.module，
    不会把 bug 里其他字段（title、severity 等）清掉。
    """
    for k, v in u.items():
        if isinstance(v, dict) and isinstance(d.get(k), dict):
            _deep_update(d[k], v)  # 两者都是 dict → 递归合并
        else:
            d[k] = v               # 否则直接覆盖


def list_queue():
    """
    列出队列中所有 bug（按文件名倒序 = 按时间倒序）。

    返回精简的摘要信息，不包含完整的 description（队列页面不需要，省流量）。

    返回:
        list[dict] - 每个元素包含 id, status, mode, brief, module, created_at 等
    """
    _ensure_dir()
    bugs = []
    # glob 按文件名排序，最新的在前面（因为文件名以时间戳开头）
    for f in sorted(glob.glob(os.path.join(QUEUE_DIR, '*.json')), reverse=True):
        try:
            with open(f, 'r', encoding='utf-8') as fp:
                data = json.load(fp)
            bugs.append({
                "id": data["id"],
                "status": data["status"],
                "mode": data.get("mode", "detail"),
                "brief": data["bug"].get("brief_description", ""),
                "assignee": data["bug"].get("assignee", ""),
                "module": data["bug"].get("module", ""),
                "created_at": data.get("created_at", ""),
                "submitted_at": data.get("submitted_at"),
                "feishu_work_item_id": data.get("feishu_work_item_id"),
            })
        except (json.JSONDecodeError, IOError, KeyError, TypeError, ValueError):
            continue  # 文件损坏了就跳过，别影响整个列表
    return bugs


def delete_bug(bug_id):
    """
    删除 bug（直接删除 JSON 文件，不可恢复！）。

    返回:
        bool - True=删除成功, False=文件不存在
    """
    if not _validate_bug_id(bug_id):
        return False
    with lock_bug(bug_id):
        filepath = os.path.join(QUEUE_DIR, f"{bug_id}.json")
        if os.path.exists(filepath):
            os.remove(filepath)
            return True
        return False


def confirm_bug(bug_id, preview=None, assignee=None):
    """
    确认 bug — 用户点击"确认提交"。

    状态变更：preview_ready → confirmed
    这一步会触发 server.py 启动 auto_submit.py 后台提交。

    并发安全：同一 bug 的确认由 lock_bug 串行化，只有 preview_ready 允许
    流转到 confirmed；重复点击确认不会二次流转，避免起多个提交进程。

    返回:
        (dict, bool) 或 None —— (最新数据, 是否真的发生了
        preview_ready→confirmed 流转)
    """
    if not _validate_bug_id(bug_id):
        return None
    with lock_bug(bug_id):
        data = get_bug(bug_id)
        if data is None:
            return None
        if data.get('status') != 'preview_ready':
            return data, False
        updates = {"status": "confirmed", "error": None}
        if preview:
            updates["preview"] = preview
            updates["bug"] = {
                "title": preview.get("title", ""),
                "module": preview.get("module", ""),
                "severity": preview.get("severity", "2"),
                "priority": preview.get("priority", "1"),
                "description": preview.get("description", ""),
            }
        if assignee:
            updates.setdefault("bug", {})["assignee"] = assignee
        _deep_update(data, updates)
        _write_bug_json(data)
        return data, True


def mark_submitted(bug_id, work_item_id=None):
    """
    标记 bug 为已提交 — 这是 bug 生命周期的终点。

    状态变更：confirmed → submitted

    参数:
        bug_id: str
        work_item_id: str - 飞书项目中的工作项 ID（如 "7039522154"）

    返回:
        dict 或 None
    """
    if not _validate_bug_id(bug_id):
        return None
    with lock_bug(bug_id):
        data = get_bug(bug_id)
        if data is None:
            return None
        data["status"] = "submitted"
        data["submitted_at"] = time.strftime('%Y-%m-%dT%H:%M:%S')
        if work_item_id:
            data["feishu_work_item_id"] = work_item_id
        _write_bug_json(data)
    return data
