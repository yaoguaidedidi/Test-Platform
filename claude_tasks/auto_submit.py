#!/usr/bin/env python3
"""
虎测平台 v1.5 - 飞书项目自动提交脚本
所有标准字段、附件上传、工作项创建和结果校验均由确定性脚本完成。
AI 不参与外部写入。
"""
import json, os, sys, subprocess, time, mimetypes, re, shutil, threading
from concurrent.futures import ThreadPoolExecutor
from collections import Counter

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
QUEUE_DIR = os.path.join(BASE_DIR, 'data', 'bug_queue')
DATA_DIR = os.path.join(BASE_DIR, 'data')
PENDING_DIR = os.path.join(DATA_DIR, 'pending_submit')
LOG_FILE = os.path.join(DATA_DIR, 'auto_submit.log')
MEEGLE = os.environ.get('MEEGLE_BIN', 'meegle')
_LOG_LOCK = threading.Lock()
PROJECT_KEY = os.environ.get('HUTEST_PROJECT_KEY', '699e5b9087ec7c1e141bda73')
WORK_ITEM_TYPE = os.environ.get('HUTEST_WORK_ITEM_TYPE', 'issue')
TEMPLATE_ID = os.environ.get('HUTEST_TEMPLATE_ID', '6377663')
ATTACHMENT_FIELD_KEY = os.environ.get('HUTEST_ATTACHMENT_FIELD_KEY', 'field_512c55')
LEGACY_ATTACHMENT_FIELD_KEY = 'multi_attachment'
DEFAULT_USER_KEY = os.environ.get('HUTEST_DEFAULT_USER_KEY', '7612458870287420634')
USER_KEY_CACHE = {
    '萌虎': '7612458870287420634',
    '小鹿': '7610344703577345212',
}
CURRENT_USER_KEY = None


def _resolve_user_key(assignee):
    if not assignee:
        return DEFAULT_USER_KEY
    if assignee in USER_KEY_CACHE:
        return USER_KEY_CACHE[assignee]

    try:
        result = _run_meegle([
            'user', 'search',
            '--project-key', PROJECT_KEY,
            '--user-keys', assignee,
            '--format', 'json',
            '--select', 'user_key,name,email',
        ], timeout=20)
        if result.returncode == 0:
            data = _parse_json_output(result.stdout)
            candidates = data if isinstance(data, list) else [data]
            for item in candidates:
                if isinstance(item, dict) and item.get('user_key'):
                    uk = item['user_key']
                    USER_KEY_CACHE[assignee] = uk
                    return uk
    except Exception:
        pass

    return DEFAULT_USER_KEY


def _resolve_reporter_user_key():
    """报告人始终取当前 Meegle 登录账号，绝不跟随经办人。"""
    global CURRENT_USER_KEY
    if CURRENT_USER_KEY:
        return CURRENT_USER_KEY
    try:
        result = _run_meegle(['user', 'me', '--format', 'json'], timeout=20)
        if result.returncode == 0:
            data = _parse_json_output(result.stdout)
            if isinstance(data, dict) and data.get('user_key'):
                CURRENT_USER_KEY = str(data['user_key'])
                return CURRENT_USER_KEY
    except Exception:
        pass
    return DEFAULT_USER_KEY


sys.path.insert(0, BASE_DIR)
from lib.feishu import download_attachment
from lib.bug_queue import lock_bug


def log(msg):
    ts = time.strftime('%Y-%m-%d %H:%M:%S')
    line = f'[{ts}] {msg}'
    with _LOG_LOCK:
        if os.environ.get('HUTEST_SUPPRESS_LOG_PRINT') != '1':
            print(line, flush=True)
        with open(LOG_FILE, 'a') as f:
            f.write(line + '\n')


