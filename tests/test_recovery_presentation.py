"""All recovery surfaces share the same evidence-based activity labels."""
import subprocess
from pathlib import Path


def test_recovery_presentation_distinguishes_waiting_from_activity():
    source=Path('cloakup/web/platform.js').read_text()
    harness=r'''
const state={account_execution:{owner_run_id:'run',members:[{id:'task',status:'running',availability:{state:'outside_window',next_at:4102444800}}]},runs:[{id:'run',status:'running',autopilot_stage:'selection_retry',active_case_id:'case',executor_heartbeat_at:Date.now()/1000}]};
const active=new Set(['running','paused','queued']);const document={addEventListener(){}};
'''+source+r'''
const record={task_id:'task',case_id:'case',status:'retrying',attempts:3};
function check(expected){const actual=recoveryPresentation(record);if(actual.status!==expected)throw Error(expected+' != '+actual.status);return actual;}
if(check('outside_window').label!=='等待执行时段 · 会话待核验')throw Error('wrong label');
state.account_execution.members[0].availability.state='resting';check('resting');
state.account_execution.members[0].availability.state='allowed';record.next_check_at=Date.now()/1000+120;check('scheduled');
record.next_check_at=null;check('verifying');
state.runs[0].autopilot_stage='waiting_next_cycle';check('pending');
state.account_execution.members[0].status='paused';check('paused');
record.status='blocked';check('blocked');
state.account_execution.members[0].status='running';check('pending');
state.runs=[];check('disconnected');
record.status='recovered';check('recovered');
'''
    subprocess.run(['node','-e',harness],check=True,capture_output=True,text=True)
