const fs=require('fs');
const vm=require('vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync('frontend/ros2_dashboard.html','utf8');
class Node {
  constructor(){this.children=[];this.style={};this.textContent='';this.hidden=false;}
  appendChild(child){this.children.push(child);}
  append(...children){this.children.push(...children);}
  replaceChildren(){this.children=[];}
}
const nodes=new Map();
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);},createElement(){return new Node();}};
const context=vm.createContext({document,window:{setInterval(){return 1;},clearInterval(){}},refreshStatus(){}});
vm.runInContext(html.slice(html.indexOf('    const taskTypeNames'),html.indexOf('    const dashboardApi')),context);
const task={task_id:'0x80001',intent_id:'<img src=x>',task_type:2,status:'ONGOING',progress:60,dispatch_state:'SENT',observed_in_latest:true,control_state:'DELETE_REQUESTED'};
context.tasks=[task];
vm.runInContext('renderTasks(tasks)',context);
assert.equal(nodes.get('taskCount').textContent,1);
assert.equal(nodes.get('taskRows').children[0].children[1].textContent,task.intent_id);
assert.deepEqual(nodes.get('taskRows').children[0].children[3].children.map(n=>n.textContent),['SENT · 已写入传输']);
assert.deepEqual(nodes.get('taskRows').children[0].children[4].children.map(n=>n.textContent),['ONGOING · 执行中','DELETE_REQUESTED · 删除请求已发送，待确认']);
context.tasks=[{...task,status:'DELETE_REQUESTED',last_observed_status:'ONGOING',observation_precedes_control:true}];
vm.runInContext('renderTasks([]); renderTasks(tasks,true)',context);
assert.equal(nodes.get('taskCount').textContent,0);
assert.equal(nodes.get('taskRows').children.length,0);
assert.equal(nodes.get('historyRows').children.length,1);
assert.deepEqual(nodes.get('historyRows').children[0].children[4].children.map(n=>n.textContent),['执行状态未确认','上次回传：ONGOING','DELETE_REQUESTED · 删除请求已发送，待确认']);
assert.equal(nodes.get('historyCounter').textContent,'1 records');
assert.equal(nodes.get('emptyTasks').style.display,'block');
assert.equal(nodes.get('emptyHistory').style.display,'none');

// A persisted send, including a failed send, is not robot execution feedback.
context.tasks=[
  {...task,status:'SENT',observed_in_latest:false,control_state:null},
  {...task,status:'FAILED',dispatch_state:'FAILED',observed_in_latest:false,control_state:null,error:'<script>failure</script>'},
  {...task,status:'UNKNOWN',last_observed_status:'FINISH',control_state:null},
  {...task,status:'FINISH',last_observed_status:'FINISH',control_state:null},
];
vm.runInContext('renderTasks(tasks,true)',context);
const records=nodes.get('historyRows').children;
assert.equal(records[0].children[4].children[0].textContent,'等待执行回传');
assert.equal(records[1].children[3].children[0].textContent,'FAILED · 发送失败');
assert.equal(records[1].children[3].children[1].textContent,'<script>failure</script>');
assert.equal(records[1].children[4].children[0].textContent,'执行状态未确认');
assert.equal(records[2].children[4].children[0].textContent,'执行状态未确认');
assert.equal(records[2].children[4].children[1].textContent,'上次回传：FINISH');
assert.equal(records[3].children[4].children[0].textContent,'FINISH · 执行完成');
assert(!JSON.stringify(records).includes('60%'),'fixed status progress must never appear as measured progress');

// Poll failures retain the last successful snapshot and both timestamp meanings.
context.status={mcp_connected:true,telemetry_fresh:true,host:'gateway.example',port:9090,
  snapshot:{last_update:'2025-01-23T08:28:14Z',water_depth_m:120.5,active_tasks:[task],task_history:[]}};
vm.runInContext('renderStatus(status)',context);
const received=nodes.get('lastUpdate').textContent;
const pulled=nodes.get('lastSuccessfulPull').textContent;
assert(received.includes('2025'));
assert.notEqual(received,pulled,'browser pull time must not be the telemetry receipt time');
assert.equal(nodes.get('dataNotice').hidden,true);
assert.equal(nodes.get('gatewayValue').textContent,'ws://gateway.example:9090');
const retainedTask=nodes.get('taskRows').children[0];
vm.runInContext("showStatusFailure(new Error('network unavailable'))",context);
assert.equal(nodes.get('depthValue').textContent,'120.5');
assert.equal(nodes.get('taskRows').children[0],retainedTask);
assert.equal(nodes.get('lastUpdate').textContent,received);
assert.equal(nodes.get('lastSuccessfulPull').textContent,pulled);
assert.equal(nodes.get('bridgeState').textContent,'连接状态未确认');
assert.equal(nodes.get('telemetryState').textContent,'更新中断 · 保留快照');
assert.equal(nodes.get('dataNotice').hidden,false);
assert(nodes.get('dataNotice').textContent.includes('不代表当前状态'));
vm.runInContext('renderStatus(status)',context);
assert.equal(nodes.get('dataNotice').hidden,true);
assert.equal(nodes.get('errorBox').style.display,'none');
context.status={...context.status,mcp_connected:false,telemetry_fresh:false};
vm.runInContext('renderStatus(status)',context);
assert.equal(nodes.get('dataNotice').hidden,false);
assert.equal(nodes.get('telemetryState').textContent,'已过期 · 最近快照');
assert.equal(nodes.get('depthValue').textContent,'120.5');
assert.equal(vm.runInContext("formatTime('not-a-timestamp')",context),'--');
assert.equal(vm.runInContext("number('')",context),'--');
console.log('dashboard lifecycle rendering passed');
