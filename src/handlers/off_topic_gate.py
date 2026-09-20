"""
src/handlers/off_topic_gate.py - 离题门禁与正则过滤模块

职责：
1. L1 确定性离题正则黑名单与白名单判定（0 token，快速过滤）；
2. 强领域放行白名单匹配；
3. LLM 输出离题内容黑名单审查。
"""

from __future__ import annotations

import logging
import re
from typing import Optional

from src.extraction.prompts import OFF_TOPIC_REJECT_TEMPLATE

logger = logging.getLogger(__name__)

_OFF_TOPIC_BLACKLIST_RE = re.compile(
    r"七言|绝句|律诗|写诗|写词|诗歌|作诗|赋词|笑话|段子|讲个笑|讲个段子|讲故事|小说创作|"
    r"安装\s*Python|pip\s*install|conda\s*install|安装包|软件安装|教程怎么装|Python\s*3\.|环境配置|"
    r"菜谱|做饭|怎么煮|怎么炒|食谱|今天吃什么|菜怎么做|"
    r"今天天气|天气预报|多少度|下雨吗|晴天吗|"
    r"星座|算命|运势|塔罗|占卜|生辰八字|面相|手相|"
    r"编程作业|C\+\+作业|Python作业|写代码|帮我写|代写代码|作业题|"
    r"闲聊|陪聊|打发时间|聊天|说说话|逗我|"
    r"早安|晚安|节日祝福|生日快乐|拜年",
    re.IGNORECASE,
)

_STRONG_DOMAIN_WHITELIST_RE = re.compile(
    r"水下|油田|ROV|机器人|AUV|管缆|管线|电缆|巡检|检测|维修|阀门|采油树|井口|海底|海床|深海|浅海|"
    r"海流|水流|浑浊|清澈|浑浊度|能见度|障碍物|礁石|沉积物|"
    r"载荷|工具|传感器|声呐|机械臂|摄像机|相机|采样器|切割器|扳手|FLS|DVL|USBL|MBES|SBL|LBL|CTD|ADCP|"
    r"阴极电位|电位计|厚度测定|探伤仪|液压扳手|切断刀|空化水射流|水射流|刷洗工具|"
    r"导管架|水下生产系统|SPS|脐带缆|PLET|PLEM|Manifold|管汇|飞线|Jumper|防沉板|浮体|夹持器|软管|"
    r"支持船|母船|作业船|支援船|热带风暴|波浪|快换接头|飞线插拔|海管|海缆|"
    r"任务|准入|准入条件|任务状态|设备状态|运行状态|"
    r"设备|装备|型号能力|参数|性能|功率|"
    r"水深|作业深度|最大作业水深|经纬度|坐标|经度|纬度|起始点|结束点|定位|导航|"
    r"水下作业|海上作业|船舶|作业现场|海洋工程|深水油田|浅水油田|海工|水下工程|油气田|平台|钻井|"
    r"流花|陆丰|西江|番禺|惠州|崖城|东方|陵水|渤中|锦州|绥中|"
    r"管缆类型|管道类型|电缆类型|油气管道|电力电缆|光纤通信缆|通信缆|光缆|配载|携带|带上|"
    r"观察级|工作级|轻型|重型|履带式|作业级|通用型|专用|"
    r"天鹰座|金牛座|御夫座|奇点|双子座|凤凰座|"
    r"一号机|二号机|三号机|001号|002号|003号|"
    r"起始点坐标|结束点坐标|起点|终点|"
    r"管缆|巡检任务|作业任务|管缆巡检|阀门操作|采油树|CT任务|PI任务",
    re.IGNORECASE,
)

_OFF_TOPIC_WHITELIST_RE = re.compile(
    r"水下|油田|ROV|机器人|AUV|管缆|管线|电缆|巡检|检测|维修|阀门|采油树|井口|海底|海床|深海|浅海|"
    r"海流|水流|浑浊|清澈|浑浊度|能见度|障碍物|礁石|沉积物|"
    r"载荷|工具|传感器|声呐|机械臂|摄像机|相机|采样器|切割器|扳手|FLS|DVL|USBL|"
    r"支持船|母船|作业船|支援船|热带风暴|波浪|快换接头|飞线插拔|海管|海缆|"
    r"任务|状态|阶段|槽位|发布|准入|确认|发布管理|任务状态|设备状态|运行状态|"
    r"设备|装备|型号|编号|系列|类别|型号能力|参数|性能|功率|尺寸|"
    r"水深|作业深度|最大作业水深|经纬度|坐标|经度|纬度|起始点|结束点|位置|定位|导航|"
    r"水下作业|海上作业|船舶|作业现场|海洋工程|油气田|平台|钻井|"
    r"流花|陆丰|西江|番禺|惠州|崖城|东方|陵水|渤中|锦州|绥中|"
    r"今天|明天|后天|大后天|昨日|前日|早上|早晨|上午|中午|下午|晚上|傍晚|凌晨|深夜|"
    r"点钟|点半|点整|小时|分钟|持续时间|时长|多久|开始|结束|时间|日期|期限|计划|"
    r"本周|上周|下周|星期一|星期二|星期三|星期四|星期五|星期六|星期日|周一|周二|周三|周四|周五|周六|周日|星期|本月|下月|下个月|"
    r"修改|调整|更改|改为|换成|设置|补充|添加|删除|更新|修正|变更|指定|选择|选定|采用|使用|换成|改成|换成|保留|"
    r"一样|相同|保持|不变|同样|照旧|一致|类似|差不多|沿用|继续|"
    r"管缆类型|管道类型|电缆类型|油气管道|电力电缆|光纤通信缆|通信缆|光缆|配载|携带|带上|"
    r"观察级|工作级|轻型|重型|履带式|作业级|通用型|专用|"
    r"一号机|二号机|三号机|001号|002号|003号|"
    r"开始时间|结束时间|起始点坐标|结束点坐标|起点|终点|"
    r"管缆|巡检任务|作业任务|管缆巡检|阀门操作|采油树|CT任务|PI任务",
    re.IGNORECASE,
)

_OFF_TOPIC_OUTPUT_BLACKLIST_RE = re.compile(
    r"七言|绝句|律诗|诗歌|笑话|段子|Python\s*安装|pip\s*install|conda\s*install|菜谱|食谱|天气.*度|下雨|晴天|星座|运势",
    re.IGNORECASE,
)


def check_off_topic_gate(user_message: str) -> Optional[str]:
    """L1 确定性离题正则门控（0 token，不进 LLM）。
    返回 None 表示放行，返回 str 表示直接返回拒绝模板。"""
    if not user_message:
        return None
    msg = user_message.strip()
    if not msg:
        return None
    black_hit = _OFF_TOPIC_BLACKLIST_RE.search(msg) is not None
    if black_hit:
        strong_white_hit = _STRONG_DOMAIN_WHITELIST_RE.search(msg) is not None
        if not strong_white_hit:
            logger.info(
                "[OFF_TOPIC_GATE_L1] blocked blacklist_hit=%s strong_white_hit=%s msg=%r",
                black_hit,
                strong_white_hit,
                msg[:120],
            )
            return OFF_TOPIC_REJECT_TEMPLATE
    return None


_check_off_topic_gate = check_off_topic_gate


def is_off_topic_output(text: str) -> bool:
    """检查 LLM 输出是否命中了离题黑名单。"""
    if not text:
        return False
    return _OFF_TOPIC_OUTPUT_BLACKLIST_RE.search(text) is not None
