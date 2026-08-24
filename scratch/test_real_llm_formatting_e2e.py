import uuid
import sys
from src.knowledge_retriever import KnowledgeBase
from src.dialogue_manager import DialogueManager

def test_real_llm_formatting():
    kb = KnowledgeBase()
    dm = DialogueManager(kb)
    
    print("=== 真实大模型 DialogueManager 自然语言格式化验证 ===")

    # Turn 1: 确定任务模板
    print("\n--- Turn 1: 在流花11-1油田执行管缆巡检作业 ---")
    r1 = dm.process("在流花11-1油田执行管缆巡检作业")
    print("Turn 1 Reply:\n", r1)

    # Turn 2: 传入带有 {"lat":19.8,"lon":113.2} JSON 格式及水深 130.0 裸数的输入
    print("\n--- Turn 2: 提供起始点经纬度{\"lat\":19.8,\"lon\":113.2}，结束点经纬度{\"lat\":19.9,\"lon\":113.6}，水深130.0米 ---")
    r2 = dm.process("起始点经纬度{\"lat\":19.8,\"lon\":113.2}，结束点经纬度{\"lat\":19.9,\"lon\":113.6}，水深130.0米")

    print("\n--- Turn 2 完整回复内容 ---")
    print(r2)

    task_state = dm.task_state
    print("\n--- 当前 Task State ---")
    print("start_point:", task_state.get("start_point"))
    print("end_point:", task_state.get("end_point"))
    print("water_depth:", task_state.get("water_depth"))

    print("\n--- 关键格式断言验证 ---")
    # 1. 验证回复中彻底去除了原始 JSON 字符串
    assert '{"lat":19.8,"lon":113.2}' not in r2, f"回复中仍包含原始 JSON 字符串:\n{r2}"
    assert '{"lat":19.9,"lon":113.6}' not in r2, f"回复中仍包含原始 JSON 字符串:\n{r2}"
    print("✅ 回复文本中成功剔除原始 JSON 坐标！")

    # 2. 验证回复中使用了自然语言坐标包装
    assert "北纬 19.8 度，东经 113.2 度" in r2, f"回复中未出现 '北纬 19.8 度，东经 113.2 度':\n{r2}"
    assert "北纬 19.9 度，东经 113.6 度" in r2, f"回复中未出现 '北纬 19.9 度，东经 113.6 度':\n{r2}"
    print("✅ 回复文本成功包含自然语言坐标包装！")

    # 3. 验证回复中水深包含自然语言包装
    assert "水深（米）：130 米" in r2 or "水深：130 米" in r2 or "130 米" in r2, f"回复中水深未按自然语言包装 '130 米':\n{r2}"
    print("✅ 回复文本中水深成功包装为自然语言 '130 米'！")

    print("\n🎉 真实大模型 DialogueManager 格式化 100% 验证成功！")

if __name__ == "__main__":
    test_real_llm_formatting()
