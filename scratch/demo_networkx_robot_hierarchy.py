import networkx as nx
import yaml
from pathlib import Path

# 1. 加载 YAML
fleet_path = Path("/root/mzy/seagent1.0-main_asr/config/robot_fleet.yaml")
with open(fleet_path, "r", encoding="utf-8") as f:
    fleet_data = yaml.safe_load(f)

# 2. 构建有向无环图 (DAG: Class -> Family -> Variant -> Unit)
G = nx.DiGraph()

# 添加 Class -> Family 边
for fam_id, fam_info in fleet_data.get("robot_families", {}).items():
    cls_id = fam_info.get("robot_class")
    if cls_id:
        G.add_edge(cls_id, fam_id, type="class_to_family")

# 添加 Family -> Variant 边
for var_id, var_info in fleet_data.get("model_variants", {}).items():
    fam_id = var_info.get("family_id")
    if fam_id:
        G.add_edge(fam_id, var_id, type="family_to_variant")

# 添加 Variant -> Unit 边
for unit_info in fleet_data.get("fleet_units", []):
    var_id = unit_info.get("variant_id")
    unit_id = unit_info.get("unit_id")
    if var_id and unit_id:
        G.add_edge(var_id, unit_id, type="variant_to_unit")


print("==========================================================================")
print(" NetworkX 图关系解决四级推导实测")
print("==========================================================================")

# 场景 1：向上反向推导祖先 (Upward Promotion)
target_family = "light_work_class_rov" # 轻型工作级深海机器人
ancestors = list(nx.ancestors(G, target_family))
print(f"\n1. 【向上反向推导】节点 '{target_family}' 的所有祖先节点:")
print(f"   -> 自动求得 Class 祖先为: {ancestors}")

# 场景 2：向下级联求后代子图 (Downward Descendants Filtering)
target_class = "observation_rov" # 观察级ROV
descendants = list(nx.descendants(G, target_class))
print(f"\n2. 【向下级联求后代】大类 '{target_class}' 下涵盖的所有下级节点 (Family/Variant/Unit):")
print(f"   -> 自动求得后代全集: {descendants}")

# 场景 3：1 行代码判断层级关系合法性 (Check Path Validity)
unit_test = "LROV-150-001"
class_test = "observation_rov"
has_rel = nx.has_path(G, class_test, unit_test)
print(f"\n3. 【层级合法性校验】单机 '{unit_test}' 是否归属于大类 '{class_test}':")
print(f"   -> nx.has_path 返回结果: {has_rel}")
