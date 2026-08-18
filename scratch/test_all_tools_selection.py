from src.normalizer import FieldNormalizer

normalizer = FieldNormalizer()
allowed = ["高清水下摄像机", "LED水下照明灯", "前视声呐", "成像声呐", "TSS管缆跟踪传感器"]

test_phrases = [
    "全选",
    "全部",
    "所有",
    "携带全部工具",
    "带上全部工具",
    "选择所有工具",
    "全部带上",
    "全都要",
    "携带所有载荷",
    "配置全部工具",
]

for phrase in test_phrases:
    res = normalizer.normalize(phrase, allowed, field_type="list")
    print(f"Phrase '{phrase}' -> Result:", res)
