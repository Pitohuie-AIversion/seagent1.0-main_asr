import re

assets_path = "/root/mzy/seagent1.0-main_asr/config/assets.yaml"

with open(assets_path, "r", encoding="utf-8") as f:
    content = f.read()

# 在 payload_catalog 的各个定义项中，在 capabilities: [...] 下方增加质量占位属性
def add_weight_placeholder(match):
    indent = match.group(1)
    caps_line = match.group(2)
    return (
        f"{indent}{caps_line}\n"
        f"{indent}weight_in_air_kg: null # 空气中质量(kg)，静态预留字段（暂不启用逻辑校验）\n"
        f"{indent}weight_in_water_kg: null # 水下质量(kg)，静态预留字段（暂不启用逻辑校验）"
    )

# 匹配 capabilities 行（只有在 payload_catalog 区块中每个工具都有 capabilities 行）
updated_content = re.sub(
    r"^(\s{4})(capabilities:\s*\[.*?\])",
    add_weight_placeholder,
    content,
    flags=re.MULTILINE
)

with open(assets_path, "w", encoding="utf-8") as f:
    f.write(updated_content)

print("assets.yaml updated successfully.")
