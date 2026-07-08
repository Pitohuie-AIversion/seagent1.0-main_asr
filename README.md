# README_ASR

## 一、分支主要更新

本版本在原有系统基础上集成了 ASR（Automatic Speech Recognition，自动语音识别）接口，新增语音输入能力，使系统支持通过语音完成任务指令输入，并进一步提升人机交互的自然性与便捷性。

考虑到通用语音识别模型对水下机器人、水下任务、水下油田等领域专业术语的识别敏感度不足，准确率不高的问题，本版本采用方法二和方法三对语音识别效果进行针对性微调与优化，以提升专业场景下语音输入的准确性和系统交互稳定性。

## 二、候选词 + 上下文纠错

对应脚本：`src/asr_normalizer.py`

### 2.1 规则结构

每条规则由这些信息组成：

| 字段 | 含义 |
| --- | --- |
| `target` | 要纠正成的标准词 |
| `aliases` | ASR 可能听错的候选词 |
| `context_words` | 只有附近出现这些上下文词，才认为可以纠错 |
| `category` | 词条类别，如采油树、阀门、管缆、油田，不参与打分逻辑 |
| `base_score` | 命中候选词后的基础分 |
| `threshold` | 分数阈值 |

例如：

| ASR 可能输出 | 标准词 | 触发上下文示例 |
| --- | --- | --- |
| 采油数、采油书、柴油树 | 采油树 | 控制面板、井口、阀门、ROV、机械臂 |
| 卧室、卧试、或式 | 卧式 | 采油树、控制面板、阀门、类型 |
| 历史、力士、立事 | 立式 | 采油树、控制面板、井口、类型 |
| 管蓝、管栏、观览 | 管缆 | 巡检、管线、坐标、观察级 ROV |
| 逆阀、一阀、义阀 | 翼阀 | 目标阀门、主阀、采油树、控制面板 |
| 声纳 | 声呐 | 巡检、管线、多波束、侧扫 |

### 2.2 处理流程

1. 遍历所有规则，查找文本里是否出现 `aliases`。
2. 找到候选词后，取候选词前后窗口文本。
3. 如果窗口内命中业务上下文词，则增加分数；命中候选词同样增加分数。
4. 分数达到阈值(>=5)，生成唯一候选替换项（target）。
5. 没达到阈值则保留原转写文本不变。


## 三、油田实体 Link

对应脚本：`src/oilfield_linker.py`

### 3.1 背景

油田名通常独立出现，缺少稳定上下文，因此不能靠二纠错。

它通常由中文名和数字段组成，所以采用实体 Link：根据名称、别名、拼音相似度、数字段和坐标等信息打分，把用户输入链接到环境知识库 `config/environment.yaml`中的标准油田实体。

与候选词+上下文是对语音处理不同，它是在转写后对文本进行处理。


### 3.2 匹配逻辑

`OilfieldEntityLinker.link(raw_name, coords)` 会对每个标准油田打分。

如果用户输入和标准名或某个别名完全一致，文本分给到 95 分；如果存在包含关系，比如用户只说了简称，给到 82 分；否则会继续用 `SequenceMatcher` 算字符相似度，最高贡献 35 分，并用宽松拼音相似度处理 ASR 近音错字，拼音足够相似时最高贡献 55 分。对于“陵水17-2”这类带数字段的名称，如果用户输入和候选名提取出的数字段一致，还会额外加 28 分。最后如果用户还给了坐标，坐标落入该油田范围会加 40 分，接近中心点会加 15 分。所有候选实体按总分排序，最高分大于等于 75 且比第二名至少高 8 分时才会被接受为标准油田实体；否则会返回 ambiguous 或 unmatched。

### 3.3 接受和拒绝

匹配结果分两类：

| 状态 | 含义 | 系统行为 |
| --- | --- | --- |
| `accepted`  | >=75分，分数高，并且和第二名拉开差距 | 自动写入标准油田名和实体 ID |
| `ambiguous/unmatched` | vice versa | 不强行改名，保留候选供后续确认 |

## 四、模型直送开关 

direct_to_llm: 决定是否将转写文本直接输入llm（是否保留文本存在于对话框中供人工修改）

它是一个true和false的开关值，可以在config/asr.yaml中修改。

## 五、新增脚本索引

| 脚本 | 作用 |
| --- | --- |
| `config/asr.yaml` | ASR 模型路径、语言、上传大小、允许格式、是否直送 |
| `src/asr_service.py` | 加载本地 Qwen ASR 模型，提供 `transcribe_file()`给后端调用 |
| `src/asr_normalizer.py` | 候选词 + 上下文纠错规则 |
| `src/oilfield_entity_linker.py` | 油田实体 link |

## 六、ASR 模型和配置

| 项       | 当前值                      |
| -------- | --------------------------- |
| 模型     | Qwen ASR 0.6B               |
| 本地路径 | `.../model/Qwen-asr-0.6B`   |
| 配置文件 | `config/asr.yaml`           |
| 服务封装 | `src/asr_service.py`        |
| Web 接口 | `web_backend.py` `/api/asr` |

```yaml
model_path: /root/autodl-tmp/model/Qwen-asr-0.6B
device: auto
language: Chinese
max_new_tokens: 256
max_inference_batch_size: 1
max_upload_mb: 25
direct_to_llm: false
allowed_extensions:
  - wav
  - mp3
  - flac
  - m4a
  - ogg
  - webm
```

## 七、后续维护建议

- 新增 ASR 常见真值和混淆词：改`src/context_rule_normalizer.py` 里的 `TERM_RULES`。
- 新增油田标准实体：改 `config/environment.yaml` 的 `oil_fields`，并同时增删改`src/oilfield_entity_linker.py`里的`_PINYIN`。
- 调整油田 link 规则： `src/oilfield_entity_linker.py` 