def _atomic_write(path, data):
    """原子写回 JSON，避免服务端并发读取时看到半截文件。"""
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def submit_bug(bug_id):
    path = os.path.join(QUEUE_DIR, f'{bug_id}.json')
    if not os.path.exists(path):
        log(f'ERROR: file not found: {path}')
        return False
    if not re.fullmatch(r'[\w\u4e00-\u9fff.,，。【】（）()：:+-]+', bug_id or ''):
        log(f'ERROR: 非法 bug id: {bug_id}')
        return False

    # 同一 bug 的提交全程持锁：重复确认/并发重试时，只有第一个进程会真正
    # 创建工作项，其余进程等锁释放后看到状态已变化便跳过。
    # 注意：不能因为“已有 work_item_id”就跳过——创建成功但附件校验失败的
    # 重试场景需要继续走 _meegle_create_issue 的复用/补传分支。
    with lock_bug(bug_id, queue_dir=QUEUE_DIR):
        with open(path) as f:
            bug = json.load(f)

        if bug['status'] != 'confirmed':
            log(f'SKIP: status={bug["status"]}')
            return False

        # 收集附件
        survey_attachments = bug.get('survey_attachments', [])
        local_attachments = bug['bug'].get('attachments', [])
        all_attachments = []

        if survey_attachments:
            save_dir = os.path.join(DATA_DIR, 'survey_attachments')
            os.makedirs(save_dir, exist_ok=True)
            for att in survey_attachments:
                ft = att.get('file_token', '')
                name = att.get('name', ft)
                if not ft: continue
                log(f'Downloading: {name}')
                try:
                    local_path = download_attachment(ft, save_dir)
                    all_attachments.append({'name': name, 'local_path': local_path})
                except Exception as e:
                    log(f'  FAILED: {e}')

        for a in local_attachments:
            if isinstance(a, str) and os.path.exists(a):
                all_attachments.append({'name': _display_name(a), 'local_path': a})
            elif isinstance(a, dict):
                local_path = a.get('filepath') or a.get('local_path')
                if local_path and os.path.exists(local_path):
                    all_attachments.append({
                        'name': a.get('filename') or _display_name(local_path),
                        'local_path': local_path
                    })

        log(f'Attachments: {len(all_attachments)} files')

        # 唯一提交路径：Meegle CLI。失败后进入可重试队列，绝不交给 AI 重建。
        if _meegle_create_issue(bug_id, path, bug, all_attachments):
            return True

        log(f'Meegle CLI failed: {bug.get("error") or "unknown error"}')
        return _mark_failed(path, bug, bug.get('error') or '提交飞书失败，请重试')


def _mark_failed(path, bug, message):
    log(f'FAILED: {message}')
    os.makedirs(PENDING_DIR, exist_ok=True)
    bug['status'] = 'submission_failed'
    bug['error'] = message
    bug['pending_since'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    _atomic_write(path, bug)
    pending_path = os.path.join(PENDING_DIR, f'{bug["id"]}.json')
    _atomic_write(pending_path, bug)
    log(f'Queued: {bug["id"]}')
    return False


def _display_name(filepath):
    """去掉平台为防重名添加的时间戳前缀，恢复用户看到的文件名。"""
    name = os.path.basename(filepath)
    return re.sub(r'^\d+_[0-9a-f]{8}_', '', name)


def _run_meegle(args, timeout=180):
    if not shutil.which(MEEGLE):
        raise RuntimeError(f'Meegle CLI not found: {MEEGLE}')
    return subprocess.run(
        [MEEGLE] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=BASE_DIR,
    )


def _json_dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'))


def _parse_json_output(stdout):
    text = (stdout or '').strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r'(\{.*\}|\[.*\])', text, re.S)
        if not match:
            return None
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError:
            return None


def _walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_json(child)


def _extract_work_item_id(stdout):
    data = _parse_json_output(stdout)
    if data is not None:
        for obj in _walk_json(data):
            for key in ('work_item_id', 'workItemID', 'workItemId', 'id'):
                val = obj.get(key)
                if isinstance(val, (int, str)) and re.fullmatch(r'\d{9,12}', str(val)):
                    return str(val)
    match = re.search(r'\b(\d{9,12})\b', stdout or '')
    return match.group(1) if match else None


def _extract_file_token(stdout):
    data = _parse_json_output(stdout)
    if data is not None:
        for obj in _walk_json(data):
            for key in ('file_token', 'fileToken', 'token'):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    return val
    for pattern in (r'"file_token"\s*:\s*"([^"]+)"', r'"fileToken"\s*:\s*"([^"]+)"'):
        match = re.search(pattern, stdout or '')
        if match:
            return match.group(1)
    return None


def _extract_file_url(stdout):
    data = _parse_json_output(stdout)
    if data is not None:
        for obj in _walk_json(data):
            for key in ('file_url', 'fileUrl', 'url'):
                val = obj.get(key)
                if isinstance(val, str) and val:
                    return val
    return None


def _preview_submission_fields(bug):
    preview = bug.get('preview') or {}
    return {
        'name': preview.get('title') or bug.get('bug', {}).get('title', ''),
        'description': preview.get('description', ''),
        'severity': str(preview.get('severity', '2')),
        'priority': str(preview.get('priority', '1')),
    }


