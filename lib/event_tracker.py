"""
虎测平台 - 打点事件追踪模块
============================
管理 adb logcat 进程，实时抓取 TA SDK 的打点日志。

TA SDK 日志格式 (ThinkingAnalytics.DataHandle tag):
    [ThinkingData] Info: Enqueue data(d0b7):
    {
        "#event_name": "...",
        "#account_id": "...",
        "properties": { ... },
        "base_data_property": { ... }
    }
"""

import json
import re
import subprocess
import threading
import time

# TA SDK 使用的 logcat tag
TA_TAG = 'ThinkingAnalytics.DataHandle'
# 新事件开始的标记行
TA_EVENT_START = re.compile(r'\[ThinkingData\] Info: Enqueue data')
# JSON 起止行
JSON_START = re.compile(r'^\s*\{')
JSON_END = re.compile(r'^\s*\}')


def _parse_event(json_str):
    """解析 TA 事件 JSON，提取结构化字段"""
    data = json.loads(json_str)

    event_name = data.get('#event_name', '')
    account_id = data.get('#account_id', data.get('#distinct_id', ''))
    event_time = data.get('#time', '')

    properties = data.get('properties', {})
    if isinstance(properties, dict):
        props = {k: v for k, v in properties.items()
                 if not k.startswith('#') and k != 'base_data_property'}
        super_props = {k.lstrip('#'): v for k, v in properties.items() if k.startswith('#')}
        base = properties.get('base_data_property', {})
        if isinstance(base, dict):
            for k, v in base.items():
                props[k] = v  # 不加前缀，直接保留原始名
    else:
        props = {}
        super_props = {}
        base = {}

    # 展平嵌套对象，避免前端显示 [object Object]
    flat_props = {}
    for k, v in props.items():
        if isinstance(v, dict):
            for dk, dv in v.items():
                flat_props[f'{k}.{dk}'] = dv
        else:
            flat_props[k] = v

    # 给属性附加中文说明
    props_with_desc = {}
    for k, v in flat_props.items():
        desc = PARAM_DESC.get(k, '')
        props_with_desc[k] = {'value': v, 'desc': desc} if desc else v

    return {
        'ts': time.strftime('%H:%M:%S'),
        'event_name': event_name,
        'account_id': account_id,
        'event_time': event_time,
        'properties': props_with_desc,
        'super_props': super_props,
        'raw': data,
    }


