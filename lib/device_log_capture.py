"""Android USB App 日志采集、分卷保存与历史会话管理。"""

from collections import deque
from datetime import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
import uuid
import zipfile


ANDROID_LINE = re.compile(
    r'^(?P<time>\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+)\s+'
    r'(?P<pid>\d+)\s+(?P<tid>\d+)\s+(?P<level>[VDIWEF])\s+'
    r'(?P<tag>.*?):\s?(?P<message>.*)$'
)

CRASH_PATTERN = re.compile(
    r'\b(?:FATAL EXCEPTION|ANR in|SIG(?:ABRT|SEGV|BUS|ILL|TRAP)|'
    r'native crash|tombstone|fatal error|abort message)\b',
    re.IGNORECASE,
)
NETWORK_PATTERN = re.compile(
    r'\b(?:connection (?:refused|reset|failed)|request failed|network (?:error|failed)|'
    r'timed? ?out|timeout|unable to resolve host|unknownhostexception|'
    r'socketexception|sslhandshakeexception|statuscode\s*[=:]\s*5\d\d|'
    r'http(?:/[\d.]+)?\s+5\d\d)\b',
    re.IGNORECASE,
)
ERROR_PATTERN = re.compile(
    r'(?:exception\b|\berror\b|\bassert(?:ion)? failed\b|'
    r'\bfailed to\b|\bfailure\b)',
    re.IGNORECASE,
)
STACK_LINE_PATTERN = re.compile(
    r'^(?:at\s+)?[A-Za-z_$<][\w.$+<>`]*(?::|\.)'
    r'[A-Za-z_$<][\w$<>`]*\([^)]*\)(?:\s+\[0x[0-9a-f]+\].*)?$',
    re.IGNORECASE,
)
NESTED_EMPTY_ANDROID_LINE = re.compile(
    r'^\d\d-\d\d\s+\d\d:\d\d:\d\d\.\d+\s+\d+\s+\d+\s+'
    r'[VDIWEF]\s+Unity\s*:\s*$',
    re.IGNORECASE,
)
ANDROID_ART_GC_PATTERN = re.compile(
    r'^(?:(?:Background|Explicit)\s+)?(?:young\s+)?concurrent\s+'
    r'(?:mark compact|copying)\s+GC freed\b',
    re.IGNORECASE,
)
UNITY_ASTC_FALLBACK_PATTERN = re.compile(
    r'^WARNING:\s+(?:RGBA?|RGB)\s+Compressed\s+ASTC\d+X\d+\s+\S+\s+'
    r'format is not supported,\s*decompressing texture\s*$',
    re.IGNORECASE,
)
EMULATOR_MESA_QSRI_PATTERN = re.compile(
    r'^exportSyncFdForQSRILocked:\s*'
    r'(?:call for image 0x[0-9a-f]+ hos timage handle 0x[0-9a-f]+|'
    r'got fd:\s*\d+)\s*$',
    re.IGNORECASE,
)
UNITY_UNUSED_ASSET_DETAIL_PATTERNS = (
    re.compile(
        r'^Unloading \d+ Unused Serialized files '
        r'\(Serialized files now loaded: \d+\)$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^Unloading \d+ unused Assets / \([^)]*\)\. '
        r'Loaded Objects now: \d+\.$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^Memory consumption went from \d+(?:\.\d+)? (?:KB|MB|GB) '
        r'to \d+(?:\.\d+)? (?:KB|MB|GB)\.$',
        re.IGNORECASE,
    ),
    re.compile(
        r'^Total: \d+(?:\.\d+)? ms \(FindLiveObjects: .+ '
        r'CreateObjectMapping: .+ MarkObjects: .+ DeleteObjects: .+\)$',
        re.IGNORECASE,
    ),
)
def _run(args, timeout=12):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


def _safe_id(value):
    value = str(value or '')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', value):
        raise ValueError('设备或 App 标识格式不正确')
    return value


SIGNATURE_LIMIT = 140  # 报错签名保留的字符数(见 _normalise_signature)


