"""
虎测平台 - 飞书 Open API 客户端
================================

这个文件负责跟飞书服务器通信，主要做三件事：
  1. 管理飞书 Access Token（自动刷新，过期了重新拿）
  2. 读取飞书多维表格中的问卷数据（get_records）
  3. 下载问卷中的附件（download_attachment）

飞书 API 的认证方式：
  每次调飞书 API 都要在 HTTP Header 里带上 Authorization: Bearer <token>
  token 有效期 2 小时，过期后需要用 refresh_token 换新的
  refresh_token 也存在 credentials.json 里

如果你是新手上路，建议先看：
  1. _get_token() —— 理解 token 是怎么获取和刷新的
  2. _api()       —— 理解怎么调飞书 API
  3. fetch_records() —— 理解怎么读多维表格数据
"""

import json
import os
import re
import time
import hashlib
import requests       # pip install requests —— 发 HTTP 请求比 Python 自带的 urllib 好用
import tempfile
import fcntl
import difflib


# ============================================================
# 配置
# ============================================================

# 飞书凭证文件位置（feishu-user-plugin 工具管理这个文件）
CREDS_PATH = os.path.expanduser('~/.feishu-user-plugin/credentials.json')

# 数据缓存目录
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'data')
CACHE_FILE = os.path.join(CACHE_DIR, 'survey_cache.json')
TEST_CASE_CACHE_DIR = os.path.join(CACHE_DIR, 'testcase_cache')
TEST_CASE_FILE_CACHE_DIR = os.path.join(TEST_CASE_CACHE_DIR, 'files')
TEST_CASE_SEARCH_CACHE_DIR = os.path.join(TEST_CASE_CACHE_DIR, 'search')

# 飞书 API 的根地址
BASE_URL = 'https://open.feishu.cn/open-apis'

# 飞书多维表格的 ID（相当于"数据库名"和"表名"）
APP_TOKEN = 'Goi4bAXU1ahb1ksNhcPc4gtFnue'   # 多维表格应用 ID
TABLE_ID = 'tbl5ZnKsEfueGTaP'                # 工作表 ID

# —— Token 缓存（全局变量，内存中保存，避免每次调 API 都重新登录）——
_token = None           # 当前的 access_token
_token_expiry = 0       # token 过期时间（Unix 时间戳）


# ============================================================
# Token 管理
# ============================================================

def _load_creds():
    """
    从本地文件读取飞书凭证。

    credentials.json 的结构：
    {
      "profiles": {
        "default": {
          "LARK_APP_ID": "cli_xxx",
          "LARK_APP_SECRET": "xxx",
          "LARK_USER_ACCESS_TOKEN": "xxx",    ← 短期访问令牌（2小时有效）
          "LARK_USER_REFRESH_TOKEN": "xxx",   ← 长期刷新令牌（用来换新的 access_token）
          "LARK_UAT_EXPIRES": 1783412345       ← access_token 的过期时间戳
        }
      }
    }

    返回: dict - default profile 的凭证
    """
    if not os.path.exists(CREDS_PATH):
        raise FileNotFoundError(f'Credentials not found: {CREDS_PATH}')
    with open(CREDS_PATH) as f:
        data = json.load(f)
    profile_name = data.get('active', 'default')
    return data['profiles'][profile_name]


def _save_refreshed_creds(access_token, refresh_token, expires_at):
    """原子写回当前 profile，避免服务重启后重新读到旧 token。"""
    with open(CREDS_PATH, encoding='utf-8') as f:
        data = json.load(f)
    profile_name = data.get('active', 'default')
    profile = data.setdefault('profiles', {}).setdefault(profile_name, {})
    profile['LARK_USER_ACCESS_TOKEN'] = access_token
    profile['LARK_USER_REFRESH_TOKEN'] = refresh_token
    profile['LARK_UAT_EXPIRES'] = int(expires_at)

    directory = os.path.dirname(CREDS_PATH)
    fd, temp_path = tempfile.mkstemp(prefix='.credentials.', suffix='.tmp', dir=directory)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(temp_path, 0o600)
        os.replace(temp_path, CREDS_PATH)
    finally:
        if os.path.exists(temp_path):
            os.unlink(temp_path)