class EventTracker:
    def __init__(self):
        self._process = None
        self._running = False
        self._events = []
        self._next_cursor = 1
        self._lock = threading.Lock()
        self._buffer = []  # 多行 JSON 缓冲区

    @property
    def events(self):
        with self._lock:
            return list(self._events)

    def events_since(self, since_ts):
        """兼容旧版秒级时间戳查询。新客户端应使用 events_after。"""
        with self._lock:
            if not since_ts:
                return list(self._events)
            return [e for e in self._events if e['ts'] > since_ts]

    def events_after(self, cursor):
        """返回游标之后的事件，避免同一秒内的事件被漏掉。"""
        with self._lock:
            return [e for e in self._events if e.get('cursor', 0) > cursor]

    @property
    def latest_cursor(self):
        with self._lock:
            return self._next_cursor - 1

    def start(self):
        if self._running:
            return
        state = subprocess.run(
            ['adb', 'get-state'], capture_output=True, text=True, timeout=5
        )
        if state.returncode != 0 or state.stdout.strip() != 'device':
            raise RuntimeError(state.stderr.strip() or '未检测到可用的 adb 设备')
        cleared = subprocess.run(
            ['adb', 'logcat', '-c'], capture_output=True, text=True, timeout=5
        )
        if cleared.returncode != 0:
            raise RuntimeError(cleared.stderr.strip() or '清理 logcat 失败')
        self._process = subprocess.Popen(
            ['adb', 'logcat', '-s', TA_TAG],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            bufsize=1,
        )
        self._running = True
        threading.Thread(target=self._read_loop, daemon=True).start()

    def stop(self):
        self._running = False
        process = self._process
        self._process = None
        if process:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)

    def clear(self):
        with self._lock:
            self._events.clear()
            return self._next_cursor - 1

    def _strip_prefix(self, line):
        """去掉 logcat 前缀 (timestamp pid tid level TAG: ) 返回消息内容"""
        # 格式: MM-DD HH:MM:SS.mmm PID TID LEVEL TAG: MESSAGE
        idx = line.find(f'{TA_TAG}: ')
        if idx >= 0:
            return line[idx + len(TA_TAG) + 2:]
        return line

    def _read_loop(self):
        collecting = False
        process = self._process
        if not process or not process.stdout:
            self._running = False
            return
        try:
            for line in process.stdout:
                if not self._running:
                    break
                line = self._strip_prefix(line.rstrip())

                # 检测新事件开始；新标记也会丢弃上一条不完整数据。
                if TA_EVENT_START.search(line):
                    self._buffer = []
                    collecting = True
                    continue

                if not collecting:
                    continue

                self._buffer.append(line)
                full = '\n'.join(self._buffer).strip()
                if not full:
                    continue

                # 直接交给 JSON 解析器判断是否完整，不再统计字符串中的 {}。
                try:
                    event = _parse_event(full)
                except (json.JSONDecodeError, KeyError):
                    continue

                self._buffer = []
                collecting = False
                with self._lock:
                    event['cursor'] = self._next_cursor
                    self._next_cursor += 1
                    self._events.insert(0, event)
        finally:
            # adb 进程意外退出时不能继续向前端报告“抓取中”。
            if self._process is process:
                self._process = None
                self._running = False
            if process.poll() is not None:
                process.wait()

    @property
    def is_running(self):
        return self._running