def _meegle_base_fields(bug):
    data = _preview_submission_fields(bug)
    assignee = bug.get('bug', {}).get('assignee', '萌虎')
    uk = _resolve_user_key(assignee)
    reporter_uk = _resolve_reporter_user_key()
    return [
        {'field_key': 'template', 'field_value': str(TEMPLATE_ID)},
        {'field_key': 'name', 'field_value': data['name']},
        {'field_key': 'description', 'field_value': data['description']},
        {'field_key': 'severity', 'field_value': data['severity']},
        {'field_key': 'priority', 'field_value': data['priority']},
        {'field_key': 'issue_operator', 'field_value': _json_dumps([uk])},
        {'field_key': 'issue_reporter', 'field_value': _json_dumps([reporter_uk])},
    ]


def _meegle_create_issue(bug_id, path, bug, attachments):
    try:
        status = _run_meegle(['auth', 'status'], timeout=20)
        if status.returncode != 0:
            bug['error'] = f'Meegle 未登录或授权失效: {(status.stderr or status.stdout).strip()[:180]}'
            return False

        wid = bug.get('feishu_work_item_id')
        if wid:
            log(f'Meegle: reusing existing issue: {wid}')
            if attachments and _verify_meegle_attachments(wid, attachments, attempts=1):
                log(f'Meegle: {len(attachments)} attachments already present on {wid}')
                return _mark_submit_success(bug_id, path, bug, wid)
        else:
            uploaded = _meegle_upload_attachments(None, attachments) if attachments else []
            if uploaded:
                log('Meegle: prepared attachments: ' + _json_dumps([
                    {
                        'name': item.get('name'),
                        'size': item.get('size'),
                        'token_tail': str(item.get('fileToken', ''))[-12:],
                    }
                    for item in uploaded
                ]))
            fields = _meegle_base_fields(bug)
            if uploaded:
                fields.append({
                    'field_key': ATTACHMENT_FIELD_KEY,
                    'field_value': _json_dumps(uploaded),
                })
            log(f'Meegle: creating issue with {len(attachments)} attachments...')
            create = _run_meegle([
                'workitem', 'create',
                '--project-key', PROJECT_KEY,
                '--work-item-type', WORK_ITEM_TYPE,
                '--fields', _json_dumps(fields),
                '--format', 'json',
            ], timeout=180)
            if create.returncode != 0:
                bug['error'] = f'Meegle 创建失败: {(create.stderr or create.stdout).strip()}'
                return False
            wid = _extract_work_item_id(create.stdout)
            if not wid:
                bug['error'] = f'Meegle 创建成功但未返回工作项 ID: {create.stdout.strip()[:220]}'
                return False
            bug['feishu_work_item_id'] = wid
            _atomic_write(path, bug)
            log(f'Meegle: created issue {wid}')
            if attachments:
                if not _verify_meegle_attachments(wid, attachments):
                    bug['error'] = f'Meegle 创建成功，但附件字段校验失败（期望 {len(attachments)} 个）'
                    return False
                log(f'Meegle: verified {len(attachments)} attachments on {wid}')
            return _mark_submit_success(bug_id, path, bug, wid)

        # 仅兼容历史上已经创建、但附件尚未关联的工作项。新工作项在创建时直接带附件。
        if attachments:
            uploaded = _meegle_upload_attachments(wid, attachments)
            fields = [{
                'field_key': ATTACHMENT_FIELD_KEY,
                'field_value': _json_dumps(uploaded),
            }]
            # 飞书服务端偶发字段校验竞态（-214028 Failed to check field），重试最多3次
            update = None
            last_err = ''
            for retry in range(3):
                if retry > 0:
                    time.sleep(2)
                try:
                    update = _run_meegle([
                        'workitem', 'update',
                        '--project-key', PROJECT_KEY,
                        '--work-item-id', str(wid),
                        '--fields', _json_dumps(fields),
                        '--format', 'json',
                    ], timeout=180)
                    if update.returncode == 0:
                        break
                    last_err = (update.stderr or update.stdout).strip()[:160]
                except Exception as e:
                    last_err = str(e)
                log(f'Meegle: update_field retry {retry+1}/3: {last_err}')
            if update is None or update.returncode != 0:
                err = last_err
                if update is not None:
                    err = (update.stderr or update.stdout or last_err).strip()
                bug['error'] = f'Meegle 附件字段更新失败: {err[:220]}'
                return False
            if not _verify_meegle_attachments(wid, attachments):
                bug['error'] = f'Meegle 附件更新返回成功，但字段校验失败（期望 {len(attachments)} 个）'
                return False
            log(f'Meegle: attached and verified {len(uploaded)} files on {wid}')

        return _mark_submit_success(bug_id, path, bug, wid)
    except Exception as e:
        bug['error'] = f'Meegle 提交失败: {e}'
        return False