class DeviceLogCapture:
    def __init__(
        self, data_dir, part_size=50 * 1024 * 1024, on_problem=None,
        max_total_size=50 * 1024 * 1024,
    ):
        self.root = os.path.join(data_dir, 'device_logs')
        os.makedirs(self.root, exist_ok=True)
        self.part_size = max(1, int(part_size))
        self.max_total_size = max(self.part_size, int(max_total_size))
        self._lock = threading.RLock()
        self._lines = deque(maxlen=10000)
        self._cursor = 0
        self._session = None
        self._thread = None
        self._process = None
        self._stop = threading.Event()
        self._problems = {}
        self._recent = deque(maxlen=16)
        self._context_after = {}
        self._on_problem = on_problem
        # 被用户忽略的报错签名(跨会话:不录屏、不进问题总览)
        self._ignored_signatures = self._load_ignored()
        self._migrate_ignored_signatures()

    def environment(self):
        tools = {'adb': bool(shutil.which('adb'))}
        return {
            'ok': True,
            'platform': 'macOS',
            'tools': tools,
            'android_ready': tools['adb'],
        }

    def devices(self):
        rows = []
        if shutil.which('adb'):
            result = _run(['adb', 'devices', '-l'])
            for line in result.stdout.splitlines()[1:]:
                parts = line.split()
                if len(parts) < 2 or parts[1] != 'device':
                    continue
                attrs = dict(
                    item.split(':', 1) for item in parts[2:] if ':' in item
                )
                name = attrs.get('model') or attrs.get('device') or parts[0]
                if parts[0].startswith('emulator-'):
                    avd = _run([
                        'adb', '-s', parts[0], 'emu', 'avd', 'name'
                    ], timeout=5)
                    avd_name = avd.stdout.splitlines()[0].strip() if avd.stdout else ''
                    if avd.returncode == 0 and avd_name:
                        name = avd_name
                rows.append({
                    'platform': 'android', 'id': parts[0],
                    'name': name,
                    'version': '', 'status': 'ready',
                })
        return {'ok': True, 'devices': rows}

    def apps(self, platform, device_id):
        device_id = _safe_id(device_id)
        apps = []
        if platform == 'android':
            if not shutil.which('adb'):
                raise RuntimeError('未安装 adb')
            result = _run([
                'adb', '-s', device_id, 'shell', 'pm', 'list', 'packages', '-3'
            ])
            if result.returncode != 0:
                raise RuntimeError(result.stderr.strip() or '读取 Android App 失败')
            for line in result.stdout.splitlines():
                if line.startswith('package:'):
                    package = line[8:].strip()
                    apps.append({'id': package, 'name': package, 'executable': package})
        else:
            raise ValueError('不支持的平台')
        apps.sort(key=lambda item: (item['name'].lower(), item['id'].lower()))
        return {'ok': True, 'apps': apps}

    def start(self, platform, device_id, app_id, app_name='', executable=''):
        if platform != 'android':
            raise ValueError('不支持的平台')
        device_id, app_id = _safe_id(device_id), _safe_id(app_id)
        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError('已有日志采集会话正在运行')
            now = datetime.now()
            session_id = now.strftime('%Y%m%d_%H%M%S_') + uuid.uuid4().hex[:8]
            session_dir = os.path.join(self.root, session_id)
            os.makedirs(session_dir)
            self._lines.clear()
            self._cursor = 0
            self._problems = {}
            self._recent.clear()
            self._context_after = {}
            self._stop.clear()
            self._session = {
                'id': session_id, 'platform': platform, 'device_id': device_id,
                'app_id': app_id, 'app_name': app_name or app_id,
                'executable': executable or app_name or app_id,
                'started_at': now.isoformat(timespec='seconds'), 'ended_at': '',
                'status': 'starting', 'message': '正在启动', 'parts': [],
                'size': 0, 'line_count': 0, 'problem_count': 0,
                'last_error_at': 0,
            }
            self._write_meta()
            self._thread = threading.Thread(target=self._worker, daemon=True)
            self._thread.start()
            return dict(self._session)

    def stop(self):
        with self._lock:
            thread = self._thread
            if not thread or not thread.is_alive():
                return self.status()
            self._stop.set()
            process = self._process
            if process and process.poll() is None:
                process.terminate()
        thread.join(timeout=5)
        return self.status()

    def status(self):
        with self._lock:
            return {'ok': True, 'session': dict(self._session) if self._session else None}

    def lines_after(self, cursor=0, limit=1000):
        try:
            cursor = int(cursor)
        except (TypeError, ValueError):
            cursor = 0
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = 1000
        limit = max(1, min(limit, 2000))
        with self._lock:
            rows = [line for line in self._lines if line['cursor'] > cursor]
            rows = rows[:limit]
            return {
                'ok': True, 'lines': rows,
                'cursor': rows[-1]['cursor'] if rows else cursor,
                'latest_cursor': self._cursor,
            }

    def sessions(self):
        rows = []
        for name in os.listdir(self.root):
            path = os.path.join(self.root, name, 'metadata.json')
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding='utf-8') as handle:
                    rows.append(json.load(handle))
            except (OSError, ValueError):
                continue
        rows.sort(key=lambda item: item.get('started_at', ''), reverse=True)
        return {'ok': True, 'sessions': rows, 'total_size': sum(row.get('size', 0) for row in rows)}

    def problems(self, session_id=''):
        """返回当前或指定会话中已去重的问题摘要。"""
        with self._lock:
            current_id = (self._session or {}).get('id', '')
            if not session_id or session_id == current_id:
                rows = [
                    self._problem_summary(item)
                    for item in self._problems.values()
                    if not self._is_ignored_problem(item)
                ]
                resolved_id = current_id
            else:
                resolved_id = _safe_id(session_id)
                rows = [
                    self._problem_summary(item)
                    for item in self._load_problems(resolved_id).values()
                    if not self._is_ignored_problem(item)
                ]
        rows.sort(key=lambda item: item.get('last_cursor', 0), reverse=True)
        return {'ok': True, 'session_id': resolved_id, 'problems': rows}

    def problem(self, problem_id, session_id=''):
        """返回单个问题及其最近一次出现时的上下文。"""
        problem_id = _safe_id(problem_id)
        with self._lock:
            current_id = (self._session or {}).get('id', '')
            if not session_id or session_id == current_id:
                item = self._problems.get(problem_id)
                resolved_id = current_id
            else:
                resolved_id = _safe_id(session_id)
                item = self._load_problems(resolved_id).get(problem_id)
            if not item:
                raise FileNotFoundError('日志问题不存在')
            result = dict(item)
            result['session_id'] = resolved_id
            return result

    def mark_problem_bug(self, problem_id, bug_id, session_id=''):
        """记录问题对应的 Bug，保证重复点击不会生成多个草稿。"""
        problem_id = _safe_id(problem_id)
        with self._lock:
            current_id = (self._session or {}).get('id', '')
            if session_id and session_id != current_id:
                raise RuntimeError('历史会话暂不支持生成 Bug')
            item = self._problems.get(problem_id)
            if not item:
                raise FileNotFoundError('日志问题不存在')
            item['bug_id'] = bug_id
            self._write_problems()

    def problem_snapshot(self, problem_id, output_dir, session_id=''):
        """按点击时的文件长度制作不可变纯日志快照。"""
        problem = self.problem(problem_id, session_id)
        resolved_id = problem['session_id']
        if not resolved_id:
            raise FileNotFoundError('日志会话不存在')
        session_dir = os.path.join(self.root, resolved_id)
        if not os.path.isdir(session_dir):
            raise FileNotFoundError('日志会话不存在')
        with self._lock:
            captured = []
            for name in sorted(os.listdir(session_dir)):
                path = os.path.join(session_dir, name)
                if os.path.isfile(path) and name.endswith('.log'):
                    captured.append((name, path, os.path.getsize(path)))
        os.makedirs(output_dir, exist_ok=True)
        filename = f"device_logs_{resolved_id}_{problem_id}_{int(time.time())}.log"
        log_path = os.path.join(output_dir, filename)
        self._write_readable_log(log_path, captured)
        return log_path, problem

    def session_snapshot(self, output_dir, session_id=''):
        """在没有自动识别问题时，也可冻结当前会话的纯日志。"""
        with self._lock:
            current_id = (self._session or {}).get('id', '')
            resolved_id = _safe_id(session_id) if session_id else current_id
            if not resolved_id:
                raise FileNotFoundError('请先开始日志抓取')
            if resolved_id != current_id:
                raise RuntimeError('历史会话请使用下载功能')
            if not self._session.get('line_count'):
                raise RuntimeError('当前还没有抓到日志')
            self._write_meta()
            session = dict(self._session)
            session_dir = os.path.join(self.root, resolved_id)
            captured = []
            for name in sorted(os.listdir(session_dir)):
                path = os.path.join(session_dir, name)
                if os.path.isfile(path) and name.endswith('.log'):
                    captured.append((name, path, os.path.getsize(path)))
        os.makedirs(output_dir, exist_ok=True)
        filename = f"device_logs_{resolved_id}_manual_{int(time.time())}.log"
        log_path = os.path.join(output_dir, filename)
        self._write_readable_log(log_path, captured)
        return log_path, session

    @staticmethod
    def _write_plain_log_snapshot(output_path, captured):
        """合并分卷日志，不夹带元数据、问题汇总或其他辅助文件。"""
        if not captured:
            raise RuntimeError('当前没有可用日志')
        tmp = output_path + '.tmp'
        try:
            with open(tmp, 'wb') as target:
                for _name, path, size in captured:
                    with open(path, 'rb') as source:
                        remaining = size
                        while remaining:
                            chunk = source.read(min(1024 * 1024, remaining))
                            if not chunk:
                                break
                            target.write(chunk)
                            remaining -= len(chunk)
            os.replace(tmp, output_path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    @classmethod
    def _write_readable_log(cls, output_path, captured):
        """合并日志并为每行补充明确的可读级别。"""
        if not captured:
            raise RuntimeError('当前没有可用日志')
        labels = {
            'V': 'VERBOSE', 'D': 'DEBUG', 'I': 'INFO',
            'W': 'WARNING', 'E': 'ERROR', 'F': 'FATAL',
        }
        tmp = output_path + '.tmp'
        try:
            with open(tmp, 'w', encoding='utf-8') as target:
                for _name, path, size in captured:
                    with open(path, encoding='utf-8', errors='replace') as source:
                        remaining = size
                        while remaining > 0:
                            raw = source.readline(remaining)
                            if not raw:
                                break
                            remaining -= len(raw.encode('utf-8', errors='replace'))
                            parsed = cls.parse_line(raw.rstrip('\r\n'), 'android')
                            level = parsed.get('level', '')
                            label = labels.get(level, '未分类')
                            if level:
                                source_name = '/'.join(filter(None, (
                                    parsed.get('process', ''),
                                    parsed.get('tag', ''),
                                )))
                                target.write(
                                    f"{parsed.get('time', '')} [{label}] "
                                    f"{source_name} {parsed.get('message', '')}\n"
                                )
                            else:
                                target.write(f"[{label}] {raw.rstrip()}\n")
            os.replace(tmp, output_path)
        except Exception:
            try:
                os.remove(tmp)
            except OSError:
                pass
            raise

    def session_zip(self, session_id):
        session_id = _safe_id(session_id)
        session_dir = os.path.join(self.root, session_id)
        if not os.path.isdir(session_dir):
            raise FileNotFoundError('日志会话不存在')
        zip_path = os.path.join(self.root, session_id + '.zip')
        captured = []
        with zipfile.ZipFile(zip_path + '.tmp', 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(os.listdir(session_dir)):
                if name.endswith('.log') or name in ('metadata.json', 'problems.json'):
                    path = os.path.join(session_dir, name)
                    archive.write(path, arcname=name)
                    if name.endswith('.log'):
                        captured.append((name, path, os.path.getsize(path)))
            if captured:
                readable_path = os.path.join(self.root, session_id + '.readable.tmp')
                try:
                    self._write_readable_log(readable_path, captured)
                    archive.write(readable_path, arcname='带类型的完整日志.log')
                finally:
                    try:
                        os.remove(readable_path)
                    except OSError:
                        pass
        os.replace(zip_path + '.tmp', zip_path)
        return zip_path

    def delete_session(self, session_id):
        session_id = _safe_id(session_id)
        with self._lock:
            if self._session and self._session.get('id') == session_id and self._thread and self._thread.is_alive():
                raise RuntimeError('不能删除正在采集的会话')
        session_dir = os.path.join(self.root, session_id)
        if not os.path.isdir(session_dir):
            raise FileNotFoundError('日志会话不存在')
        shutil.rmtree(session_dir)
        zip_path = os.path.join(self.root, session_id + '.zip')
        if os.path.exists(zip_path):
            os.remove(zip_path)
        with self._lock:
            if self._session and self._session.get('id') == session_id:
                self._session = None
                self._thread = None
                self._process = None
                self._lines.clear()
                self._cursor = 0
                self._problems = {}
                self._recent.clear()
                self._context_after = {}
        return {'ok': True}

    def clear_problems(self, session_id=''):
        """清空问题总览(保留当前日志抓取继续)。"""
        with self._lock:
            if session_id:
                current_id = (self._session or {}).get('id', '')
                if current_id and session_id != current_id:
                    # 清理非当前会话的问题文件(保留该会话其他数据)
                    path = os.path.join(
                        self.root, _safe_id(session_id), 'problems.json'
                    )
                    if os.path.exists(path):
                        os.remove(path)
                    return {'ok': True}
            self._problems = {}
            self._context_after = {}
            self._write_problems()
        return {'ok': True}

    def _ignored_file(self):
        return os.path.join(
            os.path.dirname(self.root), 'ignored_problem_signatures.json'
        )

    def _load_ignored(self):
        """加载已忽略报错 {signature: {'title','ignored_at'}}。"""
        try:
            path = self._ignored_file()
            if os.path.exists(path):
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    # 原始键直接返回,归一化/截断迁移统一交给
                    # _migrate_ignored_signatures(),以便把结果写回文件
                    return dict(data)
                if isinstance(data, list):  # 兼容旧格式(set 存成的 list)
                    return {s: {'title': s} for s in data}
        except Exception:
            pass
        return {}

    def _normalise_problem_signature(self, signature):
        """把签名里的数字(进程号等)归一化并按 SIGNATURE_LIMIT 截断,保证跨会话稳定。

        直接复用 _normalise_signature,保证"写忽略列表"和"识别报错"用同一套规则,
        否则长签名与短签名对不上,会出现"忽略了还会再冒出来"的问题。
        """
        return self._normalise_signature(signature)

    def _migrate_ignored_signatures(self):
        """把历史忽略记录迁移到当前签名规则(去 PID、截断到 SIGNATURE_LIMIT)。"""
        normalised = {}
        for sig, info in self._ignored_signatures.items():
            normalised.setdefault(self._normalise_problem_signature(sig), info)
        if list(normalised.keys()) != list(self._ignored_signatures.keys()):
            self._ignored_signatures = normalised
            self._save_ignored()

    def _is_ignored_problem(self, item):
        """该问题(按归一化签名)是否已被用户忽略。"""
        if not item:
            return False
        sig = self._normalise_problem_signature(item.get('signature', ''))
        return bool(sig) and sig in self._ignored_signatures

    def _save_ignored(self):
        try:
            path = self._ignored_file()
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(self._ignored_signatures, f,
                          ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        except Exception:
            pass

    def ignore_problem(self, problem_id, session_id=''):
        """忽略指定报错:该报错(签名)不再录屏、不再进问题总览。"""
        problem_id = _safe_id(problem_id)
        with self._lock:
            item = self._problems.get(problem_id)
            sig = self._normalise_problem_signature(
                (item or {}).get('signature', '')
            )
            if sig:
                self._ignored_signatures[sig] = {
                    'title': (item or {}).get('title', sig),
                    'message': (item or {}).get('message', ''),
                    'ignored_at': (item or {}).get('last_at')
                        or datetime.now().isoformat(timespec='seconds'),
                }
                self._save_ignored()
            self._problems.pop(problem_id, None)
            self._context_after.pop(problem_id, None)
            self._write_problems()
        return {'ok': True, 'signature': sig}

    def ignored_problems(self):
        """返回已忽略的报错列表(用于恢复)。"""
        with self._lock:
            rows = []
            for sig, info in self._ignored_signatures.items():
                rows.append({
                    'signature': sig,
                    'title': info.get('title', sig),
                    'message': info.get('message', ''),
                    'ignored_at': info.get('ignored_at', ''),
                })
            rows.sort(key=lambda r: r['ignored_at'], reverse=True)
            return {'ok': True, 'ignored': rows}

    def unignore_problem(self, signature):
        """取消忽略:恢复该报错的录屏和问题总览显示。"""
        with self._lock:
            self._ignored_signatures.pop(signature, None)
            self._save_ignored()
        return {'ok': True}

    def clear_sessions(self):
        """删除全部已结束会话，保留仍在采集的当前会话。"""
        with self._lock:
            active_id = ''
            if (
                self._session
                and self._thread
                and self._thread.is_alive()
            ):
                active_id = self._session.get('id', '')
        session_ids = [
            row.get('id', '') for row in self.sessions().get('sessions', [])
            if row.get('id') and row.get('id') != active_id
        ]
        deleted = []
        for session_id in session_ids:
            self.delete_session(session_id)
            deleted.append(session_id)
        return {
            'ok': True,
            'deleted_count': len(deleted),
            'deleted_session_ids': deleted,
        }

    def _worker(self):
        try:
            self._android_worker()
            if not self._stop.is_set():
                self._finish('stopped', '采集进程已结束')
            else:
                self._finish('stopped', '已停止')
        except Exception as exc:
            self._finish('error', str(exc))

    def _android_worker(self):
        serial, package = self._session['device_id'], self._session['app_id']
        while not self._stop.is_set():
            state = _run(['adb', '-s', serial, 'get-state'], timeout=5)
            if state.returncode != 0 or state.stdout.strip() != 'device':
                raise RuntimeError('Android 设备已断开')
            pid_result = _run(['adb', '-s', serial, 'shell', 'pidof', package], timeout=5)
            pids = pid_result.stdout.strip().split()
            if not pids:
                self._set_state('waiting', '等待 App 启动')
                self._stop.wait(1)
                continue
            self._set_state('running', '正在采集')
            args = ['adb', '-s', serial, 'logcat', '--pid=' + pids[0], '-v', 'threadtime', '-T', '1']
            # 监听 App 进程存活：logcat --pid 在目标进程退出后不会自己结束，
            # 需要主动终止才能回到循环、重新绑定新进程继续抓取。
            self._stream_process(args, None, watch_package=package)
            if not self._stop.is_set():
                self._set_state('waiting', 'App 已退出，等待重新启动')
                self._stop.wait(1)

    def _stream_process(self, args, targets, watch_package=None):
        process = subprocess.Popen(
            args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, errors='replace', bufsize=1
        )
        with self._lock:
            self._process = process
        watcher = None
        if watch_package:
            watcher = threading.Thread(
                target=self._watch_app_alive,
                args=(process, watch_package),
                daemon=True,
            )
            watcher.start()
        try:
            for raw in process.stdout:
                if self._stop.is_set():
                    break
                line = raw.rstrip('\r\n')
                if targets and not any(target in line.lower() for target in targets):
                    continue
                self._append(line)
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
            with self._lock:
                if self._process is process:
                    self._process = None
            if watcher:
                watcher.join(timeout=2)

    def _watch_app_alive(self, process, package):
        """监控目标 App 是否还在运行；进程退出则终止 logcat，
        让抓取循环尽快回到等待状态，App 重新启动后继续抓取。"""
        serial = self._session.get('device_id', '')
        while not self._stop.is_set() and process.poll() is None:
            state = _run(['adb', '-s', serial, 'get-state'], timeout=5)
            if state.returncode != 0 or state.stdout.strip() != 'device':
                self._terminate_process(process)
                return
            pid_result = _run(
                ['adb', '-s', serial, 'shell', 'pidof', package], timeout=5
            )
            pids = pid_result.stdout.strip().split()
            if not pids:
                self._terminate_process(process)
                return
            self._stop.wait(1)

    @staticmethod
    def _terminate_process(process):
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except Exception:
                try:
                    process.kill()
                except Exception:
                    pass

    def _append(self, raw):
        parsed = self.parse_line(raw, self._session['platform'])
        if self._is_discard_noise(parsed):
            return
        callback_problem = None
        with self._lock:
            self._cursor += 1
            parsed['cursor'] = self._cursor
            parsed['received_epoch'] = time.time()
            if parsed.get('level') in ('E', 'F'):
                self._session['last_error_at'] = parsed['received_epoch']
            self._lines.append(parsed)
            self._session['line_count'] += 1
            self._write_raw(raw + '\n')
            context_line = {
                'cursor': parsed['cursor'], 'time': parsed.get('time', ''),
                'level': parsed.get('level', ''), 'source': '/'.join(
                    value for value in (parsed.get('process', ''), parsed.get('tag', ''))
                    if value
                ),
                'message': parsed.get('message') or parsed.get('raw') or '',
            }
            for problem_id in list(self._context_after):
                item = self._problems.get(problem_id)
                if item:
                    item.setdefault('context', []).append(context_line)
                    item['context'] = item['context'][-40:]
                self._context_after[problem_id] -= 1
                if self._context_after[problem_id] <= 0:
                    del self._context_after[problem_id]
            self._recent.append(context_line)
            detected = self._detect_problem(parsed)
            if detected:
                if detected.get('signature') in self._ignored_signatures:
                    detected = None
                else:
                    is_new = self._record_problem(detected)
                    if (
                        is_new and self._on_problem
                        and detected.get('kind') != 'network'
                    ):
                        callback_problem = dict(self._problems[detected['id']])
                        callback_problem['session_id'] = self._session['id']
        if callback_problem:
            try:
                self._on_problem(callback_problem)
            except Exception:
                pass

    @staticmethod
    def parse_line(raw, platform):
        match = ANDROID_LINE.match(raw) if platform == 'android' else None
        if not match:
            return {'time': '', 'level': '', 'process': '', 'tag': '', 'message': raw, 'raw': raw}
        data = match.groupdict()
        level = data.get('level') or ''
        return {
            'time': data.get('time') or '', 'level': level.upper(),
            'process': (data.get('process') or data.get('pid') or '').strip(),
            'tag': (data.get('tag') or '').strip(),
            'message': data.get('message') or '', 'raw': raw,
        }

    def _write_raw(self, text):
        encoded = text.encode('utf-8', errors='replace')
        parts = self._session['parts']
        if not parts or parts[-1]['size'] + len(encoded) > self.part_size:
            seq = 0
            for part in parts:
                match = re.match(r'^part-(\d+)\.log$', part.get('name', ''))
                if match:
                    seq = max(seq, int(match.group(1)))
            parts.append({'name': f'part-{seq + 1:03d}.log', 'size': 0})
        part = parts[-1]
        path = os.path.join(self.root, self._session['id'], part['name'])
        with open(path, 'ab') as handle:
            handle.write(encoded)
        part['size'] += len(encoded)
        self._session['size'] += len(encoded)
        if self._session['line_count'] % 100 == 0:
            self._write_meta()
        self._trim_parts()

    def _trim_parts(self):
        """超过 max_total_size 时滚动删除最早分片，控制单会话磁盘占用。"""
        parts = self._session['parts']
        trimmed = False
        while len(parts) > 1 and self._session['size'] > self.max_total_size:
            oldest = parts.pop(0)
            try:
                os.remove(os.path.join(
                    self.root, self._session['id'], oldest['name'],
                ))
            except OSError:
                pass
            self._session['size'] -= oldest['size']
            trimmed = True
        if trimmed:
            self._write_meta()

    def _set_state(self, status, message):
        with self._lock:
            self._session['status'], self._session['message'] = status, message
            self._write_meta()

    def _finish(self, status, message):
        with self._lock:
            if not self._session:
                return
            self._session['status'], self._session['message'] = status, message
            self._session['ended_at'] = datetime.now().isoformat(timespec='seconds')
            self._write_meta()

    def _write_meta(self):
        if not self._session:
            return
        session_dir = os.path.join(self.root, self._session['id'])
        path = os.path.join(session_dir, 'metadata.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(self._session, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)

    @staticmethod
    def _problem_summary(item):
        return {
            key: item.get(key)
            for key in (
                'id', 'kind', 'severity', 'title', 'message', 'source',
                'level', 'first_at', 'last_at', 'first_cursor', 'last_cursor',
                'first_epoch', 'last_epoch', 'count', 'bug_id',
                'recording_status', 'recording_duration', 'recording_error',
            )
        }

    @staticmethod
    def _normalise_signature(text, limit=SIGNATURE_LIMIT):
        value = (text or '').lower()
        value = re.sub(r'0x[0-9a-f]+', '<hex>', value)
        value = re.sub(
            r'\b[0-9a-f]{8}-[0-9a-f-]{27,}\b', '<uuid>', value
        )
        value = re.sub(r'\b\d+(?:\.\d+)?(?:ms|s|kb|mb|gb)?\b', '<n>', value)
        # 只取前 limit 个字符做签名:崩溃上报里带整段堆栈,取全量会让"同一个报错"
        # 因为堆栈长度不同被拆成多个签名,出现"忽略了还会再冒出来"的问题
        return re.sub(r'\s+', ' ', value).strip()[:limit]

    @staticmethod
    @staticmethod
    def _is_discard_noise(parsed):
        """抓取时直接丢弃的无价值噪音（不写盘、不进内存、不影响问题识别）。

        与 _is_noise 的区别：不包含 Unity 堆栈行——堆栈是报错排查的关键
        证据，必须保留。
        """
        source = f"{parsed.get('process', '')} {parsed.get('tag', '')}".lower()
        message = (parsed.get('message') or parsed.get('raw') or '').strip()
        lower = message.lower()
        if any(name in source for name in (
            'egl_emulation', 'openglrenderer', 'hostconnection', 'gralloc',
            'bufferqueue', 'graphicbuffer', 'hwui',
        )):
            return True
        if 'app_time_stats:' in lower or message in ('<private>', ''):
            return True
        if NESTED_EMPTY_ANDROID_LINE.match(message):
            return True
        if ANDROID_ART_GC_PATTERN.match(message):
            return True
        if UNITY_ASTC_FALLBACK_PATTERN.match(message):
            return True
        if ((parsed.get('tag') or '').lower() == 'mesa'
                and EMULATOR_MESA_QSRI_PATTERN.match(message)):
            return True
        if any(pattern.match(message) for pattern in
               UNITY_UNUSED_ASSET_DETAIL_PATTERNS):
            return True
        return False

    @staticmethod
    def _is_noise(parsed):
        source = f"{parsed.get('process', '')} {parsed.get('tag', '')}".lower()
        message = (parsed.get('message') or parsed.get('raw') or '').strip()
        lower = message.lower()
        if any(name in source for name in (
            'egl_emulation', 'openglrenderer', 'hostconnection', 'gralloc',
            'bufferqueue', 'graphicbuffer', 'hwui',
        )):
            return True
        if 'app_time_stats:' in lower or message in ('<private>', ''):
            return True
        if NESTED_EMPTY_ANDROID_LINE.match(message):
            return True
        if ANDROID_ART_GC_PATTERN.match(message):
            return True
        if UNITY_ASTC_FALLBACK_PATTERN.match(message):
            return True
        if ((parsed.get('tag') or '').lower() == 'mesa'
                and EMULATOR_MESA_QSRI_PATTERN.match(message)):
            return True
        if any(pattern.match(message) for pattern in
               UNITY_UNUSED_ASSET_DETAIL_PATTERNS):
            return True
        if (parsed.get('tag') or '').lower() == 'unity' and (
            STACK_LINE_PATTERN.match(message)
            or message.startswith('(Filename:')
            or message.startswith('--- End of stack trace')
        ):
            return True
        return False

    def _detect_problem(self, parsed):
        if self._is_noise(parsed):
            return None
        message = (parsed.get('message') or parsed.get('raw') or '').strip()
        level = (parsed.get('level') or '').upper()
        source = '/'.join(
            value for value in (parsed.get('process', ''), parsed.get('tag', ''))
            if value
        )
        if CRASH_PATTERN.search(message) or level == 'F':
            kind, severity = 'crash', 'critical'
        elif NETWORK_PATTERN.search(message):
            kind, severity = 'network', 'error'
        elif level == 'E' or ERROR_PATTERN.search(message):
            kind, severity = 'exception' if re.search(
                r'exception\b|\bassert', message, re.IGNORECASE
            ) else 'error', 'error'
        else:
            return None
        # source 里的进程号(PID)每次进游戏都变,必须归一化,否则跨会话签名不稳定
        source_key = re.sub(
            r'\b\d+(?:\.\d+)?\b', '<n>', source.lower()
        )
        signature = '|'.join((
            kind, source_key, self._normalise_signature(message)
        ))
        problem_id = hashlib.sha1(signature.encode('utf-8')).hexdigest()[:12]
        return {
            'id': problem_id, 'signature': signature, 'kind': kind,
            'severity': severity, 'title': message[:180] or source or '未知错误',
            'message': message, 'source': source, 'level': level,
            'time': parsed.get('time', ''), 'cursor': parsed['cursor'],
            'epoch': parsed.get('received_epoch') or 0,
        }

    def _record_problem(self, detected):
        problem_id = detected['id']
        now = detected.get('time') or datetime.now().isoformat(timespec='seconds')
        item = self._problems.get(problem_id)
        if item:
            is_new = False
            item['count'] += 1
            item['last_at'] = now
            item['last_epoch'] = detected.get('epoch') or 0
            item['last_cursor'] = detected['cursor']
            item['message'] = detected['message']
        else:
            is_new = True
            item = {
                key: detected[key] for key in (
                    'id', 'signature', 'kind', 'severity', 'title', 'message',
                    'source', 'level',
                )
            }
            item.update({
                'first_at': now, 'last_at': now,
                'first_epoch': detected.get('epoch') or 0,
                'last_epoch': detected.get('epoch') or 0,
                'first_cursor': detected['cursor'],
                'last_cursor': detected['cursor'], 'count': 1, 'bug_id': '',
                'recording_status': (
                    'unavailable'
                    if detected.get('kind') == 'network'
                    else ('pending' if self._on_problem else 'unavailable')
                ),
                'recording_path': '', 'recording_duration': 0,
                'recording_error': '',
            })
            self._problems[problem_id] = item
        item['context'] = list(self._recent)
        self._context_after[problem_id] = 12
        self._session['problem_count'] = len(self._problems)
        self._write_problems()
        self._write_meta()
        return is_new

    def mark_problem_recording(
        self, problem_id, path='', duration=0, error='', session_id='',
        status=None,
    ):
        """记录异常出现时自动冻结的现场录屏。"""
        problem_id = _safe_id(problem_id)
        with self._lock:
            current_id = (self._session or {}).get('id', '')
            if session_id and current_id and session_id != current_id:
                return
            item = self._problems.get(problem_id)
            if not item:
                return
            if status == 'pending':
                item['recording_status'] = 'pending'
                item['recording_path'] = ''
                item['recording_duration'] = 0
                item['recording_error'] = ''
            elif status == 'unavailable':
                item['recording_status'] = 'unavailable'
                item['recording_path'] = ''
                item['recording_duration'] = 0
                item['recording_error'] = ''
            else:
                item['recording_status'] = 'ready' if path else 'failed'
                item['recording_path'] = path
                item['recording_duration'] = round(float(duration or 0), 1)
                item['recording_error'] = str(error or '')
            self._write_problems()

    def _write_problems(self):
        if not self._session:
            return
        path = os.path.join(self.root, self._session['id'], 'problems.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(
                {'session_id': self._session['id'], 'problems': self._problems},
                handle, ensure_ascii=False, indent=2,
            )
        os.replace(tmp, path)

    def _load_problems(self, session_id):
        path = os.path.join(self.root, session_id, 'problems.json')
        if not os.path.isfile(path):
            return {}
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
            return data.get('problems', {}) if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}
