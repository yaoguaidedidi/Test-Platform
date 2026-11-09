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
        # TA 顶层类型：track=事件；user_set/user_setOnce/user_add/... = 用户属性上报（没有事件名）
        'ta_type': str(data.get('#type', '') or ''),
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
    'result': '玩家操作结果',
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


# 规范里的取值说明形如：
#   1：过关
#   2：二合合成物
# 只有每一行都是「数字：说明」时才当成枚举；写成「0及以上：…」这类
# 范围或自由说明的，不做取值检查。
_VALUE_ENUM_LINE = re.compile(r'^\s*(-?\d+)\s*[：:]\s*(.*)$')


def parse_value_enum(value_desc):
    if not value_desc:
        return None
    lines = [line.strip() for line in str(value_desc).splitlines() if line.strip()]
    if not lines:
        return None
    allowed = {}
    for line in lines:
        match = _VALUE_ENUM_LINE.match(line)
        if not match:
            return None
        allowed[int(match.group(1))] = match.group(2).strip()
    return allowed


def _enum_span_text(allowed):
    values = sorted(allowed)
    if values and values == list(range(values[0], values[0] + len(values))):
        return f'{values[0]}～{values[-1]}'
    return '、'.join(str(value) for value in values)


def analyze_events(events):
    """分析事件，返回错误/警告列表"""
    spec = load_spec()
    spec_events = spec.get('events', {})
    spec_fields = spec.get('fields', {})
    event_fields = spec.get('event_fields', {})
    cp_fields = event_fields.get('cp', {})
    enum_cache = {}

    def allowed_values(definition):
        desc = definition.get('value_desc') or ''
        if desc not in enum_cache:
            enum_cache[desc] = parse_value_enum(desc)
        return enum_cache[desc]

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
                elif (isinstance(actual_value, int)
                      and not isinstance(actual_value, bool)):
                    allowed = allowed_values(sf)
                    if allowed is not None and actual_value not in allowed:
                        issues.append({
                            'event': ename, 'field': pname,
                            'level': 'error',
                            'msg': (f'"{pname}" 取值 {actual_value} 不在规范允许的取值内'
                                    f'（合法值 {_enum_span_text(allowed)}）'),
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


# =============================================================
# 跨事件关联校验
# =============================================================
#
# analyze_events 只看单个事件内部（事件名/字段归属/类型/漏报），
# 无法判断"值取得对不对"。这里的关联校验覆盖三类问题：
#
#   pair     —— A 先打、B 后打，B 里带着 A 的答案，两边应该对得上。
#   window   —— 一段过程（一关/一章/两次订单之间）的流水，应该等于
#               结束时报的总数。
#   sequence —— 库存链：本次变动后的库存 = 上次库存 ± 本次数量。
#
# 规则全部是声明式的，加规则只改 RELATION_RULES，不动算法。

# 期待事件的等待窗口（秒）。多数规则用"下一次某事件"当边界，
# 这个值只是兜底上限。
RELATION_WINDOW_SECONDS = 300.0

# TA 的 #time 是 "2026-09-15 17:13:52.432" 或毫秒；缺省时回落到抓取时刻。
_CLOCK_TA = 'ta'
_CLOCK_LOG = 'log'

RELATION_RULES = (
    # ---------- 1. 过关奖励：关卡结算 vs 道具入账 ----------
    {
        'kind': 'pair',
        'id': 'lvl_end_eng_get_vs_it_get_energy',
        'label': '过关奖励体力',
        'title': 'lvl_end.eng_get = it_get[res_id=Energy,get_src=1].get_cnt',
        'trigger': {'event': 'lvl_end', 'require': {'lvl_fs': 1}},
        'expect': {'event': 'it_get', 'match': {'res_id': 'Energy', 'get_src': 1}},
        'compare': (('eng_get', 'get_cnt'),),
        'boundary': 'lvl_st',
        'missing_level': 'warn',
        'plain': (
            '过关胜利时，「lvl_end」(关卡结束) 里上报的奖励体力（eng_get）'
            '应该等于随后「it_get」(道具获得) 里「资源是体力 res_id=Energy、'
            '来源是过关 get_src=1」那一条的数量（get_cnt）。两个数对不上算错误；'
            '下一个关卡开始前还没等到那条记录，提示"可能漏报"（警告）。'
            '胜利了却没上报 eng_get、或者取到的不是数字，同样提示无法核对（警告）。'
            '过关失败、主动退出都不检查。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'lvl_end_coin_get_vs_coin_get',
        'label': '过关奖励金币',
        'title': 'lvl_end.coin_get = coin_get[get_src=1].get_cnt',
        'trigger': {'event': 'lvl_end', 'require': {'lvl_fs': 1}},
        'expect': {'event': 'coin_get', 'match': {'get_src': 1}},
        'compare': (('coin_get', 'get_cnt'),),
        'boundary': 'lvl_st',
        'missing_level': 'warn',
        'plain': (
            '过关胜利时，「lvl_end」里上报的获得金币（coin_get）应该等于随后'
            '「coin_get」里来源是过关（get_src=1）那一条的数量（get_cnt）。'
            '对不上算错误；下一关开始前没等到就算漏报（警告）。'
            '注意这里只认"来源=过关"的金币，合成物产出等其他来源不参与。'
        ),
    },
    # ---------- 2. 进出关卡口径 ----------
    {
        'kind': 'pair',
        'id': 'lvl_st_vs_lvl_end_fields',
        'label': '进出关卡口径',
        'title': 'lvl_st 与 lvl_end 的关卡身份字段应一致',
        'trigger': {'event': 'lvl_st'},
        'expect': {'event': 'lvl_end'},
        'compare': (
            ('lvl_ne', 'lvl_ne'),
            ('lvl_ty', 'lvl_ty'),
            ('st_cnt_ne', 'st_cnt_ne'),
            ('st_cnt_id', 'st_cnt_id'),
            ('pp_use', 'pp_use'),
        ),
        'boundary': 'lvl_st',
        'missing_level': None,
        'plain': (
            '同一关的开始事件（lvl_st）和结束事件（lvl_end）应该说的是同一关、同一把：'
            '关卡文件名、关卡类型、两个维度的挑战次数、前置道具这几项必须完全一样。'
            '不一致说明进关和结算对不上（比如中途换了关、或者计数没同步）。'
            '玩家中途退出导致没有结算的，不报。'
        ),
    },
    # ---------- 4. 订单 ----------
    {
        'kind': 'pair',
        'id': 'ord_do_vs_ord_cmt',
        'label': '订单身份',
        'title': 'ord_do.ord_id = ord_cmt.ord_id',
        'trigger': {'event': 'ord_do'},
        'expect': {'event': 'ord_cmt'},
        'compare': (('ord_id', 'ord_id'),),
        'boundary': 'ord_do',
        'missing_level': None,
        'plain': (
            '订单上出现 Build 按钮时（ord_do）上报的订单号，应该就是玩家最终提交时'
            '（ord_cmt）上报的订单号。对不上说明提交的不是同一个订单。'
            '点了 Build 没提交属于正常操作，不报。'
        ),
    },
    # ---------- 5. 补单 ----------
    {
        'kind': 'pair',
        'id': 'iap_end_vs_iap_cmp',
        'label': '补单一致性',
        'title': 'iap_end_t2 与 iap_cmp_t2 的订单/商品/金额应一致',
        'trigger': {'event': 'iap_end_t2', 'require': {'rslt': 1}},
        'expect': {'event': 'iap_cmp_t2'},
        'compare': (
            ('ord_id', 'ord_id'),
            ('prod_id', 'prod_id'),
            ('iap', 'iap'),
            ('pay_typ', 'pay_typ'),
        ),
        'boundary': 'iap_end_t2',
        'window_seconds': 3600.0,
        'missing_level': None,
        'plain': (
            '补单成功（iap_cmp_t2）时上报的订单号、商品 id、金额、付费类型，'
            '应该和原支付结果（iap_end_t2）完全一致。对不上说明补单补错了单或补错了金额。'
            '没有发生补单的不报。'
        ),
    },
    # ---------- 6. 特殊生成器体力 ----------
    {
        'kind': 'pair',
        'id': 'sp_gen_energy_use',
        'label': '特殊生成器体力',
        'title': 'sp_gen_*.eng_use = it_use[res_id=Energy,use_src=6].use_cnt',
        'trigger': {'event': ['sp_gen_qstn', 'sp_gen_ptr', 'sp_gen_scr',
                              'sp_gen_fruit']},
        'expect': {'event': 'it_use', 'match': {'res_id': 'Energy', 'use_src': 6}},
        'compare': (('eng_use', 'use_cnt'),),
        'allow_reverse': True,
        'window_seconds': 60.0,
        'missing_level': None,
        # 倒计时结束、根本没被使用的生成器不带 eng_use，属于正常情况
        'skip_when': {'require': {'is_t_end': 1}},
        'plain': (
            '特殊生成器结算时上报的消耗体力（eng_use），应该等于它消耗时'
            '「it_use」里来源是特殊生成器（use_src=6）那条的数量（use_cnt）。'
            '注意顺序是"先扣体力、后结算"，和过关那条相反。'
            '倒计时结束没被使用的生成器不带 eng_use，不参与比对。'
        ),
    },
    # ---------- 7/8. 章节总账 ----------
    {
        'kind': 'window',
        'id': 'chp_end_lvl_eng_sum',
        'label': '章节打关体力总账',
        'title': 'chp_end.chp_lvl_eng = 本章胜利关卡 eng_get 之和',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'sum', 'event': 'lvl_end', 'require': {'lvl_fs': 1},
             'field': 'eng_get', 'target': 'chp_lvl_eng',
             'label': '本章打关获得体力'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束（chp_end）时报的"本章节打关获得体力"（chp_lvl_eng），'
            '应该等于本章所有胜利关卡的 eng_get 之和。对不上说明章节统计漏算或重复算了。'
            '只有从章节开始（chp_st）就一直录着才校验，中间日志断了会跳过。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_eng_use_sum',
        'label': '章节体力消耗总账',
        'title': 'chp_end.chp_eng_use = 本章 it_use[Energy] 之和',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'sum', 'event': 'it_use', 'match': {'res_id': 'Energy'},
             'field': 'use_cnt', 'target': 'chp_eng_use',
             'label': '本章消耗体力'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"本章总消耗体力"（chp_eng_use），应该等于本章所有'
            '体力（Energy）消耗（it_use.use_cnt）之和。对不上说明章节统计漏算或重复算了。'
            '只有从章节开始（chp_st）就一直录着才校验，中间日志断了会跳过。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_tp_cnt_delta',
        'label': '章节点击生成器次数',
        'title': 'chp_end.chp_tp_cnt = 章节结束 tp_cnt - 章节开始 tp_cnt',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'delta', 'field': 'tp_cnt', 'target': 'chp_tp_cnt',
             'label': '本章点击生成器次数'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"本章点击生成器次数"（chp_tp_cnt），应该等于'
            '会话累计的点击生成器次数（tp_cnt）从章节开始到章节结束的增长量。'
            '对不上说明章节计数漏加、重复加，或者中途重置了。'
            '开始或结束时没带 tp_cnt、以及中间日志断了的，不校验。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_mg_cnt_delta',
        'label': '章节合成次数',
        'title': 'chp_end.chp_mg_cnt = 章节结束 mg_cnt - 章节开始 mg_cnt',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'delta', 'field': 'mg_cnt', 'target': 'chp_mg_cnt',
             'label': '本章合成次数'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"本章合成次数"（chp_mg_cnt），应该等于'
            '会话累计的合成次数（mg_cnt）从章节开始到章节结束的增长量。'
            '对不上说明章节计数漏加、重复加，或者中途重置了。'
            '开始或结束时没带 mg_cnt、以及中间日志断了的，不校验。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_rem_eng_sum',
        'label': '章节剩余合成物折算体力',
        'title': 'chp_end.chp_rem_eng = 本章 it_get[Energy,get_src=6] 之和',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'sum', 'event': 'it_get',
             'require': {'res_id': 'Energy', 'get_src': 6},
             'field': 'get_cnt', 'target': 'chp_rem_eng',
             'label': '本章剩余合成物折算体力'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"本章棋盘剩余合成物折算体力"（chp_rem_eng），'
            '应该等于本章里来源是章节转化（get_src=6）的体力入账之和。'
            '对不上说明棋盘结算折算的体力与实际发放的体力不一致。'
            '只有从章节开始（chp_st）就一直录着才校验，中间日志断了会跳过。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_gen_use_count',
        'label': '章节用掉生成器个数',
        'title': 'chp_end.chp_gen_use = 普通生成器用完数 + 特殊生成器用完数',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'components', 'target': 'chp_gen_use',
             'label': '本章用掉的生成器个数', 'skip_if_empty': True,
             'components': (
                 {'op': 'count', 'event': 'gen_tap', 'require': {'gen_num': 1},
                  'label': '普通生成器用完'},
                 {'op': 'end_field', 'field': 'chp_sp_gen_use',
                  'label': '特殊生成器用完'},
             )},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"本章用掉的生成器个数"（chp_gen_use），包含普通生成器'
            '和特殊生成器。普通生成器按 gen_tap 中只剩最后一个元素（gen_num=1）'
            '的次数计算；特殊生成器直接采用同一条 chp_end 里的 chp_sp_gen_use。'
            '两项相加应等于 chp_gen_use。没有 gen_tap 明细时这条不校验，避免误报。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_sp_gen_use_count',
        'label': '章节消耗特殊生成器个数',
        'title': 'chp_end.chp_sp_gen_use = 本章消耗完的特殊生成器个数',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'distinct',
             'event': ['sp_gen_qstn', 'sp_gen_ptr', 'sp_gen_scr',
                       'sp_gen_pnta', 'sp_gen_fruit'],
             'field': 'gen_idx', 'consumed': True,
             'target': 'chp_sp_gen_use',
             'label': '本章消耗完的特殊生成器个数'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"本章使用特殊生成器个数"（chp_sp_gen_use），应该等于'
            '本章里真正被用完的特殊生成器个数：同一个生成器按编号只算一次；'
            '倒计时结束、没被用掉的不算；还有剩余次数（lft_st 不为 0）的也不算。'
        ),
    },
    {
        'kind': 'window',
        'id': 'chp_end_st_eng_vs_start_eng',
        'label': '章节带入体力',
        'title': 'chp_end.chp_st_eng = 上一章结束时的 eng',
        'start': {'event': 'chp_st'},
        'end': {'event': 'chp_end'},
        'aggregates': (
            {'op': 'previous_end', 'field': 'eng', 'target': 'chp_st_eng',
             'label': '章节带入体力', 'initial_value': 0,
             'initial_require': {'chp_id': 'Chapter_1'}},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '章节结束时报的"章节带入体力"（chp_st_eng），应该等于上一章结算后'
            '剩下的体力，也就是上一章 chp_end 上的 eng。第一章前面没有上一章，'
            '固定按 0 校验。日志从中间章节开始时、或者中间断档时，不校验。'
        ),
    },
    # ---------- 11. 本关金币消耗 ----------
    {
        'kind': 'window',
        'id': 'lvl_end_coin_use_sum',
        'label': '本关金币消耗',
        'title': 'lvl_end.coin_use = 本关 coin_use[use_src=1/3] 之和',
        'start': {'event': 'lvl_st'},
        'end': {'event': 'lvl_end'},
        'aggregates': (
            {'op': 'sum', 'event': 'coin_use', 'match': {'use_src': [1, 3]},
             'field': 'use_cnt', 'target': 'coin_use',
             'label': '本关金币消耗（步数+关内道具）'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '关卡结束时上报的"金币消耗（步数+关内道具）"（coin_use），'
            '应该等于这一关里所有续步（use_src=1）和关内道具购买（use_src=3）'
            '消耗金币的金额之和。对不上说明结算少算或多算了金币。'
            '只有从进关（lvl_st）就开始录着才校验。'
        ),
    },
    # ---------- 12. 续步次数 ----------
    {
        'kind': 'window',
        'id': 'lvl_end_ego_cnt',
        'label': '续步次数',
        'title': 'lvl_end.ego_cnt = 本关 coin_use[use_src=1] 的条数',
        'start': {'event': 'lvl_st'},
        'end': {'event': 'lvl_end'},
        'aggregates': (
            {'op': 'count', 'event': 'coin_use', 'match': {'use_src': 1},
             'target': 'ego_cnt', 'label': '续步次数'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '关卡结束时上报的续步次数（ego_cnt），应该等于这一关里'
            '"续步"消耗金币（use_src=1）发生的次数。对不上说明次数统计错了。'
        ),
    },
    # ---------- 13. 关内道具 ----------
    {
        'kind': 'window',
        'id': 'lvl_end_item_use_sum',
        'label': '关内道具使用数',
        'title': 'lvl_end 的 it1~it4_use 之和 = 本关 it_use[use_src=7] 之和',
        'start': {'event': 'lvl_st'},
        'end': {'event': 'lvl_end'},
        'aggregates': (
            {'op': 'sum', 'event': 'it_use', 'match': {'use_src': 7},
             'field': 'use_cnt', 'target': ['it1_use', 'it2_use', 'it3_use', 'it4_use'],
             'label': '关内道具使用数'},
        ),
        'max_gap_seconds': 300.0,
        'plain': (
            '关卡结束时报的四个关内道具使用数（it1_use~it4_use）加起来，'
            '应该等于这一关里"关内道具使用"（use_src=7）产生的数量之和。'
            '说人话：结算说用了几个道具，流水里就得扣掉几个。'
        ),
    },
    # ---------- 14. 订单区间统计 ----------
    {
        'kind': 'window',
        'id': 'ord_cmt_counters',
        'label': '订单区间统计',
        'title': 'ord_cmt 的区间体力消耗/通关数 = 距上次订单的明细累加',
        'start': {'previous_event': 'ord_cmt'},
        'end': {'event': 'ord_cmt'},
        'aggregates': (
            {'op': 'sum', 'event': 'it_use',
             'match': {'res_id': 'Energy', 'use_src': [5, 6]},
             'field': 'use_cnt', 'target': 'eng_use',
             'label': '距上次订单的二合体力消耗'},
            {'op': 'count', 'event': 'lvl_end', 'require': {'lvl_fs': 1},
             'target': 'lvl_cnt',
             'label': '距上次订单的通关数'},
        ),
        'max_gap_seconds': 1800.0,
        'plain': (
            '提交订单（ord_cmt）时会报"距上次提交订单以来"的体力消耗和通关数，'
            '这两个数应该等于这段区间里实际发生的明细累加：体力消耗 = 普通/特殊生成器'
            '扣的体力之和，通关数 = 这段区间里胜利关卡的条数。'
            '第一个订单没有"上一次"作参照，不校验；区间内日志断了会跳过。'
        ),
    },
    # ---------- 15. 活动通关数 ----------
    {
        'kind': 'window',
        'id': 'evt_pc_lvl_cnt',
        'label': '活动通关数',
        'title': 'evt_pc_rd.evt_lvl_cnt = 活动开启以来胜利关卡数',
        'start': {'event': 'evt_pc_st'},
        'end': {'event': 'evt_pc_rd'},
        'cumulative': True,
        'aggregates': (
            {'op': 'count', 'event': 'lvl_end', 'require': {'lvl_fs': 1},
             'target': 'evt_lvl_cnt', 'label': '活动开启以来通过关卡数'},
        ),
        'max_gap_seconds': 1800.0,
        'plain': (
            '活动阶段奖励（evt_pc_rd）上报的"活动开启到目前为止通过的关卡数"'
            '（evt_lvl_cnt），应该等于活动开启（evt_pc_st）以来胜利关卡的条数。'
            '这个数是累计值，所以每阶段领奖时都会往上加。'
            '活动开启事件没录到的（比如中途才开始抓），不校验。'
        ),
    },
    # ---------- 16/17. 库存链 ----------
    {
        'kind': 'sequence',
        'id': 'coin_balance_chain',
        'label': '金币库存链',
        'title': 'coin_get/coin_use.fn_cnt 应与上一次库存连续',
        'events': (
            {'event': 'coin_get', 'delta': 'get_cnt', 'sign': 1},
            {'event': 'coin_use', 'delta': 'use_cnt', 'sign': -1},
        ),
        'field': 'fn_cnt',
        'max_gap_seconds': 900.0,
        'plain': (
            '每次金币变动后上报的库存（fn_cnt），应该等于上一次库存加上（获得）'
            '或减去（消耗）本次数量。对不上说明中间漏报了一条、重复报了一条，'
            '或者数量算错了。'
        ),
    },
    {
        'kind': 'sequence',
        'id': 'resource_balance_chain',
        'label': '资源库存链',
        'title': 'it_get/it_use.fn_cnt 应与上一次库存连续（按资源分开）',
        'events': (
            {'event': 'it_get', 'delta': 'get_cnt', 'sign': 1},
            {'event': 'it_use', 'delta': 'use_cnt', 'sign': -1},
        ),
        'field': 'fn_cnt',
        'group_by': 'res_id',
        'exclude_res_ids': ('Lives',),
        'max_gap_seconds': 900.0,
        'plain': (
            '每种资源（体力、各类道具）每次变动后的库存（fn_cnt），应该等于上一次'
            '库存加减本次数量。不同资源分开算。'
            '三消体力（Lives）会随时间自然恢复，本来就对不上，所以不校验；'
            '切换账号、日志断档时会重新起算，不报错。'
        ),
    },
    # ---------- 18. 新手漏斗 B 组：引导开始/结束成对 ----------
    {
        'kind': 'pair',
        'id': 'tt_b_go_merge_pair',
        'label': 'B组二合按钮引导',
        'title': 'tt_b_04_go_merge_start → tt_b_06_go_merge_end',
        'trigger': {'event': 'tt_b_04_go_merge_start'},
        'expect': {'event': 'tt_b_06_go_merge_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组二合按钮引导开始（tt_b_04_go_merge_start）之后，应该出现对应的结束事件'
            '（tt_b_06_go_merge_end）。只看到开始、300 秒内没有结束，提示可能漏报；'
            '只看到结束、没有开始，不报，避免中途才开始抓日志时误报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_merge_task_pair',
        'label': 'B组订单介绍引导',
        'title': 'tt_b_07_merge_task_start → tt_b_08_merge_task_end',
        'trigger': {'event': 'tt_b_07_merge_task_start'},
        'expect': {'event': 'tt_b_08_merge_task_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组订单介绍引导开始（tt_b_07_merge_task_start）之后，应该出现对应的结束事件'
            '（tt_b_08_merge_task_end）。只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_merge_generator_01_pair',
        'label': 'B组生成器点击引导',
        'title': 'tt_b_09_merge_generator_01_start → tt_b_10_merge_generator_01_end',
        'trigger': {'event': 'tt_b_09_merge_generator_01_start'},
        'expect': {'event': 'tt_b_10_merge_generator_01_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组第一次生成器点击引导开始（tt_b_09_merge_generator_01_start）之后，'
            '应该出现对应的结束事件（tt_b_10_merge_generator_01_end）。'
            '只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_merge_gameplay_01_pair',
        'label': 'B组合成1引导',
        'title': 'tt_b_11_merge_gameplay_01_start → tt_b_13_merge_gameplay_01_end',
        'trigger': {'event': 'tt_b_11_merge_gameplay_01_start'},
        'expect': {'event': 'tt_b_13_merge_gameplay_01_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组合成1引导开始（tt_b_11_merge_gameplay_01_start）之后，应该出现对应的'
            '结束事件（tt_b_13_merge_gameplay_01_end）。中间的合成结果事件可以重复上报；'
            '只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_merge_gameplay_03_pair',
        'label': 'B组订单提交引导',
        'title': 'tt_b_14_merge_gameplay_03_start → tt_b_16_merge_gameplay_03_end',
        'trigger': {'event': 'tt_b_14_merge_gameplay_03_start'},
        'expect': {'event': 'tt_b_16_merge_gameplay_03_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组订单提交引导开始（tt_b_14_merge_gameplay_03_start）之后，应该出现对应的'
            '结束事件（tt_b_16_merge_gameplay_03_end）。中间的点击结果事件可以重复上报；'
            '只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_not_enough_energy_pair',
        'label': 'B组体力不足引导',
        'title': 'tt_b_17_not_enough_energy_start → tt_b_18_not_enough_energy_end',
        'trigger': {'event': 'tt_b_17_not_enough_energy_start'},
        'expect': {'event': 'tt_b_18_not_enough_energy_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组体力不足引导不一定触发；一旦出现开始事件（tt_b_17_not_enough_energy_start），'
            '关闭对话时应该出现结束事件（tt_b_18_not_enough_energy_end）。'
            '只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_level_button_pair',
        'label': 'B组LEVEL按钮引导',
        'title': 'tt_b_20_level_button_start → tt_b_21_level_button_end',
        'trigger': {'event': 'tt_b_20_level_button_start'},
        'expect': {'event': 'tt_b_21_level_button_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组LEVEL按钮引导不一定触发；一旦出现开始事件（tt_b_20_level_button_start），'
            '应该出现对应的结束事件（tt_b_21_level_button_end）。'
            '只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
    {
        'kind': 'pair',
        'id': 'tt_b_sp_gen_pair',
        'label': 'B组特殊生成器引导',
        'title': 'tt_b_22_sp_gen_start → tt_b_24_sp_gen_end',
        'trigger': {'event': 'tt_b_22_sp_gen_start'},
        'expect': {'event': 'tt_b_24_sp_gen_end'},
        'compare': (),
        'missing_level': 'warn',
        'plain': (
            'B组特殊生成器引导开始（tt_b_22_sp_gen_start）之后，应该出现对应的结束事件'
            '（tt_b_24_sp_gen_end）。中间的点击结果事件可以重复上报；'
            '只看到开始、300 秒内没有结束，提示可能漏报。'
        ),
    },
)


# ---------------------------------------------------------------
# 取值与匹配工具
# ---------------------------------------------------------------

def _prop_value(props, name):
    """取字段原始值。_parse_event 会给带中文说明的字段套一层 {'value','desc'}。"""
    value = props.get(name)
    if isinstance(value, dict) and 'value' in value:
        return value.get('value')
    return value


def _as_int(value):
    """宽松转 int：字符串数字也认，布尔和转换失败返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def _cmp_value(value):
    """比对用的归一化：数字统一成 int，字符串去空格；无效值返回 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return ('n', int(value))
    if isinstance(value, str):
        text = value.strip()
        try:
            return ('n', int(text))
        except ValueError:
            return ('s', text) if text else None
    return None


def _event_clock(event):
    """事件时刻 -> (来源, 秒)。两个来源的数值不可直接相减，必须同源。"""
    raw = event.get('event_time')
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        if raw > 0:
            return _CLOCK_TA, float(raw) / 1000.0
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            ms = int(text)
            if ms > 0:
                return _CLOCK_TA, ms / 1000.0
        except ValueError:
            pass
        for fmt in ('%Y-%m-%d %H:%M:%S.%f', '%Y-%m-%d %H:%M:%S'):
            try:
                from datetime import datetime
                return _CLOCK_TA, datetime.strptime(text, fmt).timestamp()
            except ValueError:
                continue

    ts = event.get('ts') or ''
    parts = ts.split(':')
    if len(parts) == 3:
        try:
            hours, minutes, seconds = (int(p) for p in parts)
        except ValueError:
            return None, None
        return _CLOCK_LOG, hours * 3600 + minutes * 60 + seconds
    return None, None


def _clock_gap(earlier, later):
    """两个事件的时间差（秒）；来源不同或缺失时返回 None。"""
    source_a, seconds_a = _event_clock(earlier)
    source_b, seconds_b = _event_clock(later)
    if source_a is None or source_a != source_b:
        return None
    return seconds_b - seconds_a


def _matches(event, spec):
    """事件是否满足 spec：{event: str|list, require: {...}, match: {...}}。"""
    if not spec:
        return False
    names = spec.get('event')
    if names is not None:
        if isinstance(names, str):
            names = (names,)
        if (event.get('event_name') or '') not in names:
            return False
    props = event.get('properties') or {}
    for key, want in (spec.get('require') or {}).items():
        got = _cmp_value(_prop_value(props, key))
        if got is None:
            return False
        wants = want if isinstance(want, (list, tuple, set)) else (want,)
        if got not in {_cmp_value(item) for item in wants}:
            return False
    for key, want in (spec.get('match') or {}).items():
        got = _cmp_value(_prop_value(props, key))
        if got is None:
            return False
        wants = want if isinstance(want, (list, tuple, set)) else (want,)
        if got not in {_cmp_value(item) for item in wants}:
            return False
    return True


def _context(trigger_event, expect_event):
    """把两边的定位信息拼成可复核的上下文。"""
    trigger_props = trigger_event.get('properties') or {}
    expect_props = (expect_event or {}).get('properties') or {}
    return {
        'trigger': {
            'event': trigger_event.get('event_name') or '',
            'cursor': trigger_event.get('cursor'),
            'lvl_ne': _prop_value(trigger_props, 'lvl_ne'),
            'lvl_id': _prop_value(trigger_props, 'lvl_id'),
            'lvl_id_end': _prop_value(trigger_props, 'end_lvl_id'),
        },
        'expect': {
            'event': (expect_event or {}).get('event_name') or '',
            'cursor': (expect_event or {}).get('cursor'),
            'lvl_ne': _prop_value(expect_props, 'lvl_ne'),
            'lvl_id': _prop_value(expect_props, 'lvl_id'),
            'get_src': _prop_value(expect_props, 'get_src'),
        },
    }


def _issue(rule, event, field, level, msg, expect_event=None):
    return {
        'event': event.get('event_name') or '',
        'field': field or '',
        'level': level,
        'msg': msg,
        'relation_id': rule['id'],
        'context': _context(event, expect_event),
    }


def _location(event):
    """消息里用的定位串：关卡号 + 关卡文件名 + 发生时间。"""
    props = event.get('properties') or {}
    parts = []
    for key in ('end_lvl_id', 'lvl_ne', 'lvl_id'):
        value = _prop_value(props, key)
        if value not in (None, ''):
            parts.append(f'{key}={value}')
    raw = event.get('event_time')
    if isinstance(raw, str) and len(raw) >= 19:
        parts.append(f'time={raw[11:19]}')
    elif event.get('ts'):
        parts.append(f'time={event["ts"]}')
    return ' '.join(parts) if parts else f'cursor={event.get("cursor")}'


# ---------------------------------------------------------------
# pair：A → B，逐字段比对
# ---------------------------------------------------------------

def _pair_source_problem(rule, trigger_event):
    """触发事件里的比对字段缺失或取不到值 —— 返回提示，正常则返回 None。"""
    props = trigger_event.get('properties') or {}
    problems = []
    for trigger_field, _ in rule['compare']:
        if trigger_field not in props:
            problems.append(f'没有上报 {trigger_field}')
        elif _cmp_value(_prop_value(props, trigger_field)) is None:
            problems.append(f'{trigger_field}={_prop_value(props, trigger_field)!r} 不是有效值')
    if not problems:
        return None
    return _issue(
        rule, trigger_event, rule['compare'][0][0], 'warn',
        f'{rule["label"]}：{"，".join(problems)}，无法核对｜{_location(trigger_event)}',
    )


def _pair_compare(rule, trigger_event, expect_event):
    """逐字段比对一条已配对的关联，不一致才产出 issue。"""
    diffs = []
    for trigger_field, expect_field in rule['compare']:
        got = _cmp_value(_prop_value(trigger_event.get('properties') or {}, trigger_field))
        want = _cmp_value(_prop_value(expect_event.get('properties') or {}, expect_field))
        if got is None or want is None:
            return [_issue(
                rule, trigger_event, trigger_field, 'warn',
                f'{rule["label"]}：{trigger_field}={got} 或 {expect_field}={want}，无法比对'
                f'｜{_location(trigger_event)}',
                expect_event,
            )]
        if got != want:
            diffs.append(f'{trigger_field}={got[1]}，但 {expect_field}={want[1]}')
    if not diffs:
        return []
    return [_issue(
        rule, trigger_event, rule['compare'][0][0], 'error',
        f'{rule["label"]}对不上：{"；".join(diffs)}｜{_location(trigger_event)}',
        expect_event,
    )]


def _pair_missing(rule, trigger_event, reason):
    level = rule.get('missing_level')
    if not level:
        return None
    expect = rule['expect']
    name = expect.get('event')
    if isinstance(name, (list, tuple)):
        name = '/'.join(name)
    condition = '、'.join(f'{k}={v}' for k, v in (expect.get('match') or {}).items())
    detail = f'（{condition}）' if condition else ''
    return _issue(
        rule, trigger_event,
        rule['compare'][0][0] if rule.get('compare') else 'event', level,
        f'{rule["label"]}未等到对应的 {name}{detail}｜{reason}｜{_location(trigger_event)}',
    )


def _analyze_pair(rule, events, default_window):
    issues = []
    checked = 0
    window = float(rule.get('window_seconds', default_window))
    reverse = bool(rule.get('allow_reverse'))
    boundary = rule.get('boundary')
    skip_when = rule.get('skip_when')
    pending = []
    buffered = []

    for event in events:
        name = event.get('event_name') or ''

        if reverse:
            if _matches(event, rule['expect']):
                buffered.append(event)
            if _matches(event, rule['trigger']):
                if skip_when and _matches(event, skip_when):
                    continue
                checked += 1
                chosen = None
                for candidate in list(buffered):
                    gap = _clock_gap(candidate, event)
                    if gap is None or gap <= window:
                        chosen = candidate
                        buffered.remove(candidate)
                        break
                if chosen is not None:
                    issues.extend(_pair_compare(rule, event, chosen))
                else:
                    problem = _pair_source_problem(rule, event)
                    if problem:
                        issues.append(problem)
                    else:
                        missing = _pair_missing(rule, event, '没有对应的消耗记录')
                        if missing:
                            issues.append(missing)
            continue

        # 边界事件：取消还没配上的期待
        if boundary and name == boundary and pending:
            for trig in pending:
                missing = _pair_missing(rule, trig, '已到下一次关卡开始')
                if missing:
                    issues.append(missing)
            pending = []

        # 先关期待，再判断是否是新触发（同一事件不会两者都是）
        if pending and _matches(event, rule['expect']):
            trig = pending.pop(0)
            issues.extend(_pair_compare(rule, trig, event))
            continue

        if _matches(event, rule['trigger']):
            if skip_when and _matches(event, skip_when):
                continue
            checked += 1
            problem = _pair_source_problem(rule, event)
            if problem:
                issues.append(problem)
            else:
                pending.append(event)

    # 扫描结束：只有确实过了窗口还没等到，才算漏报（抓取刚结束的不误报）
    if pending:
        latest = events[-1]
        for trig in pending:
            gap = _clock_gap(trig, latest)
            if gap is None or window <= gap <= 3600.0:
                missing = _pair_missing(rule, trig, f'已过 {gap:.0f}s 仍未出现' if gap else '已到日志末尾')
                if missing:
                    issues.append(missing)
    return issues, checked


# ---------------------------------------------------------------
# window：一段过程的明细累加 vs 结束时的总数
# ---------------------------------------------------------------

def _aggregate_has_source(agg, segment):
    """窗口里是否有可用于计算的明细事件。"""
    if agg.get('op') == 'components':
        return any(
            _aggregate_has_source(component, segment)
            for component in agg.get('components', ())
        )
    if agg.get('op') == 'end_field':
        return False
    return any(_matches(event, agg) for event in segment)


def _aggregate_with_detail(agg, segment, end_event):
    op = agg.get('op', 'sum')
    if op == 'components':
        total = 0
        parts = []
        for component in agg.get('components', ()):
            value, _detail = _aggregate_with_detail(component, segment, end_event)
            if value is None:
                return None, ''
            total += value
            parts.append(f'{component.get("label", "分项")} {value}')
        return total, ' + '.join(parts)
    if op == 'end_field':
        value = _as_int(_prop_value(
            (end_event or {}).get('properties') or {}, agg['field']))
        return value, ''

    total = 0
    seen = set()
    for event in segment:
        if not _matches(event, agg):
            continue
        if agg.get('consumed') and not _is_consumed_generator(event):
            continue
        if op == 'distinct':
            value = _prop_value(event.get('properties') or {}, agg['field'])
            if value is not None:
                seen.add(str(value))
            continue
        if op == 'count':
            total += 1
            continue
        value = _as_int(_prop_value(event.get('properties') or {}, agg['field']))
        if value is not None:
            total += value
    return (len(seen) if op == 'distinct' else total), ''


def _aggregate(agg, segment, end_event=None):
    value, _detail = _aggregate_with_detail(agg, segment, end_event)
    return value


def _is_consumed_generator(event):
    """特殊生成器是否被真正用完：倒计时结束、还有剩余次数的都不算。"""
    props = event.get('properties') or {}
    if _as_int(_prop_value(props, 'is_t_end')) == 1:
        return False
    remaining = _as_int(_prop_value(props, 'lft_st'))
    if remaining is not None and remaining != 0:
        return False
    return any(_prop_value(props, field) is not None
               for field in ('eng_use', 'rd', 'fs'))


def _target_value(event, target):
    props = event.get('properties') or {}
    names = target if isinstance(target, (list, tuple)) else (target,)
    total = 0
    for name in names:
        value = _as_int(_prop_value(props, name))
        if value is None:
            return None
        total += value
    return total


def _window_gap_ok(segment, start_event, end_event, max_gap):
    """窗口内是否有过大的空档（抓取中断）——有就跳过，避免把断档当成漏算。"""
    chain = [start_event] + list(segment) + [end_event]
    for earlier, later in zip(chain, chain[1:]):
        gap = _clock_gap(earlier, later)
        if gap is not None and gap > max_gap:
            return False
    return True


def _analyze_window(rule, events):
    issues = []
    checked = 0
    max_gap = float(rule.get('max_gap_seconds', 300.0))
    end_spec = rule['end']
    start_spec = rule.get('start') or {}
    previous_mode = bool(start_spec.get('previous_event'))
    cumulative = bool(rule.get('cumulative'))

    window_start = None
    segment = []
    previous_end = None

    for event in events:
        if _matches(event, end_spec):
            if (previous_end is not None
                    and previous_end.get('account_id') != event.get('account_id')):
                previous_end = None
            if window_start is not None and _window_gap_ok(segment, window_start, event, max_gap):
                found, count = _window_compare(
                    rule, window_start, event, segment, previous_end)
                issues.extend(found)
                checked += count
            if previous_mode or not cumulative:
                # "上一次同类事件"模式：本次结束事件同时开启下一段
                window_start = event
                segment = []
            previous_end = event
            continue

        if not previous_mode and _matches(event, start_spec):
            window_start = event
            segment = []
            continue

        if window_start is not None:
            segment.append(event)

    return issues, checked


def _window_compare(rule, start_event, end_event, segment, previous_end=None):
    issues = []
    checked = 0
    for agg in rule.get('aggregates', ()):
        if agg.get('op') == 'delta':
            start_value = _as_int(_prop_value(
                start_event.get('properties') or {},
                agg.get('start_field') or agg['field']))
            end_value = _as_int(_prop_value(
                end_event.get('properties') or {}, agg['field']))
            reported = _target_value(end_event, agg['target'])
            if start_value is None or end_value is None or reported is None:
                continue
            checked += 1
            total = end_value - start_value
            detail = (f'按会话累计值算 {total}'
                      f'（{agg["field"]} 从 {start_value} 到 {end_value}）')
        elif agg.get('op') == 'start':
            start_value = _as_int(_prop_value(
                start_event.get('properties') or {}, agg['field']))
            reported = _target_value(end_event, agg['target'])
            if start_value is None or reported is None:
                continue
            checked += 1
            total = start_value
            detail = f'按章节开始时 {agg["field"]}={start_value}'
        elif agg.get('op') == 'previous_end':
            reported = _target_value(end_event, agg['target'])
            if previous_end is not None:
                total = _as_int(_prop_value(
                    previous_end.get('properties') or {}, agg['field']))
                if total is None:
                    continue
                detail = f'上一章结束时 {agg["field"]}={total}'
            else:
                initial_require = agg.get('initial_require')
                if initial_require and not _matches(
                        start_event, {'require': initial_require}):
                    continue
                total = _as_int(agg.get('initial_value', 0))
                if total is None:
                    continue
                detail = f'第一章带入 {agg["field"]}={total}'
            if reported is None:
                continue
            checked += 1
        else:
            if agg.get('skip_if_empty') and not _aggregate_has_source(agg, segment):
                continue
            total, detail_parts = _aggregate_with_detail(agg, segment, end_event)
            reported = _target_value(end_event, agg['target'])
            if total is None or reported is None:
                continue
            checked += 1
            detail = f'明细累加 {total}'
            if detail_parts:
                detail += f'（{detail_parts}）'
        if total == reported:
            continue
        target_name = agg['target'] if isinstance(agg['target'], str) else '+'.join(agg['target'])
        level = 'error'
        issues.append(_issue(
            rule, end_event, target_name, level,
            f'{rule["label"]}对不上：{agg.get("label", target_name)}（{target_name}）上报 {reported}，'
            f'{detail}｜{_location(end_event)}',
            start_event,
        ))
    return issues, checked


# ---------------------------------------------------------------
# sequence：库存链
# ---------------------------------------------------------------

def _analyze_sequence(rule, events):
    issues = []
    checked = 0
    max_gap = float(rule.get('max_gap_seconds', 900.0))
    group_by = rule.get('group_by')
    excluded = set(rule.get('exclude_res_ids') or ())
    field = rule['field']
    state = {}

    for event in events:
        spec = None
        for candidate in rule['events']:
            if _matches(event, {'event': candidate['event']}):
                spec = candidate
                break
        if spec is None:
            continue

        props = event.get('properties') or {}
        group = None
        if group_by:
            group = _prop_value(props, group_by)
            if group in excluded:
                continue
        key = (event.get('account_id'), group)

        current = _as_int(_prop_value(props, field))
        delta = _as_int(_prop_value(props, spec['delta']))
        if current is None or delta is None:
            continue
        signed = delta * int(spec.get('sign', 1))

        previous = state.get(key)
        if previous is not None:
            previous_value, previous_event = previous
            gap = _clock_gap(previous_event, event)
            continuous = gap is None or gap <= max_gap
            if continuous:
                checked += 1
            if continuous and previous_value + signed != current:
                issues.append(_issue(
                    rule, event, field, 'error',
                    f'{rule["label"]}断了：上次 {field}={previous_value}，'
                    f'本次{spec.get("delta")}={delta}，按说应该是 '
                    f'{previous_value + signed}，实际报 {current}'
                    f'｜资源 {group or "-"}｜{_location(event)}',
                    previous_event,
                ))
        state[key] = (current, event)
    return issues, checked


# ---------------------------------------------------------------
# 统一入口
# ---------------------------------------------------------------

def analyze_relations(events, window_seconds=RELATION_WINDOW_SECONDS, stats=None):
    """跨事件关联校验。

    输入是任意顺序的事件列表（内部按 cursor 排成发生顺序），输出与
    analyze_events 同形的 issue 列表，便于前端用同一套渲染。
    纯函数、幂等：同一批事件重复调用结果一致。

    """
    if not events:
        return []

    ordered = sorted(events, key=lambda event: event.get('cursor', 0))
    issues = []
    for rule in RELATION_RULES:
        kind = rule.get('kind', 'pair')
        if kind == 'pair':
            found, checked = _analyze_pair(rule, ordered, window_seconds)
        elif kind == 'window':
            found, checked = _analyze_window(rule, ordered)
        elif kind == 'sequence':
            found, checked = _analyze_sequence(rule, ordered)
        else:
            continue
        if stats is not None:
            stats[rule['id']] = {'checked': checked, 'issues': len(found)}
        issues.extend(found)
    return issues


# ---------------------------------------------------------------
# 校验规则说明（界面「校验规则」面板用）
# ---------------------------------------------------------------

# 单事件校验规则（说人话版）。改 analyze_events 或前端 labEvaluate 时同步更新；
# 关联校验的文字由 RELATION_RULES 里的 plain 字段提供，加规则不用动这里。
#
# 按需求，面板只列现在生效的校验；以下两类内容不展示（需要时口头同步）：
#   - 明确跳过的：cp 事件整条跳过；cp 页签定义的公共参数出现在哪条事件里
#     都跳过、只统计个数；user_set 这类账号属性上报不算事件、不参与校验。
#   - 现在没覆盖的：写成「0 及以上」这类范围的取值说明不判断；值域/区间/
#     字段联动这类业务规则还没有。

SINGLE_EVENT_RULES = (
    {
        'title': '事件名要一模一样',
        'level': 'error',
        'plain': (
            '上报的事件名必须和《打点规范》里的完全一致，包括大小写。'
            '名字对不上直接算错误，而且这条事件的其它检查都不再做了——'
            '省得报一堆没用的错。'
        ),
    },
    {
        'title': '不能多打不属于这个事件的参数',
        'level': 'error',
        'plain': (
            '上报的字段必须是这个事件规范里规定有的。不属于这个事件的参数算错误；'
            '如果只是大小写和规范不一样，会单独提示"大小写不对"。'
        ),
    },
    {
        'title': '参数类型要对',
        'level': 'error',
        'plain': (
            '规范写 INT / LONG 的要是整数，FLOAT 要是数字，STRING 要是文本，'
            'ARRAY_ 开头和"对象组"要能装多个值。类型不符合算错误'
            '（判断用的是上报的值本身，不是开发写的类型声明）。'
        ),
    },
    {
        'title': '取值要在规范允许的范围内',
        'level': 'error',
        'plain': (
            '有些字段在规范里逐个写死了能填哪几个值（比如资源来源、关卡结果、'
            '开关状态）。上报的值不在这个清单里就算错误——通常代表代码漏传、'
            '传了默认的 0，或者新加的来源没登记到打点表。'
            '只检查能一行一条列出取值的字段；写成"0 及以上"这种范围的字段不参与。'
        ),
    },
    {
        'title': '规范里规定的参数不能漏',
        'level': 'warn',
        'plain': (
            '规范里有、但这条事件报文里没找到的字段，提示"规范参数未上报"。'
            '算警告不算错误——因为有些参数本来就要看条件才带。'
        ),
    },
)

SPEC_SELF_CHECK_RULES = (
    {
        'title': '同名字段在不同事件里类型要对得上',
        'level': 'error',
        'plain': (
            '同一个字段名在两条事件的规范里写了不同类型（比如一处 INT、一处 STRING），'
            '会在"问题总览"里报成规范错误。这是规范本身写错了，需要先找策划确认改规范。'
        ),
    },
)


def describe_rules():
    """整理当前生效的全部校验规则，供界面「校验规则」面板展示。"""
    spec = load_spec()
    fields = spec.get('fields', {}) or {}

    relations = []
    for rule in RELATION_RULES:
        relations.append({
            'id': rule['id'],
            'title': rule.get('title') or rule.get('label') or rule['id'],
            'label': rule.get('label') or '',
            'kind': rule.get('kind', 'pair'),
            'level': 'error',
            'plain': rule.get('plain', ''),
        })

    return {
        'window_seconds': int(RELATION_WINDOW_SECONDS),
        'spec_event_count': len(spec.get('events', {}) or {}),
        'spec_field_count': len(fields),
        'value_desc_count': sum(
            1 for definition in fields.values()
            if isinstance(definition, dict) and (definition.get('value_desc') or '').strip()
        ),
        'rule_count': len(RELATION_RULES),
        'sections': [
            {
                'key': 'single_event',
                'title': '一、单条事件自己的检查',
                'intro': '每条事件单独看，和其他事件没关系。',
                'items': list(SINGLE_EVENT_RULES),
            },
            {
                'key': 'relation',
                'title': '二、事件之间对不对得上',
                'intro': '同一件事被上报了两次，或者一段流水和最后的总账，这里把它们对起来看。',
                'items': relations,
            },
            {
                'key': 'spec_self_check',
                'title': '三、规范自己有没有写错',
                'intro': '检查《打点规范》表本身，跟游戏上报的内容无关。',
                'items': list(SPEC_SELF_CHECK_RULES),
            },
        ],
    }