def _meegle_upload_one(wid, att):
    """上传单个附件并返回飞书附件字段数据。"""
    local_path = att['local_path']
    name = att['name']
    mime_type = mimetypes.guess_type(name)[0] or 'application/octet-stream'
    size = os.path.getsize(local_path)
    log(f'Meegle: uploading attachment {name}')
    scope = (
        ['--work-item-id', str(wid)] if wid
        else ['--work-item-type', WORK_ITEM_TYPE]
    )
    result = _run_meegle([
        'attachment', '+upload', local_path,
        '--project-key', PROJECT_KEY,
        *scope,
        '--resource-type', '15',
        '--field-key', ATTACHMENT_FIELD_KEY,
        '--filename', name,
        '--content-type', mime_type,
        '--format', 'json',
    ], timeout=300)
    if result.returncode != 0:
        raise RuntimeError(f'附件上传失败 {name}: {(result.stderr or result.stdout).strip()[:220]}')
    token = _extract_file_token(result.stdout)
    if not token:
        raise RuntimeError(f'附件上传成功但未返回 file_token {name}: {result.stdout.strip()[:220]}')
    file_url = _extract_file_url(result.stdout)
    return {
        'name': name,
        'type': mime_type,
        'size': str(size),
        'fileToken': token,
        # 后端多文件 unique 校验实际使用 uid；只传 fileToken 时，
        # 两个文件的 uid 都为空，会被误判为重复。
        'uid': token,
        'url': file_url or token,
    }


def _meegle_upload_attachments(wid, attachments):
    """并行上传全部附件，返回顺序与输入一致（不压缩、保持原格式）。"""
    if not attachments:
        return []
    workers = min(len(attachments), 4)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        return list(pool.map(
            lambda att: _meegle_upload_one(wid, att), attachments
        ))


def _normalized_attachment_name(name):
    """飞书手动上传重名文件时可能追加“ (数字)”，校验时忽略该后缀。"""
    return re.sub(r' \(\d+\)(?=\.[^.]+$)', '', str(name or '')).casefold()


def _meegle_attachment_values(wid):
    field_keys = list(dict.fromkeys([
        ATTACHMENT_FIELD_KEY, LEGACY_ATTACHMENT_FIELD_KEY,
    ]))
    result = _run_meegle([
        'workitem', 'get',
        '--project-key', PROJECT_KEY,
        '--work-item-id', str(wid),
        '--fields', _json_dumps(field_keys),
        '--format', 'json',
    ], timeout=60)
    if result.returncode != 0:
        raise RuntimeError(
            f'读取附件字段失败: {(result.stderr or result.stdout).strip()[:220]}'
        )
    data = _parse_json_output(result.stdout) or {}
    fields = data.get('work_item_fields') or data.get('fields') or []
    if isinstance(fields, dict):
        values = []
        for field_key in field_keys:
            value = fields.get(field_key, [])
            if isinstance(value, list):
                values.extend(value)
        return values
    values = []
    for field in fields if isinstance(fields, list) else []:
        if field.get('key') in field_keys or field.get('field_key') in field_keys:
            value = field.get('value', [])
            if isinstance(value, list):
                values.extend(value)
    return values


def _verify_meegle_attachments(wid, attachments, attempts=4):
    expected = Counter(_normalized_attachment_name(item.get('name')) for item in attachments)
    last_error = ''
    for attempt in range(max(1, attempts)):
        if attempt:
            time.sleep(min(attempt, 2))
        try:
            actual_values = _meegle_attachment_values(wid)
            actual = Counter(
                _normalized_attachment_name(item.get('name'))
                for item in actual_values if isinstance(item, dict)
            )
            if all(actual[name] >= count for name, count in expected.items()):
                return True
            last_error = f'期望 {dict(expected)}，实际 {dict(actual)}'
        except Exception as exc:
            last_error = str(exc)
    log(f'Meegle: attachment verification failed on {wid}: {last_error}')
    return False


def _mark_submit_success(bug_id, path, bug, wid):
    # 更新状态
    bug['status'] = 'submitted'
    bug['error'] = None
    bug['feishu_work_item_id'] = wid
    bug['submitted_at'] = time.strftime('%Y-%m-%dT%H:%M:%S')
    _atomic_write(path, bug)
    pending_path = os.path.join(PENDING_DIR, f'{bug_id}.json')
    if os.path.exists(pending_path):
        os.remove(pending_path)
    log(f'DONE: {bug_id} -> {wid}')
    return True


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print('Usage: auto_submit.py <bug_id>')
        sys.exit(1)
    submit_bug(sys.argv[1])