# 代码参数名 → 规范中文说明 映射
PARAM_DESC = {
    # 公参
    'se_id': '会话ID',
    'did': '设备ID',
    'seq_id': '事件序号',
    'se_t': '会话持续秒数',
    'ck_tg': '点击目标UI路径',
    # 场景切换
    'chg_fr': '切换前场景 (main/merge/match)',
    'chg_to': '切换后场景',
    'chg_rs': '切换原因 (1:手动 2:自动 3:关卡 4:章节)',
    'chg_mg_cnt': '切换时合成次数',
    # 关卡
    'lvl_ne': '关卡名称/ID',
    'lvl_id': '关卡ID',
    'lvl_no': '关卡编号',
    'end_lvl_id': '结束关卡ID',
    'st_cnt_ne': '尝试次数',
    'st_cnt_id': '尝试序号',
    'lvl_st_cnt': '关卡尝试次数',
    'lvl_fs': '关卡首次通关? (0/1)',
    'lvl_ne_ne': '失败原因',
    'tp_cnt': '点击次数',
    'mg_cnt': '合成次数',
    'move_left': '剩余步数',
    'obj_left': '剩余目标比例',
    'pp_use': '道具使用数',
    'sc': '当前场景 (1:merge 2:match 3:main)',
    # 订单
    'ord_id': '订单ID',
    'ord_sh': '订单展示数',
    'ord_cmt': '订单完成数',
    'ord_do': '订单进行中数',
    # 生成器
    'gen_id': '生成器ID',
    'gen_type': '生成器类型',
    # 道具
    'it_id': '道具ID',
    'it_cnt': '道具数量',
    'coin': '金币数',
    'eng': '体力值',
    # 货币
    'currency_type': '货币类型',
    'currency_cnt': '货币数量',
    'chg_src': '变更来源',
    # 公参 (cp)
    're_d': '从首次打开游戏起的留存天数(UTC)',
    'uid': '用户的 user id',
    'aid': '用户的 account id',
    'cv': '客户端版本号',
    'bd_id': '大地图ID',
    'chp_id': '章节ID',
    'tp_cnt': '点击生成器事件总次数(公参)',
    'mg_cnt': '合成事件总次数(公参)',
    'chp_do_ord': '当前章节已完成订单数量(公参)',
    'dg_lft': '当前死地剩余数量',
    'lg_lft': '当前活地剩余数量',
    'sc': '当前场景(1:map 2:merge 3:level)',
    'lvl_st_cnt': '关卡总挑战次数(公参)',
    'coin': '金币(库存个数)',
    'eng': 'Merge体力(库存个数)',
    'ht': '三消体力(库存个数)',
    'rkt': '前置道具-横消(库存个数)',
    'bmb': '前置道具-炸弹(库存个数)',
    'cb': '前置道具-彩球(库存个数)',
    'it1': '关内道具1(库存个数)',
    'it2': '关内道具2(库存个数)',
    'it3': '关内道具3(库存个数)',
    'it4': '关内道具4(库存个数)',
    'bf': '当前激活的buff(0-15)',
    # 场景切换 (sn_chg_test2)
    'at_ty': '会话类型(1:start 2:end 3:pause 4:resume)',
    # 订单 (ord_*_test2)
    'ord_sh': '新订单IDs',
    'ing_ord': '当前进行中订单IDs',
    'eng_use': 'Merge体力消耗(距上次提交订单)',
    'lvl_cnt': '通关数(距上次提交订单)',
    'tp_cnt_fr_cmt': '点击生成器次数(距上次提交订单)',
    'mg_cnt_fr_cmt': '合成次数(距上次提交订单)',
    # 生成器 (sp_gen_*_test2)
    'gen_idx': '生成器队列编号',
    'lft_t': '剩余倒计时(秒)',
    'cfg_id': '生成器配置ID',
    'is_t_end': '是否倒计时结束(0:否 1:是)',
    'res_id': '合成物ID',
    'lft_res_ids': '剩余可选合成物IDs',
    'off': '折扣',
    'lft_off': '剩余折扣',
    # 关卡 (lvl_*_test2)
    'lvl_fs': '关卡首次通关(0/1)',
    'end_rs': '关卡结束结果(1:胜利 2:失败 3:退出)',
    't_use': '关卡耗时(毫秒)',
    'prc': '进度百分比(×100)',
    'stp_use': '消耗步数',
    'lft_stp': '剩余步数',
    'pp_use': '道具使用次数',
    'it1_use': '关内道具1使用次数',
    'it2_use': '关内道具2使用次数',
    'it3_use': '关内道具3使用次数',
    'it4_use': '关内道具4使用次数',
    'ego_cnt': 'Ego计数',
    'at_rf_cnt': '自动重试次数',
    'coin_use': '消耗金币',
    'coin_get': '获得金币',
    'eng_get': '获得体力',
    'fish': '鱼元素数量',
    'red': '红色元素数量',
    'cb_lg_cnt': '大型连击次数',
    # 章节统计
    'chp_db_eng': '本章节debug获得体力数',
    'chp_eng_use': '当前章节总消耗体力数',
    'chp_gen_use': '当前章节使用总的生成器个数',
    'chp_lk_cnt': '当前章节Lucky次数',
    'chp_lvl_eng': '本章节打关获得体力数',
    'chp_mg_cnt': '当前章节总合成次数',
    'chp_rem_eng': '当前章节棋盘剩余合成物折算体力数',
    'chp_sp_gen_use': '当前章节使用总的特殊生成器个数',
    'chp_st_eng': '章节转化与章节领奖后剩余体力数',
    'chp_tp_cnt': '当前章节总有效点击普通生成器次数',
    # 关内选择
    'cho': '选择左、中、右的哪个',
    # 货币变更
    'fn_cnt': '消耗后最终数值',
    'fs': 'final state 最终的状态',
    'get_cnt': '获得的数值',
    'get_src': '获取渠道',
    'use_cnt': '消耗的数值',
    'use_src': '消耗渠道',
    # 付费
    'iap': '金额（美分）',
    'pay_fail_rs': '失败原因',
    'pay_gw': '付费入口',
    'pay_id': '付费项配置id',
    'pay_sc': '付费场景',
    'pay_typ': '付费项配置类型',
    'prod_id': '商品id',
    # 其他
    'lft_st': '剩余可玩次数',
    'pnta_tp_cnt': '总点击次数',
    'rd': '本次获得的合成物种类/数量',
    'rslt': '结果',
}

