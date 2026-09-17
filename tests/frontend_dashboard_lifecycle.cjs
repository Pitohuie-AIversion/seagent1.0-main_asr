const fs=require('fs');
const vm=require('vm');
const assert=require('node:assert/strict');
const html=fs.readFileSync('frontend/ros2_dashboard.html','utf8');
class Node {
  constructor(){this.children=[];this.style={};this.textContent='';}
  appendChild(child){this.children.push(child);}
  replaceChildren(){this.children=[];}
}
const nodes=new Map();
const document={getElementById(id){if(!nodes.has(id))nodes.set(id,new Node());return nodes.get(id);},createElement(){return new Node();}};
const context=vm.createContext({document});
vm.runInContext(html.slice(html.indexOf('    const taskTypeNames'),html.indexOf('    function displayValue')),context);
const task={task_id:'0x80001',intent_id:'<img src=x>',task_type:2,status:'ONGOING',progress:60,control_state:'DELETE_REQUESTED'};
context.tasks=[task];
vm.runInContext('renderTasks(tasks)',context);
assert.equal(nodes.get('taskCount').textContent,1);
assert.equal(nodes.get('taskRows').children[0].children[1].textContent,task.intent_id);
assert.deepEqual(nodes.get('taskRows').children[0].children[3].children.map(n=>n.textContent),['ONGOING','DELETE_REQUESTED']);
context.tasks=[{...task,status:'DELETE_REQUESTED',last_observed_status:'ONGOING'}];
vm.runInContext('renderTasks([]); renderTasks(tasks,true)',context);
assert.equal(nodes.get('taskCount').textContent,0);
assert.equal(nodes.get('taskRows').children.length,0);
assert.equal(nodes.get('historyRows').children.length,1);
assert.equal(nodes.get('historyRows').children[0].children[3].title,'上次观测：ONGOING');
assert.equal(nodes.get('historyCounter').textContent,'1 records');
assert.equal(nodes.get('emptyTasks').style.display,'block');
assert.equal(nodes.get('emptyHistory').style.display,'none');
console.log('dashboard lifecycle rendering passed');