def _refresh_token():
    """
    用 refresh_token 向飞书换一个新的 access_token。

    什么时候调用？
      access_token 过期后，_get_token() 会自动调用这个函数。

    调飞书的哪个接口？
      POST https://open.feishu.cn/open-apis/authen/v1/refresh_access_token

    请求参数：
      grant_type: "refresh_token"     — 告诉飞书"我要刷新 token"
      refresh_token: "xxx"           — 长期刷新令牌
      app_id: "cli_xxx"              — 应用 ID
      app_secret: "xxx"              — 应用密钥

    如果 refresh_token 也过期了怎么办？
      飞书会返回 code=20026 "refresh token not found"
      这时候需要用户重新 OAuth 授权（工具页的"重新授权"按钮）
    """
    global _token, _token_expiry

    lock_path = f'{CREDS_PATH}.refresh.lock'
    os.makedirs(os.path.dirname(lock_path), exist_ok=True)
    with open(lock_path, 'a+') as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        creds = _load_creds()
        r = requests.post(f'{BASE_URL}/authen/v1/refresh_access_token', json={
            'grant_type': 'refresh_token',
            'refresh_token': creds['LARK_USER_REFRESH_TOKEN'],
            'app_id': creds['LARK_APP_ID'],
            'app_secret': creds['LARK_APP_SECRET'],
        }, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get('code') != 0:
            raise Exception(
                f'Token refresh failed: code={data.get("code")} msg={data.get("msg")}'
            )

        _token = data['access_token']
        refresh_token = data.get('refresh_token') or creds['LARK_USER_REFRESH_TOKEN']
        expires_in = data.get('expires_in') or data.get('expire') or 7200
        expires_at = int(time.time() + expires_in)
        _token_expiry = expires_at - 60
        _save_refreshed_creds(_token, refresh_token, expires_at)
        return _token


def _get_token(force_refresh=False):
    """
    获取有效的 access_token。

    策略（优先用缓存，避免不必要的刷新）：
      1. 如果内存中的 token 还没过期 → 直接用
      2. 如果 force_refresh=True → 强制刷新
      3. 否则优先用 credentials.json 中的 access_token（即使时间戳显示过期了也先试试）
         → 因为时间戳可能不准（今天遇到过：expires 显示 3.9 小时前过期，但 token 实际还能用）
      4. 如果 API 调用的确返回了 401 → _api() 会调 force_refresh=True 重试

    参数:
        force_refresh: bool - 是否强制刷新
    返回:
        str - access_token
    """
    global _token, _token_expiry

    # 情况1：缓存有效（但每5分钟检查一次文件，确保OAuth刷新后能及时生效）
    if _token and time.time() < _token_expiry and not force_refresh:
        if time.time() - getattr(_get_token, '_last_file_check', 0) > 300:
            creds = _load_creds()
            _get_token._last_file_check = time.time()
            fresh = creds.get('LARK_USER_ACCESS_TOKEN', '')
            if fresh and fresh != _token:
                _token = fresh
                _token_expiry = int(creds.get('LARK_UAT_EXPIRES', 0)) - 60
        return _token

    # 情况2：凭证文件中的 access_token 仍有效
    if not force_refresh:
        creds = _load_creds()
        expires_at = int(creds.get('LARK_UAT_EXPIRES') or 0)
        if creds.get('LARK_USER_ACCESS_TOKEN') and expires_at > time.time() + 60:
            _token = creds['LARK_USER_ACCESS_TOKEN']
            _token_expiry = expires_at - 60
            return _token

    # 情况3：已过期或强制刷新
    return _refresh_token()


# ============================================================
# 通用 API 调用
# ============================================================

def _api(method, path, **kwargs):
    """
    通用的飞书 API 调用函数。

    所有飞书 API 请求都经过这个函数，它负责：
      1. 自动加上 Authorization 头（access_token）
      2. 如果返回 401（未授权），自动刷新 token 后重试一次

    参数:
        method: str   - HTTP 方法：'GET', 'POST' 等
        path: str     - API 路径，如 '/bitable/v1/apps/xxx/tables/yyy/records'
        **kwargs      - 传给 requests.request 的额外参数（params, json, timeout 等）

    返回:
        dict - 飞书 API 的响应数据（已经提取了 data 字段）

    示例:
        _api('GET', '/bitable/v1/apps/xxx/tables/yyy/records', params={'page_size': 50})
    """
    url = f'{BASE_URL}{path}'
    headers = kwargs.pop('headers', {})

    # 第一次请求
    headers['Authorization'] = f'Bearer {_get_token()}'
    r = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    # 飞书有些接口用 HTTP 200 + 业务错误码表示 token 过期
    try:
        first_data = r.json()
    except ValueError:
        first_data = {}
    auth_codes = {99991661, 99991663, 99991668, 99991671, 99991672, 99991677}
    if r.status_code == 401 or first_data.get('code') in auth_codes:
        headers['Authorization'] = f'Bearer {_get_token(force_refresh=True)}'
        r = requests.request(method, url, headers=headers, timeout=30, **kwargs)

    r.raise_for_status()
    data = r.json()

    if data.get('code') != 0:
        raise Exception(f'API error: code={data.get("code")} msg={data.get("msg")}')

    # 飞书的返回格式是 {"code": 0, "data": {...}}
    # 这里提取 data 字段
    result = data.get('data')
    return result if result is not None else data


# ============================================================
# 多维表格数据读取
# ============================================================

def fetch_records(page_size=50):
    """
    从飞书多维表格拉取全部问卷记录（自动翻页）。

    飞书多维表格 API 一次只返回一页数据（最多 50 条）。
    这个函数会自动翻页，直到拉完所有数据。

    返回的每条记录包含：
      - record_id: 飞书记录 ID
      - number: 编号
      - title: 标题（玩家填的 bug 简述）
      - description: Bug 描述
      - env_info: 环境信息（build 号、设备等）
      - status: 状态（已录入/待观察/无效等）
      - submitter: 提交人
      - submit_time: 提交时间（毫秒时间戳）
      - attachments: 附件列表 [{name, file_token, size, type}, ...]

    返回: list[dict]
    """
    records = []
    page_token = None

    # 先试一页看看能不能连上
    try:
        data = _api('GET', f'/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records',
                    params={'page_size': 1})
    except Exception as e:
        raise Exception(f'API连接失败: {e}')

    items = data.get('items', [])
    if not items and not data.get('has_more'):
        return records  # 表格确实是空的

    # 全量拉取，逐页拼接
    page_token = None
    while True:
        params = {'page_size': page_size}
        if page_token:
            params['page_token'] = page_token

        data = _api('GET', f'/bitable/v1/apps/{APP_TOKEN}/tables/{TABLE_ID}/records',
                    params=params)
        items = data.get('items', [])

        for item in items:
            f = item.get('fields', {})

            # 飞书多维表格的文本字段返回的是数组格式：
            # [{"text": "hello", "type": "text"}, ...]
            # 用 _text() 函数提取纯文本
            def _text(val):
                if isinstance(val, list):
                    return ''.join([
                        t.get('text', '') if isinstance(t, dict) else str(t)
                        for t in val
                    ])
                if isinstance(val, dict):
                    return val.get('text', '')
                return str(val) if val else ''

            # 提取各个字段
            title = _text(f.get('标题', ''))
            desc = _text(f.get('Bug 描述', ''))
            env = _text(f.get('环境信息（用户ID/build号/版本号/设备信息等）', ''))
            note = _text(f.get('备注', ''))

            # 提取提交人（飞书的"人员"字段是数组，取第一个）
            submitter_raw = f.get('提交人', [])
            if isinstance(submitter_raw, list) and submitter_raw:
                submitter = submitter_raw[0].get('name', '') if isinstance(submitter_raw[0], dict) else str(submitter_raw[0])
            elif isinstance(submitter_raw, dict):
                submitter = submitter_raw.get('name', '')
            else:
                submitter = str(submitter_raw) if submitter_raw else ''

            records.append({
                'record_id': item.get('record_id', ''),
                'number': f.get('编号', ''),
                'title': title,
                'description': desc,
                'env_info': env,
                'note': note,
                'status': f.get('状态', ''),
                'submitter': submitter,
                'submit_time': f.get('提交时间', 0),
                'attachments': [
                    {
                        'name': a.get('name', '') if isinstance(a, dict) else str(a),
                        'file_token': a.get('file_token', '') if isinstance(a, dict) else '',
                        'size': a.get('size', 0) if isinstance(a, dict) else 0,
                        'type': a.get('type', '') if isinstance(a, dict) else '',
                    }
                    for a in f.get('附件（截图/视频/日志）', [])
                ] if isinstance(f.get('附件（截图/视频/日志）'), list) else [],
            })

        # 检查是否还有下一页
        if not data.get('has_more'):
            break
        page_token = data.get('page_token', '')
        if not page_token:
            break

    return records


# ============================================================
# 附件下载
# ============================================================

def download_attachment(file_token, save_dir=None):
    """
    从飞书下载附件到本地。

    飞书的附件存储在"飞书云盘"中，用 file_token 标识。
    下载接口：GET /drive/v1/medias/{file_token}/download

    参数:
        file_token: str - 飞书文件的唯一标识（从问卷记录的 attachments 中取得）
        save_dir: str   - 保存目录，默认 data/survey_attachments/

    返回:
        str - 本地文件路径
    """
    if save_dir is None:
        save_dir = os.path.join(CACHE_DIR, 'survey_attachments')
    os.makedirs(save_dir, exist_ok=True)

    url = f'{BASE_URL}/drive/v1/medias/{file_token}/download'
    headers = {'Authorization': f'Bearer {_get_token()}'}

    # allow_redirects=True：飞书会重定向到真正的下载地址
    r = requests.get(url, headers=headers, timeout=120, allow_redirects=True)
    r.raise_for_status()

    # 从响应头提取文件名（Content-Disposition: attachment; filename="xxx.png"）
    cd = r.headers.get('Content-Disposition', '')
    fname = file_token  # 默认用 file_token 做文件名

    if 'filename*=' in cd:
        # RFC 5987 编码的文件名: filename*=UTF-8''%E6%88%AA%E5%B1%8F.png
        fname = cd.split('filename*=')[-1].split(';')[0].strip()
        if "''" in fname:
            fname = fname.split("''")[-1]
        from urllib.parse import unquote
        fname = unquote(fname.strip('\"'))
    elif 'filename=' in cd:
        # 普通文件名: filename="screenshot.png"
        fname = cd.split('filename=')[-1].split(';')[0].strip('\"')

    # 只保留文件名部分：去掉路径分隔符和目录穿越（../../ 等），空名回退到 token
    fname = os.path.basename((fname or '').replace('\\', '/')).strip()
    fname = fname or file_token

    fpath = os.path.join(save_dir, fname)
    # 同名冲突时用 file_token 区分，避免问卷两个同名附件互相覆盖
    if os.path.exists(fpath):
        stem, ext = os.path.splitext(fname)
        fpath = os.path.join(save_dir, f'{stem}_{file_token[:8]}{ext}')

    # 写入本地文件
    with open(fpath, 'wb') as f:
        f.write(r.content)

    return fpath


# ============================================================
# 缓存机制
# ============================================================
# 问卷数据不经常变，但每次调飞书 API 都要几秒钟。
# 所以加一层本地缓存：5 分钟内重复访问直接用缓存，不发网络请求。
# 飞书 API 挂了也能降级返回旧缓存。

def get_cached_records(max_age=300):
    """
    读取本地缓存的问卷数据。

    参数:
        max_age: int - 缓存有效期（秒）。0 表示返回任意时间的缓存。
                       默认 300 秒 = 5 分钟。

    返回:
        list 或 None - 有效缓存则返回记录列表，否则返回 None
    """
    if os.path.exists(CACHE_FILE):
        with open(CACHE_FILE) as f:
            cached = json.load(f)
        if max_age <= 0 or time.time() - cached.get('ts', 0) < max_age:
            return cached.get('records', [])
    return None


def save_cache(records):
    """把问卷数据写入本地缓存文件。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    with open(CACHE_FILE, 'w') as f:
        json.dump({'ts': time.time(), 'records': records}, f, ensure_ascii=False)


# ============================================================
# 云盘 / 文档读取（不依赖 MCP，直接用 OAuth token）
# ============================================================

def list_drive_files(folder_token='', page_size=50):
    """
    列出飞书云盘目录下的文件和文件夹。

    参数:
        folder_token: str - 文件夹 token，空字符串表示根目录
        page_size: int
    返回:
        list[dict] - 文件/文件夹列表，每个包含 name, token, type, url
    """
    items = []
    page_token = None
    while True:
        params = {'page_size': page_size}
        if folder_token:
            params['folder_token'] = folder_token
        if page_token:
            params['page_token'] = page_token
        data = _api('GET', '/drive/v1/files', params=params)
        files = data.get('files', [])
        for f in files:
            items.append({
                'name': f.get('name', ''),
                'token': f.get('token', ''),
                'type': f.get('type', ''),
                'url': f.get('url', ''),
            })
        if not data.get('has_more'):
            break
        page_token = data.get('page_token', '')
        if not page_token:
            break
    return items


def read_drive_doc(file_token):
    """读取飞书 docx 的纯文本内容。"""
    data = _api('GET', f'/docx/v1/documents/{file_token}/raw_content')
    return data.get('content', '')


def read_drive_sheet(file_token):
    """读取飞书 sheet 的所有内容（第一个 tab）。"""
    meta = _api('GET', f'/sheets/v2/spreadsheets/{file_token}/metainfo')
    sheets = meta.get('sheets', [])
    if not sheets:
        return ''
    sid = sheets[0]['sheetId']
    data = _api('GET', f'/sheets/v2/spreadsheets/{file_token}/values/{sid}')
    rows = data.get('valueRange', {}).get('values', [])
    lines = []
    for row in rows:
        cells = [str(c) if c else '' for c in row]
        line = '\t'.join(cells).strip()
        if line:
            lines.append(line)
    return '\n'.join(lines)


# 测试用例存档目录
TEST_CASE_ROOT = 'ZndSfzhSbl9q0MdQr2gcwYmTncc'
TEST_CASE_FILE_CACHE_TTL = 24 * 60 * 60
TEST_CASE_SEARCH_CACHE_TTL = 30 * 60
TEST_CASE_EMPTY_SEARCH_CACHE_TTL = 10 * 60
TEST_CASE_WEAK_KEYWORDS = {
    '关卡', '地图', '元素', '系统', '问题', '错误', '异常', '显示',
    '位置', '界面', '功能', '操作', '移动', '点击', '进入', '退出',
}
TEST_CASE_FOLDER_HINTS = [
    (('元素', '碎冰机', '碎木机', '草机', '炸弹', '彩球', '蜂蜜', '传送门', '关卡', 'lowmatch', '棋子'), '关卡逻辑以及元素测试用例'),
    (('特殊玩法', '体力', '浮木', '漂流瓶', '游泳圈'), '特殊玩法'),
    (('图鉴',), '系统功能用例'),
    (('剧情', '引导'), '系统功能用例'),
    (('生成器',), '系统功能用例'),
    (('热更新',), '系统功能用例'),
    (('账号',), '系统功能用例'),
]


def _cache_get(path, max_age):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        if time.time() - data.get('cached_at', 0) <= max_age:
            return data
    except Exception:
        return None
    return None


def _cache_set(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f'{path}.tmp'
    data = dict(data)
    data['cached_at'] = time.time()
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _search_cache_path(keywords):
    normalized = '|'.join(sorted(set(keywords)))
    digest = hashlib.sha1(normalized.encode('utf-8')).hexdigest()
    return os.path.join(TEST_CASE_SEARCH_CACHE_DIR, f'{digest}.json')


def _file_cache_path(file_token):
    safe = re.sub(r'[^A-Za-z0-9_.-]+', '_', file_token)
    return os.path.join(TEST_CASE_FILE_CACHE_DIR, f'{safe}.json')


def search_test_cases(module_keywords):
    """
    在飞书测试用例存档中递归搜索匹配的文档，返回内容摘要。

    参数:
        module_keywords: list[str] - 模块关键词，如 ['碎冰机', '关卡']
    返回:
        str - 匹配到的测试用例文本摘要
    """
    keywords = [str(kw).strip().lower() for kw in module_keywords if str(kw).strip()]
    if not keywords:
        return ''
    strong_keywords = [kw for kw in keywords if kw not in TEST_CASE_WEAK_KEYWORDS]

    search_cache_path = _search_cache_path(keywords)
    search_cache = _cache_get(search_cache_path, TEST_CASE_SEARCH_CACHE_TTL)
    if search_cache and not search_cache.get('result'):
        search_cache = _cache_get(search_cache_path, TEST_CASE_EMPTY_SEARCH_CACHE_TTL)
    if search_cache is not None:
        return search_cache.get('result', '')

    results = []
    scan = {'files': 0}
    try:
        root_items = _routed_testcase_roots(keywords)
        for root in root_items:
            _search_folder(root['token'], keywords, strong_keywords, results, scan, depth=0)
            if len(results) >= 3:
                break
    except Exception as e:
        scan['api_failed'] = str(e)

    result = '\n---\n'.join(results[:3]) if results else ''
    if not scan.get('api_failed'):
        _cache_set(search_cache_path, {
            'keywords': keywords,
            'result': result,
            'scan': scan,
        })
    return result


def _routed_testcase_roots(keywords):
    """按关键词优先限制扫描范围，减少无关文档和耗时。"""
    root_items = list_drive_files(TEST_CASE_ROOT)

    selected = []
    lowered = ' '.join(keywords)
    for hints, folder_name in TEST_CASE_FOLDER_HINTS:
        if any(hint.lower() in lowered for hint in hints):
            for item in root_items:
                if item.get('type') == 'folder' and folder_name in item.get('name', ''):
                    selected.append(item)
                    # 特殊玩法在元素目录下一层，后续递归会继续限制扫描。
                    break
    if not selected:
        selected = [item for item in root_items if item.get('type') == 'folder']

    # 去重并保留顺序
    seen = set()
    unique = []
    for item in selected:
        token = item.get('token')
        if token and token not in seen:
            seen.add(token)
            unique.append(item)
    return unique or [{'name': 'root', 'token': TEST_CASE_ROOT, 'type': 'folder'}]


def _matched_keywords(text, keywords):
    lower = (text or '').lower()
    return [kw for kw in keywords if kw and kw in lower]


def _fuzzy_name_hit(name, strong_keywords):
    """文件名一字之差时也允许命中，如"碎木机"≈"碎冰机"。"""
    normalized = re.sub(r'[\s:：_\-【】\[\]（）()]+', '', (name or '').lower())
    for kw in strong_keywords:
        if len(kw) < 3:
            continue
        if kw in normalized:
            return True
        width = len(kw)
        for i in range(0, max(0, len(normalized) - width + 1)):
            part = normalized[i:i + width]
            if difflib.SequenceMatcher(None, kw, part).ratio() >= 0.66:
                return True
    return False


def _is_relevant_case(name, content, keywords, strong_keywords):
    name_hits = _matched_keywords(name, keywords)
    content_hits = _matched_keywords(content, keywords)
    all_hits = set(name_hits + content_hits)
    strong_hits = [kw for kw in all_hits if kw in strong_keywords]
    if strong_hits:
        return True
    if _fuzzy_name_hit(name, strong_keywords):
        return True
    # 没有特征词时，至少两个关键词同时命中才认为相关，避免"关卡/地图"误撞打点文档。
    return len(all_hits) >= 2


def _read_testcase_content(item):
    token = item.get('token', '')
    if not token:
        return ''
    cache_path = _file_cache_path(token)
    cached = _cache_get(cache_path, TEST_CASE_FILE_CACHE_TTL)
    if cached is not None:
        return cached.get('content', '')

    content = ''
    if item.get('type') == 'sheet':
        content = read_drive_sheet(token)
    elif item.get('type') in ('doc', 'docx'):
        content = read_drive_doc(token)

    if content:
        _cache_set(cache_path, {
            'token': token,
            'name': item.get('name', ''),
            'type': item.get('type', ''),
            'content': content,
        })
    return content


def _search_folder(token, keywords, strong_keywords, results, scan, depth):
    if depth > 3 or len(results) >= 3 or scan.get('files', 0) >= 30:
        return
    try:
        items = list_drive_files(token)
    except Exception as e:
        scan['api_failed'] = str(e)
        return
    for it in items:
        if len(results) >= 3 or scan.get('files', 0) >= 30:
            return
        name = it.get('name', '')
        if it['type'] == 'folder':
            _search_folder(it['token'], keywords, strong_keywords, results, scan, depth + 1)
        elif it['type'] == 'sheet':
            try:
                scan['files'] = scan.get('files', 0) + 1
                content = _read_testcase_content(it)
                if content and _is_relevant_case(name, content, keywords, strong_keywords):
                    results.append(f'[{name}]\n{content[:2000]}')
            except Exception:
                pass
        elif it['type'] in ('doc', 'docx'):
            try:
                scan['files'] = scan.get('files', 0) + 1
                content = _read_testcase_content(it)
                if content and _is_relevant_case(name, content, keywords, strong_keywords):
                    results.append(f'[{name}]\n{content[:2000]}')
            except Exception:
                pass


# ============================================================
# 公开接口
# ============================================================

def get_records(force_refresh=False, _track_fallback=False):
    """
    获取问卷记录 —— 这是外部调用的主入口。

    策略（三级降级）：
      1. 优先用本地缓存（5 分钟内有效）→ 秒返回
      2. 缓存过期 → 调飞书 API
      3. API 失败 → 降级返回旧缓存（不管多旧都能用）

    参数:
        force_refresh: bool   - True 强制刷新（跳过缓存）
        _track_fallback: bool - True 返回 (records, is_fallback) 元组

    返回:
        list 或 (list, bool) - 问卷记录列表。_track_fallback=True 时额外返回是否用了降级
    """
    if not force_refresh:
        cached = get_cached_records()
        if cached is not None:
            # 缓存命中，直接返回
            return (cached, False) if _track_fallback else cached

    try:
        # 调飞书 API 获取最新数据
        records = fetch_records()
        save_cache(records)  # 更新缓存
        return (records, False) if _track_fallback else records
    except Exception:
        # API 失败，降级返回任意时间的旧缓存
        fallback = get_cached_records(max_age=0)
        if fallback is not None:
            return (fallback, True) if _track_fallback else fallback
        # 连旧缓存都没有，只能抛异常了
        raise