# 从 PARAM_DESC 中删除代码里没实际使用的字段
for _fake in ['chg_src', 'currency_cnt', 'currency_type', 'gen_id', 'gen_type',
              'it_cnt', 'it_id', 'lvl_ne_ne', 'lvl_no', 'move_left',
              'obj_left', 'ord_cmt', 'ord_do']:
    PARAM_DESC.pop(_fake, None)

def load_spec():
    """加载打点规范"""
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'data', 'event_spec.json')
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {'events': {}, 'fields': {}}


def analyze_events(events):
    """分析事件，返回错误/警告列表"""
    spec = load_spec()
    spec_events = spec.get('events', {})
    spec_fields = spec.get('fields', {})
    event_fields = spec.get('event_fields', {})
    cp_fields = event_fields.get('cp', {})
    issues = []

    for e in events:
        ename = e['event_name']
        # cp 及其公共参数按当前约定完全忽略。
        if ename == 'cp':
            continue

        # 事件名必须与规范完全一致，不接受别名、大小写差异或模糊匹配。
        if ename not in spec_events:
            issues.append({
                'event': ename,
                'level': 'error',
                'msg': f'事件 "{ename}" 与规范事件名不一致',
            })
            continue

        # 新规范按事件保存字段。兼容尚未重新同步的旧文件，但旧格式无法
        # 判断字段归属，也不会执行缺失字段检查。
        has_event_field_map = ename in event_fields
        allowed_fields = event_fields.get(ename, spec_fields)

        # Check properties
        props = e.get('properties', {})
        for pname, pval in props.items():
            if pname.startswith('⚙ '):
                continue
            if pname in cp_fields:
                continue
            if pname in allowed_fields:
                sf = allowed_fields[pname]
                # Type check
                etype = sf.get('type', '')
                actual_value = (
                    pval.get('value')
                    if isinstance(pval, dict) and 'value' in pval
                    else pval
                )
                normalized_type = etype.strip().upper()
                type_valid = True
                if normalized_type in ('INT', 'LONG'):
                    type_valid = (
                        isinstance(actual_value, int)
                        and not isinstance(actual_value, bool)
                    )
                elif normalized_type == 'FLOAT':
                    type_valid = (
                        isinstance(actual_value, (int, float))
                        and not isinstance(actual_value, bool)
                    )
                elif normalized_type == 'STRING':
                    type_valid = isinstance(actual_value, str)
                elif normalized_type.startswith('ARRAY_'):
                    type_valid = isinstance(actual_value, list)
                elif normalized_type == '对象组':
                    type_valid = isinstance(actual_value, (list, dict))
                if normalized_type and not type_valid:
                    issues.append({
                        'event': ename, 'field': pname,
                        'level': 'error',
                        'msg': f'"{pname}" 应为 {normalized_type}，实际为 {type(actual_value).__name__}',
                    })
            else:
                # 参数必须属于当前事件；大小写错误同样算错误。
                found_similar = False
                for sf_name in allowed_fields:
                    if pname.lower() == sf_name.lower() and pname != sf_name:
                        issues.append({
                            'event': ename, 'field': pname,
                            'level': 'error',
                            'msg': f'"{pname}" 大小写不对，规范中是 "{sf_name}"',
                        })
                        found_similar = True
                        break
                if not found_similar:
                    issues.append({
                        'event': ename, 'field': pname,
                        'level': 'error',
                        'msg': f'"{pname}" 不属于事件 "{ename}"',
                    })

        if has_event_field_map:
            for field_name in allowed_fields:
                if field_name not in props:
                    issues.append({
                        'event': ename, 'field': field_name,
                        'level': 'warn',
                        'msg': f'规范参数 "{field_name}" 未上报',
                    })

    return issues
