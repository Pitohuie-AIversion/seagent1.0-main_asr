import os
import sys

# Add project root to sys.path
sys.path.insert(0, os.path.abspath(os.path.dirname(__file__) + "/.."))

from src.dialogue_manager import DialogueManager
from src.knowledge_retriever import KnowledgeBase
from src.llm_client import LLMClient

def main():
    print("================ INITIALIZING REAL LLM TEST ================")
    kb = KnowledgeBase()
    llm = LLMClient()
    dm = DialogueManager(llm=llm, kb=kb)

    turn1_input = "任务从现在开始，两个小时后结束，管缆巡检管道，水深130m，作业坐标从 (19.8, 113.2) 到 (19.9, 113.6)，选择观察级深海机器人 75HP (OBSROV-75-001)"
    print(f"\n--- TURN 1 INPUT ---\n{turn1_input}")
    reply1 = dm.process(turn1_input)
    print(f"\n--- TURN 1 REPLY (Missing fields present) ---\n{reply1}")

    # Assertions for Turn 1
    has_state_check_header = "状态核验" in reply1 or "实时环境" in reply1 or "各子系统" in reply1
    print(f"\nTurn 1 state check output prematurely present? -> {has_state_check_header}")

    turn2_input = "支持船编号选择 海洋石油 681"
    print(f"\n--- TURN 2 INPUT ---\n{turn2_input}")
    reply2 = dm.process(turn2_input)
    print(f"\n--- TURN 2 REPLY (All required fields collected) ---\n{reply2}")

    print("\n================ TEST SUMMARY ================")
    print(f"Phase after Turn 1: {dm.phase}")
    print(f"Phase after Turn 2: {dm.phase}")

if __name__ == "__main__":
    main()
