"""Android 循环录屏。

使用 Android 自带的 ``screenrecord`` 录制无声短片段，只保留最近一段时间。
生成 Bug 时结束当前片段，合并“问题前 40 秒 + 问题后 20 秒”为一个 MP4：
先拉取已保留的历史片段，再等待续录的后 20 秒一起导出，随后继续录制。
"""

from datetime import datetime
import json
import os
import re
import shutil
import subprocess
import threading
import time


def _safe_id(value):
    value = str(value or '')
    if not re.fullmatch(r'[A-Za-z0-9_.:-]+', value):
        raise ValueError('设备或会话标识格式不正确')
    return value


def _run(args, timeout=15):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout, check=False
    )


class DeviceScreenRecorder:
    """管理单个 Android 设备的循环录屏。"""

    def __init__(
        self, data_dir, segment_seconds=15, history_seconds=90,
        before_seconds=60, after_seconds=20, bit_rate=4_000_000,
    ):
        self.root = os.path.join(data_dir, 'device_recordings')
        os.makedirs(self.root, exist_ok=True)
        self.segment_seconds = max(2, int(segment_seconds))
        self.history_seconds = max(self.segment_seconds, int(history_seconds))
        self.before_seconds = max(1, int(before_seconds))
        self.after_seconds = max(0, int(after_seconds))
        self.bit_rate = max(1_000_000, int(bit_rate))
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._cut_requested = threading.Event()
        self._segment_finished = threading.Event()
        self._thread = None
        self._process = None
        self._current_started = 0.0
        self._session = None

    def environment(self):
        return {
            'ok': True,
            'adb': bool(shutil.which('adb')),
            'ffmpeg': bool(shutil.which('ffmpeg')),
            'ready': bool(shutil.which('adb') and shutil.which('ffmpeg')),
            'history_seconds': self.history_seconds,
            'before_seconds': self.before_seconds,
            'after_seconds': self.after_seconds,
            'audio': False,
        }

    def start(self, device_id, session_id, app_name=''):
        device_id, session_id = _safe_id(device_id), _safe_id(session_id)
        if not shutil.which('adb'):
            raise RuntimeError('未安装 adb，无法录屏')
        if not shutil.which('ffmpeg'):
            raise RuntimeError('未安装 ffmpeg，无法生成最近一分钟视频')
        state = _run(['adb', '-s', device_id, 'get-state'], timeout=5)
        if state.returncode != 0 or state.stdout.strip() != 'device':
            raise RuntimeError('Android 设备未连接')

        with self._lock:
            if self._thread and self._thread.is_alive():
                raise RuntimeError('已有循环录屏正在运行')
            recording_dir = os.path.join(self.root, session_id)
            os.makedirs(recording_dir, exist_ok=True)
            for name in os.listdir(recording_dir):
                if name.startswith('segment-') and name.endswith('.mp4'):
                    os.remove(os.path.join(recording_dir, name))
            self._stop.clear()
            self._cut_requested.clear()
            self._segment_finished.clear()
            self._session = {
                'id': session_id,
                'device_id': device_id,
                'app_name': app_name,
                'status': 'starting',
                'message': '正在启动循环录屏',
                'started_at': datetime.now().isoformat(timespec='seconds'),
                'ended_at': '',
                'segments': [],
                'snapshot_count': 0,
                'last_snapshot': '',
                'audio': False,
                'history_seconds': self.history_seconds,
                'before_seconds': self.before_seconds,
                'after_seconds': self.after_seconds,
            }
            self._write_meta()
            self._thread = threading.Thread(
                target=self._worker, name='android-screen-recorder', daemon=True
            )
            self._thread.start()
            return self.status()

    def stop(self):
        with self._lock:
            thread = self._thread
            if not thread or not thread.is_alive():
                return self.status()
            self._stop.set()
        self._interrupt_screenrecord()
        thread.join(timeout=12)
        with self._lock:
            if thread.is_alive() and self._process:
                self._process.terminate()
            if self._session:
                self._session['status'] = 'stopped'
                self._session['message'] = '循环录屏已停止'
                self._session['ended_at'] = datetime.now().isoformat(
                    timespec='seconds'
                )
                self._write_meta()
        return self.status()

    def delete_session(self, session_id):
        session_id = _safe_id(session_id)
        with self._lock:
            if (
                self._session
                and self._session.get('id') == session_id
                and self._thread
                and self._thread.is_alive()
            ):
                raise RuntimeError('不能删除正在录屏的会话')
        directory = os.path.join(self.root, session_id)
        if os.path.isdir(directory):
            shutil.rmtree(directory)
        with self._lock:
            if self._session and self._session.get('id') == session_id:
                self._session = None
                self._thread = None
                self._process = None
                self._current_started = 0.0

    def status(self):
        with self._lock:
            session = dict(self._session) if self._session else None
            if session:
                session['segments'] = [
                    dict(item) for item in self._session.get('segments', [])
                ]
                available = sum(
                    item.get('duration', 0)
                    for item in self._session.get('segments', [])
                )
                if (
                    self._current_started
                    and session.get('status') in ('starting', 'running')
                ):
                    available += max(0, time.monotonic() - self._current_started)
                session['available_seconds'] = round(
                    min(self.history_seconds, available), 1
                )
                session['active'] = bool(
                    self._thread and self._thread.is_alive()
                    and not self._stop.is_set()
                )
            return {'ok': True, 'session': session}

    def has_session(self, session_id):
        """录屏进程重启后，也能识别磁盘中已保留的同会话片段。"""
        session_id = _safe_id(session_id)
        with self._lock:
            if (
                self._session
                and self._session.get('id') == session_id
                and self._session.get('segments')
            ):
                return True
        metadata = os.path.join(self.root, session_id, 'metadata.json')
        if not os.path.isfile(metadata):
            return False
        try:
            with open(metadata, encoding='utf-8') as handle:
                session = json.load(handle)
            return any(
                os.path.isfile(item.get('path', ''))
                for item in session.get('segments', [])
            )
        except (OSError, ValueError, TypeError):
            return False

    def snapshot(self, output_dir, session_id='', anchor_time=None,
                 before_seconds=None, after_seconds=None):
        """冻结问题前 before_seconds 秒 + 问题后 after_seconds 秒视频。

        运行中会先结束当前片段，收集已保留的历史片段作为“前段”，
        再等待续录的“后段”片段（约 after_seconds 秒）一起合并导出。
        非活动会话（例如服务重启后）没有后段可录，只导出历史前段。

        anchor_time：日志错误时刻（电脑 epoch 秒，收到日志的时间）。提供时，导出窗口以
        该时刻为基准（错误前 before_seconds 秒 + 错误后 after_seconds
        秒），与调用时刻无关；不提供时回退为“调用时刻前 before_seconds
        秒 + 调用时刻后 after_seconds 秒”。

        before_seconds / after_seconds：可选覆盖默认窗口（默认 40 / 20）。
        例如“实时保存”传 before_seconds=60、after_seconds=0，立即导出
        点击前最近 60 秒，不等待后续片段。
        """
        before_seconds = (
            self.before_seconds if before_seconds is None
            else max(1, int(before_seconds))
        )
        after_seconds = (
            self.after_seconds if after_seconds is None
            else max(0, int(after_seconds))
        )
        requested_id = _safe_id(session_id) if session_id else ''
        with self._lock:
            current_matches = bool(
                self._session
                and (not requested_id or requested_id == self._session['id'])
            )
            session = dict(self._session) if current_matches else None
            active = bool(
                current_matches and self._thread and self._thread.is_alive()
            )
            if active:
                self._cut_requested.set()
                self._segment_finished.clear()

        if not session:
            if not requested_id:
                raise RuntimeError('尚未开始循环录屏')
            metadata = os.path.join(
                self.root, requested_id, 'metadata.json'
            )
            try:
                with open(metadata, encoding='utf-8') as handle:
                    session = json.load(handle)
            except (OSError, ValueError, TypeError):
                raise RuntimeError('录屏与当前日志会话不一致')

        if active:
            self._interrupt_screenrecord()
            if not self._segment_finished.wait(timeout=15):
                raise RuntimeError('结束当前录屏片段超时，请重试')

        if active:
            with self._lock:
                session = dict(self._session)
        segments = [
            dict(item) for item in session.get('segments', [])
            if os.path.isfile(item.get('path', ''))
        ]
        if not segments:
            raise RuntimeError('录屏时间太短，暂时还没有可用视频')

        base_count = len(segments)
        after, after_duration = [], 0.0
        if active:
            after, after_duration = self._collect_after_segments(
                session.get('id', ''), base_count, after_seconds
            )
            with self._lock:
                segments = [
                    dict(item) for item in self._session.get('segments', [])
                    if os.path.isfile(item.get('path', ''))
                ]

        if anchor_time is not None:
            selected, skip, out_duration = self._select_window(
                segments, float(anchor_time),
                before_seconds, after_seconds,
            )
            if not selected:
                selected, skip, out_duration = self._tail_window(
                    segments, after, after_seconds, before_seconds
                )
        else:
            selected, skip, out_duration = self._tail_window(
                segments, after, after_seconds, before_seconds
            )

        os.makedirs(output_dir, exist_ok=True)
        stamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        output_path = os.path.join(
            output_dir, f"screen_recording_{session['id']}_{stamp}.mp4"
        )
        args = ['ffmpeg', '-hide_banner', '-loglevel', 'error', '-y']
        for item in selected:
            args.extend(['-i', item['path']])
        filters = []
        labels = []
        for index, item in enumerate(selected):
            item_duration = float(
                item.get('duration') or self.segment_seconds
            )
            label = f'v{index}'
            labels.append(f'[{label}]')
            filters.append(
                f'[{index}:v]'
                f'tpad=stop_mode=clone:stop_duration={item_duration:.3f},'
                f'trim=duration={item_duration:.3f},'
                'setpts=PTS-STARTPTS,'
                'scale=720:1280:force_original_aspect_ratio=decrease,'
                'pad=720:1280:(ow-iw)/2:(oh-ih)/2:black,'
                f'setsar=1[{label}]'
            )
        filters.append(
            ''.join(labels)
            + f'concat=n={len(selected)}:v=1:a=0[joined]'
        )
        filters.append(
            f'[joined]trim=start={skip:.3f}:duration={out_duration:.3f},'
            'setpts=PTS-STARTPTS[outv]'
        )
        args.extend([
            '-filter_complex', ';'.join(filters), '-map', '[outv]',
            '-an', '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '24',
            '-pix_fmt', 'yuv420p', '-movflags', '+faststart', output_path,
        ])
        result = _run(args, timeout=120)
        if (
            result.returncode != 0
            or not os.path.isfile(output_path)
            or os.path.getsize(output_path) < 1024
        ):
            try:
                os.remove(output_path)
            except OSError:
                pass
            raise RuntimeError(
                '合并问题现场录屏失败：'
                + (result.stderr.strip() or 'ffmpeg 未生成视频')
            )

        with self._lock:
            if self._session and self._session.get('id') == session['id']:
                self._session['snapshot_count'] += 1
                self._session['last_snapshot'] = output_path
                self._write_meta()
            else:
                session['snapshot_count'] = (
                    int(session.get('snapshot_count') or 0) + 1
                )
                session['last_snapshot'] = output_path
                self._write_session_meta(session)
        return output_path, {
            'session_id': session['id'],
            'duration': round(out_duration, 1),
            'before_seconds': round(
                min(before_seconds, out_duration), 1
            ),
            'after_seconds': round(
                max(0.0, out_duration - before_seconds), 1
            ),
            'captured_at': datetime.now().isoformat(timespec='seconds'),
            'size': os.path.getsize(output_path),
        }

    def _tail_window(self, segments, after, after_seconds,
                     before_seconds=None):
        """按调用时刻回退窗口：前段取尾部 before 秒，后段取新录片段。"""
        if before_seconds is None:
            before_seconds = self.before_seconds
        before, before_duration = self._take_tail(segments, before_seconds)
        selected = before + after
        after_take = min(
            sum(
                float(item.get('duration') or self.segment_seconds)
                for item in after
            ),
            after_seconds,
        )
        before_take = min(before_duration, before_seconds)
        skip = max(0.0, before_duration - before_seconds)
        return selected, skip, before_take + after_take

    def _select_window(self, segments, anchor_time, before_seconds, after_seconds):
        """按日志错误时刻选择录屏片段，返回 (片段, 起始偏移, 时长)。"""
        window_start = anchor_time - before_seconds
        window_end = anchor_time + after_seconds
        selected = []
        for item in segments:
            start = item.get('start_epoch')
            end = item.get('end_epoch')
            if start is None or end is None:
                continue
            if end < window_start:
                continue
            if start > window_end:
                break
            selected.append(item)
        if not selected:
            return [], 0.0, 0.0
        first_start = float(selected[0].get('start_epoch') or 0)
        last_end = float(selected[-1].get('end_epoch') or 0)
        duration = min(window_end, last_end) - max(window_start, first_start)
        if duration <= 0:
            return [], 0.0, 0.0
        skip = max(0.0, window_start - first_start)
        return selected, skip, duration

    def _take_tail(self, segments, seconds):
        """从片段列表尾部取累计至少 seconds 秒的片段（保留时间顺序）。"""
        selected = []
        duration = 0.0
        for item in reversed(segments):
            selected.append(item)
            duration += float(item.get('duration') or self.segment_seconds)
            if duration >= seconds:
                break
        selected.reverse()
        return selected, duration

    def _collect_after_segments(self, session_id, base_count, after_seconds):
        """等待循环录屏续录，收集中断后新增的 after_seconds 秒片段。"""
        if after_seconds <= 0:
            return [], 0.0
        deadline = (
            time.monotonic() + after_seconds + self.segment_seconds * 2 + 10
        )
        collected = []
        duration = 0.0
        while True:
            with self._lock:
                current = self._session
                running = bool(
                    current
                    and current.get('id') == session_id
                    and self._thread
                    and self._thread.is_alive()
                    and not self._stop.is_set()
                )
                items = list(current.get('segments', [])) if current else []
            new_items = items[base_count:]
            if new_items:
                collected = list(new_items)
                duration = sum(
                    float(item.get('duration') or self.segment_seconds)
                    for item in collected
                )
                if duration >= after_seconds:
                    break
            if not running or time.monotonic() >= deadline:
                break
            time.sleep(0.5)
        return collected, duration

    def _worker(self):
        try:
            self._record_loop()
            status = 'stopped'
            message = '循环录屏已停止'
        except Exception as exc:
            status = 'error'
            message = str(exc)
        with self._lock:
            self._current_started = 0.0
            if self._session:
                self._session['status'] = status
                self._session['message'] = message
                self._session['ended_at'] = datetime.now().isoformat(
                    timespec='seconds'
                )
                self._write_meta()
            self._segment_finished.set()

    def _record_loop(self):
        sequence = 0
        while not self._stop.is_set():
            with self._lock:
                serial = self._session['device_id']
                session_id = self._session['id']
            state = _run(['adb', '-s', serial, 'get-state'], timeout=5)
            if state.returncode != 0 or state.stdout.strip() != 'device':
                raise RuntimeError('Android 设备已断开，循环录屏已停止')

            sequence += 1
            remote = f'/sdcard/hutest_{session_id}_{sequence:05d}.mp4'
            start_epoch = time.time()
            process = subprocess.Popen([
                'adb', '-s', serial, 'shell', 'screenrecord',
                '--bit-rate', str(self.bit_rate),
                '--time-limit', str(self.segment_seconds), remote,
            ], stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            started = time.monotonic()
            with self._lock:
                self._process = process
                self._current_started = started
                self._session['status'] = 'running'
                self._session['message'] = '正在循环录屏（保留最近 60 秒）'
                self._write_meta()
            process.communicate()
            end_epoch = time.time()
            duration = max(0.0, time.monotonic() - started)
            with self._lock:
                if self._process is process:
                    self._process = None
                self._current_started = 0.0

            local = os.path.join(
                self.root, session_id, f'segment-{sequence:05d}.mp4'
            )
            pulled = _run(
                ['adb', '-s', serial, 'pull', remote, local], timeout=30
            )
            _run(['adb', '-s', serial, 'shell', 'rm', '-f', remote], timeout=8)
            cut_requested = self._cut_requested.is_set()
            self._cut_requested.clear()
            if (
                pulled.returncode == 0
                and os.path.isfile(local)
                and os.path.getsize(local) >= 1024
            ):
                with self._lock:
                    self._session['segments'].append({
                        'name': os.path.basename(local),
                        'path': local,
                        'duration': round(duration, 3),
                        'size': os.path.getsize(local),
                        'start_epoch': start_epoch,
                        'end_epoch': end_epoch,
                        'ended_at': datetime.now().isoformat(timespec='seconds'),
                    })
                    self._trim_segments()
                    self._write_meta()
            elif not self._stop.is_set() and not cut_requested:
                raise RuntimeError(
                    '读取录屏片段失败：'
                    + (pulled.stderr.strip() or pulled.stdout.strip() or '未知错误')
                )
            else:
                try:
                    os.remove(local)
                except OSError:
                    pass
            self._segment_finished.set()

    def _interrupt_screenrecord(self):
        with self._lock:
            serial = (self._session or {}).get('device_id', '')
        if not serial:
            return
        pids = _run(
            ['adb', '-s', serial, 'shell', 'pidof', 'screenrecord'], timeout=5
        )
        for pid in pids.stdout.strip().split():
            if pid.isdigit():
                _run(
                    ['adb', '-s', serial, 'shell', 'kill', '-2', pid],
                    timeout=5,
                )

    def _trim_segments(self):
        keep_seconds = self.history_seconds + self.segment_seconds * 2
        kept = []
        duration = 0.0
        for item in reversed(self._session['segments']):
            if kept and duration >= keep_seconds:
                try:
                    os.remove(item['path'])
                except OSError:
                    pass
                continue
            kept.append(item)
            duration += float(item.get('duration') or self.segment_seconds)
        self._session['segments'] = list(reversed(kept))

    def _write_meta(self):
        if not self._session:
            return
        self._write_session_meta(self._session)

    def _write_session_meta(self, session):
        directory = os.path.join(self.root, session['id'])
        os.makedirs(directory, exist_ok=True)
        path = os.path.join(directory, 'metadata.json')
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as handle:
            json.dump(session, handle, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
