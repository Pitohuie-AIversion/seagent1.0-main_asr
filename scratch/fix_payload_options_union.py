import openpyxl
import re
import yaml

excel_file = '/root/mzy/seagent1.0-main_asr/config/SEAgent_ROV_AUV_Payload能力矩阵_括号清理版.xlsx'
assets_file = '/root/mzy/seagent1.0-main_asr/config/assets.yaml'

wb = openpyxl.load_workbook(excel_file, read_only=True, data_only=True)
sheet2 = wb['02_工作类型展示']

current_type = None
sheet2_tools = {}

for row in sheet2.iter_rows(values_only=True):
    if not row or not any(v is not None for v in row):
        continue
    first_col = str(row[0]) if row[0] is not None else ''
    if '｜' in first_col and '支持工具' in first_col:
        current_type = first_col.split('｜')[0].strip()
        sheet2_tools[current_type] = []
        continue
    
    if current_type:
        tool_name = row[0]
        if tool_name and str(tool_name).strip() not in ['支持工具', '名称', '说明', '黄色：新增 / 统一名称', '口径：工具名称与机器人配置完全一致；同一工具在同一工作类型内仅计一次']:
            if not str(tool_name).startswith('已统一') and not str(tool_name).startswith('工作类型'):
                c_tname = re.sub(r'[\（\(][^\）\)]*[\）\)]', '', str(tool_name)).strip()
                if c_tname and c_tname not in sheet2_tools[current_type]:
                    sheet2_tools[current_type].append(c_tname)

wb.close()

# 映射 Excel 任务名 -> assets.yaml 中的 key
type_map = {
    "pipeline_inspection": "管缆巡检",
    "pipeline_burial": "管缆埋设",
    "tree_valve_operation": "采油树控制面板插拔操作"
}

# 读现有 assets.yaml
with open(assets_file, 'r', encoding='utf-8') as f:
    orig_content = f.read()

data = yaml.safe_load(orig_content)
options = data.get('payload_options', {})

for opt_key, excel_type_name in type_map.items():
    if opt_key in options:
        orig_common = options[opt_key].get('common', [])
        new_from_sheet2 = sheet2_tools.get(excel_type_name, [])
        
        # 合并去重并保持顺序
        union_list = []
        for item in orig_common + new_from_sheet2:
            if item not in union_list:
                union_list.append(item)
                
        options[opt_key]['common'] = union_list

data['payload_options'] = options

# 只把修改写回，并保持 header (Line 1 ~ 56) 不变
with open(assets_file, 'r', encoding='utf-8') as f:
    orig_lines = f.readlines()

header_lines = []
for line in orig_lines:
    if line.startswith("payload_options:"):
        break
    header_lines.append(line)

yaml_payload_str = yaml.dump({
    "payload_options": data['payload_options'],
    "payload_catalog": data['payload_catalog']
}, allow_unicode=True, sort_keys=False)

final_output = "".join(header_lines) + yaml_payload_str

with open(assets_file, 'w', encoding='utf-8') as f:
    f.write(final_output)

print("payload_options union updated successfully.")
