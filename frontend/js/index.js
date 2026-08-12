(function () {
  if (window.__seagentFrontendInitialized) return;
  window.__seagentFrontendInitialized = true;

  function initFrontend() {
    // 动态检测 API 基础路径（兼容 Jupyter Proxy 代理访问）
    const API_BASE = (() => {
      const path = window.location.pathname;
      // 匹配 /jupyter/proxy/8890/ 或 /proxy/8890/ 等代理路径
      const proxyMatch = path.match(/^(.*\/proxy\/\d+)\/?/);
      return proxyMatch ? proxyMatch[1] : '';
    })();

    let sessionId = null;
    let isDone = false;
    // Issue #31: 并发请求防重控制
    let isSending = false;
    let currentRequestSeq = 0;
    let currentAbortController = null;
    let sessionGeneration = 0;
    window.sessionGeneration = sessionGeneration;
    let currentActions = { can_send: true };
    let currentReadOnly = false;
    let timeUpdateInterval = null;
    let isTimePickerEditing = false;


    const messageContainer = document.getElementById('messages');
    const messageInput = document.getElementById('messageInput');
    const voiceBtn = document.getElementById('voiceBtn');
    const asrPanel = document.getElementById('asrPanel');
    const sendBtn = document.getElementById('sendBtn');
    const resetBtn = document.getElementById('resetBtn');

    let mediaStream = null;
    let audioContext = null;
    let recorderSource = null;
    let recorderProcessor = null;
    let recordedChunks = [];
    let recordingSampleRate = 16000;
    let isRecording = false;

    // --- i18n configurations ---
    let currentLang = 'zh';
    let lastResponseData = null;

    const I18N = {
      zh: {
        title: "水下多智能体任务决策系统",
        subtitle: "任务准入｜状态解析｜环境理解｜智能应答",
        simtime: "模拟时间",
        setBtn: "设置",
        simtimeHint: "点击设置可自定义基准时间",
        curtask: "当前任务",
        collected: "已收集字段",
        missing: "缺失字段",
        finaljson: "最终任务JSON",
        history: "历史记录",
        historyBtn: "查看历史",
        resetBtn: "⟳ 重新开始",
        inputPlaceholder: "描述您的水下任务...",
        voiceBtn: "语音",
        voiceStop: "停止",
        sendBtn: "发送",
        none: "暂无",
        allCollected: "✅ 所有必填字段已齐全",
        transTranslate: "翻译 (Translate)",
        transOriginal: "显示原文 (Show Original)",
        transTranslating: "正在翻译...",
        transFailed: "翻译失败",
        transNetError: "网络错误",
        transRetrying: "翻译重试中...",
        transFallback: "⚠️ 翻译质量不佳，显示原文",
        historyTitle: "历史记录",
        historyNone: "暂无历史记录",
        historyLoadedReadOnly: "📜 已加载历史任务记录（只读模式）。如需创建新任务，请点击\"重新开始\"。",
        taskConfirmMsg: "✅ 信息收集完成，当前为【立即执行任务】，任务已生成并下发。",
        taskPlanMsg: "✅ 信息收集完成，当前为【未来规划任务】，已加入计划池。",
        taskCancelMsg: "任务已取消。如需重新规划，请重新开始。",
        taskRejectedMsg: "⚠️ 任务已被拒绝，请修改参数后重新开始。",
        taskSuccessMsg: "✅ 任务信息已补全并通过约束检查，最终任务描述如下：",
        welcomeMsg: `您好！我是水下多智能体任务决策助手，当前支持以下两项核心能力，我会根据您的输入自动识别并进入对应流程：

【知识问答】
回答机器人能力、设备参数、任务流程和系统功能等问题，不会写入或修改任务信息。
示例：“金牛座一号机的最大作业水深是多少？”

【任务创建与准入】
收集作业目标、时间、位置、环境、机器人和载荷等任务信息，执行约束检查；通过准入后，由您确认并发布任务。
示例：“在流花11-1油田执行管缆巡检，水深300米，使用观察级深海机器人。”

请直接描述您的任务需求或需要查询的问题。`,
        timeAlert: "请选择要设置的模拟时间",
        timeSuccess: "模拟时间已更新",
        timeError: "设置模拟时间失败",
        timeNetError: "设置模拟时间网络错误，请检查服务是否启动",
        voiceRecording: "**正在录音...**",
        voiceStopping: "**正在停止录音...**",
        voiceTranscribing: "**正在转写...**",
        voiceNoAudio: "**没有录到音频，请重试。**",
        voiceEmptyText: "**未识别到有效文本，请重新录音或手动输入。**",
        voiceError: "**语音转写失败：**",
        voiceNotSupport: "**当前浏览器不支持麦克风录音**",
        voiceNotAvailable: "**录音不可用：**",
        rawTranscript: "原始转写：",
        correctedTranscript: "纠错后：",
        replacementList: "替换列表：",
        warningsLabel: "警告：",
        autoSentLabel: "已自动发送至对话。",
        manualConfirmLabel: "请确认或编辑后点击发送。",
        restoreFailed: "还原失败: ",
        restoreNetError: "还原失败，请检查网络",
        networkError: "网络错误，请重试。",
        emergencyBadge: "紧急",

        // Telemetry labels
        labelLink: "通信链强",
        labelLatency: "信道延迟",
        labelBattery: "潜器电量",
        labelSonarRadar: "声呐雷达扫描",
        labelRange: "扫描半径",
        labelFreq: "载波频率",
        labelEnvMonitor: "水下环境监视",
        labelWaterDepth: "水深",
        labelCurDepth: "当前深度",
        labelPressure: "静水压力",
        labelWaterTemp: "环境水温",
        labelSystemDiag: "动力分配与诊断",
        labelTelemetryStream: "系统遥测事件流"
      },
      en: {
        title: "Underwater Multi-Robot Decision System",
        subtitle: "Task Admission | State Parsing | Environmental Recognition | Intelligent Response",
        simtime: "Simulated Time",
        setBtn: "Set",
        simtimeHint: "Click Set to customize baseline time",
        curtask: "Current Task",
        collected: "Collected Fields",
        missing: "Missing Fields",
        finaljson: "Final Task JSON",
        history: "History",
        historyBtn: "View History",
        resetBtn: "⟳ Restart",
        inputPlaceholder: "Describe your underwater task...",
        voiceBtn: "Voice",
        voiceStop: "Stop",
        sendBtn: "Send",
        none: "None",
        allCollected: "✅ All required fields collected",
        transTranslate: "Translate",
        transOriginal: "Original",
        transTranslating: "Translating...",
        transFailed: "Translation failed",
        transNetError: "Network error",
        transRetrying: "Retrying translation...",
        transFallback: "⚠️ Low-quality translation, showing original",
        historyTitle: "History",
        historyNone: "No history records",
        historyLoadedReadOnly: "📜 History task loaded (Read-Only). Click 'Restart' to create a new task.",
        taskConfirmMsg: "✅ Information collection complete, executing task immediately, task generated and dispatched.",
        taskPlanMsg: "✅ Information collection complete, task planned for future, added to the planning pool.",
        taskCancelMsg: "Task cancelled. Click 'Restart' to plan a new one.",
        taskRejectedMsg: "⚠️ Task rejected. Please modify parameters and restart.",
        taskSuccessMsg: "✅ Task information completed and verified. Final task description:",
        welcomeMsg: `Hello! I am the underwater multi-robot task decision assistant. I currently support two core capabilities and will automatically select the appropriate workflow based on your request:

[Knowledge Q&A]
Answers questions about robot capabilities, equipment parameters, task workflows, and system functions without creating or modifying task data.
Example: “What is the maximum operating depth of Taurus Unit 1?”

[Task Creation & Admission]
Collects task information such as the objective, time, location, environment, robot, and payload, then performs constraint checks. After admission is approved, you confirm and publish the task.
Example: “Inspect the subsea pipeline at Liuhua 11-1 Oilfield at a depth of 300 m using an observation-class deep-sea robot.”

Please describe your task request or ask a question directly.`,
        timeAlert: "Please select a time to set",
        timeSuccess: "Simulated time updated",
        timeError: "Failed to set simulated time",
        timeNetError: "Failed to set simulated time, please check if the server is running",
        voiceRecording: "**Recording...**",
        voiceStopping: "**Stopping recording...**",
        voiceTranscribing: "**Transcribing...**",
        voiceNoAudio: "**No audio captured, please try again.**",
        voiceEmptyText: "**No valid text transcribed. Re-record or type manually.**",
        voiceError: "**Speech transcription failed: **",
        voiceNotSupport: "**Microphone recording not supported by current browser**",
        voiceNotAvailable: "**Recording unavailable: **",
        rawTranscript: "Raw Transcript: ",
        correctedTranscript: "Corrected: ",
        replacementList: "Replacements: ",
        warningsLabel: "Warnings: ",
        autoSentLabel: "Sent to chat automatically.",
        manualConfirmLabel: "Please confirm/edit and click Send.",
        restoreFailed: "Restore failed: ",
        restoreNetError: "Restore failed, please check network",
        networkError: "Network error, please try again.",
        emergencyBadge: "Emergency",

        // Telemetry labels
        labelLink: "LINK STRENGTH",
        labelLatency: "LATENCY",
        labelBattery: "ROV BATTERY",
        labelSonarRadar: "Sonar Radar Sweep",
        labelRange: "Scan Range",
        labelFreq: "Frequency",
        labelEnvMonitor: "Subsea Environment",
        labelWaterDepth: "Depth",
        labelCurDepth: "Current Depth",
        labelPressure: "Hydro Pressure",
        labelWaterTemp: "Water Temp",
        labelSystemDiag: "Diagnostics",
        labelTelemetryStream: "Telemetry stream logs"
      }
    };

    const FIELDS = {
      task_id: { zh: "任务编号", en: "Task ID" },
      task_type: { zh: "任务类型", en: "Task Type" },
      start_time: { zh: "任务开始时间", en: "Task Start Time" },
      end_time: { zh: "任务结束时间", en: "Task End Time" },
      cable_position: { zh: "管缆位置", en: "Cable Position" },
      cable_type: { zh: "管缆类型", en: "Cable Type" },
      start_point: { zh: "起始点经纬度", en: "Start Coordinates" },
      end_point: { zh: "结束点经纬度", en: "End Coordinates" },
      water_depth: { zh: "水深（米）", en: "Water Depth (m)" },
      equipment_family: { zh: "作业机器人系列", en: "Robot Family" },
      equipment_type: { zh: "作业设备型号", en: "Equipment Model" },
      equipment_unit_id: { zh: "具体机器人编号", en: "Robot Unit ID" },
      equipment_name: { zh: "设备全称", en: "ROV Name" },
      payload: { zh: "携带工具", en: "Payload" },
      support_vessel: { zh: "支持船编号", en: "Support Vessel ID" },
      oilfield_name: { zh: "油田名称", en: "Oilfield Name" },
      oilfield_coordinates: { zh: "油田经纬度坐标", en: "Oilfield Coordinates" },
      wellhead_id: { zh: "井口编号", en: "Wellhead ID" }
    };

    const VALUES = {
      // 任务模板键与规范任务类型（来源：task_schemas.yaml）
      "pipeline_inspection": { zh: "管缆巡检", en: "Pipeline Inspection" },
      "pipeline_burial": { zh: "管缆埋设", en: "Pipeline Burial" },
      "tree_valve_operation": { zh: "采油树控制面板阀门插拔", en: "Tree Valve Operation" },
      "管缆巡检": { zh: "管缆巡检", en: "Pipeline Inspection" },
      "管缆埋设": { zh: "管缆埋设", en: "Pipeline Burial" },
      "采油树控制面板插入": { zh: "采油树控制面板插入", en: "Tree Valve Insertion" },
      "采油树控制面板拔出": { zh: "采油树控制面板拔出", en: "Tree Valve Extraction" },
      "采油树控制面板插拔": { zh: "采油树控制面板插拔", en: "Tree Valve Operation" },
      "未识别": { zh: "未识别", en: "Unidentified" },

      // 管缆类型（来源：assets.yaml）
      "海底油气管道": { zh: "海底油气管道", en: "Subsea Oil/Gas Pipeline" },
      "电力电缆": { zh: "电力电缆", en: "Power Cable" },
      "光纤通信缆": { zh: "光纤通信缆", en: "Fiber Optic Cable" },

      // 机器人大类、机器人族与标准型号（来源：robot_fleet.yaml）
      "管缆埋设机器人": { zh: "管缆埋设机器人", en: "Cable Burial Robot" },
      "工作级ROV": { zh: "工作级 ROV", en: "Work-Class ROV" },
      "观察级ROV": { zh: "观察级 ROV", en: "Observation-Class ROV" },
      "AUV": { zh: "AUV", en: "Autonomous Underwater Vehicle (AUV)" },
      "履带式海底重载作业机器人": { zh: "履带式海底重载作业机器人", en: "Crawler-Type Heavy-Duty Seabed Robot" },
      "拖曳式海底重载作业机器人": { zh: "拖曳式海底重载作业机器人", en: "Towed Heavy-Duty Seabed Robot" },
      "特种工作级深海机器人": { zh: "特种工作级深海机器人", en: "Special Work-Class Deep-Sea Robot" },
      "通用工作级深海机器人": { zh: "通用工作级深海机器人", en: "General Work-Class Deep-Sea ROV" },
      "轻型工作级深海机器人": { zh: "轻型工作级深海机器人", en: "Light Work-Class Deep-Sea ROV" },
      "观察级深海机器人": { zh: "观察级深海机器人", en: "Observation-Class Deep-Sea ROV" },
      "水下无人自主航行器": { zh: "水下无人自主航行器", en: "Autonomous Underwater Vehicle" },
      "履带式海底重载作业机器人 1600HP": { zh: "履带式海底重载作业机器人 1600HP", en: "Crawler-Type Heavy-Duty Seabed Robot 1600HP" },
      "拖曳式海底重载作业机器人 1500HP": { zh: "拖曳式海底重载作业机器人 1500HP", en: "Towed Heavy-Duty Seabed Robot 1500HP" },
      "特种工作级深海机器人 600HP": { zh: "特种工作级深海机器人 600HP", en: "Special Work-Class Deep-Sea Robot 600HP" },
      "通用工作级深海机器人 250HP": { zh: "通用工作级深海机器人 250HP", en: "General Work-Class Deep-Sea ROV 250HP" },
      "轻型工作级深海机器人 HP": { zh: "轻型工作级深海机器人 HP", en: "Light Work-Class Deep-Sea ROV HP" },
      "观察级深海机器人 HP": { zh: "观察级深海机器人 HP", en: "Observation-Class Deep-Sea ROV HP" },
      "水下无人自主航行器 HP": { zh: "水下无人自主航行器 HP", en: "Autonomous Underwater Vehicle HP" },

      // 实体机器人编号与展示名称（来源：robot_fleet.yaml）
      "CRAWLER-1600-001": { zh: "CRAWLER-1600-001", en: "CRAWLER-1600-001" },
      "TOWED-1500-001": { zh: "TOWED-1500-001", en: "TOWED-1500-001" },
      "SPECIAL-600-001": { zh: "SPECIAL-600-001", en: "SPECIAL-600-001" },
      "WROV-250-001": { zh: "WROV-250-001", en: "WROV-250-001" },
      "LROV-HP-001": { zh: "LROV-HP-001", en: "LROV-HP-001" },
      "LROV-HP-002": { zh: "LROV-HP-002", en: "LROV-HP-002" },
      "OBSROV-HP-001": { zh: "OBSROV-HP-001", en: "OBSROV-HP-001" },
      "AUV-HP-001": { zh: "AUV-HP-001", en: "AUV-HP-001" },
      "履带式海底重载作业机器人1600HP-001": { zh: "履带式海底重载作业机器人1600HP-001", en: "Crawler-Type Heavy-Duty Seabed Robot 1600HP-001" },
      "拖曳式海底重载作业机器人1500HP-001": { zh: "拖曳式海底重载作业机器人1500HP-001", en: "Towed Heavy-Duty Seabed Robot 1500HP-001" },
      "特种工作级深海机器人600HP-001": { zh: "特种工作级深海机器人600HP-001", en: "Special Work-Class Deep-Sea Robot 600HP-001" },
      "通用工作级深海机器人250HP-001": { zh: "通用工作级深海机器人250HP-001", en: "General Work-Class Deep-Sea ROV 250HP-001" },
      "轻型工作级深海机器人HP-001": { zh: "轻型工作级深海机器人HP-001", en: "Light Work-Class Deep-Sea ROV HP-001" },
      "轻型工作级深海机器人HP-002": { zh: "轻型工作级深海机器人HP-002", en: "Light Work-Class Deep-Sea ROV HP-002" },
      "观察级深海机器人HP-001": { zh: "观察级深海机器人HP-001", en: "Observation-Class Deep-Sea ROV HP-001" },
      "水下无人自主航行器HP-001": { zh: "水下无人自主航行器HP-001", en: "Autonomous Underwater Vehicle HP-001" },

      // 支持船（来源：assets.yaml）
      "海洋石油681": { zh: "海洋石油681", en: "Haiyang Shiyou 681" },
      "海洋石油286": { zh: "海洋石油286", en: "Haiyang Shiyou 286" },
      "海洋石油708": { zh: "海洋石油708", en: "Haiyang Shiyou 708" },
      "DSV-Oceanic": { zh: "DSV-Oceanic", en: "DSV-Oceanic" },

      // 合法任务载荷（来源：assets.yaml）
      "高清水下摄像机": { zh: "高清水下摄像机", en: "HD Underwater Camera" },
      "LED水下照明灯": { zh: "LED水下照明灯", en: "LED Underwater Light" },
      "激光标尺": { zh: "激光标尺", en: "Laser Scale" },
      "前视声呐": { zh: "前视声呐", en: "Forward-Looking Sonar" },
      "INS惯性导航系统": { zh: "INS惯性导航系统", en: "INS Inertial Navigation System" },
      "DVL多普勒测速仪": { zh: "DVL多普勒测速仪", en: "DVL Doppler Velocity Log" },
      "USBL定位设备": { zh: "USBL定位设备", en: "USBL Positioning Equipment" },
      "深度传感器": { zh: "深度传感器", en: "Depth Sensor" },
      "高压水射流喷冲埋设模块": { zh: "高压水射流喷冲埋设模块", en: "High-Pressure Water-Jet Burial Module" },
      "机械切割开沟模块（可选）": { zh: "机械切割开沟模块（可选）", en: "Mechanical Cutting and Trenching Module (Optional)" },
      "海缆压埋/保持装置": { zh: "海缆压埋/保持装置", en: "Subsea Cable Burial/Hold-Down Device" },
      "埋深控制装置": { zh: "埋深控制装置", en: "Burial Depth Control Device" },
      "TSS管缆跟踪传感器": { zh: "TSS管缆跟踪传感器", en: "TSS Cable/Pipeline Tracking Sensor" },
      "激光标尺（可选）": { zh: "激光标尺（可选）", en: "Laser Scale (Optional)" },
      "成像声呐": { zh: "成像声呐", en: "Imaging Sonar" },
      "多波束声呐（可选）": { zh: "多波束声呐（可选）", en: "Multibeam Sonar (Optional)" },
      "水下定位信标（可选）": { zh: "水下定位信标（可选）", en: "Underwater Positioning Beacon (Optional)" },
      "声学应答器（可选）": { zh: "声学应答器（可选）", en: "Acoustic Transponder (Optional)" },
      "海床地质探测设备（可选）": { zh: "海床地质探测设备（可选）", en: "Seabed Geological Survey Equipment (Optional)" },
      "腐蚀检测设备（可选）": { zh: "腐蚀检测设备（可选）", en: "Corrosion Inspection Equipment (Optional)" },
      "温度/压力传感器模块（可选）": { zh: "温度/压力传感器模块（可选）", en: "Temperature/Pressure Sensor Module (Optional)" },
      "多功能液压机械臂": { zh: "多功能液压机械臂", en: "Multifunctional Hydraulic Manipulator" },
      "电液机械臂": { zh: "电液机械臂", en: "Electro-Hydraulic Manipulator" },
      "双目视觉模块（可选）": { zh: "双目视觉模块（可选）", en: "Stereo Vision Module (Optional)" },
      "LED照明系统": { zh: "LED照明系统", en: "LED Lighting System" },
      "三维视觉系统（可选）": { zh: "三维视觉系统（可选）", en: "3D Vision System (Optional)" },
      "成像声呐（可选）": { zh: "成像声呐（可选）", en: "Imaging Sonar (Optional)" },
      "USBL超短基线定位系统": { zh: "USBL超短基线定位系统", en: "USBL Positioning System" },
      "高度计": { zh: "高度计", en: "Altimeter" },
      "泄漏检测传感器（可选）": { zh: "泄漏检测传感器（可选）", en: "Leak Detection Sensor (Optional)" },

      // 标准油田名称（来源：environment.yaml）
      "流花11-1油田": { zh: "流花11-1油田", en: "Liuhua 11-1 Oilfield" },
      "陵水17-2油田": { zh: "陵水17-2油田", en: "Lingshui 17-2 Oilfield" },
      "蓬莱19-3油田": { zh: "蓬莱19-3油田", en: "Penglai 19-3 Oilfield" },
      "春晓26-1油田": { zh: "春晓26-1油田", en: "Chunxiao 26-1 Oilfield" }
    };

    document.getElementById('langSelect').addEventListener('change', (e) => {
      currentLang = e.target.value;
      updateLanguage();
    });

    function updateLanguage() {
      document.getElementById('ui-title').innerText = I18N[currentLang].title;
      document.getElementById('ui-subtitle').innerText = I18N[currentLang].subtitle;
      document.getElementById('title-simtime').innerText = I18N[currentLang].simtime;
      document.getElementById('setTimeBtn').innerText = I18N[currentLang].setBtn;
      document.getElementById('hint-simtime').innerText = I18N[currentLang].simtimeHint;
      document.getElementById('title-curtask').innerText = I18N[currentLang].curtask;
      document.getElementById('title-collected').innerText = I18N[currentLang].collected;
      document.getElementById('title-missing').innerText = I18N[currentLang].missing;
      document.getElementById('title-finaljson').innerText = I18N[currentLang].finaljson;
      document.getElementById('title-history').innerText = I18N[currentLang].history;
      document.getElementById('historyBtn').innerText = I18N[currentLang].historyBtn;
      document.getElementById('resetBtn').innerText = I18N[currentLang].resetBtn;

      document.getElementById('messageInput').placeholder = I18N[currentLang].inputPlaceholder;
      if (!isRecording) {
        document.getElementById('voiceBtn').innerText = I18N[currentLang].voiceBtn;
      } else {
        document.getElementById('voiceBtn').innerText = I18N[currentLang].voiceStop;
      }
      document.getElementById('sendBtn').innerText = I18N[currentLang].sendBtn;



      if (lastResponseData) {
        updateSidebar(lastResponseData);
      } else {
        document.getElementById('collectedFields').innerHTML = I18N[currentLang].none;
      }

      // 1. 优先实时跟进更新欢迎消息 (直接加载 I18N[currentLang].welcomeMsg，不调用 /api/translate 翻译)
      const welcomeMsgDiv = document.querySelector('.message[data-message-kind="welcome"]');
      if (welcomeMsgDiv) {
        const welcomeContent = I18N[currentLang].welcomeMsg;
        welcomeMsgDiv.setAttribute('data-original', welcomeContent);
        const bubble = welcomeMsgDiv.querySelector('.bubble');
        if (bubble) {
          bubble.innerHTML = renderMessageContent(welcomeContent, 'bot');
        }
      }

      // 2. 普通对话消息翻译循环（跳过 welcome 类型的消息）
      document.querySelectorAll('.message').forEach(msgDiv => {
        if (msgDiv.dataset.messageKind === 'welcome') {
          return;
        }
        const originalText = msgDiv.getAttribute('data-original');
        const btn = msgDiv.querySelector('.msg-translate-bar');
        const bubble = msgDiv.querySelector('.bubble');
        if (originalText && btn && bubble) {
          const hasZh = hasChinese(originalText);
          if (currentLang === 'en' && hasZh) {
            autoTranslateMessage(msgDiv);
          } else {
            bubble.innerHTML = renderMessageContent(originalText, msgDiv.getAttribute('data-role'));
            btn.innerText = I18N[currentLang].transTranslate;
            btn.setAttribute('data-translated', 'false');
          }
        }
      });
    }

    function hasChinese(text) {
      return /[\u4e00-\u9fa5]/.test(text);
    }

    /**
     * 校验翻译结果是否为脏数据，与后端 _is_dirty_translation 逻辑保持一致。
     */
    function isDirtyTranslation(targetLang, translated) {
      if (!translated) return true;
      const t = translated.trim();
      if (t.startsWith('{') || t.startsWith('[')) return true;
      if (targetLang === 'English' && /[\u4e00-\u9fa5]/.test(translated)) return true;
      return false;
    }

    /**
     * 公共翻译函数：带超时、重试、脏数据校验。
     * @param {string} text - 原文
     * @param {string} targetLang - 目标语言
     * @param {number} [retries=1] - 剩余重试次数
     * @returns {Promise<{translated: string, warning: string|null}>}
     */
    async function fetchTranslation(text, targetLang, retries = 1) {
      const TIMEOUT_MS = 30000; // 30s 超时
      const controller = new AbortController();
      const timerId = setTimeout(() => controller.abort(), TIMEOUT_MS);

      try {
        const res = await fetch(API_BASE + '/api/translate', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ text, target_lang: targetLang }),
          signal: controller.signal
        });
        clearTimeout(timerId);

        if (!res.ok) throw new Error(`HTTP ${res.status}`);
        const data = await res.json();

        if (data.code === 200 && data.translated_text) {
          // 后端 quality_warning 或前端脏数据校验
          if (data.quality_warning || isDirtyTranslation(targetLang, data.translated_text)) {
            return { translated: data.translated_text, warning: 'low_quality' };
          }
          return { translated: data.translated_text, warning: null };
        }
        throw new Error(data.msg || 'empty translated_text');
      } catch (err) {
        clearTimeout(timerId);
        if (retries > 0) {
          // 自动重试一次
          await new Promise(r => setTimeout(r, 1500));
          return fetchTranslation(text, targetLang, retries - 1);
        }
        throw err;
      }
    }

    async function autoTranslateMessage(msgDiv) {
      const originalText = msgDiv.getAttribute('data-original');
      const btn = msgDiv.querySelector('.msg-translate-bar');
      const bubble = msgDiv.querySelector('.bubble');
      if (!originalText || !btn || !bubble) return;

      btn.innerText = I18N[currentLang].transTranslating;
      try {
        const { translated, warning } = await fetchTranslation(originalText, 'English');
        msgDiv.setAttribute('data-translation-cache', translated);
        if (currentLang === 'en') {
          bubble.innerHTML = renderMessageContent(translated, msgDiv.getAttribute('data-role'));
          if (warning === 'low_quality') {
            btn.innerText = I18N[currentLang].transFallback;
            btn.setAttribute('data-translated', 'warn');
          } else {
            btn.innerText = I18N[currentLang].transOriginal;
            btn.setAttribute('data-translated', 'true');
          }
        }
      } catch (err) {
        btn.innerText = err.name === 'AbortError'
          ? I18N[currentLang].transNetError
          : I18N[currentLang].transFailed;
      }
    }

    async function toggleMessageTranslation(btn) {
      const wrapper = btn.closest('.bubble-wrapper');
      const bubble = wrapper.querySelector('.bubble');
      const messageDiv = btn.closest('.message');
      const originalText = messageDiv.getAttribute('data-original');
      const isTranslated = btn.getAttribute('data-translated');

      if (isTranslated === 'true' || isTranslated === 'warn') {
        // 显示原文
        bubble.innerHTML = renderMessageContent(originalText, messageDiv.getAttribute('data-role'));
        btn.innerText = I18N[currentLang].transTranslate;
        btn.setAttribute('data-translated', 'false');
      } else {
        const targetLang = hasChinese(originalText) ? 'English' : 'Chinese';
        btn.innerText = I18N[currentLang].transTranslating;
        try {
          const { translated, warning } = await fetchTranslation(originalText, targetLang);
          messageDiv.setAttribute('data-translation-cache', translated);
          bubble.innerHTML = renderMessageContent(translated, messageDiv.getAttribute('data-role'));
          if (warning === 'low_quality') {
            btn.innerText = I18N[currentLang].transFallback;
            btn.setAttribute('data-translated', 'warn');
          } else {
            btn.innerText = I18N[currentLang].transOriginal;
            btn.setAttribute('data-translated', 'true');
          }
        } catch (err) {
          btn.innerText = err.name === 'AbortError'
            ? I18N[currentLang].transNetError
            : I18N[currentLang].transFailed;
        }
      }
    }

    function escapeHtml(str) {
      if (window.SEAgentMarkdown && typeof window.SEAgentMarkdown.escapeHtml === 'function') {
        return window.SEAgentMarkdown.escapeHtml(str);
      }
      if (typeof str !== 'string') str = String(str);
      return str.replace(/[&<>"']/g, function (m) {
        if (m === '&') return '&amp;';
        if (m === '<') return '&lt;';
        if (m === '>') return '&gt;';
        if (m === '"') return '&quot;';
        if (m === "'") return '&#39;';
        return m;
      });
    }

    function renderMessageContent(content, role) {
      if (window.SEAgentMarkdown && typeof window.SEAgentMarkdown.render === 'function') {
        return window.SEAgentMarkdown.render(content, role);
      }
      return escapeHtml(content).replace(/\r\n?|\n/g, '<br>');
    }

    function getFieldLabel(key) {
      if (FIELDS[key]) {
        return FIELDS[key][currentLang];
      }
      return key;
    }

    function translateValue(key, val) {
      if (val === null || val === undefined) return currentLang === 'zh' ? '暂无' : 'None';

      if (Array.isArray(val)) {
        return val.map(item => translateValue(key, item)).join(currentLang === 'zh' ? ' / ' : ' / ');
      }

      if (typeof val === 'object') {
        if (val.lat !== undefined && val.lon !== undefined) {
          if (currentLang === 'zh') {
            return `(${val.lat}, ${val.lon})`;
          } else {
            const latStr = val.lat >= 0 ? `${val.lat}°N` : `${Math.abs(val.lat)}°S`;
            const lonStr = val.lon >= 0 ? `${val.lon}°E` : `${Math.abs(val.lon)}°W`;
            return `${latStr}, ${lonStr}`;
          }
        }
        return JSON.stringify(val);
      }

      const valStr = String(val);
      if (VALUES[valStr]) {
        return VALUES[valStr][currentLang];
      }
      return valStr;
    }

    // Sleek vector bot/user visors
    const botAvatarSvg = `
  <svg class="bot-avatar-svg" viewBox="0 0 24 24" width="22" height="22">
    <rect x="4" y="6" width="16" height="12" rx="3" stroke="currentColor" stroke-width="1.8" fill="none" />
    <path d="M2 10h2M20 10h2M9 16l3 2 3-2" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" />
    <rect x="7" y="9" width="10" height="2" rx="1" fill="currentColor" class="visor-glow" />
  </svg>`;
    const userAvatarSvg = `
  <svg class="user-avatar-svg" viewBox="0 0 24 24" width="22" height="22">
    <circle cx="12" cy="8" r="4" stroke="currentColor" stroke-width="1.8" fill="none" />
    <path d="M4 20c0-4 4-5 8-5s8 1 8 5" stroke="currentColor" stroke-width="1.8" fill="none" stroke-linecap="round" />
  </svg>`;

    function addMessage(role, content, options = {}) {
      const div = document.createElement('div');
      const displayRole = role === 'assistant' ? 'bot' : role;
      div.className = `message ${displayRole}`;
      if (options.kind) {
        div.dataset.messageKind = options.kind;
      }
      const isBot = (role === 'bot' || role === 'assistant');
      const avatarHtml = isBot ? `<div class="avatar">${botAvatarSvg}</div>` : `<div class="avatar">${userAvatarSvg}</div>`;

      div.setAttribute('data-original', content);
      div.setAttribute('data-role', role);

      const renderedContent = renderMessageContent(content, role);

      let translateBtn = '';
      if (content.trim() && options.kind !== 'welcome') {
        translateBtn = `<div class="msg-translate-bar" data-action="translate-message">${I18N[currentLang].transTranslate}</div>`;
      }

      div.innerHTML = avatarHtml + `<div class="bubble-wrapper" style="display:flex; flex-direction:column; align-items: ${role === 'user' ? 'flex-end' : 'flex-start'};"><div class="bubble">${renderedContent}</div>${translateBtn}</div>`;
      messageContainer.appendChild(div);
      messageContainer.scrollTop = messageContainer.scrollHeight;

      if (options.kind !== 'welcome' && currentLang === 'en' && hasChinese(content)) {
        autoTranslateMessage(div);
      }

      return div;
    }

    function addWelcomeMessage() {
      addMessage('bot', I18N[currentLang].welcomeMsg, { kind: 'welcome' });
    }



    // inline warning SVG for missing fields
    const svgWarning = `
  <svg class="tech-icon warning" style="width:12px; height:12px; margin-right:4px;" viewBox="0 0 24 24">
    <path d="M12 2l8.66 5v10L12 22l-8.66-5V7z" />
    <path d="M12 8v5M12 16h.01" stroke-width="2" />
  </svg>`;

    /**
     * applyInteractionState - 集中控制所有可交互元素的状态。
     * Issue #31: 前端不再自行从 missing.length 或本地变量推断按钮状态。
     * @param {Object} actions  - ui_state.actions
     * @param {boolean} readOnly - ui_state.read_only
     */
    function applyInteractionState(actions, readOnly) {
      currentActions = actions || { can_send: true };
      window.currentActions = currentActions;
      window.applyInteractionState = applyInteractionState;
      currentReadOnly = !!readOnly;
      const canSend = !!currentActions.can_send && !currentReadOnly && !isSending;
      messageInput.disabled = !canSend;
      sendBtn.disabled = !canSend;
      voiceBtn.disabled = !canSend;
      isDone = currentReadOnly;
    }

    function cancelActiveRequest() {
      currentRequestSeq += 1;
      if (currentAbortController) {
        try { currentAbortController.abort(); } catch (e) {}
      }
      currentAbortController = null;
      isSending = false;
      applyInteractionState(currentActions, currentReadOnly);
    }


    /**
     * updateSidebar - 渲染任务字段面板。
     * Issue #31: 优先使用 data.ui_state（新路径），降级到旧 collected/missing 字段（compat 路径）。
     */
    function updateSidebar(data) {
      window.updateSidebar = updateSidebar;
      lastResponseData = data;

      const uiState = data.ui_state;

      // ── 任务类型和 ID ──────────────────────────────────────────────
      let taskTypeDisplay = '未识别';
      let displayedTaskId = '';
      let isEmergency = false;
      let taskIdIsPreview = false;

      if (uiState) {
        const ttk = uiState.task_type_key;
        const taskTypeSlot = Array.isArray(uiState.slots) ? uiState.slots.find(slot => slot.key === 'task_type') : null;
        const taskTypeValue = taskTypeSlot && taskTypeSlot.status !== 'missing' ? (taskTypeSlot.candidate_value ?? taskTypeSlot.value) : null;
        if (taskTypeValue) taskTypeDisplay = String(taskTypeValue);
        else if (ttk) taskTypeDisplay = String(ttk);
        if (uiState.task_id) {
          displayedTaskId = uiState.task_id;
        } else if (uiState.task_id_preview) {
          displayedTaskId = uiState.task_id_preview;
          taskIdIsPreview = true;
        }
        isEmergency = uiState.mode === 'emergency' || uiState.dialogue_mode === 'emergency_intervention';
      } else {
        const collected = data.collected || {};
        if (collected.task_type) {
          taskTypeDisplay = collected.task_type;
        } else if (data.task_type === 'tree_valve_operation') {
          taskTypeDisplay = '采油树控制面板插拔';
        } else if (data.task_type === 'pipeline_inspection') {
          taskTypeDisplay = '管缆巡检';
        } else if (data.task_type) {
          taskTypeDisplay = data.task_type;
        }
        const officialTaskId = (data.collected && data.collected.task_id) ? data.collected.task_id : (data.task_id || '');
        const previewTaskId = officialTaskId ? '' : (data.task_id_preview || '');
        const taskIdStr = officialTaskId || previewTaskId;
        displayedTaskId = taskIdStr;
        taskIdIsPreview = !!previewTaskId;
        isEmergency = !!data.emergency;
      }

      const localizedTaskType = uiState ? String(taskTypeDisplay) : translateValue('task_type', taskTypeDisplay);
      const taskIdPrefix = taskIdIsPreview ? (currentLang === 'zh' ? '预计 ' : 'Estimated ') : '';
      const taskInfo = document.getElementById('taskInfo');
      taskInfo.replaceChildren();
      const typeElement = document.createElement('strong');
      typeElement.textContent = localizedTaskType;
      taskInfo.appendChild(typeElement);
      if (displayedTaskId) {
        const taskIdElement = document.createElement('span');
        taskIdElement.className = 'badge task-id';
        taskIdElement.style.cssText = 'background: rgba(0, 240, 255, 0.15); border: 1px solid rgba(0, 240, 255, 0.4); color: var(--accent-color, #00f0ff); margin-left: 6px; font-family: monospace; font-size: 0.85em; padding: 2px 6px; border-radius: 4px;';
        taskIdElement.textContent = `${taskIdPrefix}${displayedTaskId}`;
        taskInfo.appendChild(taskIdElement);
      }
      if (isEmergency) {
        const emergencyElement = document.createElement('span');
        emergencyElement.className = 'badge emergency';
        emergencyElement.textContent = I18N[currentLang].emergencyBadge;
        taskInfo.appendChild(emergencyElement);
      }

      const collectedDiv = document.getElementById('collectedFields');
      const missingDiv = document.getElementById('missingFields');

      function formatUiStateValue(slot, customVal = undefined) {
        if (slot.display_value !== undefined && slot.display_value !== null && customVal === undefined) {
          return String(slot.display_value);
        }
        const val = customVal !== undefined ? customVal : slot.value;
        if (val === null || val === undefined) {
          return currentLang === 'zh' ? '暂无' : 'None';
        }
        if (typeof val === 'boolean') {
          return val ? (currentLang === 'zh' ? '是' : 'True') : (currentLang === 'zh' ? '否' : 'False');
        }
        if (typeof val === 'number' || typeof val === 'string') {
          return String(val);
        }
        if (Array.isArray(val)) {
          return val.map(v => (v !== null && typeof v === 'object') ? JSON.stringify(v) : String(v)).join(' / ');
        }
        if (typeof val === 'object') {
          if (val.lat !== undefined && val.lon !== undefined) {
            return `lat: ${val.lat}, lon: ${val.lon}`;
          }
          try {
            return JSON.stringify(val);
          } catch (e) {
            return String(val);
          }
        }
        return String(val);
      }

      function getSlotLabel(slot) {
        const labelObj = slot.label || {};
        return (typeof labelObj === 'string') ? labelObj : (labelObj[currentLang] || labelObj.zh || slot.key);
      }

      function renderStandardSlot(slot) {
        const statusClass = slot.status === 'valid' ? 'valid' : (slot.status === 'invalid' ? 'invalid' : 'candidate');
        const statusIcon = slot.status === 'valid' ? '✅' : (slot.status === 'invalid' ? '❌' : '⏳');
        const label = getSlotLabel(slot);

        const row = document.createElement('div');
        row.className = `field-row ${statusClass}`;

        const labelSpan = document.createElement('span');
        labelSpan.className = 'field-label';
        labelSpan.textContent = `${statusIcon} ${label}`;
        row.appendChild(labelSpan);

        const valueSpan = document.createElement('span');
        valueSpan.className = 'field-value';

        const displayVal = (slot.status === 'candidate' || slot.status === 'pending')
          ? (slot.candidate_value !== null && slot.candidate_value !== undefined ? slot.candidate_value : slot.value)
          : (slot.status === 'invalid' ? (slot.raw_value ?? slot.candidate_value ?? slot.value) : slot.value);

        if (Array.isArray(displayVal)) {
          const ul = document.createElement('ul');
          for (const item of displayVal) {
            const li = document.createElement('li');
            li.textContent = formatUiStateValue(slot, item);
            ul.appendChild(li);
          }
          valueSpan.appendChild(ul);
        } else {
          valueSpan.textContent = formatUiStateValue(slot, displayVal);
        }
        row.appendChild(valueSpan);

        if (slot.validation_error) {
          const errEl = document.createElement('div');
          errEl.className = 'field-error';
          errEl.style.cssText = 'color: var(--error-color, #ff4d4d); font-size: 0.8em; margin-top: 2px;';
          errEl.textContent = slot.validation_error;
          row.appendChild(errEl);
        }
        return row;
      }

      function renderConflictSlot(slot) {
        const label = getSlotLabel(slot);
        const row = document.createElement('div');
        row.className = 'field-row conflict';

        const labelSpan = document.createElement('span');
        labelSpan.className = 'field-label';
        labelSpan.textContent = `⚔️ ${label}`;
        row.appendChild(labelSpan);

        const detailDiv = document.createElement('div');
        detailDiv.className = 'field-conflict-detail';
        detailDiv.style.cssText = 'font-size: 0.85em; margin-top: 4px;';

        const currValP = document.createElement('div');
        currValP.style.cssText = 'color: var(--text-color, #e0e0e0);';
        currValP.textContent = `${currentLang === 'zh' ? '当前有效值: ' : 'Current Value: '}${formatUiStateValue(slot, slot.value)}`;
        detailDiv.appendChild(currValP);

        const candValP = document.createElement('div');
        candValP.style.cssText = 'color: var(--warning-color, #ffaa00); font-weight: 500;';
        candValP.textContent = `${currentLang === 'zh' ? '冲突候选值: ' : 'Conflict Candidate: '}${formatUiStateValue(slot, slot.candidate_value)}`;
        detailDiv.appendChild(candValP);

        if (slot.validation_error) {
          const errEl = document.createElement('div');
          errEl.className = 'field-error';
          errEl.style.cssText = 'color: var(--error-color, #ff4d4d); margin-top: 2px;';
          errEl.textContent = slot.validation_error;
          detailDiv.appendChild(errEl);
        }

        row.appendChild(detailDiv);
        return row;
      }

      function renderUnresolvedSlot(slot) {
        const label = getSlotLabel(slot);
        const row = document.createElement('div');
        row.className = 'field-row unresolved';

        const labelSpan = document.createElement('span');
        labelSpan.className = 'field-label';
        labelSpan.textContent = `❓ ${label}`;
        row.appendChild(labelSpan);

        const detailDiv = document.createElement('div');
        detailDiv.className = 'field-unresolved-detail';
        detailDiv.style.cssText = 'font-size: 0.85em; margin-top: 4px;';

        const rawP = document.createElement('div');
        rawP.textContent = `${currentLang === 'zh' ? '原始输入: ' : 'Raw Input: '}${slot.raw_value ?? (currentLang === 'zh' ? '暂无' : 'None')}`;
        detailDiv.appendChild(rawP);

        if (slot.candidate_value !== null && slot.candidate_value !== undefined) {
          const candP = document.createElement('div');
          candP.textContent = `${currentLang === 'zh' ? '候选解析: ' : 'Candidate: '}${formatUiStateValue(slot, slot.candidate_value)}`;
          detailDiv.appendChild(candP);
        }

        if (slot.allowed_values && slot.allowed_values.length > 0) {
          const allowedP = document.createElement('div');
          allowedP.style.cssText = 'opacity: 0.8;';
          const allowedStr = Array.isArray(slot.allowed_values) ? slot.allowed_values.join(', ') : String(slot.allowed_values);
          allowedP.textContent = `${currentLang === 'zh' ? '可选范围: ' : 'Allowed: '}${allowedStr}`;
          detailDiv.appendChild(allowedP);
        }

        const hintP = document.createElement('div');
        hintP.style.cssText = 'color: var(--warning-color, #ffaa00); margin-top: 2px;';
        hintP.textContent = currentLang === 'zh' ? '⚠️ 存在歧义，请做出明确选择' : '⚠️ Ambiguity detected, please specify choice';
        detailDiv.appendChild(hintP);

        row.appendChild(detailDiv);
        return row;
      }

      if (uiState) {
        // ── 新路径：按 6 个互斥集合渲染字段面板 ───────────────────────
        const slots = Array.isArray(uiState.slots) ? uiState.slots : [];
        const validSlots = slots.filter(s => s.status === 'valid');
        const candidateSlots = slots.filter(s => s.status === 'candidate' || s.status === 'pending');
        const invalidSlots = slots.filter(s => s.status === 'invalid');
        const conflictSlots = slots.filter(s => s.status === 'conflict');
        const unresolvedSlots = slots.filter(s => s.status === 'unresolved');
        const missingSlots = slots.filter(s => s.status === 'missing');

        collectedDiv.innerHTML = '';
        if (validSlots.length === 0 && candidateSlots.length === 0 && invalidSlots.length === 0 && conflictSlots.length === 0 && unresolvedSlots.length === 0) {
          collectedDiv.innerHTML = I18N[currentLang].none;
        } else {
          for (const slot of validSlots) {
            collectedDiv.appendChild(renderStandardSlot(slot));
          }
          for (const slot of candidateSlots) {
            collectedDiv.appendChild(renderStandardSlot(slot));
          }
          for (const slot of invalidSlots) {
            collectedDiv.appendChild(renderStandardSlot(slot));
          }
          for (const slot of conflictSlots) {
            collectedDiv.appendChild(renderConflictSlot(slot));
          }
          for (const slot of unresolvedSlots) {
            collectedDiv.appendChild(renderUnresolvedSlot(slot));
          }
        }

        // 阶段 badge + 约束状态 + 缺失字段
        const phase = uiState.phase;
        const taskPhaseLabels = currentLang === 'zh' ? { collecting: '任务收集', confirming: '待确认', blocked_soft: '软警告', blocked_hard: '硬阻断', done: '已完成', rejected: '已拒绝' } : { collecting: 'Task collection', confirming: 'Confirming', blocked_soft: 'Soft warning', blocked_hard: 'Hard blocked', done: 'Done', rejected: 'Rejected' };
        let phaseLabel = taskPhaseLabels[phase] || phase;
        if (uiState.dialogue_mode === 'knowledge_qa') phaseLabel = currentLang === 'zh' ? '知识问答' : 'Knowledge Q&A';
        if (uiState.dialogue_mode === 'emergency_intervention' || uiState.mode === 'emergency') phaseLabel = currentLang === 'zh' ? '紧急模式' : 'Emergency';
        const phasePrefix = currentLang === 'zh' ? '阶段：' : 'Phase: ';
        let missingHtml = `<div class="phase-badge" style="margin-bottom:6px; font-size:0.8em; opacity:0.7;">${phasePrefix}${phaseLabel}</div>`;

        const cs = uiState.constraint_state || {};
        if (cs.hard_violations && cs.hard_violations.length > 0) {
          missingHtml += `<div class="constraint-block hard" style="background: rgba(255,77,77,0.1); border: 1px solid rgba(255,77,77,0.4); border-radius:6px; padding:8px; margin-bottom:6px;"><div style="color:#ff4d4d; font-weight:600; margin-bottom:4px;">⛔ 硬约束</div>`;
          for (const v of cs.hard_violations) {
            const nameEl = document.createElement('div');
            nameEl.style.cssText = 'font-size:0.85em; margin-bottom:2px;';
            nameEl.textContent = `[${v.code || ''}] ${v.message || ''}`;
            missingHtml += nameEl.outerHTML;
          }
          missingHtml += '</div>';
        }

        if (cs.soft_warnings && cs.soft_warnings.length > 0) {
          missingHtml += `<div class="constraint-block soft" style="background: rgba(255,190,0,0.1); border: 1px solid rgba(255,190,0,0.4); border-radius:6px; padding:8px; margin-bottom:6px;"><div style="color:#ffbe00; font-weight:600; margin-bottom:4px;">⚠️ 软警告</div>`;
          for (const v of cs.soft_warnings) {
            const nameEl = document.createElement('div');
            nameEl.style.cssText = 'font-size:0.85em; margin-bottom:2px;';
            nameEl.textContent = `[${v.code || ''}] ${v.message || ''}`;
            missingHtml += nameEl.outerHTML;
          }
          if (uiState.actions && uiState.actions.can_ignore_soft_warning) {
            missingHtml += `<div style="margin-top:6px; font-size:0.8em; opacity:0.7;">输入"忽略警告"可继续。</div>`;
          }
          missingHtml += '</div>';
        }

        if (missingSlots.length === 0 && validSlots.length > 0 && candidateSlots.length === 0 && invalidSlots.length === 0) {
          missingHtml += I18N[currentLang].allCollected;
        } else {
          for (const slot of missingSlots) {
            const labelObj = slot.label || {};
            const label = (typeof labelObj === 'string') ? labelObj : (labelObj[currentLang] || labelObj.zh || slot.key);
            const row = document.createElement('div');
            row.className = 'field-row missing';
            row.innerHTML = svgWarning;
            const labelEl = document.createElement('span');
            labelEl.textContent = label;
            row.appendChild(labelEl);
            missingHtml += row.outerHTML;
          }
        }
        missingDiv.innerHTML = missingHtml;

      } else {
        // ── compat 路径：旧 collected/missing 字段 ────────────────────────
        const collected = data.collected || {};
        if (Object.keys(collected).length === 0) {
          collectedDiv.innerHTML = I18N[currentLang].none;
        } else {
          let html = '';
          for (const [k, v] of Object.entries(collected)) {
            const label = getFieldLabel(k);
            const translatedVal = translateValue(k, v);
            html += `
          <div class="field-row">
            <span class="field-label">${label}</span>
            <span class="field-value">${escapeHtml(translatedVal)}</span>
          </div>
        `;
          }
          collectedDiv.innerHTML = html;
        }
        const missing = data.missing || [];
        if (missing.length === 0 && Object.keys(collected).length > 0) {
          missingDiv.innerHTML = I18N[currentLang].allCollected;
        } else {
          let html = '';
          for (const m of missing) {
            const label = getFieldLabel(m);
            html += `<div class="field-row missing">${svgWarning}${label}</div>`;
          }
          missingDiv.innerHTML = html;
        }
      }

      const finalJson = data.final_json || (uiState && uiState.phase === 'done' ? data.built_json : null);
      if (finalJson) {
        document.getElementById('resultCard').style.display = 'block';
        document.getElementById('finalJson').innerText = JSON.stringify(finalJson, null, 2);
      } else {
        document.getElementById('resultCard').style.display = 'none';
      }

      // Issue #31: 集中应用交互状态
      if (uiState && uiState.actions) {
        applyInteractionState(uiState.actions, uiState.read_only);
      }
    }

    async function updateSimulatedTime(isInit = false) {
      try {
        const res = await fetch(API_BASE + '/api/time/current');
        const data = await res.json();
        if (data.code === 200) {
          const dt = new Date(data.current_time);
          const formatted = dt.toLocaleString(currentLang === 'zh' ? 'zh-CN' : 'en-US', { hour12: false });
          document.getElementById('simulatedTimeDisplay').innerText = formatted;

          const picker = document.getElementById('timePicker');
          if (picker && (isInit || !isTimePickerEditing)) {
            const year = dt.getFullYear();
            const month = String(dt.getMonth() + 1).padStart(2, '0');
            const day = String(dt.getDate()).padStart(2, '0');
            const hours = String(dt.getHours()).padStart(2, '0');
            const minutes = String(dt.getMinutes()).padStart(2, '0');
            const localValue = `${year}-${month}-${day}T${hours}:${minutes}`;
            if (picker.value !== localValue) {
              picker.value = localValue;
            }
          }
        } else {
          console.warn('获取模拟时间失败:', data);
        }
      } catch (err) {
        console.error('获取模拟时间网络错误:', err);
      }
    }

    async function setSimulatedTime() {
      const picker = document.getElementById('timePicker');
      const timeValue = picker.value;
      if (!timeValue) {
        alert(I18N[currentLang].timeAlert);
        return;
      }

      try {
        const res = await fetch(API_BASE + '/api/time/set', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ time: timeValue }),
        });
        const data = await res.json();
        if (data.code === 200) {
          isTimePickerEditing = false;
          await updateSimulatedTime(true);
          alert(I18N[currentLang].timeSuccess);
        } else {
          console.warn('设置模拟时间失败:', data);
          alert(data.msg || I18N[currentLang].timeError);
        }
      } catch (err) {
        console.error('设置模拟时间网络错误:', err);
        alert(I18N[currentLang].timeNetError);
      }
    }

    function setAsrStatus(message, visible = true) {
      asrPanel.classList.toggle('visible', visible);
      asrPanel.innerHTML = message ? message : '';
    }

    function renderAsrNormalization(data, autoSent = false) {
      const rawText = (data.text || data.transcript || '').trim();
      const correctedText = (data.corrected_text || rawText).trim();
      // const replacements = Array.isArray(data.replacements) ? data.replacements : [];
      const warnings = Array.isArray(data.warnings) ? data.warnings : [];
      let html = `<strong>${I18N[currentLang].rawTranscript}</strong>${escapeHtml(rawText)}<br>`;
      html += `<strong>${I18N[currentLang].correctedTranscript}</strong>${escapeHtml(correctedText)}<br>`;
      // if (replacements.length) {
      //   html += `<strong>${I18N[currentLang].replacementList}</strong><br>`;
      //   html += replacements.map(item => {
      //     const evidence = Array.isArray(item.evidence) ? item.evidence.join('；') : '';
      //     return `${escapeHtml(item.source || '')} → ${escapeHtml(item.target || '')}`
      //       + (evidence ? ` <span style="color:#8b93b0;">(${escapeHtml(evidence)})</span>` : '');
      //   }).join('<br>');
      //   html += '<br>';
      // }
      if (warnings.length) {
        html += `<strong>${I18N[currentLang].warningsLabel}</strong>${escapeHtml(warnings.join('；'))}<br>`;
      }
      html += autoSent ? I18N[currentLang].autoSentLabel : I18N[currentLang].manualConfirmLabel;
      return html;
    }

    function writeString(view, offset, string) {
      for (let i = 0; i < string.length; i++) {
        view.setUint8(offset + i, string.charCodeAt(i));
      }
    }

    // Convert float to 16bit PCM
    function floatTo16BitPCM(output, offset, input) {
      for (let i = 0; i < input.length; i++, offset += 2) {
        let s = Math.max(-1, Math.min(1, input[i]));
        output.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
      }
    }

    // Merge float32 audio chunks
    function mergeFloat32Chunks(chunks) {
      const length = chunks.reduce((sum, chunk) => sum + chunk.length, 0);
      const result = new Float32Array(length);
      let offset = 0;
      for (const chunk of chunks) {
        result.set(chunk, offset);
        offset += chunk.length;
      }
      return result;
    }

    // WAV audio encoder
    function encodeWav(samples, sampleRate) {
      const buffer = new ArrayBuffer(44 + samples.length * 2);
      const view = new DataView(buffer);

      writeString(view, 0, 'RIFF');
      view.setUint32(4, 36 + samples.length * 2, true);
      writeString(view, 8, 'WAVE');
      writeString(view, 12, 'fmt ');
      view.setUint32(16, 16, true);
      view.setUint16(20, 1, true);
      view.setUint16(22, 1, true);
      view.setUint32(24, sampleRate, true);
      view.setUint32(28, sampleRate * 2, true);
      view.setUint16(32, 2, true);
      view.setUint16(34, 16, true);
      writeString(view, 36, 'data');
      view.setUint32(40, samples.length * 2, true);
      floatTo16BitPCM(view, 44, samples);

      return new Blob([view], { type: 'audio/wav' });
    }

    async function uploadAudioForAsr(audioBlob) {
      const formData = new FormData();
      formData.append('audio', audioBlob, 'voice_input.wav');
      formData.append('language', currentLang === 'zh' ? 'Chinese' : 'English');

      const res = await fetch(API_BASE + '/api/asr', {
        method: 'POST',
        body: formData
      });

      const data = await res.json();
      if (!res.ok || data.code !== 200) {
        throw new Error(data.msg || 'ASR 转写失败');
      }
      return data;
    }

    async function startVoiceRecording() {
      if (!navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
        throw new Error(I18N[currentLang].voiceNotSupport);
      }

      mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: {
          echoCancellation: true,
          noiseSuppression: true,
          autoGainControl: true
        }
      });

      audioContext = new (window.AudioContext || window.webkitAudioContext)();
      recordingSampleRate = audioContext.sampleRate;
      recorderSource = audioContext.createMediaStreamSource(mediaStream);
      recorderProcessor = audioContext.createScriptProcessor(4096, 1, 1);
      recordedChunks = [];

      recorderProcessor.onaudioprocess = (event) => {
        const input = event.inputBuffer.getChannelData(0);
        recordedChunks.push(new Float32Array(input));
      };

      recorderSource.connect(recorderProcessor);
      recorderProcessor.connect(audioContext.destination);

      isRecording = true;
      voiceBtn.classList.add('recording');
      voiceBtn.textContent = I18N[currentLang].voiceStop;
      setAsrStatus(I18N[currentLang].voiceRecording);

      // Show center pulsing visual
      document.getElementById("audioWaveformWrapper").style.display = "flex";
    }

    async function stopVoiceRecording() {
      isRecording = false;
      voiceBtn.classList.remove('recording');
      voiceBtn.textContent = I18N[currentLang].voiceBtn;

      // Hide center pulsing visual
      document.getElementById("audioWaveformWrapper").style.display = "none";

      if (recorderProcessor) recorderProcessor.disconnect();
      if (recorderSource) recorderSource.disconnect();
      if (mediaStream) mediaStream.getTracks().forEach(track => track.stop());
      if (audioContext) await audioContext.close();

      recorderProcessor = null;
      recorderSource = null;
      mediaStream = null;
      audioContext = null;

      if (!recordedChunks.length) {
        setAsrStatus(I18N[currentLang].voiceNoAudio);
        return;
      }

      const samples = mergeFloat32Chunks(recordedChunks);
      const audioBlob = encodeWav(samples, recordingSampleRate);

      setAsrStatus(I18N[currentLang].voiceTranscribing);
      voiceBtn.disabled = true;

      try {
        const data = await uploadAudioForAsr(audioBlob);
        const transcript = (data.corrected_text || data.text || data.transcript || '').trim();
        const directToLlm = data.direct_to_llm !== false;

        if (!transcript) {
          setAsrStatus(I18N[currentLang].voiceEmptyText);
          return;
        }

        const hasRiskOrChanges = (data.warnings && data.warnings.length > 0) ||
                                 (data.replacements && data.replacements.length > 0) ||
                                 !!data.normalization_changed;
        const shouldAutoSend = directToLlm && !hasRiskOrChanges;

        if (shouldAutoSend) {
          setAsrStatus(renderAsrNormalization(data, true), true);
          await sendMessage(transcript, { source: 'voice' });
        } else {
          messageInput.value = transcript;
          setAsrStatus(renderAsrNormalization(data, false), true);
          messageInput.focus();
        }
      } catch (err) {
        setAsrStatus(`${I18N[currentLang].voiceError}${escapeHtml(err.message || err)}`);
      } finally {
        if (!isDone) voiceBtn.disabled = false;
      }
    }

    async function toggleVoiceRecording() {
      try {
        if (isRecording) {
          await stopVoiceRecording();
        } else {
          await startVoiceRecording();
        }
      } catch (err) {
        isRecording = false;
        voiceBtn.classList.remove('recording');
        voiceBtn.textContent = I18N[currentLang].voiceBtn;
        voiceBtn.disabled = false;
        setAsrStatus(`${I18N[currentLang].voiceNotAvailable}${escapeHtml(err.message || err)}`);
      }
    }

    // ==================== 历史记录功能 ====================
    async function loadHistoryList() {
      try {
        const res = await fetch(API_BASE + '/api/history/list');
        const data = await res.json();
        if (data.code !== 200) return;
        const listDiv = document.getElementById('historyList');
        if (data.data.length === 0) {
          listDiv.innerHTML = `<div style="padding:8px; color:var(--text-secondary);">${I18N[currentLang].historyNone}</div>`;
        } else {
          let html = '';
          for (const rec of data.data) {
            const localizedType = translateValue('task_type_key', rec.task_type);
            html += `
          <div class="history-item" data-id="${rec.id}" style="padding:6px; border-bottom:1px solid rgba(0, 240, 255, 0.08); cursor:pointer; font-size:0.75rem;">
            <div><strong>${escapeHtml(rec.task_id)}</strong> (${escapeHtml(localizedType)})</div>
            <div style="font-size:0.65rem; color:var(--text-secondary);">${new Date(rec.saved_at).toLocaleString()}</div>
          </div>
        `;
          }
          listDiv.innerHTML = html;
          document.querySelectorAll('.history-item').forEach(el => {
            el.addEventListener('click', async (e) => {
              const hid = el.getAttribute('data-id');
              await restoreHistory(hid);
              listDiv.style.display = 'none';
            });
          });
        }
        if (listDiv.style.display === 'none') {
          listDiv.style.display = 'block';
        } else {
          listDiv.style.display = 'none';
        }
      } catch (err) {
        console.error('加载历史列表失败', err);
      }
    }

    async function restoreHistory(historyId) {
      cancelActiveRequest();
      isSending = true;
      applyInteractionState(currentActions, currentReadOnly);
      const restoreSeq = ++currentRequestSeq;
      const restoreGen = sessionGeneration;
      currentAbortController = new AbortController();
      let effectiveSessionId = sessionId;
      if (!effectiveSessionId) {
        effectiveSessionId = 'temp_' + Date.now();
      }

      try {
        const res = await fetch(API_BASE + '/api/history/load', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ history_id: historyId, session_id: effectiveSessionId }),
          signal: currentAbortController.signal,
        });
        const data = await res.json();
        if (restoreSeq !== currentRequestSeq || restoreGen !== sessionGeneration) return;
        if (data.code !== 200) {
          alert(I18N[currentLang].restoreFailed + (data.msg || 'unknown'));
          return;
        }
        if (data.session_id) sessionId = data.session_id;
        else sessionId = effectiveSessionId;
        try { localStorage.setItem('seagent_session_id', sessionId); } catch(e){}

        messageContainer.innerHTML = '';
        for (const msg of data.conversation_history) {
          addMessage(msg.role, msg.content);
        }

        // Issue #31: 优先使用 ui_state 恢复状态
        if (data.ui_state) {
          updateSidebar(data);
          // applyInteractionState 已在 updateSidebar 内调用
          if (data.ui_state.phase === 'done' || data.ui_state.read_only) {
            addMessage('bot', I18N[currentLang].historyLoadedReadOnly);
          }
        } else {
          // compat 路径
          const isCompleted = (data.phase === 'done');
          updateSidebar({
            task_type: data.task_type,
            emergency: data.mode === 'emergency',
            collected: data.built_json,
            missing: data.missing,
            final_json: isCompleted ? data.built_json : null
          });
          if (isCompleted) {
            document.getElementById('resultCard').style.display = 'block';
            document.getElementById('finalJson').innerText = JSON.stringify(data.built_json, null, 2);
            messageInput.disabled = true;
            sendBtn.disabled = true;
            addMessage('bot', I18N[currentLang].historyLoadedReadOnly);
          } else {
            document.getElementById('resultCard').style.display = 'none';
            messageInput.disabled = false;
            sendBtn.disabled = false;
          }
        }

        document.getElementById('historyList').style.display = 'none';
      } catch (err) {
        if (err.name === 'AbortError') return;
        console.error('还原历史失败', err);
        alert(I18N[currentLang].restoreNetError);
      } finally {
        if (restoreSeq === currentRequestSeq && restoreGen === sessionGeneration) {
          isSending = false;
          currentAbortController = null;
          applyInteractionState(currentActions, currentReadOnly);
        }
      }
    }

    async function sendMessage(msg, options = {}) {
      // Issue #31: isSending 防重发送
      if (isSending || !msg.trim() || isDone) return;
      const source = options.source || 'text';

      isSending = true;
      applyInteractionState(currentActions, currentReadOnly);
      const mySeq = ++currentRequestSeq;
      const myGen = sessionGeneration;
      currentAbortController = new AbortController();
      addMessage('user', msg);
      messageInput.value = '';

      let data = {};
      try {
        const res = await fetch(API_BASE + '/api/chat', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ session_id: sessionId, message: msg, source }),
          signal: currentAbortController.signal,
        });

        if (mySeq !== currentRequestSeq || myGen !== sessionGeneration) return;

        let rawText = '';
        try {
          rawText = await res.text();
        } catch (e) {
          addMessage('bot', `响应读取失败 (HTTP ${res.status})`);
          return;
        }

        if (mySeq !== currentRequestSeq || myGen !== sessionGeneration) return;

        try {
          data = JSON.parse(rawText);
        } catch (e) {
          addMessage('bot', `服务响应非标准格式 (HTTP ${res.status})`);
          return;
        }

        if (!res.ok || data.ok === false || (data.code && data.code !== 200)) {
          const errMsg = data.msg || data.message || '请求处理异常';
          const reqId = data.request_id ? ` [request_id: ${data.request_id}]` : '';
          const retryHint = data.retryable ? ' (可尝试重试)' : '';
          addMessage('bot', `⛔ 错误 (${data.error || 'ServerErr'}): ${errMsg}${reqId}${retryHint}`);
          return;
        }

        if (data.session_id) {
          sessionId = data.session_id;
          try { localStorage.setItem('seagent_session_id', sessionId); } catch(e){}
        }

        if (data.reply) {
          addMessage('bot', data.reply);
        }

        const phase = data.ui_state ? data.ui_state.phase : (data.done ? 'done' : null);
        if (phase === 'done' && !data.rejected) {
          if (data.final_json) {
            addMessage('bot', I18N[currentLang].taskSuccessMsg);
            addMessage('bot', '```json\n' + JSON.stringify(data.final_json, null, 2) + '\n```');
          }
        } else if (data.rejected || phase === 'rejected') {
          addMessage('bot', I18N[currentLang].taskRejectedMsg);
        }

        updateSidebar(data);
      } catch (err) {
        if (err.name === 'AbortError') return;
        addMessage('bot', I18N[currentLang].networkError);
      } finally {
        if (mySeq === currentRequestSeq && myGen === sessionGeneration) {
          isSending = false;
          currentAbortController = null;
          applyInteractionState(currentActions, currentReadOnly);
          messageInput.focus();
        } else {
          isSending = false;
        }
      }
    }

    async function restoreSessionFromStorage() {
      let savedSid = null;
      try { savedSid = localStorage.getItem('seagent_session_id'); } catch(e){}
      if (!savedSid) return false;
      try {
        const res = await fetch(API_BASE + '/api/session/state?session_id=' + encodeURIComponent(savedSid));
        const data = await res.json();
        if (data.ok && data.exists) {
          sessionId = savedSid;
          messageContainer.innerHTML = '';
          if (data.history && data.history.length > 0) {
            for (const m of data.history) {
              addMessage(m.role, m.content);
            }
          } else {
            addWelcomeMessage();
          }
          // Issue #31: 优先使用 ui_state 恢复状态
          if (data.ui_state) {
            updateSidebar(data);
            // applyInteractionState 已在 updateSidebar 内调用
          } else {
            isDone = !!data.done;
            updateSidebar(data);
          }
          return true;
        }
      } catch (e) {
        console.error('Session restore failed', e);
      }
      return false;
    }

    async function reset() {
      window.reset = reset;
      if (currentAbortController) { try { currentAbortController.abort(); } catch(e){} }
      currentRequestSeq++;
      sessionGeneration++;
      window.sessionGeneration = sessionGeneration;
      isSending = false;
      sendBtn.disabled = true;
      messageInput.disabled = true;

      try { localStorage.removeItem('seagent_session_id'); } catch(e){}

      if (sessionId) {
        try {
          await fetch(API_BASE + '/api/reset', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ session_id: sessionId })
          });
        } catch (err) {
          console.error('Reset failed', err);
        }
      }
      sessionId = null;
      isDone = false;
      messageContainer.innerHTML = '';
      addWelcomeMessage();
      document.getElementById('taskInfo').innerHTML = '-';
      document.getElementById('collectedFields').innerHTML = I18N[currentLang].none;
      document.getElementById('missingFields').innerHTML = '-';
      document.getElementById('resultCard').style.display = 'none';
      document.getElementById('historyList').style.display = 'none';
      messageInput.value = '';

      sendBtn.disabled = false;
      messageInput.disabled = false;
      voiceBtn.disabled = false;
      voiceBtn.classList.remove('recording');
      voiceBtn.textContent = I18N[currentLang].voiceBtn;
      setAsrStatus('', false);
      messageInput.focus();

    }

    sendBtn.addEventListener('click', () => sendMessage(messageInput.value));
    // Issue #31: 改用 keydown，检查 isComposing 防止中文输入法误提交；Shift+Enter 换行
    messageInput.addEventListener('keydown', (e) => {
      if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
        e.preventDefault();
        sendMessage(messageInput.value);
      }
    });
    voiceBtn.addEventListener('click', toggleVoiceRecording);
    resetBtn.addEventListener('click', reset);
    messageContainer.addEventListener('click', (event) => {
      const translateButton = event.target.closest('[data-action="translate-message"]');
      if (translateButton && messageContainer.contains(translateButton)) {
        toggleMessageTranslation(translateButton);
      }
    });
    const timePicker = document.getElementById('timePicker');
    const setTimeBtn = document.getElementById('setTimeBtn');
    timePicker.addEventListener('focus', () => {
      isTimePickerEditing = true;
    });
    timePicker.addEventListener('blur', (event) => {
      if (event.relatedTarget !== setTimeBtn) {
        isTimePickerEditing = false;
      }
    });
    setTimeBtn.addEventListener('click', setSimulatedTime);
    document.getElementById('historyBtn').addEventListener('click', loadHistoryList);



    window.reset = reset;
    window.restoreHistory = restoreHistory;
    window.sendMessage = sendMessage;
    window.updateSidebar = updateSidebar;
    window.applyInteractionState = applyInteractionState;

    restoreSessionFromStorage().then(restored => {
      if (!restored) reset();
    });

    updateSimulatedTime(true);
    if (timeUpdateInterval) clearInterval(timeUpdateInterval);
    timeUpdateInterval = setInterval(() => updateSimulatedTime(false), 1000);
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initFrontend);
  } else {
    initFrontend();
  }
})();
